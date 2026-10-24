"""Idempotent persistence of decoded spans and their trace aggregates.

Spans are keyed by (project_id, trace_id, span_id) and inserted with
ON CONFLICT DO NOTHING; trace aggregates are updated only from the spans that
were actually inserted, so re-delivering a batch (exporter retries, queue
redelivery) changes nothing. LLM spans are priced on the way in.
"""

from __future__ import annotations

import uuid
from collections import defaultdict
from decimal import Decimal
from typing import Any

from sqlalchemy import case, func, literal_column
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from obzerve_server.models import Span, Trace
from obzerve_server.pricing import apply_costs, needs_price

# Statements are built once and executed with parameter lists ("executemany"), so
# SQLAlchemy compiles them once instead of per request; it still sends multi-row
# INSERTs with RETURNING under the hood.
_SPAN_CHUNK = 500
_INSERT_SPANS = (
    pg_insert(Span.__table__)
    .on_conflict_do_nothing(index_elements=["project_id", "trace_id", "span_id"])
    .returning(Span.__table__.c.trace_id, Span.__table__.c.span_id)
)


async def persist_spans(
    session: AsyncSession, project_id: uuid.UUID, rows: list[dict[str, Any]]
) -> int:
    """Insert spans and update trace aggregates in one transaction. Returns inserted count."""
    if not rows:
        return 0
    # Drop duplicates within the batch, then sort: a consistent ordering avoids
    # lock-order deadlocks between concurrent batches.
    unique = {(r["trace_id"], r["span_id"]): {**r, "project_id": project_id} for r in rows}
    rows = [unique[k] for k in sorted(unique)]
    await apply_costs(session, project_id, rows)
    inserted: list[dict[str, Any]] = []
    for i in range(0, len(rows), _SPAN_CHUNK):
        chunk = rows[i : i + _SPAN_CHUNK]
        keys = {(r.trace_id, r.span_id) for r in await session.execute(_INSERT_SPANS, chunk)}
        inserted.extend(r for r in chunk if (r["trace_id"], r["span_id"]) in keys)

    if inserted:
        await _upsert_traces(session, project_id, inserted)
    await session.commit()
    return len(inserted)


async def _upsert_traces(
    session: AsyncSession, project_id: uuid.UUID, spans: list[dict[str, Any]]
) -> None:
    by_trace: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for span in spans:
        by_trace[span["trace_id"]].append(span)

    values = []
    for trace_id in sorted(by_trace):
        group = by_trace[trace_id]
        root = next((s for s in group if s["parent_span_id"] is None), None)
        earliest = min(group, key=lambda s: s["start_time"])
        start = earliest["start_time"]
        end = max(s["end_time"] for s in group)
        values.append(
            {
                "project_id": project_id,
                "trace_id": trace_id,
                "root_span_id": root["span_id"] if root else None,
                "name": (root or earliest)["name"],
                "service_name": (root or earliest)["service_name"],
                "status": root["status"] if root else "unset",
                "start_time": start,
                "end_time": end,
                "duration_ms": (end - start).total_seconds() * 1000,
                "span_count": len(group),
                "error_count": sum(s["status"] == "error" for s in group),
                "retry_count": sum(s["retry_count"] for s in group),
                "input_tokens": sum(s["input_tokens"] or 0 for s in group),
                "output_tokens": sum(s["output_tokens"] or 0 for s in group),
                "cached_input_tokens": sum(s["cached_input_tokens"] or 0 for s in group),
                "estimated_cost_usd": sum(
                    (s["estimated_cost_usd"] for s in group if s["estimated_cost_usd"] is not None),
                    start=Decimal(0),
                ),
                "unpriced_llm_calls": sum(
                    needs_price(s) and s["estimated_cost_usd"] is None for s in group
                ),
                **_replay_fields(group, root),
            }
        )

    await session.execute(_UPSERT_TRACES, values)


# Trace columns taken from the root span's attributes.
_ROOT_ATTRIBUTES = {
    "replay_of": "obzerve.replay.of",
    "replay_mode": "obzerve.replay.mode",
    "replay_label": "obzerve.replay.label",
    "prompt_name": "obzerve.prompt.name",
    "prompt_version": "obzerve.prompt.version",
    "eval_run_id": "obzerve.eval.run_id",
}


def _replay_fields(group: list[dict[str, Any]], root: dict[str, Any] | None) -> dict[str, Any]:
    points = [s for s in group if "obzerve.replay.key" in s["attributes"]]
    fields: dict[str, Any] = {
        "replay_points": len(points),
        "replay_points_incomplete": sum(
            s["attributes"].get("obzerve.replay.complete") is not True for s in points
        ),
        "replay_side_effects": sum(
            s["attributes"].get("obzerve.replay.side_effect") is True for s in points
        ),
        "root_input_complete": None,
    }
    root_attrs = root["attributes"] if root else {}
    if root is not None:
        fields["root_input_complete"] = (
            root["input"] is not None and root_attrs.get("obzerve.replay.complete") is True
        )
    for column, key in _ROOT_ATTRIBUTES.items():
        value = root_attrs.get(key)
        fields[column] = None if value is None else str(value)[:500]
    return fields


def _build_trace_upsert():
    table = Trace.__table__
    stmt = pg_insert(table)
    excluded = stmt.excluded
    has_root = excluded.root_span_id.is_not(None)
    start = func.least(table.c.start_time, excluded.start_time)
    end = func.greatest(table.c.end_time, excluded.end_time)

    def add(column: str):
        return table.c[column] + excluded[column]

    return stmt.on_conflict_do_update(
        index_elements=["project_id", "trace_id"],
        set_={
            "root_span_id": func.coalesce(excluded.root_span_id, table.c.root_span_id),
            "name": case(
                (has_root, excluded.name), else_=func.coalesce(table.c.name, excluded.name)
            ),
            "service_name": case(
                (has_root, excluded.service_name),
                else_=func.coalesce(table.c.service_name, excluded.service_name),
            ),
            "status": case((has_root, excluded.status), else_=table.c.status),
            **{
                column: case((has_root, excluded[column]), else_=table.c[column])
                for column in ("root_input_complete", *_ROOT_ATTRIBUTES)
            },
            "start_time": start,
            "end_time": end,
            "duration_ms": func.extract("epoch", end - start) * 1000,
            **{
                column: add(column)
                for column in (
                    "span_count",
                    "error_count",
                    "retry_count",
                    "input_tokens",
                    "output_tokens",
                    "cached_input_tokens",
                    "estimated_cost_usd",
                    "unpriced_llm_calls",
                    "replay_points",
                    "replay_points_incomplete",
                    "replay_side_effects",
                )
            },
            "updated_at": literal_column("now()"),
        },
    )


_UPSERT_TRACES = _build_trace_upsert()
