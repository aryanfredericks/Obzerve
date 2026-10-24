"""Direct-mode behaviour under dependency failure and concurrent load."""

import asyncio
from datetime import UTC, datetime, timedelta

PROTOBUF = {"Content-Type": "application/x-protobuf"}
DEAD_DB = "postgresql+asyncpg://obzerve:obzerve@127.0.0.1:1/obzerve"
T0 = datetime(2026, 5, 1, tzinfo=UTC)


def auth(key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {key}"}


async def test_database_down_is_a_retryable_503(make_app, client_for, otlp):
    app = make_app(database_url=DEAD_DB, retry_after_s=3)
    body = otlp().span("a", "1", "agent", start=T0, duration_ms=1).body()
    async with client_for(app) as client:
        for method, url, kwargs in [
            ("POST", "/v1/traces", {"content": body, "headers": auth("k") | PROTOBUF}),
            ("GET", "/api/v1/traces", {"headers": auth("k")}),
            ("GET", "/api/v1/metrics/summary", {"headers": auth("k")}),
        ]:
            resp = await client.request(method, url, **kwargs)
            assert resp.status_code == 503, url
            assert resp.headers["retry-after"] == "3"
            assert resp.json() == {"detail": "Database unavailable"}

        ready = await client.get("/readyz")
        assert ready.status_code == 503
        assert ready.json()["database"] == "unreachable"
        assert (await client.get("/healthz")).status_code == 200
        metrics = (await client.get("/metrics")).text
        assert 'obzerve_db_errors_total{component="api"}' in metrics


async def test_concurrent_batches_for_the_same_traces(client, make_project, otlp):
    """Many exporters writing spans of the same traces at once: no deadlocks or lost counts."""
    _, key = await make_project()
    traces, batches = 5, 20

    def batch(n: int) -> bytes:
        b = otlp()
        for t in range(traces):
            b.span(
                f"c{t}",
                f"{n + 1:x}",
                f"op-{n}",
                start=T0 + timedelta(milliseconds=n),
                duration_ms=5,
                parent="ffff",
                error="Boom" if n % 4 == 0 else None,
            )
        return b.body()

    responses = await asyncio.gather(
        *[
            client.post("/v1/traces", content=batch(n), headers=auth(key) | PROTOBUF)
            for n in range(batches)
        ]
        # ...and every batch delivered twice, as an exporter retry would.
        + [
            client.post("/v1/traces", content=batch(n), headers=auth(key) | PROTOBUF)
            for n in range(batches)
        ]
    )
    assert [r.status_code for r in responses] == [200] * batches * 2

    page = (await client.get("/api/v1/traces", headers=auth(key))).json()["items"]
    assert len(page) == traces
    for trace in page:
        assert trace["span_count"] == batches
        assert trace["error_count"] == batches // 4
        assert trace["status"] == "unset"  # the root span ("ffff") never arrived


async def test_metrics_endpoint_reports_ingestion(client, make_project, otlp):
    _, key = await make_project()
    body = otlp().span("d1", "1", "agent", start=T0, duration_ms=1).body()
    for _ in range(2):
        await client.post("/v1/traces", content=body, headers=auth(key) | PROTOBUF)
    text = (await client.get("/metrics")).text
    assert 'obzerve_ingest_requests_total{mode="direct",outcome="accepted"}' in text
    assert 'obzerve_spans_persisted_total{outcome="duplicate"}' in text
    assert (
        'obzerve_http_request_seconds_count{method="POST",route="/v1/traces",status="200"}' in text
    )


def test_restarting_database_errors_are_transient():
    import asyncpg
    from sqlalchemy import exc as sa_exc

    from obzerve_server.errors import is_transient_db_error

    def wrapped(driver_error):
        # SQLAlchemy's asyncpg adapter wraps the driver error in a generic DBAPIError.
        try:
            try:
                raise driver_error
            except Exception as inner:
                raise sa_exc.DBAPIError("SELECT 1", {}, Exception(str(inner))) from inner
        except sa_exc.DBAPIError as outer:
            return outer

    for transient in (
        asyncpg.exceptions.CannotConnectNowError("the database system is shutting down"),
        asyncpg.exceptions.AdminShutdownError("terminating connection"),
        asyncpg.exceptions.TooManyConnectionsError("too many clients"),
        asyncpg.exceptions.DeadlockDetectedError("deadlock"),
        ConnectionRefusedError(111, "refused"),
    ):
        assert is_transient_db_error(wrapped(transient)), transient

    for permanent in (
        asyncpg.exceptions.UniqueViolationError("duplicate key"),
        asyncpg.exceptions.NumericValueOutOfRangeError("out of range"),
    ):
        assert not is_transient_db_error(wrapped(permanent)), permanent
