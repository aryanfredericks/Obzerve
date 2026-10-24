from obzerve import SpanKind

PROTOBUF = {"Content-Type": "application/x-protobuf"}


def auth(key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {key}"}


async def _seed(client, recorder, key):
    obs = recorder.observer
    with obs.trace("checkout_agent"):
        with obs.span("pay", kind=SpanKind.TOOL):
            pass
    with obs.trace("research_agent") as retried:
        with obs.span("llm", kind=SpanKind.LLM) as span:
            span.record_retry(1, TimeoutError("slow"))
            span.set_llm(model="mock-1")
    try:
        with obs.trace("research_agent") as failed:
            with obs.span("search_web", kind=SpanKind.TOOL):
                raise ConnectionError("search down")
    except ConnectionError:
        pass
    resp = await client.post(
        "/v1/traces", content=recorder.otlp_body(), headers=auth(key) | PROTOBUF
    )
    assert resp.status_code == 200
    return retried.trace_id, failed.trace_id


async def test_trace_filters(client, make_project, recorder):
    _, key = await make_project()
    retried, failed = await _seed(client, recorder, key)

    async def ids(**params):
        resp = await client.get("/api/v1/traces", params=params, headers=auth(key))
        assert resp.status_code == 200
        return {t["trace_id"] for t in resp.json()["items"]}

    assert len(await ids()) == 3
    assert await ids(status="error") == {failed}
    assert await ids(has_errors="true") == {failed}
    assert await ids(has_retries="true") == {retried}
    assert await ids(q="research") == {retried, failed}
    assert await ids(q="%") == set()  # LIKE wildcards are matched literally
    assert len(await ids(min_duration_ms=0)) == 3
    assert await ids(min_duration_ms=60_000) == set()


async def test_trace_pagination(client, make_project, recorder):
    _, key = await make_project()
    for i in range(5):
        with recorder.observer.trace(f"agent-{i}"):
            pass
    await client.post("/v1/traces", content=recorder.otlp_body(), headers=auth(key) | PROTOBUF)

    seen, cursor = [], None
    while True:
        params = {"limit": 2} | ({"cursor": cursor} if cursor else {})
        page = (await client.get("/api/v1/traces", params=params, headers=auth(key))).json()
        seen += [t["name"] for t in page["items"]]
        cursor = page["next_cursor"]
        if not cursor:
            break
    assert seen == [f"agent-{i}" for i in reversed(range(5))]

    resp = await client.get("/api/v1/traces", params={"cursor": "garbage"}, headers=auth(key))
    assert resp.status_code == 400


async def test_error_and_retry_search(client, make_project, recorder):
    _, key = await make_project()
    retried, failed = await _seed(client, recorder, key)

    errors = (
        await client.get("/api/v1/spans", params={"status": "error"}, headers=auth(key))
    ).json()
    by_name = {s["name"]: s for s in errors["items"]}
    assert set(by_name) == {"search_web", "research_agent"}
    assert by_name["search_web"]["error_type"] == "ConnectionError"
    assert by_name["search_web"]["status_message"] == "search down"

    by_type = (
        await client.get(
            "/api/v1/spans", params={"error_type": "ConnectionError"}, headers=auth(key)
        )
    ).json()["items"]
    assert {s["trace_id"] for s in by_type} == {failed}

    retries = (
        await client.get("/api/v1/spans", params={"has_retries": "true"}, headers=auth(key))
    ).json()["items"]
    assert [(s["name"], s["retry_count"], s["model"]) for s in retries] == [("llm", 1, "mock-1")]

    error_types = (await client.get("/api/v1/error-types", headers=auth(key))).json()
    assert error_types == [{"error_type": "ConnectionError", "count": 2}]


async def test_unknown_or_malformed_trace_id_is_404(client, make_project):
    _, key = await make_project()
    assert (await client.get("/api/v1/traces/" + "0" * 32, headers=auth(key))).status_code == 404
    assert (await client.get("/api/v1/traces/" + "z" * 32, headers=auth(key))).status_code == 404
    assert (await client.get("/api/v1/traces/abc", headers=auth(key))).status_code == 422
