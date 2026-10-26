"""Shared machinery for automatic instrumentation of LLM client libraries.

Rules every integration follows:

* **Never break the caller.** Reading attributes off requests and responses is
  wrapped so that a library change costs telemetry, not the user's call.
* **Non-streaming calls are replayable**: the response is recorded exactly and
  rebuilt as the library's own response type during a replay.
* **Streamed calls are traced** (tokens, finish reason, text) and kept open until
  the caller finishes, closes, or exits the stream. They are not recorded for
  replay yet, and say so, so the trace reads as partially replayable.
"""

from __future__ import annotations

import functools
import logging
from collections.abc import Callable
from typing import Any
from urllib.parse import urlparse

from obzerve import attributes as attrs
from obzerve.observer import _LIVE, ManualSpan, Observer, ObzerveSpan, _ReplayableCall
from obzerve.replay import ReplayMissError, active_session, input_hash

logger = logging.getLogger("obzerve.instrumentation")

# Arguments that never belong in a recorded input: transport options, and headers
# that may carry credentials.
TRANSPORT_ARGS = ("extra_headers", "extra_query", "extra_body", "timeout")

STREAM_NOT_RECORDED = "streamed responses are not recorded for replay yet"


def safely(fn: Callable[..., Any]) -> Callable[..., Any]:
    """Run telemetry code; on failure log at debug level and carry on."""

    @functools.wraps(fn)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        try:
            return fn(*args, **kwargs)
        except Exception:
            logger.debug("obzerve: could not record telemetry in %s", fn.__name__, exc_info=True)
            return None

    return wrapper


def set_present(span: ObzerveSpan, values: dict[str, Any]) -> None:
    """Set the attributes whose value is a plain, non-empty OpenTelemetry type."""
    clean = {}
    for key, value in values.items():
        if value is None or value == "" or value == []:
            continue
        if isinstance(value, (str, bool, int, float)):
            clean[key] = value
        elif isinstance(value, (list, tuple)) and all(isinstance(v, str) for v in value):
            clean[key] = list(value)
    if clean:
        span.set_attributes(clean)


def server_address(base_url: Any) -> str | None:
    try:
        parsed = urlparse(str(base_url))
        return parsed.hostname
    except Exception:
        return None


def get(obj: Any, name: str, default: Any = None) -> Any:
    """Attribute or key access, for SDK objects that are models or dicts."""
    if obj is None:
        return default
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


class Patches:
    """Replaces attributes on classes and remembers the originals."""

    def __init__(self) -> None:
        self._originals: list[tuple[Any, str, Any]] = []

    def wrap(self, owner: Any, name: str, make_wrapper: Callable[[Any], Any]) -> bool:
        original = owner.__dict__.get(name) if hasattr(owner, "__dict__") else None
        if original is None or getattr(original, "__obzerve_wrapped__", False):
            return False
        wrapper = make_wrapper(original)
        wrapper.__obzerve_wrapped__ = True
        wrapper.__wrapped__ = original
        setattr(owner, name, wrapper)
        self._originals.append((owner, name, original))
        return True

    def restore(self) -> None:
        while self._originals:
            owner, name, original = self._originals.pop()
            setattr(owner, name, original)

    def __len__(self) -> int:
        return len(self._originals)


class Call:
    """One instrumented library method: a replayable call plus attribute hooks."""

    def __init__(
        self,
        observer: Observer,
        name: str,
        original: Callable[..., Any],
        *,
        kind: str,
        encode: Callable[[Any], Any] | None = None,
        decode: Callable[[Any], Any] | None = None,
        before: Callable[[ObzerveSpan, tuple, dict], None] | None = None,
        after: Callable[[ObzerveSpan, Any], None] | None = None,
        exclude_args: tuple[str, ...] = TRANSPORT_ARGS,
        normalize_input: Callable[[dict[str, Any]], Any] | None = None,
    ):
        self.observer = observer
        self.name = name
        self.original = original
        self.kind = kind
        self.before = safely(before) if before else None
        self.after = safely(after) if after else None
        self.replayable = _ReplayableCall(
            observer, name, original, kind, encode, decode, False, exclude_args, normalize_input
        )

    def run(self, args: tuple, kwargs: dict) -> Any:
        with self.replayable.span(args, kwargs) as state:
            if self.before:
                self.before(state.span, args, kwargs)
            if state.replay_value is not _LIVE:
                return state.replay_value
            result = self.original(*args, **kwargs)
            if self.after:
                self.after(state.span, result)
            return self.replayable.record(state, result)

    async def run_async(self, args: tuple, kwargs: dict) -> Any:
        with self.replayable.span(args, kwargs) as state:
            if self.before:
                self.before(state.span, args, kwargs)
            if state.replay_value is not _LIVE:
                return state.replay_value
            result = await self.original(*args, **kwargs)
            if self.after:
                self.after(state.span, result)
            return self.replayable.record(state, result)

    def start_stream_span(self, args: tuple, kwargs: dict) -> ManualSpan:
        """A span for a streamed call, which stays open while the caller reads."""
        key = self.observer._next_replay_key(self.name)
        arguments = self.replayable._arguments(args, kwargs)
        session = active_session.get()
        if session is not None and not session.policy.is_live(self.kind):
            raise ReplayMissError(
                f"{key}: {STREAM_NOT_RECORDED}; replay with a policy that runs "
                f"'{self.kind}' calls live (e.g. LLM_LIVE)"
            )
        span = self.observer.start_span(self.name, kind=self.kind, input=arguments)
        span.set_attributes(
            {
                attrs.REPLAY_KEY: key,
                attrs.REPLAY_INPUT_HASH: input_hash(arguments),
                attrs.REPLAY_COMPLETE: False,
                attrs.REPLAY_INCOMPLETE_REASON: STREAM_NOT_RECORDED,
            }
        )
        if session is not None:
            session.live_calls += 1
            span.set_attribute(attrs.REPLAY_SOURCE, "live")
        if self.before:
            self.before(span, args, kwargs)
        return span


class Accumulator:
    """Collects what a stream delivered, for the span recorded when it ends."""

    def __init__(self) -> None:
        self.text: list[str] = []
        self.values: dict[str, Any] = {}

    def output(self) -> str:
        return "".join(self.text)


class StreamProxy:
    """Wraps a library's stream: forwards everything, observes each item, and ends
    the span exactly once (exhausted, closed, exited, or failed)."""

    def __init__(self, stream: Any, on_item: Callable[[Any], None], on_end: Callable[..., None]):
        self._stream = stream
        self._iterator = None
        self._on_item = safely(on_item)
        self._on_end = on_end
        self._ended = False

    def _end(self, error: BaseException | None = None) -> None:
        if not self._ended:
            self._ended = True
            self._on_end(error)

    def __iter__(self):
        return self

    def __next__(self):
        if self._iterator is None:
            self._iterator = iter(self._stream)
        try:
            item = next(self._iterator)
        except StopIteration:
            self._end()
            raise
        except BaseException as exc:
            self._end(exc)
            raise
        self._on_item(item)
        return item

    def close(self) -> None:
        try:
            close = getattr(self._stream, "close", None)
            if close is not None:
                close()
        finally:
            self._end()

    def __enter__(self):
        enter = getattr(self._stream, "__enter__", None)
        if enter is not None:
            enter()
        return self

    def __exit__(self, exc_type, exc, tb):
        try:
            exit_ = getattr(self._stream, "__exit__", None)
            return exit_(exc_type, exc, tb) if exit_ is not None else None
        finally:
            self._end(exc)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._stream, name)


class AsyncStreamProxy:
    """Async counterpart of ``StreamProxy``."""

    def __init__(self, stream: Any, on_item: Callable[[Any], None], on_end: Callable[..., None]):
        self._stream = stream
        self._iterator = None
        self._on_item = safely(on_item)
        self._on_end = on_end
        self._ended = False

    def _end(self, error: BaseException | None = None) -> None:
        if not self._ended:
            self._ended = True
            self._on_end(error)

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self._iterator is None:
            self._iterator = self._stream.__aiter__()
        try:
            item = await self._iterator.__anext__()
        except StopAsyncIteration:
            self._end()
            raise
        except BaseException as exc:
            self._end(exc)
            raise
        self._on_item(item)
        return item

    async def close(self) -> None:
        try:
            close = getattr(self._stream, "close", None) or getattr(self._stream, "aclose", None)
            if close is not None:
                result = close()
                if hasattr(result, "__await__"):
                    await result
        finally:
            self._end()

    async def __aenter__(self):
        enter = getattr(self._stream, "__aenter__", None)
        if enter is not None:
            await enter()
        return self

    async def __aexit__(self, exc_type, exc, tb):
        try:
            exit_ = getattr(self._stream, "__aexit__", None)
            return await exit_(exc_type, exc, tb) if exit_ is not None else None
        finally:
            self._end(exc)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._stream, name)


def finish_stream(span: ManualSpan, finish: Callable[[ManualSpan], None], error: Any) -> None:
    safely(finish)(span)
    span.end(error if isinstance(error, BaseException) else None)
