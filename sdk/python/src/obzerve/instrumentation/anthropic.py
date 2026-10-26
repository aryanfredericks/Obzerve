"""Anthropic Python SDK: ``messages.create`` (sync and async, including
``stream=True``) and the ``messages.stream()`` helper.

Token usage follows the OpenTelemetry GenAI conventions: the recorded input
tokens include prompt-cache reads and writes (Anthropic reports
``input_tokens`` excluding them), and cache reads are recorded separately as
cached input tokens, which Obzerve prices at the model's cache rate.

The SDK can also emit its own OpenTelemetry spans to the *global* tracer
provider. Obzerve uses its own provider, so the two do not collide; if you send
the SDK's spans to Obzerve as well, turn one of them off (for the SDK:
``Anthropic(open_telemetry=False)``) or each request appears twice.
"""

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
    safely,
    server_address,
    set_present,
)
from obzerve.observer import ObzerveSpan

MESSAGES = "anthropic.messages"


def _before(span: ObzerveSpan, args: tuple, kwargs: dict) -> None:
    resource = args[0]
    set_present(
        span,
        {
            "gen_ai.operation.name": "chat",
            attrs.GEN_AI_PROVIDER: "anthropic",
            "server.address": server_address(
                getattr(getattr(resource, "_client", None), "base_url", None)
            ),
            attrs.GEN_AI_REQUEST_MODEL: kwargs.get("model"),
            "gen_ai.request.max_tokens": _plain(kwargs.get("max_tokens")),
            "gen_ai.request.temperature": _plain(kwargs.get("temperature")),
            "gen_ai.request.stream": bool(kwargs.get("stream")),
        },
    )


def _plain(value: Any) -> Any:
    return value if isinstance(value, (int, float, str, bool)) else None


def _usage(span: ObzerveSpan, usage: Any) -> None:
    if usage is None:
        return
    uncached = get(usage, "input_tokens") or 0
    read = get(usage, "cache_read_input_tokens") or 0
    written = get(usage, "cache_creation_input_tokens") or 0
    span.set_llm(
        input_tokens=uncached + read + written,
        output_tokens=get(usage, "output_tokens"),
        cached_input_tokens=read or None,
    )
    if written:
        span.set_attribute("gen_ai.usage.cache_creation.input_tokens", written)


def _text(message: Any) -> str:
    return "".join(
        get(block, "text") or ""
        for block in get(message, "content") or []
        if get(block, "type") == "text"
    )


def _after(span: ObzerveSpan, message: Any) -> None:
    set_present(
        span,
        {
            attrs.GEN_AI_RESPONSE_MODEL: get(message, "model"),
            "gen_ai.response.id": get(message, "id"),
            "gen_ai.response.finish_reasons": (
                [str(get(message, "stop_reason"))] if get(message, "stop_reason") else []
            ),
        },
    )
    _usage(span, get(message, "usage"))


def _on_event(acc: Accumulator, event: Any) -> None:
    kind = get(event, "type")
    if kind == "message_start":
        message = get(event, "message")
        acc.values.update(model=get(message, "model"), id=get(message, "id"))
        acc.values["usage"] = dict(_usage_dict(get(message, "usage")))
    elif kind == "content_block_delta":
        delta = get(event, "delta")
        if get(delta, "type") == "text_delta":
            acc.text.append(get(delta, "text") or "")
    elif kind == "message_delta":
        if get(get(event, "delta"), "stop_reason"):
            acc.values["stop_reason"] = str(get(get(event, "delta"), "stop_reason"))
        # message_delta usage is cumulative: later values replace earlier ones.
        acc.values.setdefault("usage", {}).update(
            {k: v for k, v in _usage_dict(get(event, "usage")).items() if v is not None}
        )


def _usage_dict(usage: Any) -> dict[str, Any]:
    keys = (
        "input_tokens",
        "output_tokens",
        "cache_read_input_tokens",
        "cache_creation_input_tokens",
    )
    return {k: get(usage, k) for k in keys} if usage is not None else {}


def _finish_events(acc: Accumulator):
    def finish(span: ObzerveSpan) -> None:
        set_present(
            span,
            {
                attrs.GEN_AI_RESPONSE_MODEL: acc.values.get("model"),
                "gen_ai.response.id": acc.values.get("id"),
                "gen_ai.response.finish_reasons": (
                    [acc.values["stop_reason"]] if "stop_reason" in acc.values else []
                ),
            },
        )
        _usage(span, acc.values.get("usage"))
        span.set_output(acc.output())

    return finish


def _codec():
    def decode(data: Any) -> Any:
        from anthropic.types import Message

        return Message.model_validate(data)

    return (lambda message: message.model_dump(mode="json")), decode


def _wrap_create(observer, is_async: bool):
    encode, decode = _codec()

    def make(original):
        call = Call(
            observer,
            MESSAGES,
            original,
            kind=SpanKind.LLM,
            encode=encode,
            decode=decode,
            before=_before,
            after=_after,
        )

        def start(args, kwargs):
            acc = Accumulator()
            span = call.start_stream_span(args, kwargs)
            return span, (lambda e: _on_event(acc, e)), _finish_events(acc)

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


class _StreamManager:
    """Wraps ``messages.stream()``'s manager: the request is made on enter, and the
    span ends on exit with the final message the helper accumulated."""

    def __init__(self, call: Call, manager: Any, args: tuple, kwargs: dict):
        self._call, self._manager, self._args, self._kwargs = call, manager, args, kwargs
        self._span = None
        self._stream = None

    def _open(self):
        self._span = self._call.start_stream_span(self._args, self._kwargs)
        self._span.set_attribute("gen_ai.request.stream", True)

    def _close(self, exc: BaseException | None) -> None:
        if self._span is None:
            return
        snapshot = safely(lambda: self._stream.current_message_snapshot)() if self._stream else None
        if snapshot is not None:
            safely(_after)(self._span, snapshot)
            safely(self._span.set_output)(_text(snapshot))
        self._span.end(exc)

    def __enter__(self):
        self._open()
        try:
            self._stream = self._manager.__enter__()
        except BaseException as exc:
            self._span.end(exc)
            raise
        return self._stream

    def __exit__(self, exc_type, exc, tb):
        try:
            return self._manager.__exit__(exc_type, exc, tb)
        finally:
            self._close(exc)

    async def __aenter__(self):
        self._open()
        try:
            self._stream = await self._manager.__aenter__()
        except BaseException as exc:
            self._span.end(exc)
            raise
        return self._stream

    async def __aexit__(self, exc_type, exc, tb):
        try:
            return await self._manager.__aexit__(exc_type, exc, tb)
        finally:
            self._close(exc)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._manager, name)


def _wrap_stream(observer):
    def make(original):
        call = Call(observer, MESSAGES, original, kind=SpanKind.LLM, before=_before)

        def wrapper(*args: Any, **kwargs: Any) -> Any:
            return _StreamManager(call, original(*args, **kwargs), args, kwargs)

        return wrapper

    return make


def instrument(observer, patches: Patches) -> int:
    from anthropic.resources import messages

    count = patches.wrap(messages.Messages, "create", _wrap_create(observer, False))
    count += patches.wrap(messages.AsyncMessages, "create", _wrap_create(observer, True))
    count += patches.wrap(messages.Messages, "stream", _wrap_stream(observer))
    count += patches.wrap(messages.AsyncMessages, "stream", _wrap_stream(observer))
    return count
