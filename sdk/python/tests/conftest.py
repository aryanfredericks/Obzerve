import pytest
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from obzerve import Observer


class Recorder:
    """An Observer that keeps spans in memory, with helpers to read them back."""

    def __init__(self) -> None:
        self.exporter = InMemorySpanExporter()
        self.observer = Observer(exporter=self.exporter, schedule_delay_ms=10)

    def spans(self, name: str | None = None):
        self.observer.flush()
        spans = list(self.exporter.get_finished_spans())
        return [s for s in spans if name is None or s.name == name]

    def one(self, name: str):
        found = self.spans(name)
        assert len(found) == 1, f"expected one {name!r} span, got {[s.name for s in self.spans()]}"
        return found[0]


@pytest.fixture
def rec():
    recorder = Recorder()
    yield recorder
    recorder.observer.shutdown()


@pytest.fixture
def instrumented(rec):
    """Instrument the installed client libraries for one test, then undo it."""
    handle = rec.observer.instrument()
    yield rec
    handle.uninstrument()
