"""Delivery accounting and behaviour against a failing or recovering backend."""

import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from opentelemetry.sdk.trace.export import SpanExporter, SpanExportResult
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from obzerve import DeliveryStats, Observer


def _spans(observer: Observer, n: int) -> None:
    for i in range(n):
        with observer.trace(f"t{i}"):
            pass


def test_stats_count_exported_spans():
    observer = Observer(exporter=InMemorySpanExporter(), schedule_delay_ms=10)
    _spans(observer, 7)
    assert observer.flush()
    assert observer.stats() == DeliveryStats(7, 7, 0, 0, 0)
    observer.shutdown()


class BlockingExporter(SpanExporter):
    """Holds the first export until released, so the buffer fills up."""

    def __init__(self):
        self.release = threading.Event()
        self.exported = 0

    def export(self, spans):
        self.release.wait(5)
        self.exported += len(spans)
        return SpanExportResult.SUCCESS


def test_full_buffer_drops_are_counted_exactly():
    exporter = BlockingExporter()
    observer = Observer(
        exporter=exporter, max_queue_size=10, max_export_batch_size=5, schedule_delay_ms=10
    )
    _spans(observer, 25)
    stats = observer.stats()
    assert stats.spans_ended == 25
    assert stats.spans_dropped == 15  # at most 10 buffered or in flight
    exporter.release.set()
    assert observer.flush()
    stats = observer.stats()
    assert (stats.spans_exported, stats.spans_dropped, stats.spans_pending) == (10, 15, 0)
    assert exporter.exported == 10
    observer.shutdown()


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_unreachable_backend_counts_failures():
    observer = Observer(
        endpoint=f"http://127.0.0.1:{_free_port()}",
        api_key="k",
        export_timeout_s=0.3,
        schedule_delay_ms=10,
    )
    _spans(observer, 3)
    observer.flush(timeout_ms=5000)
    stats = observer.stats()
    assert (stats.spans_failed, stats.spans_exported, stats.spans_pending) == (3, 0, 0)
    observer.shutdown()


@pytest.fixture
def flaky_backend():
    """OTLP endpoint that answers 503 + Retry-After to the first N requests."""
    state = {"fail_first": 2, "requests": 0, "accepted": 0}

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            self.rfile.read(int(self.headers["Content-Length"]))
            state["requests"] += 1
            if state["requests"] <= state["fail_first"]:
                self.send_response(503)
                self.send_header("Retry-After", "0")
                self.end_headers()
                return
            state["accepted"] += 1
            self.send_response(200)
            self.send_header("Content-Type", "application/x-protobuf")
            self.end_headers()

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_port}", state
    server.shutdown()


def test_backend_503_is_retried_until_it_recovers(flaky_backend):
    endpoint, state = flaky_backend
    observer = Observer(endpoint=endpoint, api_key="k", export_timeout_s=5, schedule_delay_ms=10)
    _spans(observer, 4)
    assert observer.flush(timeout_ms=10_000)
    assert observer.stats().spans_exported == 4
    assert observer.stats().spans_failed == 0
    assert state["requests"] == state["fail_first"] + state["accepted"]
    observer.shutdown()
