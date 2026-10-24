"""Estimated LLM cost from token usage and dated, per-model prices.

Costs are estimates: they depend on the configured price table, not on the
provider's invoice. A price applies from its ``effective_from`` until a newer
price for the same model; a project-specific price overrides a global one, and
a provider-specific price overrides a provider-agnostic one.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import func, or_, select, tuple_, update
from sqlalchemy.ext.asyncio import AsyncSession

from obzerve_server.models import ModelPrice, Span, Trace

_MILLION = Decimal(1_000_000)


@dataclass(frozen=True)
class Price:
    id: uuid.UUID
    project_id: uuid.UUID | None
    provider: str | None
    model: str
    input_per_mtok: Decimal
    output_per_mtok: Decimal
    cached_input_per_mtok: Decimal | None
    effective_from: datetime

    @classmethod
    def from_row(cls, row: ModelPrice) -> Price:
        return cls(
            row.id,
            row.project_id,
            row.provider,
            row.model,
            row.input_usd_per_mtok,
            row.output_usd_per_mtok,
            row.cached_input_usd_per_mtok,
            row.effective_from,
        )


def needs_price(span: dict[str, Any]) -> bool:
    return bool(span.get("model")) and (
        span.get("input_tokens") is not None or span.get("output_tokens") is not None
    )


def match_price(
    prices: Iterable[Price], *, model: str, provider: str | None, at: datetime
) -> Price | None:
    candidates = [
        p
        for p in prices
        if p.model == model
        and (p.provider is None or p.provider == provider)
        and p.effective_from <= at
    ]
    if not candidates:
        return None
    return max(
        candidates,
        key=lambda p: (p.project_id is not None, p.provider is not None, p.effective_from),
    )


def cost_usd(
    price: Price, input_tokens: int | None, output_tokens: int | None, cached: int | None
) -> Decimal:
    input_tokens = max(input_tokens or 0, 0)
    cached = min(max(cached or 0, 0), input_tokens)
    cached_rate = (
        price.cached_input_per_mtok
        if price.cached_input_per_mtok is not None
        else price.input_per_mtok
    )
    total = (
        (input_tokens - cached) * price.input_per_mtok
        + cached * cached_rate
        + max(output_tokens or 0, 0) * price.output_per_mtok
    )
    return total / _MILLION


async def load_prices(
    session: AsyncSession, project_id: uuid.UUID, models: Iterable[str]
) -> list[Price]:
    models = sorted(set(models))
    if not models:
        return []
    rows = await session.scalars(
        select(ModelPrice).where(
            ModelPrice.model.in_(models),
            or_(ModelPrice.project_id == project_id, ModelPrice.project_id.is_(None)),
        )
    )
    return [Price.from_row(r) for r in rows]


async def apply_costs(
    session: AsyncSession, project_id: uuid.UUID, spans: list[dict[str, Any]]
) -> None:
    """Set ``estimated_cost_usd`` and ``price_id`` on span rows (in place)."""
    priced = [s for s in spans if needs_price(s)]
    prices = await load_prices(session, project_id, (s["model"] for s in priced))
    for span in spans:
        span["estimated_cost_usd"] = None
        span["price_id"] = None
    for span in priced:
        price = match_price(
            prices, model=span["model"], provider=span.get("provider"), at=span["start_time"]
        )
        if price is not None:
            span["price_id"] = price.id
            span["estimated_cost_usd"] = cost_usd(
                price, span["input_tokens"], span["output_tokens"], span["cached_input_tokens"]
            )


async def recompute_costs(
    session: AsyncSession, project_id: uuid.UUID, since: datetime | None = None, chunk: int = 2000
) -> int:
    """Re-price stored spans (e.g. after adding or correcting a price) and refresh
    trace totals. Returns the number of spans re-priced."""
    updated = 0
    touched: set[str] = set()
    last: tuple[str, str] = ("", "")
    while True:
        stmt = (
            select(Span)
            .where(
                Span.project_id == project_id,
                Span.model.is_not(None),
                or_(Span.input_tokens.is_not(None), Span.output_tokens.is_not(None)),
                tuple_(Span.trace_id, Span.span_id) > last,
            )
            .order_by(Span.trace_id, Span.span_id)
            .limit(chunk)
        )
        if since is not None:
            stmt = stmt.where(Span.start_time >= since)
        spans = list(await session.scalars(stmt))
        if not spans:
            break
        prices = await load_prices(session, project_id, (s.model for s in spans))
        for span in spans:
            price = match_price(
                prices, model=span.model, provider=span.provider, at=span.start_time
            )
            span.price_id = price.id if price else None
            span.estimated_cost_usd = (
                cost_usd(price, span.input_tokens, span.output_tokens, span.cached_input_tokens)
                if price
                else None
            )
            touched.add(span.trace_id)
        updated += len(spans)
        last = (spans[-1].trace_id, spans[-1].span_id)
        await session.flush()

    if touched:
        await _refresh_trace_costs(session, project_id, sorted(touched))
    await session.commit()
    return updated


async def _refresh_trace_costs(
    session: AsyncSession, project_id: uuid.UUID, trace_ids: list[str]
) -> None:
    priced_llm = Span.model.is_not(None) & (
        Span.input_tokens.is_not(None) | Span.output_tokens.is_not(None)
    )
    for i in range(0, len(trace_ids), 1000):
        ids = trace_ids[i : i + 1000]
        totals = (
            select(
                Span.trace_id,
                func.coalesce(func.sum(Span.estimated_cost_usd), 0).label("cost"),
                func.count()
                .filter(priced_llm & Span.estimated_cost_usd.is_(None))
                .label("unpriced"),
            )
            .where(Span.project_id == project_id, Span.trace_id.in_(ids))
            .group_by(Span.trace_id)
            .subquery()
        )
        await session.execute(
            update(Trace)
            .where(Trace.project_id == project_id, Trace.trace_id == totals.c.trace_id)
            .values(estimated_cost_usd=totals.c.cost, unpriced_llm_calls=totals.c.unpriced)
        )
