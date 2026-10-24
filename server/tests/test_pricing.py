import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from obzerve_server.models import ModelPrice
from obzerve_server.pricing import Price, cost_usd, match_price, recompute_costs

PROTOBUF = {"Content-Type": "application/x-protobuf"}
T0 = datetime(2026, 1, 1, tzinfo=UTC)


def auth(key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {key}"}


def price(model="m", *, project=None, provider=None, start=T0, inp="1", out="2", cached=None):
    return Price(
        uuid.uuid4(),
        project,
        provider,
        model,
        Decimal(inp),
        Decimal(out),
        Decimal(cached) if cached is not None else None,
        start,
    )


def test_cost_formula_with_and_without_cache_price():
    p = price(inp="3", out="15", cached="0.30")
    # 1000 input of which 400 cached, 200 output
    assert cost_usd(p, 1000, 200, 400) == (
        Decimal(600) * 3 + Decimal(400) * Decimal("0.30") + Decimal(200) * 15
    ) / Decimal(1_000_000)
    no_cache_price = price(inp="3", out="15")
    assert cost_usd(no_cache_price, 1000, 200, 400) == Decimal(1000 * 3 + 200 * 15) / 1_000_000
    # Cached tokens can never exceed input tokens; missing counts are zero.
    assert cost_usd(p, 100, None, 500) == Decimal(100) * Decimal("0.30") / 1_000_000


def test_price_matching_rules():
    project = uuid.uuid4()
    old = price(start=T0)
    new = price(start=T0 + timedelta(days=30), inp="0.5")
    provider_specific = price(provider="acme", start=T0, inp="9")
    project_specific = price(project=project, start=T0, inp="7")

    prices = [old, new]
    assert match_price(prices, model="m", provider=None, at=T0 + timedelta(days=1)) is old
    assert match_price(prices, model="m", provider=None, at=T0 + timedelta(days=31)) is new
    assert match_price(prices, model="m", provider=None, at=T0 - timedelta(days=1)) is None
    assert match_price(prices, model="other", provider=None, at=T0 + timedelta(days=1)) is None

    prices = [old, provider_specific]
    assert match_price(prices, model="m", provider="acme", at=T0) is provider_specific
    assert match_price(prices, model="m", provider="other", at=T0) is old

    prices = [new, project_specific]
    assert match_price(prices, model="m", provider=None, at=T0 + timedelta(days=60)) is (
        project_specific
    )


async def _add_price(app, **fields):
    async with app.state.sessionmaker() as session:
        session.add(
            ModelPrice(
                model="gpt-test",
                input_usd_per_mtok=Decimal("2"),
                output_usd_per_mtok=Decimal("10"),
                effective_from=T0,
                **fields,
            )
        )
        await session.commit()


def _llm_trace(otlp, trace_id="a1", model="gpt-test", start=T0 + timedelta(days=1)):
    return (
        otlp()
        .span(trace_id, "1", "agent", start=start, duration_ms=100, kind="agent")
        .span(
            trace_id,
            "2",
            "llm",
            start=start,
            duration_ms=80,
            parent="1",
            kind="llm",
            gen_ai__request__model=model,
            gen_ai__usage__input_tokens=1000,
            gen_ai__usage__output_tokens=100,
            gen_ai__usage__cache_read__input_tokens=200,
        )
    )


async def test_llm_spans_are_priced_at_ingest(app, client, make_project, otlp):
    _, key = await make_project()
    await _add_price(app, cached_input_usd_per_mtok=Decimal("0.5"))
    resp = await client.post(
        "/v1/traces", content=_llm_trace(otlp).body(), headers=auth(key) | PROTOBUF
    )
    assert resp.status_code == 200

    detail = (await client.get("/api/v1/traces/" + "a1".rjust(32, "0"), headers=auth(key))).json()
    expected = (800 * 2 + 200 * 0.5 + 100 * 10) / 1e6
    llm = next(s for s in detail["spans"] if s["name"] == "llm")
    assert llm["cached_input_tokens"] == 200
    assert llm["estimated_cost_usd"] == pytest.approx(expected)
    agent = next(s for s in detail["spans"] if s["name"] == "agent")
    assert agent["estimated_cost_usd"] is None
    assert detail["trace"]["estimated_cost_usd"] == pytest.approx(expected)
    assert detail["trace"]["unpriced_llm_calls"] == 0
    assert detail["trace"]["cached_input_tokens"] == 200


async def test_unpriced_calls_are_flagged_then_recomputed(app, client, make_project, otlp):
    project_id, key = await make_project()
    await client.post("/v1/traces", content=_llm_trace(otlp).body(), headers=auth(key) | PROTOBUF)
    trace_url = "/api/v1/traces/" + "a1".rjust(32, "0")

    trace = (await client.get(trace_url, headers=auth(key))).json()["trace"]
    assert trace["estimated_cost_usd"] == 0
    assert trace["unpriced_llm_calls"] == 1

    await _add_price(app, project_id=project_id)
    async with app.state.sessionmaker() as session:
        assert await recompute_costs(session, project_id) == 1

    trace = (await client.get(trace_url, headers=auth(key))).json()["trace"]
    assert trace["estimated_cost_usd"] == pytest.approx((1000 * 2 + 100 * 10) / 1e6)
    assert trace["unpriced_llm_calls"] == 0


async def test_another_projects_price_is_not_used(app, client, make_project, otlp):
    other_project, _ = await make_project()
    _, key = await make_project()
    await _add_price(app, project_id=other_project)
    await client.post("/v1/traces", content=_llm_trace(otlp).body(), headers=auth(key) | PROTOBUF)
    trace = (await client.get("/api/v1/traces/" + "a1".rjust(32, "0"), headers=auth(key))).json()[
        "trace"
    ]
    assert trace["unpriced_llm_calls"] == 1
