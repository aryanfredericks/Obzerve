from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from obzerve_server.models import ModelPrice

PROTOBUF = {"Content-Type": "application/x-protobuf"}
T0 = datetime(2026, 3, 2, 12, 0, tzinfo=UTC)
WINDOW = {
    "start": (T0 - timedelta(hours=1)).isoformat(),
    "end": (T0 + timedelta(hours=1)).isoformat(),
}


def auth(key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {key}"}


async def _seed(app, client, key, otlp):
    """Four 'research' requests (100/200/300/400 ms, one failing) and one 'checkout'."""
    async with app.state.sessionmaker() as session:
        session.add(
            ModelPrice(
                model="m1",
                input_usd_per_mtok=Decimal("1"),
                output_usd_per_mtok=Decimal("2"),
                effective_from=datetime(2020, 1, 1, tzinfo=UTC),
            )
        )
        await session.commit()

    b = otlp()
    for i, duration in enumerate([100, 200, 300, 400]):
        tid = f"{i + 1:x}"
        start = T0 + timedelta(minutes=i * 10)
        failed = i == 3
        b.span(
            tid,
            "1",
            "research",
            start=start,
            duration_ms=duration,
            kind="agent",
            error="LLMTimeoutError" if failed else None,
        )
        b.span(
            tid,
            "2",
            "search",
            start=start,
            duration_ms=duration / 4,
            parent="1",
            kind="tool",
            error="ConnectionError" if i == 0 else None,
        )
        b.span(
            tid,
            "3",
            "llm.generate",
            start=start,
            duration_ms=duration / 2,
            parent="1",
            kind="llm",
            error="LLMTimeoutError" if failed else None,
            gen_ai__request__model="m1",
            gen_ai__provider__name="local",
            gen_ai__usage__input_tokens=1000,
            gen_ai__usage__output_tokens=500,
            obzerve__retry__count=1 if i == 2 else 0,
        )
    b.span("99", "1", "checkout", start=T0, duration_ms=50, kind="agent")
    # Outside the window: must be ignored.
    b.span("98", "1", "research", start=T0 - timedelta(days=2), duration_ms=9999, kind="agent")
    resp = await client.post("/v1/traces", content=b.body(), headers=auth(key) | PROTOBUF)
    assert resp.status_code == 200


async def test_summary(app, client, make_project, otlp):
    _, key = await make_project()
    await _seed(app, client, key, otlp)

    s = (await client.get("/api/v1/metrics/summary", params=WINDOW, headers=auth(key))).json()
    assert s["requests"] == 5
    assert s["errors"] == 1
    assert s["error_rate"] == pytest.approx(0.2)
    assert s["retried_requests"] == 1
    # percentile_cont over 50, 100, 200, 300, 400
    assert s["latency"]["p50_ms"] == pytest.approx(200)
    assert s["latency"]["p95_ms"] == pytest.approx(380)
    assert s["latency"]["p99_ms"] == pytest.approx(396)
    assert s["input_tokens"] == 4000 and s["output_tokens"] == 2000
    assert s["estimated_cost_usd"] == pytest.approx(4 * (1000 * 1 + 500 * 2) / 1e6)
    assert s["estimated_cost_per_request_usd"] == pytest.approx(s["estimated_cost_usd"] / 5)
    assert s["unpriced_llm_calls"] == 0
    assert s["llm_calls"] == 4
    assert s["llm_timeout_rate"] == pytest.approx(0.25)
    assert s["tool_calls"] == 4
    assert s["tool_success_rate"] == pytest.approx(0.75)
    assert s["time_per_request_ms"]["llm"] == pytest.approx((50 + 100 + 150 + 200) / 5)
    assert s["time_per_request_ms"]["tool"] == pytest.approx((25 + 50 + 75 + 100) / 5)

    only_checkout = (
        await client.get(
            "/api/v1/metrics/summary", params=WINDOW | {"workflow": "checkout"}, headers=auth(key)
        )
    ).json()
    assert only_checkout["requests"] == 1
    assert only_checkout["llm_calls"] == 0


async def test_timeseries_fills_empty_buckets(app, client, make_project, otlp):
    _, key = await make_project()
    await _seed(app, client, key, otlp)

    ts = (await client.get("/api/v1/metrics/timeseries", params=WINDOW, headers=auth(key))).json()
    assert ts["bucket_seconds"] == 60  # 2 hours / 1 minute = 120 points
    points = ts["points"]
    assert len(points) == 120
    assert sum(p["requests"] for p in points) == 5
    busiest = next(p for p in points if p["time"].startswith("2026-03-02T12:00"))
    assert busiest["requests"] == 2  # research #1 and checkout
    assert all(p["p95_ms"] is None for p in points if p["requests"] == 0)

    week = {"start": (T0 - timedelta(days=7)).isoformat(), "end": T0.isoformat()}
    ts = (await client.get("/api/v1/metrics/timeseries", params=week, headers=auth(key))).json()
    assert ts["bucket_seconds"] == 6 * 3600


async def test_breakdowns(app, client, make_project, otlp):
    _, key = await make_project()
    await _seed(app, client, key, otlp)

    async def rows(group_by):
        resp = await client.get(
            "/api/v1/metrics/breakdown", params=WINDOW | {"group_by": group_by}, headers=auth(key)
        )
        assert resp.status_code == 200
        return {r["key"]: r for r in resp.json()["rows"]}

    workflows = await rows("workflow")
    assert workflows["research"]["count"] == 4
    assert workflows["research"]["error_rate"] == pytest.approx(0.25)
    assert workflows["checkout"]["count"] == 1

    ops = await rows("operation")
    assert ops["search"]["kind"] == "tool" and ops["search"]["errors"] == 1
    assert ops["llm.generate"]["p50_ms"] == pytest.approx(125)
    assert ops["llm.generate"]["retries"] == 1

    models = await rows("model")
    assert list(models) == ["m1"]
    assert models["m1"]["provider"] == "local"
    assert models["m1"]["estimated_cost_usd"] == pytest.approx(0.008)

    checkout_ops = (
        await client.get(
            "/api/v1/metrics/breakdown",
            params=WINDOW | {"group_by": "operation", "workflow": "checkout"},
            headers=auth(key),
        )
    ).json()["rows"]
    assert [r["key"] for r in checkout_ops] == ["checkout"]

    kinds = await rows("kind")
    assert {k: v["count"] for k, v in kinds.items()} == {"agent": 5, "tool": 4, "llm": 4}


async def test_metrics_are_scoped_to_project(app, client, make_project, otlp):
    _, key_a = await make_project()
    _, key_b = await make_project()
    await _seed(app, client, key_a, otlp)
    s = (await client.get("/api/v1/metrics/summary", params=WINDOW, headers=auth(key_b))).json()
    assert s["requests"] == 0
    assert s["error_rate"] is None
    assert s["latency"]["p95_ms"] is None


async def test_window_validation(client, make_project):
    _, key = await make_project()
    bad = [
        {"start": T0.isoformat(), "end": (T0 - timedelta(hours=1)).isoformat()},
        {"start": (T0 - timedelta(days=91)).isoformat(), "end": T0.isoformat()},
        {"start": "2026-03-02T00:00:00", "end": "2026-03-02T01:00:00"},
    ]
    for params in bad:
        resp = await client.get("/api/v1/metrics/summary", params=params, headers=auth(key))
        assert resp.status_code == 422, params
    resp = await client.get(
        "/api/v1/metrics/breakdown", params={"group_by": "nope"}, headers=auth(key)
    )
    assert resp.status_code == 422
