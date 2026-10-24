"""Database schema. Every telemetry table is keyed by ``project_id`` first so
that each query is scoped to one tenant; a trace ID alone never grants access."""

import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class Project(Base):
    __tablename__ = "projects"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(200), unique=True)
    settings: Mapped[dict] = mapped_column(JSONB, server_default=text("'{}'::jsonb"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ProjectApiKey(Base):
    __tablename__ = "project_api_keys"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    project_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), index=True
    )
    name: Mapped[str] = mapped_column(String(200))
    # Only a SHA-256 hash of the key is stored; the prefix helps humans identify it.
    key_prefix: Mapped[str] = mapped_column(String(16))
    key_hash: Mapped[str] = mapped_column(String(64), unique=True)
    scopes: Mapped[list[str]] = mapped_column(ARRAY(String(32)))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Trace(Base):
    """One workflow execution, aggregated from its spans as they arrive."""

    __tablename__ = "traces"

    project_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), primary_key=True
    )
    trace_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    root_span_id: Mapped[str | None] = mapped_column(String(16))
    name: Mapped[str | None] = mapped_column(Text)
    service_name: Mapped[str | None] = mapped_column(Text)
    # Root span status: ok | error | unset (unset until the root span arrives).
    status: Mapped[str] = mapped_column(String(16), server_default="unset")
    start_time: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    end_time: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    duration_ms: Mapped[float] = mapped_column(Float)
    span_count: Mapped[int] = mapped_column(Integer, server_default="0")
    error_count: Mapped[int] = mapped_column(Integer, server_default="0")
    retry_count: Mapped[int] = mapped_column(Integer, server_default="0")
    input_tokens: Mapped[int] = mapped_column(BigInteger, server_default="0")
    output_tokens: Mapped[int] = mapped_column(BigInteger, server_default="0")
    cached_input_tokens: Mapped[int] = mapped_column(BigInteger, server_default="0")
    # Sum of span cost estimates; ``unpriced_llm_calls`` counts LLM spans with token
    # usage but no matching price, in which case the estimate is a lower bound.
    estimated_cost_usd: Mapped[Decimal] = mapped_column(Numeric(20, 10), server_default="0")
    unpriced_llm_calls: Mapped[int] = mapped_column(Integer, server_default="0")
    # Replay: what this trace can replay (recorded operations) and what it replays.
    root_input_complete: Mapped[bool | None] = mapped_column(Boolean)
    replay_points: Mapped[int] = mapped_column(Integer, server_default="0")
    replay_points_incomplete: Mapped[int] = mapped_column(Integer, server_default="0")
    replay_side_effects: Mapped[int] = mapped_column(Integer, server_default="0")
    replay_of: Mapped[str | None] = mapped_column(String(32))
    replay_mode: Mapped[str | None] = mapped_column(String(32))
    replay_label: Mapped[str | None] = mapped_column(Text)
    prompt_name: Mapped[str | None] = mapped_column(Text)
    prompt_version: Mapped[str | None] = mapped_column(Text)
    eval_run_id: Mapped[str | None] = mapped_column(String(36))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        Index("ix_traces_project_start", "project_id", text("start_time DESC")),
        Index("ix_traces_project_status_start", "project_id", "status", text("start_time DESC")),
        Index("ix_traces_project_name", "project_id", "name"),
        Index(
            "ix_traces_project_replay_of",
            "project_id",
            "replay_of",
            postgresql_where=text("replay_of IS NOT NULL"),
        ),
    )


class Span(Base):
    __tablename__ = "spans"

    project_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), primary_key=True
    )
    trace_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    span_id: Mapped[str] = mapped_column(String(16), primary_key=True)
    parent_span_id: Mapped[str | None] = mapped_column(String(16))
    name: Mapped[str] = mapped_column(Text)
    kind: Mapped[str] = mapped_column(String(32))
    service_name: Mapped[str | None] = mapped_column(Text)
    start_time: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    end_time: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    duration_ms: Mapped[float] = mapped_column(Float)
    status: Mapped[str] = mapped_column(String(16))
    status_message: Mapped[str | None] = mapped_column(Text)
    error_type: Mapped[str | None] = mapped_column(Text)
    model: Mapped[str | None] = mapped_column(Text)
    provider: Mapped[str | None] = mapped_column(Text)
    input_tokens: Mapped[int | None] = mapped_column(BigInteger)
    output_tokens: Mapped[int | None] = mapped_column(BigInteger)
    cached_input_tokens: Mapped[int | None] = mapped_column(BigInteger)
    estimated_cost_usd: Mapped[Decimal | None] = mapped_column(Numeric(20, 10))
    price_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    retry_count: Mapped[int] = mapped_column(Integer, server_default="0")
    input: Mapped[str | None] = mapped_column(Text)
    output: Mapped[str | None] = mapped_column(Text)
    attributes: Mapped[dict] = mapped_column(JSONB, server_default=text("'{}'::jsonb"))
    events: Mapped[list] = mapped_column(JSONB, server_default=text("'[]'::jsonb"))
    resource: Mapped[dict] = mapped_column(JSONB, server_default=text("'{}'::jsonb"))
    ingested_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    __table_args__ = (
        Index("ix_spans_project_start", "project_id", text("start_time DESC")),
        Index(
            "ix_spans_project_errors",
            "project_id",
            "error_type",
            postgresql_where=text("status = 'error'"),
        ),
        # Covers per-kind metrics (summary, breakdown) with an index-only scan.
        Index(
            "ix_spans_project_kind_start",
            "project_id",
            "kind",
            "start_time",
            postgresql_include=["duration_ms", "status", "error_type", "retry_count"],
        ),
    )


class ModelPrice(Base):
    """Price per million tokens for a model, valid from ``effective_from`` until the
    next price for the same model. ``project_id`` NULL is a global default that a
    project-specific price overrides; ``provider`` NULL matches any provider."""

    __tablename__ = "model_prices"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    project_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE")
    )
    provider: Mapped[str | None] = mapped_column(Text)
    model: Mapped[str] = mapped_column(Text)
    input_usd_per_mtok: Mapped[Decimal] = mapped_column(Numeric(14, 6))
    output_usd_per_mtok: Mapped[Decimal] = mapped_column(Numeric(14, 6))
    # Price for prompt-cache reads; NULL means cached tokens cost the normal input price.
    cached_input_usd_per_mtok: Mapped[Decimal | None] = mapped_column(Numeric(14, 6))
    effective_from: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    note: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        Index(
            "uq_model_prices_scope",
            text("coalesce(project_id, '00000000-0000-0000-0000-000000000000'::uuid)"),
            text("coalesce(provider, '')"),
            "model",
            "effective_from",
            unique=True,
        ),
    )


class PromptVersion(Base):
    """An immutable, numbered version of a prompt/model configuration. Registering
    the same content again returns the existing version."""

    __tablename__ = "prompt_versions"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    project_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"))
    name: Mapped[str] = mapped_column(String(200))
    version: Mapped[int] = mapped_column(Integer)
    config: Mapped[dict] = mapped_column(JSONB)
    content_hash: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        UniqueConstraint("project_id", "name", "version"),
        UniqueConstraint("project_id", "name", "content_hash"),
    )


class EvalDataset(Base):
    """A named set of evaluation cases. ``version`` increases whenever a case is
    added or archived, so a run records exactly which cases it was measured on."""

    __tablename__ = "eval_datasets"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    project_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"))
    name: Mapped[str] = mapped_column(String(200))
    description: Mapped[str | None] = mapped_column(Text)
    version: Mapped[int] = mapped_column(Integer, server_default="0")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (UniqueConstraint("project_id", "name"),)


class EvalCase(Base):
    """A regression case: the input and recorded dependencies of a source trace
    (frozen at creation, so it outlives the trace) plus assertions on the outcome."""

    __tablename__ = "eval_cases"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    project_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"))
    dataset_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("eval_datasets.id", ondelete="CASCADE"), index=True
    )
    name: Mapped[str] = mapped_column(String(200))
    notes: Mapped[str | None] = mapped_column(Text)
    source_trace_id: Mapped[str | None] = mapped_column(String(32))
    bundle: Mapped[dict] = mapped_column(JSONB)
    assertions: Mapped[list] = mapped_column(JSONB)
    # How this case is replayed: "recorded" re-runs a recorded failure exactly;
    # "llm-live" tests the model/prompt against recorded tools and retrieval.
    replay_policy: Mapped[str] = mapped_column(String(32), server_default="llm-live")
    added_in_version: Mapped[int] = mapped_column(Integer)
    archived_in_version: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class EvalRun(Base):
    __tablename__ = "eval_runs"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    project_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"))
    dataset_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("eval_datasets.id", ondelete="CASCADE"), index=True
    )
    dataset_version: Mapped[int] = mapped_column(Integer)
    evaluator_version: Mapped[str] = mapped_column(String(100))
    label: Mapped[str] = mapped_column(String(200))
    replay_policy: Mapped[str] = mapped_column(String(32))
    prompt_version_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("prompt_versions.id", ondelete="SET NULL")
    )
    config: Mapped[dict] = mapped_column(JSONB)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class EvalResult(Base):
    __tablename__ = "eval_results"

    run_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("eval_runs.id", ondelete="CASCADE"), primary_key=True
    )
    case_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("eval_cases.id", ondelete="CASCADE"), primary_key=True
    )
    trace_id: Mapped[str | None] = mapped_column(String(32))
    passed: Mapped[bool] = mapped_column(Boolean)
    assertions: Mapped[list] = mapped_column(JSONB)
    output: Mapped[str | None] = mapped_column(Text)
    error: Mapped[str | None] = mapped_column(Text)
    latency_ms: Mapped[float] = mapped_column(Float)


class AuditEvent(Base):
    """Who accessed or changed captured payloads (replay bundles, eval cases)."""

    __tablename__ = "audit_events"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    project_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), index=True
    )
    api_key_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    action: Mapped[str] = mapped_column(String(64))
    target: Mapped[str | None] = mapped_column(Text)
    detail: Mapped[dict] = mapped_column(JSONB, server_default=text("'{}'::jsonb"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
