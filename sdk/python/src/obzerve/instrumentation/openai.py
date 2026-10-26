"""OpenAI Python SDK: Chat Completions, Responses, and Embeddings (sync and async,
including streams). Also covers OpenAI-compatible servers (vLLM, Ollama, TGI, ...)
reached through the OpenAI client; those are recorded as ``openai_compatible``."""

from __future__ import annotations

from typing import Any

from obzerve import attributes as attrs
from obzerve.attributes import SpanKind
from obzerve.instrumentation._common import (
    Accumulator,
    AsyncStreamProxy,
    Call,
    Patches,
    StreamProxy,
    finish_stream,
    get,
    server_address,
    set_present,
)
from obzerve.observer import ObzerveSpan

CHAT = "openai.chat.completions"
RESPONSES = "openai.responses"
EMBEDDINGS = "openai.embeddings"


def _provider(host: str | None) -> str:
    if host is None or host.endswith("openai.com"):
        return "openai"
    if host.endswith("openai.azure.com"):
        return "azure.ai.openai"
    return "openai_compatible"


def _request(operation: str):
    def before(span: ObzerveSpan, args: tuple, kwargs: dict) -> None:
        resource = args[0]
        host = server_address(getattr(getattr(resource, "_client", None), "base_url", None))
        set_present(
            span,
            {
                "gen_ai.operation.name": operation,
                attrs.GEN_AI_PROVIDER: _provider(host),
                "server.address": host,
                attrs.GEN_AI_REQUEST_MODEL: kwargs.get("model"),
                "gen_ai.request.temperature": _plain(kwargs.get("temperature")),
                "gen_ai.request.top_p": _plain(kwargs.get("top_p")),
                "gen_ai.request.max_tokens": _plain(
                    kwargs.get("max_completion_tokens", kwargs.get("max_output_tokens"))
                    or kwargs.get("max_tokens")
                ),
                "gen_ai.request.stream": bool(kwargs.get("stream")),
            },
        )

    return before


def _plain(value: Any) -> Any:
    # The SDK's "not given" sentinels are not values.
    return value if isinstance(value, (int, float, str, bool)) else None


def _chat_usage(span: ObzerveSpan, usage: Any) -> None:
    if usage is None:
        return
    details = get(usage, "prompt_tokens_details")
    span.set_llm(
        input_tokens=get(usage, "prompt_tokens"),
        output_tokens=get(usage, "completion_tokens"),
        cached_input_tokens=get(details, "cached_tokens"),
    )


def _after_chat(span: ObzerveSpan, response: Any) -> None:
    choices = get(response, "choices") or []
    set_present(
        span,
        {
            attrs.GEN_AI_RESPONSE_MODEL: get(response, "model"),
            "gen_ai.response.id": get(response, "id"),
            "gen_ai.response.finish_reasons": [
                str(get(c, "finish_reason")) for c in choices if get(c, "finish_reason")
            ],
        },
    )
    _chat_usage(span, get(response, "usage"))


def _after_responses(span: ObzerveSpan, response: Any) -> None:
    usage = get(response, "usage")
    set_present(
        span,
        {
            attrs.GEN_AI_RESPONSE_MODEL: get(response, "model"),
            "gen_ai.response.id": get(response, "id"),
            "gen_ai.response.finish_reasons": [str(get(response, "status") or "")],
        },
    )
    if usage is not None:
        span.set_llm(
            input_tokens=get(usage, "input_tokens"),
            output_tokens=get(usage, "output_tokens"),
            cached_input_tokens=get(get(usage, "input_tokens_details"), "cached_tokens"),
        )


def _after_embeddings(span: ObzerveSpan, response: Any) -> None:
    usage = get(response, "usage")
    set_present(span, {attrs.GEN_AI_RESPONSE_MODEL: get(response, "model")})
    if usage is not None:
        span.set_llm(input_tokens=get(usage, "prompt_tokens"))


def _chat_stream(acc: Accumulator, chunk: Any) -> None:
    acc.values.setdefault("model", get(chunk, "model"))
    acc.values.setdefault("id", get(chunk, "id"))
    for choice in get(chunk, "choices") or []:
        content = get(get(choice, "delta"), "content")
        if content:
            acc.text.append(content)
        if get(choice, "finish_reason"):
            acc.values["finish_reason"] = str(get(choice, "finish_reason"))
    if get(chunk, "usage") is not None:  # only with stream_options={"include_usage": True}
        acc.values["usage"] = get(chunk, "usage")


def _finish_chat_stream(acc: Accumulator):
    def finish(span: ObzerveSpan) -> None:
        set_present(
            span,
            {
                attrs.GEN_AI_RESPONSE_MODEL: acc.values.get("model"),
                "gen_ai.response.id": acc.values.get("id"),
                "gen_ai.response.finish_reasons": (
                    [acc.values["finish_reason"]] if "finish_reason" in acc.values else []
                ),
            },
        )
        _chat_usage(span, acc.values.get("usage"))
        span.set_output(acc.output())

    return finish


def _responses_stream(acc: Accumulator, event: Any) -> None:
    kind = get(event, "type")
    if kind == "response.output_text.delta":
        acc.text.append(get(event, "delta") or "")
    elif kind in ("response.completed", "response.incomplete", "response.failed"):
        acc.values["response"] = get(event, "response")


def _finish_responses_stream(acc: Accumulator):
    def finish(span: ObzerveSpan) -> None:
        if acc.values.get("response") is not None:
            _after_responses(span, acc.values["response"])
        span.set_output(acc.output())

    return finish


def _model_codec(module: str, cls: str):
    def decode(data: Any) -> Any:
        import importlib

        return getattr(importlib.import_module(module), cls).model_validate(data)

    return (lambda response: response.model_dump(mode="json")), decode


_STREAM_HANDLERS = {
    CHAT: (_chat_stream, _finish_chat_stream),
    RESPONSES: (_responses_stream, _finish_responses_stream),
}


def _wrap(observer, name: str, operation: str, after, codec, is_async: bool):
    encode, decode = codec

    def make(original):
        call = Call(
            observer,
            name,
            original,
            kind=SpanKind.LLM,
            encode=encode,
            decode=decode,
            before=_request(operation),
            after=after,
        )

        def start_stream(args, kwargs):
            on_item, make_finish = _STREAM_HANDLERS[name]
            acc = Accumulator()
            span = call.start_stream_span(args, kwargs)
            return span, (lambda item: on_item(acc, item)), make_finish(acc)

        if is_async:

            async def wrapper(*args: Any, **kwargs: Any) -> Any:
                if not (kwargs.get("stream") is True and name in _STREAM_HANDLERS):
                    return await call.run_async(args, kwargs)
                span, on_item, finish = start_stream(args, kwargs)
                try:
                    stream = await original(*args, **kwargs)
                except BaseException as exc:
                    span.end(exc)
                    raise
                return AsyncStreamProxy(stream, on_item, lambda e: finish_stream(span, finish, e))

            return wrapper

        def wrapper(*args: Any, **kwargs: Any) -> Any:
            if not (kwargs.get("stream") is True and name in _STREAM_HANDLERS):
                return call.run(args, kwargs)
            span, on_item, finish = start_stream(args, kwargs)
            try:
                stream = original(*args, **kwargs)
            except BaseException as exc:
                span.end(exc)
                raise
            return StreamProxy(stream, on_item, lambda e: finish_stream(span, finish, e))

        return wrapper

    return make


def instrument(observer, patches: Patches) -> int:
    from openai.resources import embeddings, responses
    from openai.resources.chat import completions

    targets = [
        (
            completions.Completions,
            completions.AsyncCompletions,
            CHAT,
            "chat",
            _after_chat,
            _model_codec("openai.types.chat", "ChatCompletion"),
        ),
        (
            responses.Responses,
            responses.AsyncResponses,
            RESPONSES,
            "generate_content",
            _after_responses,
            _model_codec("openai.types.responses", "Response"),
        ),
        (
            embeddings.Embeddings,
            embeddings.AsyncEmbeddings,
            EMBEDDINGS,
            "embeddings",
            _after_embeddings,
            _model_codec("openai.types", "CreateEmbeddingResponse"),
        ),
    ]
    count = 0
    for sync_cls, async_cls, name, operation, after, codec in targets:
        count += patches.wrap(
            sync_cls, "create", _wrap(observer, name, operation, after, codec, False)
        )
        count += patches.wrap(
            async_cls, "create", _wrap(observer, name, operation, after, codec, True)
        )
    return count
