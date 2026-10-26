"""The Observer: Obzerve's entry point for instrumenting an agent.

Built on the official OpenTelemetry SDK. Spans are handed to a
``BatchSpanProcessor`` that exports them from a background thread through a
bounded queue, so instrumentation never blocks on the network: when the
backend is slow or down, spans are dropped rather than buffered without limit
or surfaced as errors in the agent. ``Observer.stats()`` reports how many spans
were exported, failed, or dropped.

Operations wrapped with ``Observer.replayable`` also record their exact output so
the trace can later be replayed (see ``obzerve.replay``).
"""

from __future__ import annotations

import functools
import inspect
import json
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any, TypeVar

from opentelemetry import context as otel_context
from opentelemetry import trace as otel_trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SpanExporter
from opentelemetry.trace import Status, StatusCode
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator

from obzerve import attributes as attrs
from obzerve._version import __version__
from obzerve.attributes import SpanKind
from obzerve.delivery import AccountingProcessor, DeliveryStats
from obzerve.payloads import DEFAULT_REDACT_KEYS, PayloadSerializer
from obzerve.replay import (
    ReplayBundle,
    ReplayMissError,
    ReplayPolicy,
    ReplaySession,
    SideEffectBlocked,
    active_session,
    class_path,
    input_hash,
    rebuild_exception,
)

F = TypeVar("F", bound=Callable[..., Any])

_MAX_ERROR_MESSAGE_CHARS = 2000
_propagator = TraceContextTextMapPropagator()
# Per-trace call counters, so the n-th call of an operation gets a stable replay key.
_occurrences: ContextVar[dict[str, int] | None] = ContextVar("obzerve_occurrences", default=None)


class ObzerveSpan:
    """Thin wrapper around an OpenTelemetry span with AI-specific helpers."""

    def __init__(
        self, span: otel_trace.Span, serializer: PayloadSerializer, capture_payloads: bool
    ):
        self._span = span
        self._serializer = serializer
        self._capture = capture_payloads

    @property
    def otel_span(self) -> otel_trace.Span:
        return self._span

    @property
    def trace_id(self) -> str:
        return format(self._span.get_span_context().trace_id, "032x")

    @property
    def span_id(self) -> str:
        return format(self._span.get_span_context().span_id, "016x")

    def set_attribute(self, key: str, value: Any) -> None:
        self._span.set_attribute(key, value)

    def set_attributes(self, values: Mapping[str, Any]) -> None:
        self._span.set_attributes(dict(values))

    def set_input(self, value: Any) -> None:
        self._set_payload(attrs.INPUT, attrs.INPUT_TRUNCATED, value)

    def set_output(self, value: Any) -> None:
        self._set_payload(attrs.OUTPUT, attrs.OUTPUT_TRUNCATED, value)

    def set_llm(
        self,
        *,
        model: str | None = None,
        provider: str | None = None,
        input_tokens: int | None = None,
        output_tokens: int | None = None,
        cached_input_tokens: int | None = None,
        response_model: str | None = None,
    ) -> None:
        """Record model/provider metadata and token usage for an LLM call."""
        values = {
            attrs.GEN_AI_REQUEST_MODEL: model,
            attrs.GEN_AI_PROVIDER: provider,
            attrs.GEN_AI_INPUT_TOKENS: input_tokens,
            attrs.GEN_AI_OUTPUT_TOKENS: output_tokens,
            attrs.GEN_AI_CACHED_INPUT_TOKENS: cached_input_tokens,
            attrs.GEN_AI_RESPONSE_MODEL: response_model,
        }
        self._span.set_attributes({k: v for k, v in values.items() if v is not None})

    def record_retry(self, attempt: int, error: BaseException | None = None) -> None:
        """Note that the operation is being retried; ``attempt`` is the retry number (1-based)."""
        self._span.set_attribute(attrs.RETRY_COUNT, attempt)
        event_attrs: dict[str, Any] = {attrs.RETRY_ATTEMPT: attempt}
        if error is not None:
            event_attrs[attrs.ERROR_TYPE] = type(error).__name__
            event_attrs["exception.message"] = _truncate(str(error))
        self._span.add_event("retry", event_attrs)

    def add_event(self, name: str, attributes: Mapping[str, Any] | None = None) -> None:
        self._span.add_event(name, dict(attributes or {}))

    def set_error(self, error: BaseException | str) -> None:
        """Mark the span as failed without raising (e.g. a handled failure)."""
        if isinstance(error, BaseException):
            _record_error(self._span, error)
        else:
            self._span.set_status(Status(StatusCode.ERROR, _truncate(error)))

    def _set_payload(self, key: str, truncated_key: str, value: Any) -> None:
        if not self._capture:
            return
        text, truncated, redacted = self._serializer.serialize_with_flags(value)
        self._span.set_attribute(key, text)
        if truncated:
            self._span.set_attribute(truncated_key, True)
        if redacted and key == attrs.INPUT:
            self._span.set_attribute(attrs.INPUT_REDACTED, True)


class ManualSpan(ObzerveSpan):
    """A span ended explicitly (see ``Observer.start_span``). Ending twice is a no-op."""

    def __init__(
        self, span: otel_trace.Span, serializer: PayloadSerializer, capture_payloads: bool
    ):
        super().__init__(span, serializer, capture_payloads)
        self.ended = False

    def end(self, error: BaseException | None = None) -> None:
        if self.ended:
            return
        self.ended = True
        if error is not None:
            _record_error(self._span, error)
        elif self._span.status.status_code is StatusCode.UNSET:
            self._span.set_status(Status(StatusCode.OK))
        self._span.end()


class Observer:
    """Records agent executions as traces and ships them to an Obzerve server.

    Example::

        observer = Observer(endpoint="http://localhost:8000", api_key="...")
        with observer.trace("research_agent", input=question) as trace:
            with observer.span("search_web", kind=SpanKind.TOOL) as span:
                ...
            trace.set_output(answer)
    """

    def __init__(
        self,
        endpoint: str = "http://localhost:8000",
        api_key: str | None = None,
        service_name: str = "agent",
        *,
        capture_payloads: bool = True,
        max_payload_chars: int = 8192,
        redact_keys: tuple[str, ...] = DEFAULT_REDACT_KEYS,
        max_queue_size: int = 2048,
        max_export_batch_size: int = 512,
        schedule_delay_ms: int = 1000,
        export_timeout_s: float = 5.0,
        enabled: bool = True,
        exporter: SpanExporter | None = None,
        replay_max_payload_chars: int = 256 * 1024,
    ):
        self._capture_payloads = capture_payloads
        self._replay_max_chars = replay_max_payload_chars
        self._serializer = PayloadSerializer(redact_keys=redact_keys, max_chars=max_payload_chars)
        self._enabled = enabled

        resource = Resource.create(
            {attrs.SERVICE_NAME: service_name, attrs.SDK_VERSION: __version__}
        )
        # A private provider: Obzerve never replaces the application's global OpenTelemetry setup.
        self._provider = TracerProvider(resource=resource)
        if enabled:
            if exporter is None:
                exporter = _otlp_exporter(endpoint, api_key, export_timeout_s)
            self._processor: AccountingProcessor | None = AccountingProcessor(
                exporter,
                max_queue_size=max_queue_size,
                max_export_batch_size=max_export_batch_size,
                schedule_delay_ms=schedule_delay_ms,
                export_timeout_s=export_timeout_s,
            )
            self._provider.add_span_processor(self._processor)
        else:
            self._processor = None
        self._tracer = self._provider.get_tracer("obzerve", __version__)
        # ID of the most recently started root trace (e.g. to link to a failed run).
        self.last_trace_id: str | None = None

    @contextmanager
    def trace(
        self,
        name: str,
        *,
        kind: str = SpanKind.AGENT,
        input: Any = None,
        attributes: Mapping[str, Any] | None = None,
        parent_headers: Mapping[str, str] | None = None,
    ) -> Iterator[ObzerveSpan]:
        """Start a workflow execution (the root span of a trace).

        Pass ``parent_headers`` (W3C ``traceparent``) received from an upstream
        service to continue its trace instead of starting a new one.
        """
        token = None
        if parent_headers is not None:
            token = otel_context.attach(_propagator.extract(carrier=dict(parent_headers)))
        is_root = not otel_trace.get_current_span().get_span_context().is_valid
        counters = _occurrences.set({}) if is_root else None
        try:
            with self.span(name, kind=kind, attributes=attributes) as span:
                if is_root:
                    self._mark_root(span, input)
                elif input is not None:
                    span.set_input(input)
                yield span
        finally:
            if counters is not None:
                _occurrences.reset(counters)
            if token is not None:
                otel_context.detach(token)

    def _mark_root(self, span: ObzerveSpan, input: Any) -> None:
        self.last_trace_id = span.trace_id
        if input is not None and self._capture_payloads:
            # The root input is what a replay re-runs the agent with, so record it exactly.
            exact = self._serializer.serialize_exact(input, self._replay_max_chars)
            span.set_attribute(attrs.INPUT, exact.text)
            span.set_attribute(attrs.REPLAY_COMPLETE, exact.complete)
            if exact.reason:
                span.set_attribute(attrs.REPLAY_INCOMPLETE_REASON, exact.reason)
            if exact.redacted:
                span.set_attribute(attrs.INPUT_REDACTED, True)
        session = active_session.get()
        if session is not None and session.root_trace_id is None:
            session.root_trace_id = span.trace_id
            span.set_attributes(
                {
                    attrs.REPLAY_OF: session.bundle.trace_id,
                    attrs.REPLAY_MODE: session.policy.name,
                    **({attrs.REPLAY_LABEL: session.label} if session.label else {}),
                    **session.attributes,
                }
            )

    @contextmanager
    def span(
        self,
        name: str,
        *,
        kind: str = SpanKind.FUNCTION,
        input: Any = None,
        attributes: Mapping[str, Any] | None = None,
    ) -> Iterator[ObzerveSpan]:
        """Time one operation; it becomes a child of the currently active span."""
        with self._tracer.start_as_current_span(
            name, record_exception=False, set_status_on_exception=False
        ) as otel_span:
            span = ObzerveSpan(otel_span, self._serializer, self._capture_payloads)
            span.set_attribute(attrs.SPAN_KIND, kind)
            if attributes:
                span.set_attributes(attributes)
            if input is not None:
                span.set_input(input)
            try:
                yield span
            except BaseException as exc:
                _record_error(otel_span, exc)
                raise
            else:
                if otel_span.status.status_code is StatusCode.UNSET:
                    otel_span.set_status(Status(StatusCode.OK))

    def observe(
        self,
        name: str | None = None,
        *,
        kind: str = SpanKind.FUNCTION,
        capture_input: bool = True,
        capture_output: bool = True,
    ) -> Callable[[F], F]:
        """Decorator that wraps a sync or async function in a span."""

        def decorator(fn: F) -> F:
            span_name = name or fn.__qualname__

            def _input(args: tuple, kwargs: dict) -> Any:
                if not capture_input:
                    return None
                try:
                    bound = inspect.signature(fn).bind_partial(*args, **kwargs)
                    return {k: v for k, v in bound.arguments.items() if k not in ("self", "cls")}
                except TypeError:
                    return {"args": args, "kwargs": kwargs}

            if inspect.iscoroutinefunction(fn):

                @functools.wraps(fn)
                async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
                    with self.span(span_name, kind=kind, input=_input(args, kwargs)) as span:
                        result = await fn(*args, **kwargs)
                        if capture_output:
                            span.set_output(result)
                        return result

                return async_wrapper  # type: ignore[return-value]

            @functools.wraps(fn)
            def wrapper(*args: Any, **kwargs: Any) -> Any:
                with self.span(span_name, kind=kind, input=_input(args, kwargs)) as span:
                    result = fn(*args, **kwargs)
                    if capture_output:
                        span.set_output(result)
                    return result

            return wrapper  # type: ignore[return-value]

        return decorator

    def replayable(
        self,
        name: str | None = None,
        *,
        kind: str = SpanKind.FUNCTION,
        encode: Callable[[Any], Any] | None = None,
        decode: Callable[[Any], Any] | None = None,
        side_effect: bool = False,
        exclude_args: tuple[str, ...] = (),
    ) -> Callable[[F], F]:
        """Decorator for an operation whose result can be recorded and replayed.

        ``encode`` turns the return value into JSON-native data and ``decode``
        rebuilds it on replay (defaults: identity). Mark operations that change
        the outside world (send email, write records, pay) with
        ``side_effect=True``: replays never run them live unless explicitly
        allowed. ``exclude_args`` leaves test-harness knobs out of the input
        hash used to detect a replay diverging from the recording.
        """

        def decorator(fn: F) -> F:
            # The plain function name keeps replay keys stable across refactors.
            call = _ReplayableCall(
                self, name or fn.__name__, fn, kind, encode, decode, side_effect, exclude_args
            )
            if inspect.iscoroutinefunction(fn):

                @functools.wraps(fn)
                async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
                    with call.span(args, kwargs) as state:
                        if state.replay_value is not _LIVE:
                            return state.replay_value
                        return call.record(state, await fn(*args, **kwargs))

                return async_wrapper  # type: ignore[return-value]

            @functools.wraps(fn)
            def wrapper(*args: Any, **kwargs: Any) -> Any:
                with call.span(args, kwargs) as state:
                    if state.replay_value is not _LIVE:
                        return state.replay_value
                    return call.record(state, fn(*args, **kwargs))

            return wrapper  # type: ignore[return-value]

        return decorator

    @contextmanager
    def replay(
        self,
        bundle: ReplayBundle,
        policy: ReplayPolicy,
        *,
        label: str | None = None,
        attributes: Mapping[str, Any] | None = None,
    ) -> Iterator[ReplaySession]:
        """Run the code inside as a replay of ``bundle``.

        Call the agent's entry point with ``bundle.root_input``; its trace is
        linked to the original (``obzerve.replay.of``) and replayable calls
        follow ``policy``. ``attributes`` (e.g. a prompt version) are added to
        the replay's root span so the changed configuration is on record.
        """
        session = ReplaySession(bundle, policy, label, dict(attributes or {}))
        token = active_session.set(session)
        try:
            yield session
        finally:
            active_session.reset(token)

    def _next_replay_key(self, name: str) -> str:
        counters = _occurrences.get()
        if counters is None:  # called outside a trace: still give it a usable key
            return f"{name}#1"
        counters[name] = counters.get(name, 0) + 1
        return f"{name}#{counters[name]}"

    def start_span(
        self,
        name: str,
        *,
        kind: str = SpanKind.FUNCTION,
        input: Any = None,
        attributes: Mapping[str, Any] | None = None,
    ) -> ManualSpan:
        """Start a span that is *not* made current and is ended explicitly with
        ``end()`` -- for work that outlives a ``with`` block, such as a streamed
        response consumed later by the caller."""
        otel_span = self._tracer.start_span(name)
        span = ManualSpan(otel_span, self._serializer, self._capture_payloads)
        span.set_attribute(attrs.SPAN_KIND, kind)
        if attributes:
            span.set_attributes(attributes)
        if input is not None:
            span.set_input(input)
        return span

    def instrument(self, **libraries: bool | None):
        """Trace calls made through supported LLM client libraries automatically.
        See ``obzerve.instrumentation.instrument`` for the options."""
        from obzerve.instrumentation import instrument

        return instrument(self, **libraries)

    def current_span(self) -> ObzerveSpan:
        return ObzerveSpan(otel_trace.get_current_span(), self._serializer, self._capture_payloads)

    def inject_headers(self, headers: dict[str, str] | None = None) -> dict[str, str]:
        """Add W3C trace-context headers for calling another instrumented service."""
        headers = {} if headers is None else headers
        _propagator.inject(carrier=headers)
        return headers

    def stats(self) -> DeliveryStats:
        """Delivery counters: exported, failed, dropped (buffer full), and pending spans."""
        if self._processor is None:
            return DeliveryStats(0, 0, 0, 0, 0)
        return self._processor.stats()

    def flush(self, timeout_ms: int = 5000) -> bool:
        """Export everything buffered so far. Returns False if the timeout elapsed."""
        return self._provider.force_flush(timeout_ms)

    def shutdown(self) -> None:
        self._provider.shutdown()


_LIVE = object()


class _CallState:
    def __init__(self, span: ObzerveSpan, key: str, replay_value: Any):
        self.span = span
        self.key = key
        self.replay_value = replay_value


class _ReplayableCall:
    """Shared logic of ``Observer.replayable`` for sync and async functions."""

    def __init__(
        self,
        observer,
        name,
        fn,
        kind,
        encode,
        decode,
        side_effect,
        exclude_args,
        normalize_input: Callable[[dict[str, Any]], Any] | None = None,
    ):
        self.observer: Observer = observer
        # Turns the bound arguments into JSON-friendly data for display and hashing
        # (e.g. tensors -> lists), so the input hash does not depend on repr().
        self.normalize_input = normalize_input
        self.name = name
        self.fn = fn
        self.kind = kind
        self.encode = encode or (lambda v: v)
        self.decode = decode or (lambda v: v)
        self.side_effect = side_effect
        self.exclude_args = set(exclude_args) | {"self", "cls"}
        self.signature = inspect.signature(fn)

    def _arguments(self, args: tuple, kwargs: dict) -> dict[str, Any]:
        try:
            bound = self.signature.bind_partial(*args, **kwargs)
            return {k: v for k, v in bound.arguments.items() if k not in self.exclude_args}
        except TypeError:
            return {"args": list(args), "kwargs": kwargs}

    @contextmanager
    def span(self, args: tuple, kwargs: dict) -> Iterator[_CallState]:
        obs = self.observer
        key = obs._next_replay_key(self.name)
        arguments = self._arguments(args, kwargs)
        if self.normalize_input is not None:
            try:
                arguments = self.normalize_input(arguments)
            except Exception:  # fall back to the raw arguments; never break the call
                pass
        digest = input_hash(arguments)
        session = active_session.get()
        with obs.span(self.name, kind=self.kind, input=arguments) as span:
            span.set_attributes({attrs.REPLAY_KEY: key, attrs.REPLAY_INPUT_HASH: digest})
            if self.side_effect:
                span.set_attribute(attrs.REPLAY_SIDE_EFFECT, True)
            state = _CallState(span, key, _LIVE)
            if session is not None:
                state.replay_value = self._from_recording(session, span, key, digest)
            try:
                yield state
            except BaseException as exc:
                if state.replay_value is _LIVE:
                    # A raised exception is a recordable outcome too.
                    span.set_attributes(
                        {attrs.REPLAY_ERROR_CLASS: class_path(exc), attrs.REPLAY_COMPLETE: True}
                    )
                raise

    def _from_recording(self, session: ReplaySession, span: ObzerveSpan, key: str, digest: str):
        policy = session.policy
        live = policy.is_live(self.kind) and (not self.side_effect or policy.allow_side_effects)
        recording = None if live else session.recording_for(key)
        if live or recording is None:
            if not live and not policy.live_on_missing:
                raise ReplayMissError(f"no recording for {key} in trace {session.bundle.trace_id}")
            if self.side_effect and not policy.allow_side_effects:
                raise SideEffectBlocked(f"{key} has side effects; replay did not run it live")
            session.live_calls += 1
            span.set_attribute(attrs.REPLAY_SOURCE, "live")
            if recording is None and not live:
                span.add_event("replay_missing_recording", {attrs.REPLAY_KEY: key})
            return _LIVE

        session.recorded_calls += 1
        matched = recording.input_hash is None or recording.input_hash == digest
        span.set_attributes({attrs.REPLAY_SOURCE: "recorded", attrs.REPLAY_INPUT_MATCHED: matched})
        if not matched:
            session.divergences.append(key)
            span.add_event("replay_divergence", {attrs.REPLAY_KEY: key})
        if recording.error_type:
            if recording.error_class:
                span.set_attributes(
                    {attrs.REPLAY_ERROR_CLASS: recording.error_class, attrs.REPLAY_COMPLETE: True}
                )
            raise rebuild_exception(recording)
        if not recording.complete or recording.output is None:
            reason = recording.incomplete_reason or "output was not recorded exactly"
            raise ReplayMissError(f"recording for {key} is incomplete: {reason}")
        span.set_attributes({attrs.OUTPUT: recording.output, attrs.REPLAY_COMPLETE: True})
        return self.decode(json.loads(recording.output))

    def record(self, state: _CallState, result: Any) -> Any:
        obs = self.observer
        if obs._capture_payloads:
            try:
                encoded = self.encode(result)
            except Exception as exc:  # recording must never break the caller
                state.span.set_attributes(
                    {
                        attrs.REPLAY_COMPLETE: False,
                        attrs.REPLAY_INCOMPLETE_REASON: f"could not encode result: {exc!r}"[:300],
                    }
                )
                return result
            exact = obs._serializer.serialize_exact(encoded, obs._replay_max_chars)
            state.span.set_attributes(
                {attrs.OUTPUT: exact.text, attrs.REPLAY_COMPLETE: exact.complete}
            )
            if exact.reason:
                state.span.set_attribute(attrs.REPLAY_INCOMPLETE_REASON, exact.reason)
        return result


def _otlp_exporter(endpoint: str, api_key: str | None, timeout_s: float) -> SpanExporter:
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    return OTLPSpanExporter(
        endpoint=endpoint.rstrip("/") + "/v1/traces", headers=headers, timeout=timeout_s
    )


def _record_error(span: otel_trace.Span, exc: BaseException) -> None:
    message = _truncate(str(exc)) or type(exc).__name__
    span.set_attribute(attrs.ERROR_TYPE, type(exc).__name__)
    span.record_exception(exc, attributes={"exception.message": message})
    span.set_status(Status(StatusCode.ERROR, message))


def _truncate(text: str) -> str:
    return text if len(text) <= _MAX_ERROR_MESSAGE_CHARS else text[:_MAX_ERROR_MESSAGE_CHARS] + "…"
