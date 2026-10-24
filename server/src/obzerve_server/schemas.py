import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, computed_field

from obzerve_server.replay_info import replayability


class _Orm(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class ProjectOut(_Orm):
    id: uuid.UUID
    name: str
    created_at: datetime


class TraceSummary(_Orm):
    trace_id: str
    root_span_id: str | None
    name: str | None
    service_name: str | None
    status: str
    start_time: datetime
    end_time: datetime
    duration_ms: float
    span_count: int
    error_count: int
    retry_count: int
    input_tokens: int
    output_tokens: int
    cached_input_tokens: int
    estimated_cost_usd: float
    unpriced_llm_calls: int
    root_input_complete: bool | None = None
    replay_points: int = 0
    replay_points_incomplete: int = 0
    replay_side_effects: int = 0
    replay_of: str | None = None
    replay_mode: str | None = None
    replay_label: str | None = None
    prompt_name: str | None = None
    prompt_version: str | None = None
    eval_run_id: str | None = None

    @computed_field
    @property
    def replayability(self) -> str:
        return replayability(self)[0]  # type: ignore[arg-type]

    @computed_field
    @property
    def replay_reasons(self) -> list[str]:
        return replayability(self)[1]  # type: ignore[arg-type]


class SpanOut(_Orm):
    trace_id: str
    span_id: str
    parent_span_id: str | None
    name: str
    kind: str
    service_name: str | None
    start_time: datetime
    end_time: datetime
    duration_ms: float
    status: str
    status_message: str | None
    error_type: str | None
    model: str | None
    provider: str | None
    input_tokens: int | None
    output_tokens: int | None
    cached_input_tokens: int | None
    estimated_cost_usd: float | None
    retry_count: int
    input: str | None
    output: str | None
    attributes: dict[str, Any]
    events: list[dict[str, Any]]
    resource: dict[str, Any]


class SpanSummary(_Orm):
    """A span without its payloads, for search results."""

    trace_id: str
    span_id: str
    name: str
    kind: str
    start_time: datetime
    duration_ms: float
    status: str
    status_message: str | None
    error_type: str | None
    model: str | None
    retry_count: int


class TracePage(BaseModel):
    items: list[TraceSummary]
    next_cursor: str | None


class SpanPage(BaseModel):
    items: list[SpanSummary]
    next_cursor: str | None


class TraceDetail(BaseModel):
    trace: TraceSummary
    spans: list[SpanOut]


class ErrorTypeCount(BaseModel):
    error_type: str
    count: int
