from obzerve import SpanKind

PROTOBUF = {"Content-Type": "application/x-protobuf"}


def auth(key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {key}"}


async def _ingest_one(client, recorder, key: str, name: str = "agent") -> str:
    with recorder.observer.trace(name) as trace:
        with recorder.observer.span("tool", kind=SpanKind.TOOL):
            pass
    body = recorder.otlp_body()
    recorder.exporter.clear()
    resp = await client.post("/v1/traces", content=body, headers=auth(key) | PROTOBUF)
    assert resp.status_code == 200
    return trace.trace_id


async def test_missing_invalid_and_revoked_keys_are_rejected(client, make_project):
    _, revoked = await make_project(revoked=True)
    for headers in ({}, auth("obz_not_a_real_key"), auth(revoked)):
        assert (
            await client.post("/v1/traces", content=b"", headers=headers | PROTOBUF)
        ).status_code == 401
        assert (await client.get("/api/v1/traces", headers=headers)).status_code == 401


async def test_x_api_key_header_is_accepted(client, make_project):
    _, key = await make_project()
    assert (await client.get("/api/v1/traces", headers={"X-API-Key": key})).status_code == 200


async def test_scopes_are_enforced(client, make_project, recorder):
    _, ingest_only = await make_project(scopes=["ingest"])
    _, read_only = await make_project(scopes=["read"])

    await _ingest_one(client, recorder, ingest_only)
    assert (await client.get("/api/v1/traces", headers=auth(ingest_only))).status_code == 403
    resp = await client.post("/v1/traces", content=b"", headers=auth(read_only) | PROTOBUF)
    assert resp.status_code == 403


async def test_projects_cannot_see_each_others_traces(client, make_project, recorder):
    project_a, key_a = await make_project("a")
    _, key_b = await make_project("b")
    trace_a = await _ingest_one(client, recorder, key_a, "agent-a")
    trace_b = await _ingest_one(client, recorder, key_b, "agent-b")

    # Knowing another tenant's trace ID grants nothing.
    assert (await client.get(f"/api/v1/traces/{trace_a}", headers=auth(key_b))).status_code == 404
    assert (await client.get(f"/api/v1/traces/{trace_a}", headers=auth(key_a))).status_code == 200

    listed_b = (await client.get("/api/v1/traces", headers=auth(key_b))).json()["items"]
    assert [t["trace_id"] for t in listed_b] == [trace_b]
    spans_b = (await client.get("/api/v1/spans", headers=auth(key_b))).json()["items"]
    assert {s["trace_id"] for s in spans_b} == {trace_b}
    assert (await client.get("/api/v1/project", headers=auth(key_a))).json()["id"] == str(project_a)


async def test_same_trace_id_in_two_projects_is_stored_separately(client, make_project, recorder):
    _, key_a = await make_project()
    _, key_b = await make_project()
    with recorder.observer.trace("shared") as trace:
        pass
    body = recorder.otlp_body()
    for key in (key_a, key_b):
        assert (
            await client.post("/v1/traces", content=body, headers=auth(key) | PROTOBUF)
        ).status_code == 200

    for key in (key_a, key_b):
        detail = (await client.get(f"/api/v1/traces/{trace.trace_id}", headers=auth(key))).json()
        assert detail["trace"]["span_count"] == 1
