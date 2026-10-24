"""Aggregate performance, reliability, and cost metrics over a time window.

Requests are traces (one workflow execution each). Costs are estimates derived
from the configured price table; ``unpriced_llm_calls`` > 0 means the cost is
a lower bound.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel
from sqlalchemy import and_, func, literal, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from obzerve_server.auth import SCOPE_READ, Principal, require_scope
from obzerve_server.db import get_session
from obzerve_server.models import Span, Trace

router = APIRouter(prefix="/api/v1/metrics", tags=["metrics"])
reader = require_scope(SCOPE_READ)

_MAX_RANGE = timedelta(days=90)
_BUCKETS = [
    timedelta(minutes=1),
    timedelta(minutes=5),
    timedelta(minutes=15),
    timedelta(hours=1),
    timedelta(hours=6),
    timedelta(days=1),
]
_MAX_POINTS = 120
_ORIGIN = datetime(2000, 1, 3, tzinfo=UTC)  # a Monday, so daily buckets align to midnight UTC


class Window(BaseModel):
    start: datetime
    end: datetime


def _window(
    start: datetime | None = Query(None, description="Defaults to 24 hours before `end`"),
    end: datetime | None = Query(None, description="Defaults to now"),
) -> Window:
    end = end or datetime.now(UTC)
    start = start or end - timedelta(hours=24)
    if start.tzinfo is None or end.tzinfo is None:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "Timestamps need a timezone")
    if start >= end:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "`start` must be before `end`")
    if end - start > _MAX_RANGE:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "Range is limited to 90 days")
    return Window(start=start, end=end)


def _pct(column, fraction: float):
    return func.percentile_cont(fraction).within_group(column)


def _rate(part, whole) -> float | None:
    return round(part / whole, 6) if whole else None


def _num(value) -> float:
    return float(value) if value is not None else 0.0


class Latency(BaseModel):
    p50_ms: float | None
    p95_ms: float | None
    p99_ms: float | None


class Summary(BaseModel):
    start: datetime
    end: datetime
    requests: int
    errors: int
    error_rate: float | None
    retried_requests: int
    retry_rate: float | None
    latency: Latency
    input_tokens: int
    output_tokens: int
    cached_input_tokens: int
    estimated_cost_usd: float
    estimated_cost_per_request_usd: float | None
    unpriced_llm_calls: int
    llm_calls: int
    llm_error_rate: float | None
    llm_timeout_rate: float | None
    tool_calls: int
    tool_success_rate: float | None
    # Mean time per request spent in each kind of operation (overlapping spans count fully).
    time_per_request_ms: dict[str, float]


@router.get("/summary", response_model=Summary)
async def summary(
    window: Window = Depends(_window),
    workflow: str | None = Query(None, max_length=200, description="Exact trace name"),
    principal: Principal = Depends(reader),
    session: AsyncSession = Depends(get_session),
):
    trace_filter = and_(
        Trace.project_id == principal.project_id,
        Trace.start_time >= window.start,
        Trace.start_time < window.end,
        *([Trace.name == workflow] if workflow else []),
    )
    t = (
        await session.execute(
            select(
                func.count(),
                func.count().filter(Trace.status == "error"),
                func.count().filter(Trace.retry_count > 0),
                _pct(Trace.duration_ms, 0.5),
                _pct(Trace.duration_ms, 0.95),
                _pct(Trace.duration_ms, 0.99),
                func.coalesce(func.sum(Trace.input_tokens), 0),
                func.coalesce(func.sum(Trace.output_tokens), 0),
                func.coalesce(func.sum(Trace.cached_input_tokens), 0),
                func.coalesce(func.sum(Trace.estimated_cost_usd), 0),
                func.coalesce(func.sum(Trace.unpriced_llm_calls), 0),
            ).where(trace_filter)
        )
    ).one()
    requests, errors, retried = t[0], t[1], t[2]

    span_filter = and_(
        Span.project_id == principal.project_id,
        Span.start_time >= window.start,
        Span.start_time < window.end,
    )
    if workflow:
        span_filter = and_(
            span_filter,
            Span.trace_id.in_(select(Trace.trace_id).where(trace_filter)),
        )
    kinds = await session.execute(
        select(
            Span.kind,
            func.count(),
            func.count().filter(Span.status == "error"),
            func.count().filter(Span.error_type.ilike("%timeout%")),
            func.coalesce(func.sum(Span.duration_ms), 0),
        )
        .where(span_filter, Span.kind.in_(["llm", "tool", "retrieval"]))
        .group_by(Span.kind)
    )
    by_kind = {k: (n, err, timeouts, total_ms) for k, n, err, timeouts, total_ms in kinds}
    llm = by_kind.get("llm", (0, 0, 0, 0))
    tool = by_kind.get("tool", (0, 0, 0, 0))

    cost = _num(t[9])
    return Summary(
        start=window.start,
        end=window.end,
        requests=requests,
        errors=errors,
        error_rate=_rate(errors, requests),
        retried_requests=retried,
        retry_rate=_rate(retried, requests),
        latency=Latency(p50_ms=t[3], p95_ms=t[4], p99_ms=t[5]),
        input_tokens=t[6],
        output_tokens=t[7],
        cached_input_tokens=t[8],
        estimated_cost_usd=cost,
        estimated_cost_per_request_usd=cost / requests if requests else None,
        unpriced_llm_calls=t[10],
        llm_calls=llm[0],
        llm_error_rate=_rate(llm[1], llm[0]),
        llm_timeout_rate=_rate(llm[2], llm[0]),
        tool_calls=tool[0],
        tool_success_rate=_rate(tool[0] - tool[1], tool[0]),
        time_per_request_ms={
            k: round(_num(v[3]) / requests, 3) if requests else 0.0 for k, v in by_kind.items()
        },
    )


class Point(BaseModel):
    time: datetime
    requests: int
    errors: int
    p50_ms: float | None
    p95_ms: float | None
    input_tokens: int
    output_tokens: int
    estimated_cost_usd: float


class Timeseries(BaseModel):
    start: datetime
    end: datetime
    bucket_seconds: int
    points: list[Point]


def _bucket_for(window: Window) -> timedelta:
    span = window.end - window.start
    return next((b for b in _BUCKETS if span / b <= _MAX_POINTS), _BUCKETS[-1])


@router.get("/timeseries", response_model=Timeseries)
async def timeseries(
    window: Window = Depends(_window),
    workflow: str | None = Query(None, max_length=200),
    principal: Principal = Depends(reader),
    session: AsyncSession = Depends(get_session),
):
    bucket = _bucket_for(window)
    binned = func.date_bin(literal(bucket), Trace.start_time, literal(_ORIGIN)).label("bucket")
    stmt = (
        select(
            binned,
            func.count(),
            func.count().filter(Trace.status == "error"),
            _pct(Trace.duration_ms, 0.5),
            _pct(Trace.duration_ms, 0.95),
            func.coalesce(func.sum(Trace.input_tokens), 0),
            func.coalesce(func.sum(Trace.output_tokens), 0),
            func.coalesce(func.sum(Trace.estimated_cost_usd), 0),
        )
        .where(
            Trace.project_id == principal.project_id,
            Trace.start_time >= window.start,
            Trace.start_time < window.end,
            *([Trace.name == workflow] if workflow else []),
        )
        .group_by(text("bucket"))
    )
    rows = {r[0]: r for r in await session.execute(stmt)}

    # Emit every bucket in the window, including empty ones, so charts show gaps honestly.
    first = _ORIGIN + ((window.start - _ORIGIN) // bucket) * bucket
    points, at = [], first
    while at < window.end:
        r = rows.get(at)
        points.append(
            Point(
                time=at,
                requests=r[1] if r else 0,
                errors=r[2] if r else 0,
                p50_ms=r[3] if r else None,
                p95_ms=r[4] if r else None,
                input_tokens=r[5] if r else 0,
                output_tokens=r[6] if r else 0,
                estimated_cost_usd=_num(r[7]) if r else 0.0,
            )
        )
        at += bucket
    return Timeseries(
        start=window.start,
        end=window.end,
        bucket_seconds=int(bucket.total_seconds()),
        points=points,
    )


class BreakdownRow(BaseModel):
    key: str
    kind: str | None = None
    provider: str | None = None
    count: int
    errors: int
    error_rate: float | None
    retries: int
    p50_ms: float | None
    p95_ms: float | None
    p99_ms: float | None
    input_tokens: int
    output_tokens: int
    cached_input_tokens: int
    estimated_cost_usd: float
    unpriced_llm_calls: int


class Breakdown(BaseModel):
    group_by: str
    rows: list[BreakdownRow]


GroupBy = Literal["workflow", "service", "operation", "kind", "model"]


@router.get("/breakdown", response_model=Breakdown)
async def breakdown(
    group_by: GroupBy = "operation",
    window: Window = Depends(_window),
    workflow: str | None = Query(None, max_length=200, description="Exact trace name"),
    limit: int = Query(50, ge=1, le=500),
    principal: Principal = Depends(reader),
    session: AsyncSession = Depends(get_session),
):
    in_window = and_(
        Trace.project_id == principal.project_id,
        Trace.start_time >= window.start,
        Trace.start_time < window.end,
        *([Trace.name == workflow] if workflow else []),
    )
    if group_by in ("workflow", "service"):
        key = func.coalesce(Trace.name if group_by == "workflow" else Trace.service_name, "(none)")
        stmt = (
            select(
                key.label("key"),
                literal(None).label("kind"),
                literal(None).label("provider"),
                func.count(),
                func.count().filter(Trace.status == "error"),
                func.coalesce(func.sum(Trace.retry_count), 0),
                _pct(Trace.duration_ms, 0.5),
                _pct(Trace.duration_ms, 0.95),
                _pct(Trace.duration_ms, 0.99),
                func.coalesce(func.sum(Trace.input_tokens), 0),
                func.coalesce(func.sum(Trace.output_tokens), 0),
                func.coalesce(func.sum(Trace.cached_input_tokens), 0),
                func.coalesce(func.sum(Trace.estimated_cost_usd), 0),
                func.coalesce(func.sum(Trace.unpriced_llm_calls), 0),
            )
            .where(in_window)
            .group_by(key)
        )
    else:
        priced = Span.model.is_not(None) & (
            Span.input_tokens.is_not(None) | Span.output_tokens.is_not(None)
        )
        # (key, kind, provider) columns, and the columns to group by
        if group_by == "operation":
            key, kind, provider, groups = (
                Span.name,
                Span.kind,
                literal(None),
                [Span.name, Span.kind],
            )
        elif group_by == "kind":
            key, kind, provider, groups = Span.kind, literal(None), literal(None), [Span.kind]
        else:
            key, kind, provider = Span.model, literal("llm"), Span.provider
            groups = [Span.model, Span.provider]
        stmt = (
            select(
                key.label("key"),
                kind.label("kind"),
                provider.label("provider"),
                func.count(),
                func.count().filter(Span.status == "error"),
                func.coalesce(func.sum(Span.retry_count), 0),
                _pct(Span.duration_ms, 0.5),
                _pct(Span.duration_ms, 0.95),
                _pct(Span.duration_ms, 0.99),
                func.coalesce(func.sum(Span.input_tokens), 0),
                func.coalesce(func.sum(Span.output_tokens), 0),
                func.coalesce(func.sum(Span.cached_input_tokens), 0),
                func.coalesce(func.sum(Span.estimated_cost_usd), 0),
                func.count().filter(priced & Span.estimated_cost_usd.is_(None)),
            )
            .where(
                Span.project_id == principal.project_id,
                Span.start_time >= window.start,
                Span.start_time < window.end,
                *([Span.model.is_not(None)] if group_by == "model" else []),
                *([Span.trace_id.in_(select(Trace.trace_id).where(in_window))] if workflow else []),
            )
            .group_by(*groups)
        )
    stmt = stmt.order_by(func.count().desc(), text("key")).limit(limit)
    rows = [
        BreakdownRow(
            key=r[0],
            kind=r[1],
            provider=r[2],
            count=r[3],
            errors=r[4],
            error_rate=_rate(r[4], r[3]),
            retries=r[5],
            p50_ms=r[6],
            p95_ms=r[7],
            p99_ms=r[8],
            input_tokens=r[9],
            output_tokens=r[10],
            cached_input_tokens=r[11],
            estimated_cost_usd=_num(r[12]),
            unpriced_llm_calls=r[13],
        )
        for r in await session.execute(stmt)
    ]
    return Breakdown(group_by=group_by, rows=rows)
