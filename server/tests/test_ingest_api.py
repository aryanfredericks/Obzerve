import gzip
import json

from google.protobuf import json_format
from opentelemetry.exporter.otlp.proto.common.trace_encoder import encode_spans
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceResponse

from obzerve import SpanKind

PROTOBUF = {"Content-Type": "application/x-protobuf"}


def auth(key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {key}"}


def record_research_trace(recorder) -> str:
    obs = recorder.observer
    with obs.trace("research_agent", input="compare RAG evals") as trace:
        with obs.span("search_web", kind=SpanKind.TOOL):
            pass
        with obs.span("synthesize", kind=SpanKind.CHAIN) as chain:
            for attempt in range(2):
                try:
                    with obs.span("llm", kind=SpanKind.LLM) as llm:
                        llm.set_llm(
                            model="mock-1", provider="local", input_tokens=100, output_tokens=20
                        )
                        if attempt == 0:
                            raise TimeoutError("model timed out")
                except TimeoutError as exc:
                    chain.record_retry(attempt + 1, exc)
        trace.set_output("answer")
    return trace.trace_id


async def test_ingest_and_read_back_trace(client, make_project, recorder):
    _, key = await make_project()
    trace_id = record_research_trace(recorder)

    resp = await client.post(
        "/v1/traces", content=recorder.otlp_body(), headers=auth(key) | PROTOBUF
    )
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "application/x-protobuf"
    reply = ExportTraceServiceResponse.FromString(resp.content)
    assert reply.partial_success.rejected_spans == 0

    detail = (await client.get(f"/api/v1/traces/{trace_id}", headers=auth(key))).json()
    trace = detail["trace"]
    assert trace["name"] == "research_agent"
    assert trace["service_name"] == "test-agent"
    assert trace["status"] == "ok"
    assert trace["span_count"] == 5
    assert trace["error_count"] == 1
    assert trace["retry_count"] == 1
    assert trace["input_tokens"] == 200 and trace["output_tokens"] == 40

    spans = {s["name"]: s for s in detail["spans"]}
    root_id = spans["research_agent"]["span_id"]
    assert spans["research_agent"]["parent_span_id"] is None
    # Root inputs are stored as exact JSON, so a replay gets back the very same value.
    assert spans["research_agent"]["input"] == '"compare RAG evals"'
    assert spans["research_agent"]["output"] == "answer"
    assert spans["search_web"]["parent_span_id"] == root_id
    assert spans["search_web"]["kind"] == "tool"
    assert spans["synthesize"]["retry_count"] == 1
    assert spans["synthesize"]["events"][0]["name"] == "retry"
    llm_spans = [s for s in detail["spans"] if s["name"] == "llm"]
    failed = next(s for s in llm_spans if s["status"] == "error")
    assert failed["error_type"] == "TimeoutError"
    assert failed["status_message"] == "model timed out"
    assert failed["model"] == "mock-1" and failed["provider"] == "local"
    # Promoted attributes are not duplicated in the attribute bag.
    assert "obzerve.span.kind" not in failed["attributes"]
    assert "gen_ai.request.model" in failed["attributes"]


async def test_duplicate_delivery_is_idempotent(client, make_project, recorder):
    _, key = await make_project()
    trace_id = record_research_trace(recorder)
    body = recorder.otlp_body()

    for _ in range(3):
        resp = await client.post("/v1/traces", content=body, headers=auth(key) | PROTOBUF)
        assert resp.status_code == 200

    trace = (await client.get(f"/api/v1/traces/{trace_id}", headers=auth(key))).json()["trace"]
    assert trace["span_count"] == 5
    assert trace["error_count"] == 1
    assert trace["retry_count"] == 1
    assert trace["input_tokens"] == 200


async def test_spans_arriving_out_of_order_build_the_same_trace(client, make_project, recorder):
    _, key = await make_project()
    trace_id = record_research_trace(recorder)
    recorder.observer.flush()
    spans = list(recorder.exporter.get_finished_spans())
    # The root span finishes last; deliver it first and the children one at a time after.
    ordered = [spans[-1], *spans[:-1]]
    for span in ordered:
        body = encode_spans([span]).SerializeToString()
        assert (
            await client.post("/v1/traces", content=body, headers=auth(key) | PROTOBUF)
        ).status_code == 200

    trace = (await client.get(f"/api/v1/traces/{trace_id}", headers=auth(key))).json()["trace"]
    assert trace["name"] == "research_agent"
    assert trace["span_count"] == 5
    root = spans[-1]
    # Stored timestamps have microsecond precision.
    assert abs(trace["duration_ms"] - (root.end_time - root.start_time) / 1e6) < 0.002


async def test_children_before_root_keep_status_unset_until_root_arrives(
    client, make_project, recorder
):
    _, key = await make_project()
    trace_id = record_research_trace(recorder)
    recorder.observer.flush()
    spans = list(recorder.exporter.get_finished_spans())

    body = encode_spans(spans[:-1]).SerializeToString()
    await client.post("/v1/traces", content=body, headers=auth(key) | PROTOBUF)
    trace = (await client.get(f"/api/v1/traces/{trace_id}", headers=auth(key))).json()["trace"]
    assert trace["status"] == "unset" and trace["root_span_id"] is None

    body = encode_spans(spans[-1:]).SerializeToString()
    await client.post("/v1/traces", content=body, headers=auth(key) | PROTOBUF)
    trace = (await client.get(f"/api/v1/traces/{trace_id}", headers=auth(key))).json()["trace"]
    assert trace["status"] == "ok" and trace["name"] == "research_agent"


async def test_json_and_gzip_encodings(client, make_project, recorder):
    _, key = await make_project()
    trace_id = record_research_trace(recorder)
    recorder.observer.flush()
    request = encode_spans(recorder.exporter.get_finished_spans())

    json_body = json_format.MessageToJson(request).encode()
    resp = await client.post(
        "/v1/traces",
        content=gzip.compress(json_body),
        headers=auth(key) | {"Content-Type": "application/json", "Content-Encoding": "gzip"},
    )
    assert resp.status_code == 200
    assert json.loads(resp.content) == {}

    trace = (await client.get(f"/api/v1/traces/{trace_id}", headers=auth(key))).json()["trace"]
    assert trace["span_count"] == 5


async def test_invalid_spans_are_rejected_individually(client, make_project, recorder):
    _, key = await make_project()
    record_research_trace(recorder)
    recorder.observer.flush()
    request = encode_spans(recorder.exporter.get_finished_spans())
    bad = request.resource_spans[0].scope_spans[0].spans[0]
    bad.trace_id = b"\x00" * 16

    resp = await client.post(
        "/v1/traces", content=request.SerializeToString(), headers=auth(key) | PROTOBUF
    )
    assert resp.status_code == 200
    reply = ExportTraceServiceResponse.FromString(resp.content)
    assert reply.partial_success.rejected_spans == 1
    assert "trace_id" in reply.partial_success.error_message


async def test_nul_characters_are_stripped(client, make_project, recorder):
    _, key = await make_project()
    with recorder.observer.trace("nul\x00agent") as trace:
        trace.set_output("a\x00b")
    resp = await client.post(
        "/v1/traces", content=recorder.otlp_body(), headers=auth(key) | PROTOBUF
    )
    assert resp.status_code == 200
    detail = (await client.get(f"/api/v1/traces/{trace.trace_id}", headers=auth(key))).json()
    assert detail["trace"]["name"] == "nulagent"
    assert detail["spans"][0]["output"] == "ab"


async def test_malformed_and_unsupported_bodies(client, make_project):
    _, key = await make_project()
    resp = await client.post("/v1/traces", content=b"\xff\xff\xff", headers=auth(key) | PROTOBUF)
    assert resp.status_code == 400
    resp = await client.post(
        "/v1/traces", content=b"x", headers=auth(key) | {"Content-Type": "text/plain"}
    )
    assert resp.status_code == 415
    resp = await client.post(
        "/v1/traces",
        content=b"x",
        headers=auth(key) | PROTOBUF | {"Content-Encoding": "br"},
    )
    assert resp.status_code == 400


async def test_oversized_bodies_are_rejected(client, make_project):
    _, key = await make_project()
    resp = await client.post(
        "/v1/traces", content=b"\x00" * (300 * 1024), headers=auth(key) | PROTOBUF
    )
    assert resp.status_code == 413
    # A small gzip body that inflates past the limit (zip bomb).
    bomb = gzip.compress(b"\x00" * (2 * 1024 * 1024))
    resp = await client.post(
        "/v1/traces", content=bomb, headers=auth(key) | PROTOBUF | {"Content-Encoding": "gzip"}
    )
    assert resp.status_code == 413


async def test_health_endpoints(client):
    assert (await client.get("/healthz")).json() == {"status": "ok"}
    assert (await client.get("/readyz")).json() == {"status": "ok", "database": "ok"}
