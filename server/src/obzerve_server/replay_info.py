"""Replayability of stored traces and the replay bundles served to the SDK."""

from __future__ import annotations

import json
import uuid
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from obzerve_server.models import AuditEvent, Span, Trace

REPLAYABLE = "replayable"
PARTIAL = "partial"
NON_REPLAYABLE = "non_replayable"


def replayability(trace: Trace) -> tuple[str, list[str]]:
    """Classify a trace and explain why, from aggregates kept at ingest."""
    reasons: list[str] = []
    if trace.root_span_id is None:
        return NON_REPLAYABLE, ["the root span has not been received"]
    if trace.replay_points == 0:
        return NON_REPLAYABLE, [
            "no replayable operations were recorded (wrap tools and model calls with "
            "Observer.replayable)"
        ]
    if not trace.root_input_complete:
        return NON_REPLAYABLE, [
            "the workflow input was not recorded exactly (missing, redacted, truncated, "
            "or not JSON-serializable)"
        ]
    status = REPLAYABLE
    if trace.replay_points_incomplete:
        status = PARTIAL
        reasons.append(
            f"{trace.replay_points_incomplete} of {trace.replay_points} operations have no exact "
            "recording; a replay must run them live"
        )
    if trace.replay_side_effects:
        reasons.append(
            f"{trace.replay_side_effects} side-effecting operations: replays reuse their "
            "recordings and never re-run them unless explicitly allowed"
        )
    return status, reasons


def build_bundle(trace: Trace, spans: list[Span]) -> dict[str, Any]:
    status, reasons = replayability(trace)
    root = next((s for s in spans if s.span_id == trace.root_span_id), None)
    root_input = None
    if root is not None and trace.root_input_complete and root.input is not None:
        try:
            root_input = json.loads(root.input)
        except ValueError:
            status, reasons = NON_REPLAYABLE, ["the recorded workflow input is not valid JSON"]

    recordings: list[dict[str, Any]] = []
    seen: set[str] = set()
    for span in sorted(spans, key=lambda s: (s.start_time, s.span_id)):
        key = span.attributes.get("obzerve.replay.key")
        if not key:
            continue
        if key in seen:
            if status == REPLAYABLE:
                status = PARTIAL
            reasons.append(f"operation {key} was recorded more than once (concurrent calls?)")
            continue
        seen.add(key)
        failed = span.status == "error"
        recordings.append(
            {
                "key": key,
                "name": span.name,
                "kind": span.kind,
                "input_hash": span.attributes.get("obzerve.replay.input_hash"),
                "output": None if failed else span.output,
                "complete": span.attributes.get("obzerve.replay.complete") is True,
                "incomplete_reason": span.attributes.get("obzerve.replay.incomplete_reason"),
                "side_effect": span.attributes.get("obzerve.replay.side_effect") is True,
                "error_type": span.error_type if failed else None,
                "error_class": span.attributes.get("obzerve.replay.error_class")
                if failed
                else None,
                "error_message": span.status_message if failed else None,
            }
        )
    return {
        "trace_id": trace.trace_id,
        "replayability": status,
        "reasons": reasons,
        "root_name": trace.name,
        "root_input": root_input,
        "recordings": recordings,
        "attributes": dict(root.attributes) if root is not None else {},
    }


async def audit(
    session: AsyncSession,
    project_id: uuid.UUID,
    key_id: uuid.UUID | None,
    action: str,
    target: str | None,
    detail: dict[str, Any] | None = None,
) -> None:
    session.add(
        AuditEvent(
            project_id=project_id,
            api_key_id=key_id,
            action=action,
            target=target,
            detail=detail or {},
        )
    )
