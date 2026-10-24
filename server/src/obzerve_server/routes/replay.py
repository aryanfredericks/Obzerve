"""Replay bundles, a trace's replays, and side-by-side comparison of two traces."""

from __future__ import annotations

import re
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Path, Query, status
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from obzerve_server.auth import SCOPE_READ, Principal, require_scope
from obzerve_server.db import get_session
from obzerve_server.models import Span, Trace
from obzerve_server.replay_info import audit, build_bundle
from obzerve_server.schemas import TraceSummary

router = APIRouter(prefix="/api/v1", tags=["replay"])
reader = require_scope(SCOPE_READ)
_HEX32 = re.compile(r"^[0-9a-f]{32}$")


async def load_trace(
    session: AsyncSession, principal: Principal, trace_id: str
) -> tuple[Trace, list[Span]]:
    trace_id = trace_id.lower()
    trace = (
        await session.get(Trace, (principal.project_id, trace_id))
        if _HEX32.match(trace_id)
        else None
    )
    if trace is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"Trace {trace_id} not found")
    spans = await session.scalars(
        select(Span).where(Span.project_id == principal.project_id, Span.trace_id == trace_id)
    )
    return trace, list(spans)


@router.get("/traces/{trace_id}/replay")
async def replay_bundle(
    trace_id: str = Path(max_length=32),
    principal: Principal = Depends(reader),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """Everything the SDK needs to replay this trace. Contains captured payloads,
    so every read is audited."""
    trace, spans = await load_trace(session, principal, trace_id)
    bundle = build_bundle(trace, spans)
    await audit(session, principal.project_id, principal.key_id, "replay_bundle.read", trace_id)
    await session.commit()
    return bundle


@router.get("/traces/{trace_id}/replays", response_model=list[TraceSummary])
async def list_replays(
    trace_id: str = Path(max_length=32),
    principal: Principal = Depends(reader),
    session: AsyncSession = Depends(get_session),
):
    await load_trace(session, principal, trace_id)
    rows = await session.scalars(
        select(Trace)
        .where(Trace.project_id == principal.project_id, Trace.replay_of == trace_id.lower())
        .order_by(Trace.start_time.desc())
        .limit(100)
    )
    return [TraceSummary.model_validate(r) for r in rows]


class StepSide(BaseModel):
    span_id: str
    status: str
    duration_ms: float
    error_type: str | None
    status_message: str | None
    source: str | None  # "recorded" / "live" in a replay, None in an ordinary run
    input_matched: bool | None
    input: str | None
    output: str | None


class Step(BaseModel):
    key: str
    name: str
    kind: str
    a: StepSide | None
    b: StepSide | None
    output_changed: bool
    input_changed: bool


class Comparison(BaseModel):
    a: TraceSummary
    b: TraceSummary
    a_output: str | None
    b_output: str | None
    output_changed: bool
    steps: list[Step]


def _side(span: Span) -> StepSide:
    return StepSide(
        span_id=span.span_id,
        status=span.status,
        duration_ms=span.duration_ms,
        error_type=span.error_type,
        status_message=span.status_message,
        source=span.attributes.get("obzerve.replay.source"),
        input_matched=span.attributes.get("obzerve.replay.input_matched"),
        input=span.input,
        output=span.output,
    )


def _keyed(spans: list[Span]) -> dict[str, Span]:
    keyed: dict[str, Span] = {}
    for span in sorted(spans, key=lambda s: (s.start_time, s.span_id)):
        key = span.attributes.get("obzerve.replay.key")
        if key and key not in keyed:
            keyed[key] = span
    return keyed


@router.get("/trace-comparison", response_model=Comparison)
async def compare(
    a: str = Query(max_length=32, description="Usually the original trace"),
    b: str = Query(max_length=32, description="Usually a replay of it"),
    principal: Principal = Depends(reader),
    session: AsyncSession = Depends(get_session),
):
    trace_a, spans_a = await load_trace(session, principal, a)
    trace_b, spans_b = await load_trace(session, principal, b)
    keyed_a, keyed_b = _keyed(spans_a), _keyed(spans_b)
    # Steps in the order they first happened in either trace.
    order = {k: (s.start_time, 0) for k, s in keyed_a.items()}
    for k, s in keyed_b.items():
        order.setdefault(k, (s.start_time, 1))
    steps = []
    for key in sorted(order, key=lambda k: order[k]):
        sa, sb = keyed_a.get(key), keyed_b.get(key)
        ref = sa or sb
        steps.append(
            Step(
                key=key,
                name=ref.name,
                kind=ref.kind,
                a=_side(sa) if sa else None,
                b=_side(sb) if sb else None,
                output_changed=bool(
                    sa and sb and (sa.output != sb.output or sa.status != sb.status)
                ),
                input_changed=bool(
                    sa
                    and sb
                    and sa.attributes.get("obzerve.replay.input_hash")
                    != sb.attributes.get("obzerve.replay.input_hash")
                ),
            )
        )
    root_a = next((s for s in spans_a if s.span_id == trace_a.root_span_id), None)
    root_b = next((s for s in spans_b if s.span_id == trace_b.root_span_id), None)
    out_a = root_a.output if root_a else None
    out_b = root_b.output if root_b else None
    return Comparison(
        a=TraceSummary.model_validate(trace_a),
        b=TraceSummary.model_validate(trace_b),
        a_output=out_a,
        b_output=out_b,
        output_changed=out_a != out_b or trace_a.status != trace_b.status,
        steps=steps,
    )
