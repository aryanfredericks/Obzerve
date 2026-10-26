"""Hugging Face ``huggingface_hub`` InferenceClient: ``chat_completion`` (also
reached as ``client.chat.completions.create``) and ``text_generation``, sync and
async, including streams. Covers the serverless Inference API, Inference
Endpoints, and self-hosted TGI servers."""

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

CHAT = "huggingface.chat_completion"
TEXT = "huggingface.text_generation"


def _before(operation: str):
    def before(span: ObzerveSpan, args: tuple, kwargs: dict) -> None:
        client = args[0]
        # huggingface_hub keeps a custom endpoint (base_url) in `client.model`; a model
        # passed per call may be a name or an endpoint URL as well.
        candidates = [
            kwargs.get("model"),
            getattr(client, "base_url", None),
            getattr(client, "model", None),
        ]
        urls = [c for c in candidates if isinstance(c, str) and c.startswith("http")]
        names = [c for c in candidates if isinstance(c, str) and not c.startswith("http")]
        set_present(
            span,
            {
                "gen_ai.operation.name": operation,
                attrs.GEN_AI_PROVIDER: "huggingface",
                attrs.GEN_AI_REQUEST_MODEL: names[0] if names else None,
                "server.address": server_address(urls[0]) if urls else None,
                "gen_ai.request.max_tokens": kwargs.get("max_tokens", kwargs.get("max_new_tokens")),
                "gen_ai.request.temperature": kwargs.get("temperature"),
                "gen_ai.request.stream": bool(kwargs.get("stream")),
            },
        )

    return before


def _after_chat(span: ObzerveSpan, output: Any) -> None:
    choices = get(output, "choices") or []
    set_present(
        span,
        {
            attrs.GEN_AI_RESPONSE_MODEL: get(output, "model"),
            "gen_ai.response.id": get(output, "id"),
            "gen_ai.response.finish_reasons": [
                str(get(c, "finish_reason")) for c in choices if get(c, "finish_reason")
            ],
        },
    )
    usage = get(output, "usage")
    if usage is not None:
        span.set_llm(
            input_tokens=get(usage, "prompt_tokens"), output_tokens=get(usage, "completion_tokens")
        )


def _after_text(span: ObzerveSpan, output: Any) -> None:
    details = get(output, "details") if not isinstance(output, str) else None
    if details is not None:
        set_present(
            span,
            {"gen_ai.response.finish_reasons": [str(get(details, "finish_reason") or "")]},
        )
        span.set_llm(output_tokens=get(details, "generated_tokens"))


def _codec(type_name: str):
    def encode(output: Any) -> Any:
        return output if isinstance(output, str) else dict(output)

    def decode(data: Any) -> Any:
        if isinstance(data, str):
            return data
        import huggingface_hub

        return getattr(huggingface_hub, type_name).parse_obj_as_instance(data)

    return encode, decode


def _chat_item(acc: Accumulator, chunk: Any) -> None:
    acc.values.setdefault("model", get(chunk, "model"))
    for choice in get(chunk, "choices") or []:
        content = get(get(choice, "delta"), "content")
        if content:
            acc.text.append(content)
        if get(choice, "finish_reason"):
            acc.values["finish_reason"] = str(get(choice, "finish_reason"))
    if get(chunk, "usage") is not None:
        acc.values["usage"] = get(chunk, "usage")


def _text_item(acc: Accumulator, chunk: Any) -> None:
    if isinstance(chunk, str):
        acc.text.append(chunk)
        return
    token = get(chunk, "token")
    if token is not None and not get(token, "special"):
        acc.text.append(get(token, "text") or "")
    details = get(chunk, "details")
    if details is not None:
        acc.values["details"] = details


def _finish(acc: Accumulator, name: str):
    def finish(span: ObzerveSpan) -> None:
        if name == CHAT:
            set_present(
                span,
                {
                    attrs.GEN_AI_RESPONSE_MODEL: acc.values.get("model"),
                    "gen_ai.response.finish_reasons": (
                        [acc.values["finish_reason"]] if "finish_reason" in acc.values else []
                    ),
                },
            )
            usage = acc.values.get("usage")
            if usage is not None:
                span.set_llm(
                    input_tokens=get(usage, "prompt_tokens"),
                    output_tokens=get(usage, "completion_tokens"),
                )
        elif acc.values.get("details") is not None:
            span.set_llm(output_tokens=get(acc.values["details"], "generated_tokens"))
        span.set_output(acc.output())

    return finish


_HANDLERS = {CHAT: _chat_item, TEXT: _text_item}


def _wrap(observer, name: str, operation: str, after, type_name: str, is_async: bool):
    encode, decode = _codec(type_name)

    def make(original):
        call = Call(
            observer,
            name,
            original,
            kind=SpanKind.LLM,
            encode=encode,
            decode=decode,
            before=_before(operation),
            after=after,
        )

        def start(args, kwargs):
            acc = Accumulator()
            span = call.start_stream_span(args, kwargs)
            return span, (lambda item: _HANDLERS[name](acc, item)), _finish(acc, name)

        if is_async:

            async def wrapper(*args: Any, **kwargs: Any) -> Any:
                if kwargs.get("stream") is not True:
                    return await call.run_async(args, kwargs)
                span, on_item, finish = start(args, kwargs)
                try:
                    stream = await original(*args, **kwargs)
                except BaseException as exc:
                    span.end(exc)
                    raise
                return AsyncStreamProxy(stream, on_item, lambda e: finish_stream(span, finish, e))

            return wrapper

        def wrapper(*args: Any, **kwargs: Any) -> Any:
            if kwargs.get("stream") is not True:
                return call.run(args, kwargs)
            span, on_item, finish = start(args, kwargs)
            try:
                stream = original(*args, **kwargs)
            except BaseException as exc:
                span.end(exc)
                raise
            return StreamProxy(stream, on_item, lambda e: finish_stream(span, finish, e))

        return wrapper

    return make


def instrument(observer, patches: Patches) -> int:
    from huggingface_hub import AsyncInferenceClient, InferenceClient

    count = 0
    for cls, is_async in ((InferenceClient, False), (AsyncInferenceClient, True)):
        count += patches.wrap(
            cls,
            "chat_completion",
            _wrap(observer, CHAT, "chat", _after_chat, "ChatCompletionOutput", is_async),
        )
        count += patches.wrap(
            cls,
            "text_generation",
            _wrap(observer, TEXT, "text_completion", _after_text, "TextGenerationOutput", is_async),
        )
    return count
