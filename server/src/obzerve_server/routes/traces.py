"""Dashboard API. Every query is filtered by the caller's project."""

import re
from datetime import datetime
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Path, Query, status
from sqlalchemy import Select, and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from obzerve_server.auth import SCOPE_READ, Principal, require_scope
from obzerve_server.db import get_session
from obzerve_server.models import Project, Span, Trace
from obzerve_server.schemas import (
    ErrorTypeCount,
    ProjectOut,
    SpanOut,
    SpanPage,
    SpanSummary,
    TraceDetail,
    TracePage,
    TraceSummary,
)

router = APIRouter(prefix="/api/v1", tags=["dashboard"])
reader = require_scope(SCOPE_READ)

StatusFilter = Literal["ok", "error", "unset"]
_HEX = re.compile(r"^[0-9a-f]+$")


def _like(text: str) -> str:
    escaped = text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


def _encode_cursor(start_time: datetime, key: str) -> str:
    return f"{start_time.isoformat()}|{key}"


def _apply_cursor(stmt: Select, cursor: str | None, time_col, key_col) -> Select:
    """Keyset pagination over (start_time DESC, key DESC)."""
    if not cursor:
        return stmt
    try:
        raw_time, key = cursor.rsplit("|", 1)
        start_time = datetime.fromisoformat(raw_time)
    except ValueError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Invalid cursor") from exc
    return stmt.where(or_(time_col < start_time, and_(time_col == start_time, key_col < key)))


@router.get("/project", response_model=ProjectOut)
async def get_project(
    principal: Principal = Depends(reader), session: AsyncSession = Depends(get_session)
):
    return await session.get(Project, principal.project_id)


@router.get("/traces", response_model=TracePage)
async def list_traces(
    q: str | None = Query(None, max_length=200, description="Substring of the trace name"),
    status_: StatusFilter | None = Query(None, alias="status"),
    has_errors: bool | None = None,
    has_retries: bool | None = None,
    min_duration_ms: float | None = Query(None, ge=0),
    start_after: datetime | None = None,
    start_before: datetime | None = None,
    limit: int = Query(50, ge=1, le=200),
    cursor: str | None = None,
    principal: Principal = Depends(reader),
    session: AsyncSession = Depends(get_session),
):
    stmt = select(Trace).where(Trace.project_id == principal.project_id)
    if q:
        stmt = stmt.where(Trace.name.ilike(_like(q)))
    if status_:
        stmt = stmt.where(Trace.status == status_)
    if has_errors is not None:
        stmt = stmt.where(Trace.error_count > 0 if has_errors else Trace.error_count == 0)
    if has_retries is not None:
        stmt = stmt.where(Trace.retry_count > 0 if has_retries else Trace.retry_count == 0)
    if min_duration_ms is not None:
        stmt = stmt.where(Trace.duration_ms >= min_duration_ms)
    if start_after:
        stmt = stmt.where(Trace.start_time >= start_after)
    if start_before:
        stmt = stmt.where(Trace.start_time < start_before)
    stmt = _apply_cursor(stmt, cursor, Trace.start_time, Trace.trace_id)
    stmt = stmt.order_by(Trace.start_time.desc(), Trace.trace_id.desc()).limit(limit + 1)

    rows = list(await session.scalars(stmt))
    next_cursor = None
    if len(rows) > limit:
        rows = rows[:limit]
        next_cursor = _encode_cursor(rows[-1].start_time, rows[-1].trace_id)
    return TracePage(items=[TraceSummary.model_validate(r) for r in rows], next_cursor=next_cursor)


@router.get("/traces/{trace_id}", response_model=TraceDetail)
async def get_trace(
    trace_id: str = Path(min_length=32, max_length=32),
    principal: Principal = Depends(reader),
    session: AsyncSession = Depends(get_session),
):
    trace_id = trace_id.lower()
    if not _HEX.match(trace_id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Trace not found")
    trace = await session.get(Trace, (principal.project_id, trace_id))
    if trace is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Trace not found")
    spans = await session.scalars(
        select(Span)
        .where(Span.project_id == principal.project_id, Span.trace_id == trace_id)
        .order_by(Span.start_time, Span.span_id)
    )
    return TraceDetail(
        trace=TraceSummary.model_validate(trace),
        spans=[SpanOut.model_validate(s) for s in spans],
    )


@router.get("/spans", response_model=SpanPage)
async def search_spans(
    q: str | None = Query(None, max_length=200, description="Substring of the span name"),
    status_: StatusFilter | None = Query(None, alias="status"),
    error_type: str | None = Query(None, max_length=200),
    kind: str | None = Query(None, max_length=32),
    model: str | None = Query(None, max_length=200),
    has_retries: bool | None = None,
    limit: int = Query(50, ge=1, le=200),
    cursor: str | None = None,
    principal: Principal = Depends(reader),
    session: AsyncSession = Depends(get_session),
):
    stmt = select(Span).where(Span.project_id == principal.project_id)
    if q:
        stmt = stmt.where(Span.name.ilike(_like(q)))
    if status_:
        stmt = stmt.where(Span.status == status_)
    if error_type:
        stmt = stmt.where(Span.error_type == error_type)
    if kind:
        stmt = stmt.where(Span.kind == kind)
    if model:
        stmt = stmt.where(Span.model == model)
    if has_retries is not None:
        stmt = stmt.where(Span.retry_count > 0 if has_retries else Span.retry_count == 0)
    # Cursor key is trace_id + span_id, which is unique within a project.
    key = Span.trace_id + Span.span_id
    stmt = _apply_cursor(stmt, cursor, Span.start_time, key)
    stmt = stmt.order_by(Span.start_time.desc(), key.desc()).limit(limit + 1)

    rows = list(await session.scalars(stmt))
    next_cursor = None
    if len(rows) > limit:
        rows = rows[:limit]
        last = rows[-1]
        next_cursor = _encode_cursor(last.start_time, last.trace_id + last.span_id)
    return SpanPage(items=[SpanSummary.model_validate(r) for r in rows], next_cursor=next_cursor)


@router.get("/error-types", response_model=list[ErrorTypeCount])
async def list_error_types(
    principal: Principal = Depends(reader), session: AsyncSession = Depends(get_session)
):
    rows = await session.execute(
        select(Span.error_type, func.count())
        .where(
            Span.project_id == principal.project_id,
            Span.status == "error",
            Span.error_type.is_not(None),
        )
        .group_by(Span.error_type)
        .order_by(func.count().desc())
        .limit(100)
    )
    return [ErrorTypeCount(error_type=e, count=c) for e, c in rows]
