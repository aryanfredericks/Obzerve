import asyncio
import socket
import time

import pytest
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import StatusCode

from obzerve import Observer, SpanKind
from obzerve.payloads import REDACTED


@pytest.fixture
def exporter():
    return InMemorySpanExporter()


@pytest.fixture
def observer(exporter):
    obs = Observer(service_name="test", exporter=exporter, schedule_delay_ms=10)
    yield obs
    obs.shutdown()


def finished(observer, exporter):
    observer.flush()
    return {s.name: s for s in exporter.get_finished_spans()}


def test_nested_spans_share_trace_and_link_parents(observer, exporter):
    with observer.trace("agent") as root:
        with observer.span("retrieve", kind=SpanKind.RETRIEVAL):
            with observer.span("embed"):
                pass

    spans = finished(observer, exporter)
    assert {s.context.trace_id for s in spans.values()} == {int(root.trace_id, 16)}
    assert spans["agent"].parent is None
    assert spans["retrieve"].parent.span_id == spans["agent"].context.span_id
    assert spans["embed"].parent.span_id == spans["retrieve"].context.span_id
    assert spans["retrieve"].attributes["obzerve.span.kind"] == "retrieval"
    assert spans["agent"].status.status_code is StatusCode.OK


def test_separate_traces_get_distinct_ids(observer):
    with observer.trace("a") as a:
        pass
    with observer.trace("b") as b:
        pass
    assert a.trace_id != b.trace_id


def test_exception_marks_span_error_and_propagates(observer, exporter):
    with pytest.raises(TimeoutError):
        with observer.trace("agent"):
            with observer.span("llm", kind=SpanKind.LLM):
                raise TimeoutError("model timed out")

    spans = finished(observer, exporter)
    llm = spans["llm"]
    assert llm.status.status_code is StatusCode.ERROR
    assert llm.status.description == "model timed out"
    assert llm.attributes["error.type"] == "TimeoutError"
    assert [e.name for e in llm.events] == ["exception"]
    assert spans["agent"].status.status_code is StatusCode.ERROR


def test_handled_error_and_retries(observer, exporter):
    with observer.trace("agent") as root:
        root.record_retry(1, TimeoutError("first"))
        root.record_retry(2, TimeoutError("second"))
        root.set_error("gave up")

    span = finished(observer, exporter)["agent"]
    assert span.attributes["obzerve.retry.count"] == 2
    assert [e.attributes["obzerve.retry.attempt"] for e in span.events] == [1, 2]
    assert span.status.status_code is StatusCode.ERROR


def test_llm_metadata(observer, exporter):
    with observer.trace("agent"):
        with observer.span("llm", kind=SpanKind.LLM) as span:
            span.set_llm(model="mock-1", provider="local", input_tokens=12, output_tokens=3)

    attrs = finished(observer, exporter)["llm"].attributes
    assert attrs["gen_ai.request.model"] == "mock-1"
    assert attrs["gen_ai.provider.name"] == "local"
    assert attrs["gen_ai.usage.input_tokens"] == 12
    assert attrs["gen_ai.usage.output_tokens"] == 3


def test_payloads_are_redacted_and_truncated(exporter):
    observer = Observer(exporter=exporter, max_payload_chars=80)
    with observer.trace("agent", input={"query": "x", "api_key": "abc", "nested": {"password": 1}}):
        with observer.span("long") as span:
            span.set_output("y" * 100)
        with observer.span("bearer") as span:
            span.set_input("Authorization: Bearer abcdefghijklmnopqrstuvwxyz")

    spans = finished(observer, exporter)
    assert spans["agent"].attributes["obzerve.input"] == (
        f'{{"query": "x", "api_key": "{REDACTED}", "nested": {{"password": "{REDACTED}"}}}}'
    )
    assert spans["long"].attributes["obzerve.output"] == "y" * 80
    assert spans["long"].attributes["obzerve.output.truncated"] is True
    assert "abcdefghijklmnop" not in spans["bearer"].attributes["obzerve.input"]
    observer.shutdown()


def test_payload_capture_can_be_disabled(exporter):
    observer = Observer(exporter=exporter, capture_payloads=False)
    with observer.trace("agent", input="secret prompt") as t:
        t.set_output("secret answer")
    attrs = finished(observer, exporter)["agent"].attributes
    assert "obzerve.input" not in attrs and "obzerve.output" not in attrs
    observer.shutdown()


def test_observe_decorator_sync_and_async(observer, exporter):
    @observer.observe(kind=SpanKind.TOOL)
    def add(a, b):
        return a + b

    @observer.observe("fetch", kind=SpanKind.TOOL)
    async def fetch(url):
        await asyncio.sleep(0)
        return {"url": url}

    with observer.trace("agent"):
        assert add(1, b=2) == 3
        assert asyncio.run(fetch("http://x")) == {"url": "http://x"}

    spans = finished(observer, exporter)
    assert (
        spans["test_observe_decorator_sync_and_async.<locals>.add"].attributes["obzerve.input"]
        == '{"a": 1, "b": 2}'
    )
    assert spans["fetch"].attributes["obzerve.output"] == '{"url": "http://x"}'
    assert spans["fetch"].parent.span_id == spans["agent"].context.span_id


def test_trace_context_propagates_across_services(exporter):
    upstream = Observer(service_name="upstream", exporter=exporter)
    downstream = Observer(service_name="downstream", exporter=exporter)
    with upstream.trace("caller") as caller:
        headers = upstream.inject_headers()
    with downstream.trace("callee", parent_headers=headers) as callee:
        pass

    assert callee.trace_id == caller.trace_id
    upstream.flush()
    downstream.flush()
    spans = {s.name: s for s in exporter.get_finished_spans()}
    assert spans["callee"].parent.span_id == spans["caller"].context.span_id


def _unused_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_unreachable_backend_does_not_block_or_fail_agent():
    observer = Observer(
        endpoint=f"http://127.0.0.1:{_unused_port()}",
        api_key="k",
        export_timeout_s=0.5,
        schedule_delay_ms=10,
        max_queue_size=64,
    )
    started = time.perf_counter()
    for i in range(500):  # far more spans than the queue holds: extras are dropped
        with observer.trace(f"agent-{i}"):
            pass
    elapsed = time.perf_counter() - started
    assert elapsed < 1.0, f"instrumentation blocked for {elapsed:.2f}s"
    observer.shutdown()


def test_disabled_observer_records_nothing(exporter):
    observer = Observer(exporter=exporter, enabled=False)
    with observer.trace("agent"):
        pass
    observer.flush()
    assert exporter.get_finished_spans() == ()
