"""Bounded, accounted span delivery.

Wraps OpenTelemetry's ``BatchSpanProcessor`` and exporter so that every ended
span is counted as exactly one of: exported, failed (the backend rejected it
or was unreachable after the exporter's retries), dropped (the buffer was
full), or still pending. The buffer bound is enforced here, before the batch
processor's own queue, so drops are counted precisely.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Sequence
from dataclasses import dataclass

from opentelemetry.context import Context
from opentelemetry.sdk.trace import ReadableSpan, Span, SpanProcessor
from opentelemetry.sdk.trace.export import BatchSpanProcessor, SpanExporter, SpanExportResult

logger = logging.getLogger("obzerve")

_WARN_INTERVAL_S = 60.0


@dataclass(frozen=True)
class DeliveryStats:
    """Telemetry delivery counters since the Observer was created."""

    spans_ended: int
    spans_exported: int
    spans_failed: int
    spans_dropped: int
    spans_pending: int


class _Counters:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.ended = 0
        self.exported = 0
        self.failed = 0
        self.dropped = 0
        self.pending = 0
        self._last_warning = 0.0

    def warn(self, message: str, *args: object) -> None:
        """Log at most once per interval, so a dead backend cannot flood the agent's logs."""
        now = time.monotonic()
        if now - self._last_warning >= _WARN_INTERVAL_S:
            self._last_warning = now
            logger.warning(message, *args)


class AccountingExporter(SpanExporter):
    def __init__(self, inner: SpanExporter, counters: _Counters):
        self._inner = inner
        self._counters = counters

    def export(self, spans: Sequence[ReadableSpan]) -> SpanExportResult:
        try:
            result = self._inner.export(spans)
        except Exception:  # an exporter bug must not kill the export thread
            logger.exception("Obzerve exporter raised")
            result = SpanExportResult.FAILURE
        c = self._counters
        with c.lock:
            c.pending -= len(spans)
            if result is SpanExportResult.SUCCESS:
                c.exported += len(spans)
            else:
                c.failed += len(spans)
                failed = c.failed
        if result is not SpanExportResult.SUCCESS:
            c.warn("Obzerve could not deliver %d spans (%d failed in total)", len(spans), failed)
        return result

    def shutdown(self) -> None:
        self._inner.shutdown()

    def force_flush(self, timeout_millis: int = 30000) -> bool:
        return self._inner.force_flush(timeout_millis)


class AccountingProcessor(SpanProcessor):
    """Admits a span to the batch processor only while fewer than
    ``max_pending`` spans are buffered or being exported."""

    def __init__(
        self,
        exporter: SpanExporter,
        *,
        max_queue_size: int,
        max_export_batch_size: int,
        schedule_delay_ms: int,
        export_timeout_s: float,
    ):
        self.counters = _Counters()
        self._max_pending = max_queue_size
        # The inner queue holds at most ``max_pending`` spans, so it never drops on its own.
        self._batch = BatchSpanProcessor(
            AccountingExporter(exporter, self.counters),
            max_queue_size=max_queue_size,
            max_export_batch_size=min(max_export_batch_size, max_queue_size),
            schedule_delay_millis=schedule_delay_ms,
            export_timeout_millis=export_timeout_s * 1000,
        )

    def on_start(self, span: Span, parent_context: Context | None = None) -> None:
        self._batch.on_start(span, parent_context)

    def on_end(self, span: ReadableSpan) -> None:
        if not span.context.trace_flags.sampled:
            return
        c = self.counters
        with c.lock:
            c.ended += 1
            admitted = c.pending < self._max_pending
            if admitted:
                c.pending += 1
            else:
                c.dropped += 1
                dropped = c.dropped
        if admitted:
            self._batch.on_end(span)
        else:
            c.warn("Obzerve telemetry buffer full; dropping spans (%d dropped in total)", dropped)

    def stats(self) -> DeliveryStats:
        c = self.counters
        with c.lock:
            return DeliveryStats(c.ended, c.exported, c.failed, c.dropped, c.pending)

    def shutdown(self) -> None:
        self._batch.shutdown()

    def force_flush(self, timeout_millis: int = 30000) -> bool:
        return self._batch.force_flush(timeout_millis)
