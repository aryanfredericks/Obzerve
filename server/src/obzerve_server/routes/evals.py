"""Prompt versions, evaluation datasets and cases, eval runs, comparison, and gating.

Runs are executed by the SDK (it owns the agent code); the server stores what
was run (dataset version, evaluator version, prompt version, replay policy,
configuration) so that two runs are only compared when that is meaningful.
"""

from __future__ import annotations

import hashlib
import json
import re
import statistics
import uuid
from datetime import datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from obzerve_server.auth import SCOPE_READ, SCOPE_WRITE, Principal, require_scope
from obzerve_server.db import get_session
from obzerve_server.models import (
    EvalCase,
    EvalDataset,
    EvalResult,
    EvalRun,
    PromptVersion,
    Trace,
)
from obzerve_server.replay_info import NON_REPLAYABLE, audit, build_bundle
from obzerve_server.routes.replay import load_trace

router = APIRouter(prefix="/api/v1", tags=["evaluations"])
reader = require_scope(SCOPE_READ)
writer = require_scope(SCOPE_WRITE)

Name = Annotated[str, Field(min_length=1, max_length=200, pattern=r"^[A-Za-z0-9._:\- ]+$")]


# ---------------------------------------------------------------- assertions


class NoError(BaseModel):
    type: Literal["no_error"]


class ContainsAll(BaseModel):
    type: Literal["contains_all"]
    values: list[Annotated[str, Field(min_length=1)]] = Field(min_length=1)


class ContainsAny(BaseModel):
    type: Literal["contains_any"]
    values: list[Annotated[str, Field(min_length=1)]] = Field(min_length=1)
    min_matches: int = Field(1, ge=1)


class NotContains(BaseModel):
    type: Literal["not_contains"]
    values: list[Annotated[str, Field(min_length=1)]] = Field(min_length=1)


class Regex(BaseModel):
    type: Literal["regex"]
    pattern: str = Field(min_length=1, max_length=1000)
    must_match: bool = True

    @field_validator("pattern")
    @classmethod
    def _compiles(cls, value: str) -> str:
        try:
            re.compile(value)
        except re.error as exc:
            raise ValueError(f"invalid regular expression: {exc}") from exc
        return value


class MaxLatency(BaseModel):
    type: Literal["max_latency_ms"]
    value: float = Field(gt=0)


class MaxOutputChars(BaseModel):
    type: Literal["max_output_chars"]
    value: int = Field(gt=0)


Assertion = Annotated[
    NoError | ContainsAll | ContainsAny | NotContains | Regex | MaxLatency | MaxOutputChars,
    Field(discriminator="type"),
]


# ---------------------------------------------------------------- prompts


class PromptVersionIn(BaseModel):
    config: dict[str, Any]


class PromptVersionOut(BaseModel):
    id: uuid.UUID
    name: str
    version: int
    config: dict[str, Any]
    content_hash: str
    created_at: datetime
    created: bool = False


def _prompt_out(row: PromptVersion, created: bool = False) -> PromptVersionOut:
    return PromptVersionOut(
        id=row.id,
        name=row.name,
        version=row.version,
        config=row.config,
        content_hash=row.content_hash,
        created_at=row.created_at,
        created=created,
    )


@router.post("/prompts/{name}/versions", response_model=PromptVersionOut)
async def register_prompt_version(
    name: Name,
    body: PromptVersionIn,
    principal: Principal = Depends(writer),
    session: AsyncSession = Depends(get_session),
):
    """Register a configuration; identical content returns the existing version."""
    digest = hashlib.sha256(
        json.dumps(body.config, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    for _ in range(3):  # concurrent registrations race for the next version number
        existing = await session.scalar(
            select(PromptVersion).where(
                PromptVersion.project_id == principal.project_id,
                PromptVersion.name == name,
                PromptVersion.content_hash == digest,
            )
        )
        if existing:
            return _prompt_out(existing)
        latest = await session.scalar(
            select(func.max(PromptVersion.version)).where(
                PromptVersion.project_id == principal.project_id, PromptVersion.name == name
            )
        )
        row = PromptVersion(
            project_id=principal.project_id,
            name=name,
            version=(latest or 0) + 1,
            config=body.config,
            content_hash=digest,
        )
        session.add(row)
        try:
            await session.commit()
        except IntegrityError:
            await session.rollback()
            continue
        await session.refresh(row)
        return _prompt_out(row, created=True)
    raise HTTPException(status.HTTP_409_CONFLICT, "Could not allocate a version; retry")


@router.get("/prompts/{name}/versions", response_model=list[PromptVersionOut])
async def list_prompt_versions(
    name: Name,
    principal: Principal = Depends(reader),
    session: AsyncSession = Depends(get_session),
):
    rows = await session.scalars(
        select(PromptVersion)
        .where(PromptVersion.project_id == principal.project_id, PromptVersion.name == name)
        .order_by(PromptVersion.version.desc())
    )
    return [_prompt_out(r) for r in rows]


# ---------------------------------------------------------------- datasets & cases


class DatasetIn(BaseModel):
    description: str | None = Field(None, max_length=2000)


class DatasetOut(BaseModel):
    id: uuid.UUID
    name: str
    description: str | None
    version: int
    active_cases: int
    runs: int
    updated_at: datetime


ReplayPolicyName = Literal["recorded", "llm-live", "live"]


class CaseIn(BaseModel):
    trace_id: str = Field(pattern=r"^[0-9a-fA-F]{32}$")
    name: Name
    replay_policy: ReplayPolicyName = "llm-live"
    assertions: list[Assertion] = Field(min_length=1, max_length=50)
    notes: str | None = Field(None, max_length=4000)


class CaseOut(BaseModel):
    id: uuid.UUID
    name: str
    notes: str | None
    source_trace_id: str | None
    assertions: list[dict[str, Any]]
    replay_policy: str
    replayability: str
    replay_reasons: list[str]
    recordings: int
    root_input: Any
    added_in_version: int
    archived_in_version: int | None
    created_at: datetime
    bundle: dict[str, Any] | None = None


def _case_out(case: EvalCase, include_bundle: bool = False) -> CaseOut:
    return CaseOut(
        id=case.id,
        name=case.name,
        notes=case.notes,
        source_trace_id=case.source_trace_id,
        assertions=case.assertions,
        replay_policy=case.replay_policy,
        replayability=case.bundle.get("replayability", NON_REPLAYABLE),
        replay_reasons=case.bundle.get("reasons", []),
        recordings=len(case.bundle.get("recordings", [])),
        root_input=case.bundle.get("root_input"),
        added_in_version=case.added_in_version,
        archived_in_version=case.archived_in_version,
        created_at=case.created_at,
        bundle=case.bundle if include_bundle else None,
    )


def _active_at(version: int):
    return (EvalCase.added_in_version <= version) & (
        EvalCase.archived_in_version.is_(None) | (EvalCase.archived_in_version > version)
    )


async def _dataset(
    session: AsyncSession, principal: Principal, name: str, *, lock: bool = False
) -> EvalDataset:
    stmt = select(EvalDataset).where(
        EvalDataset.project_id == principal.project_id, EvalDataset.name == name
    )
    if lock:
        stmt = stmt.with_for_update()
    dataset = await session.scalar(stmt)
    if dataset is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"Dataset {name!r} not found")
    return dataset


async def _dataset_out(session: AsyncSession, dataset: EvalDataset) -> DatasetOut:
    active = await session.scalar(
        select(func.count()).where(EvalCase.dataset_id == dataset.id, _active_at(dataset.version))
    )
    runs = await session.scalar(select(func.count()).where(EvalRun.dataset_id == dataset.id))
    return DatasetOut(
        id=dataset.id,
        name=dataset.name,
        description=dataset.description,
        version=dataset.version,
        active_cases=active or 0,
        runs=runs or 0,
        updated_at=dataset.updated_at,
    )


@router.put("/datasets/{name}", response_model=DatasetOut)
async def upsert_dataset(
    name: Name,
    body: DatasetIn,
    principal: Principal = Depends(writer),
    session: AsyncSession = Depends(get_session),
):
    dataset = await session.scalar(
        select(EvalDataset).where(
            EvalDataset.project_id == principal.project_id, EvalDataset.name == name
        )
    )
    if dataset is None:
        dataset = EvalDataset(project_id=principal.project_id, name=name, version=0)
        session.add(dataset)
    if body.description is not None:
        dataset.description = body.description
    try:
        await session.commit()
    except IntegrityError:  # created concurrently
        await session.rollback()
        dataset = await _dataset(session, principal, name)
    await session.refresh(dataset)
    return await _dataset_out(session, dataset)


@router.get("/datasets", response_model=list[DatasetOut])
async def list_datasets(
    principal: Principal = Depends(reader), session: AsyncSession = Depends(get_session)
):
    rows = await session.scalars(
        select(EvalDataset)
        .where(EvalDataset.project_id == principal.project_id)
        .order_by(EvalDataset.updated_at.desc())
    )
    return [await _dataset_out(session, d) for d in rows]


class DatasetDetail(BaseModel):
    dataset: DatasetOut
    version: int
    cases: list[CaseOut]


@router.get("/datasets/{name}", response_model=DatasetDetail)
async def get_dataset(
    name: Name,
    version: int | None = Query(None, ge=0, description="Cases active at this version"),
    include_bundles: bool = Query(False, description="Include replay bundles (audited)"),
    principal: Principal = Depends(reader),
    session: AsyncSession = Depends(get_session),
):
    dataset = await _dataset(session, principal, name)
    at = dataset.version if version is None else version
    if at > dataset.version:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"Dataset {name!r} has no version {at}")
    cases = await session.scalars(
        select(EvalCase)
        .where(EvalCase.dataset_id == dataset.id, _active_at(at))
        .order_by(EvalCase.created_at)
    )
    out = [_case_out(c, include_bundles) for c in cases]
    if include_bundles:
        await audit(
            session,
            principal.project_id,
            principal.key_id,
            "eval_cases.read",
            name,
            {"version": at, "cases": len(out)},
        )
        await session.commit()
    return DatasetDetail(dataset=await _dataset_out(session, dataset), version=at, cases=out)


@router.post("/datasets/{name}/cases", response_model=CaseOut, status_code=201)
async def create_case(
    name: Name,
    body: CaseIn,
    principal: Principal = Depends(writer),
    session: AsyncSession = Depends(get_session),
):
    """Turn a trace into a regression case. The trace's replay bundle is copied
    into the case, so the case keeps working after the trace expires."""
    trace, spans = await load_trace(session, principal, body.trace_id)
    bundle = build_bundle(trace, spans)
    if bundle["replayability"] == NON_REPLAYABLE:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            {"message": "This trace cannot be replayed", "reasons": bundle["reasons"]},
        )
    dataset = await session.scalar(
        select(EvalDataset)
        .where(EvalDataset.project_id == principal.project_id, EvalDataset.name == name)
        .with_for_update()
    )
    if dataset is None:
        dataset = EvalDataset(project_id=principal.project_id, name=name, version=0)
        session.add(dataset)
        await session.flush()
    dataset.version += 1
    dataset.updated_at = func.now()
    case = EvalCase(
        project_id=principal.project_id,
        dataset_id=dataset.id,
        name=body.name,
        notes=body.notes,
        source_trace_id=trace.trace_id,
        bundle=bundle,
        assertions=[a.model_dump() for a in body.assertions],
        replay_policy=body.replay_policy,
        added_in_version=dataset.version,
    )
    session.add(case)
    await audit(
        session,
        principal.project_id,
        principal.key_id,
        "eval_case.create",
        str(case.id),
        {"dataset": name, "trace_id": trace.trace_id},
    )
    await session.commit()
    await session.refresh(case)
    return _case_out(case)


@router.post("/datasets/{name}/cases/{case_id}/archive", response_model=DatasetOut)
async def archive_case(
    name: Name,
    case_id: uuid.UUID,
    principal: Principal = Depends(writer),
    session: AsyncSession = Depends(get_session),
):
    dataset = await _dataset(session, principal, name, lock=True)
    case = await session.scalar(
        select(EvalCase).where(EvalCase.id == case_id, EvalCase.dataset_id == dataset.id)
    )
    if case is None or case.archived_in_version is not None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Active case not found in this dataset")
    dataset.version += 1
    dataset.updated_at = func.now()
    case.archived_in_version = dataset.version
    await session.commit()
    await session.refresh(dataset)
    return await _dataset_out(session, dataset)


# ---------------------------------------------------------------- runs


class AssertionResult(BaseModel):
    type: str
    passed: bool
    detail: str | None = Field(None, max_length=2000)


class ResultIn(BaseModel):
    case_id: uuid.UUID
    trace_id: str | None = Field(None, pattern=r"^[0-9a-f]{32}$")
    passed: bool
    assertions: list[AssertionResult]
    output: str | None = Field(None, max_length=200_000)
    error: str | None = Field(None, max_length=10_000)
    latency_ms: float = Field(ge=0)


class RunIn(BaseModel):
    # Chosen by the runner, so the run's traces can carry it (obzerve.eval.run_id).
    id: uuid.UUID | None = None
    dataset: Name
    dataset_version: int = Field(ge=1)
    evaluator_version: str = Field(min_length=1, max_length=100)
    label: str = Field(min_length=1, max_length=200)
    replay_policy: str = Field(min_length=1, max_length=32)
    prompt_version_id: uuid.UUID | None = None
    config: dict[str, Any] = {}
    started_at: datetime
    finished_at: datetime
    results: list[ResultIn] = Field(min_length=1)


class RunSummary(BaseModel):
    id: uuid.UUID
    dataset: str
    dataset_version: int
    evaluator_version: str
    label: str
    replay_policy: str
    prompt: PromptVersionOut | None
    config: dict[str, Any]
    started_at: datetime
    finished_at: datetime
    cases: int
    passed: int
    pass_rate: float
    latency_p50_ms: float | None
    latency_p95_ms: float | None
    traces_found: int
    input_tokens_per_case: float | None
    output_tokens_per_case: float | None
    estimated_cost_per_case_usd: float | None
    unpriced_llm_calls: int


class ResultOut(BaseModel):
    case_id: uuid.UUID
    case_name: str
    trace_id: str | None
    passed: bool
    assertions: list[dict[str, Any]]
    output: str | None
    error: str | None
    latency_ms: float
    input_tokens: int | None
    output_tokens: int | None
    estimated_cost_usd: float | None


class RunDetail(BaseModel):
    run: RunSummary
    results: list[ResultOut]


def _pct(values: list[float], q: float) -> float | None:
    if not values:
        return None
    if len(values) == 1:
        return values[0]
    return statistics.quantiles(values, n=100, method="inclusive")[int(q * 100) - 1]


async def _run_detail(session: AsyncSession, principal: Principal, run: EvalRun) -> RunDetail:
    dataset = await session.get(EvalDataset, run.dataset_id)
    prompt = (
        await session.get(PromptVersion, run.prompt_version_id) if run.prompt_version_id else None
    )
    rows = (
        await session.execute(
            select(EvalResult, EvalCase.name, Trace)
            .join(EvalCase, EvalCase.id == EvalResult.case_id)
            .outerjoin(
                Trace,
                (Trace.project_id == principal.project_id)
                & (Trace.trace_id == EvalResult.trace_id),
            )
            .where(EvalResult.run_id == run.id)
            .order_by(EvalCase.created_at)
        )
    ).all()
    results = [
        ResultOut(
            case_id=r.case_id,
            case_name=name,
            trace_id=r.trace_id,
            passed=r.passed,
            assertions=r.assertions,
            output=r.output,
            error=r.error,
            latency_ms=r.latency_ms,
            input_tokens=t.input_tokens if t else None,
            output_tokens=t.output_tokens if t else None,
            estimated_cost_usd=float(t.estimated_cost_usd) if t else None,
        )
        for r, name, t in rows
    ]
    traced = [(r, t) for r, _, t in rows if t is not None]
    n = len(results)
    passed = sum(r.passed for r in results)
    latencies = sorted(r.latency_ms for r in results)
    summary = RunSummary(
        id=run.id,
        dataset=dataset.name,
        dataset_version=run.dataset_version,
        evaluator_version=run.evaluator_version,
        label=run.label,
        replay_policy=run.replay_policy,
        prompt=_prompt_out(prompt) if prompt else None,
        config=run.config,
        started_at=run.started_at,
        finished_at=run.finished_at,
        cases=n,
        passed=passed,
        pass_rate=passed / n if n else 0.0,
        latency_p50_ms=_pct(latencies, 0.5),
        latency_p95_ms=_pct(latencies, 0.95),
        traces_found=len(traced),
        input_tokens_per_case=(
            sum(t.input_tokens for _, t in traced) / len(traced) if traced else None
        ),
        output_tokens_per_case=(
            sum(t.output_tokens for _, t in traced) / len(traced) if traced else None
        ),
        estimated_cost_per_case_usd=(
            float(sum(t.estimated_cost_usd for _, t in traced)) / len(traced) if traced else None
        ),
        unpriced_llm_calls=sum(t.unpriced_llm_calls for _, t in traced),
    )
    return RunDetail(run=summary, results=results)


@router.post("/eval-runs", response_model=RunDetail, status_code=201)
async def submit_run(
    body: RunIn,
    principal: Principal = Depends(writer),
    session: AsyncSession = Depends(get_session),
):
    dataset = await _dataset(session, principal, body.dataset)
    if body.dataset_version > dataset.version:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            f"Dataset {body.dataset!r} has no version {body.dataset_version}",
        )
    expected = set(
        await session.scalars(
            select(EvalCase.id).where(
                EvalCase.dataset_id == dataset.id, _active_at(body.dataset_version)
            )
        )
    )
    submitted = [r.case_id for r in body.results]
    if len(set(submitted)) != len(submitted) or set(submitted) != expected:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            "Results must cover exactly the cases of the dataset at that version "
            f"({len(expected)} cases); got {len(set(submitted))}",
        )
    if body.prompt_version_id is not None:
        prompt = await session.get(PromptVersion, body.prompt_version_id)
        if prompt is None or prompt.project_id != principal.project_id:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "Unknown prompt version")
    if body.id is not None and await session.get(EvalRun, body.id) is not None:
        raise HTTPException(status.HTTP_409_CONFLICT, f"Eval run {body.id} already exists")
    run = EvalRun(
        id=body.id or uuid.uuid4(),
        project_id=principal.project_id,
        dataset_id=dataset.id,
        dataset_version=body.dataset_version,
        evaluator_version=body.evaluator_version,
        label=body.label,
        replay_policy=body.replay_policy,
        prompt_version_id=body.prompt_version_id,
        config=body.config,
        started_at=body.started_at,
        finished_at=body.finished_at,
    )
    session.add(run)
    await session.flush()
    session.add_all(
        EvalResult(
            run_id=run.id,
            case_id=r.case_id,
            trace_id=r.trace_id,
            passed=r.passed,
            assertions=[a.model_dump() for a in r.assertions],
            output=r.output,
            error=r.error,
            latency_ms=r.latency_ms,
        )
        for r in body.results
    )
    await session.commit()
    return await _run_detail(session, principal, run)


async def _run(session: AsyncSession, principal: Principal, run_id: uuid.UUID) -> EvalRun:
    run = await session.get(EvalRun, run_id)
    if run is None or run.project_id != principal.project_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"Eval run {run_id} not found")
    return run


@router.get("/eval-runs", response_model=list[RunSummary])
async def list_runs(
    dataset: Name | None = None,
    limit: int = Query(50, ge=1, le=200),
    principal: Principal = Depends(reader),
    session: AsyncSession = Depends(get_session),
):
    stmt = select(EvalRun).where(EvalRun.project_id == principal.project_id)
    if dataset:
        stmt = stmt.where(EvalRun.dataset_id == (await _dataset(session, principal, dataset)).id)
    runs = await session.scalars(stmt.order_by(EvalRun.started_at.desc()).limit(limit))
    return [(await _run_detail(session, principal, r)).run for r in runs]


@router.get("/eval-runs/{run_id}", response_model=RunDetail)
async def get_run(
    run_id: uuid.UUID,
    principal: Principal = Depends(reader),
    session: AsyncSession = Depends(get_session),
):
    return await _run_detail(session, principal, await _run(session, principal, run_id))


class CaseDiff(BaseModel):
    case_id: uuid.UUID
    case_name: str
    baseline_passed: bool | None
    candidate_passed: bool | None
    change: Literal["regression", "fix", "still_passing", "still_failing", "added", "removed"]
    baseline_trace_id: str | None
    candidate_trace_id: str | None
    failed_assertions: list[str]


class RunComparison(BaseModel):
    baseline: RunSummary
    candidate: RunSummary
    comparable: bool
    reasons: list[str]
    regressions: int
    fixes: int
    pass_rate_delta: float
    cost_per_case_delta_pct: float | None
    latency_p95_delta_pct: float | None
    cases: list[CaseDiff]


def _delta_pct(base: float | None, cand: float | None) -> float | None:
    if base is None or cand is None:
        return None
    if base == 0:
        return 0.0 if cand == 0 else None
    return (cand - base) / base * 100


async def _compare(
    session: AsyncSession, principal: Principal, baseline_id: uuid.UUID, candidate_id: uuid.UUID
) -> RunComparison:
    base = await _run_detail(session, principal, await _run(session, principal, baseline_id))
    cand = await _run_detail(session, principal, await _run(session, principal, candidate_id))
    b, c = base.run, cand.run
    reasons = []
    if b.dataset != c.dataset:
        reasons.append(f"different datasets ({b.dataset} vs {c.dataset})")
    elif b.dataset_version != c.dataset_version:
        reasons.append(f"different dataset versions (v{b.dataset_version} vs v{c.dataset_version})")
    if b.evaluator_version != c.evaluator_version:
        reasons.append(
            f"different evaluator versions ({b.evaluator_version} vs {c.evaluator_version})"
        )
    if b.replay_policy != c.replay_policy:
        reasons.append(f"different replay policies ({b.replay_policy} vs {c.replay_policy})")

    by_case_b = {r.case_id: r for r in base.results}
    by_case_c = {r.case_id: r for r in cand.results}
    diffs = []
    for case_id in list(by_case_b) + [k for k in by_case_c if k not in by_case_b]:
        rb, rc = by_case_b.get(case_id), by_case_c.get(case_id)
        if rb and rc:
            change = (
                ("still_passing" if rc.passed else "regression")
                if rb.passed
                else ("fix" if rc.passed else "still_failing")
            )
        else:
            change = "removed" if rb else "added"
        ref = rc or rb
        diffs.append(
            CaseDiff(
                case_id=case_id,
                case_name=ref.case_name,
                baseline_passed=rb.passed if rb else None,
                candidate_passed=rc.passed if rc else None,
                change=change,
                baseline_trace_id=rb.trace_id if rb else None,
                candidate_trace_id=rc.trace_id if rc else None,
                failed_assertions=[
                    a.get("detail") or a["type"]
                    for a in (rc.assertions if rc else [])
                    if not a["passed"]
                ],
            )
        )
    return RunComparison(
        baseline=b,
        candidate=c,
        comparable=not reasons,
        reasons=reasons,
        regressions=sum(d.change == "regression" for d in diffs),
        fixes=sum(d.change == "fix" for d in diffs),
        pass_rate_delta=c.pass_rate - b.pass_rate,
        cost_per_case_delta_pct=_delta_pct(
            b.estimated_cost_per_case_usd, c.estimated_cost_per_case_usd
        ),
        latency_p95_delta_pct=_delta_pct(b.latency_p95_ms, c.latency_p95_ms),
        cases=diffs,
    )


@router.get("/eval-runs/compare/{baseline_id}/{candidate_id}", response_model=RunComparison)
async def compare_runs(
    baseline_id: uuid.UUID,
    candidate_id: uuid.UUID,
    principal: Principal = Depends(reader),
    session: AsyncSession = Depends(get_session),
):
    return await _compare(session, principal, baseline_id, candidate_id)


class GateCheck(BaseModel):
    name: str
    passed: bool
    detail: str


class GateResult(BaseModel):
    passed: bool
    checks: list[GateCheck]
    comparison: RunComparison


@router.get("/eval-runs/gate/{baseline_id}/{candidate_id}", response_model=GateResult)
async def gate(
    baseline_id: uuid.UUID,
    candidate_id: uuid.UUID,
    max_regressions: int = Query(0, ge=0),
    min_pass_rate: float | None = Query(None, ge=0, le=1),
    max_pass_rate_drop: float | None = Query(None, ge=0, le=1),
    max_cost_increase_pct: float | None = Query(None, ge=0),
    max_p95_latency_increase_pct: float | None = Query(None, ge=0),
    principal: Principal = Depends(reader),
    session: AsyncSession = Depends(get_session),
):
    """Release gate: does the candidate meet the criteria relative to the baseline?
    Incomparable runs always fail the gate."""
    cmp = await _compare(session, principal, baseline_id, candidate_id)
    checks = [
        GateCheck(
            name="comparable",
            passed=cmp.comparable,
            detail="same dataset version, evaluator and replay policy"
            if cmp.comparable
            else "; ".join(cmp.reasons),
        ),
        GateCheck(
            name="regressions",
            passed=cmp.regressions <= max_regressions,
            detail=f"{cmp.regressions} case(s) passed in the baseline and fail now "
            f"(allowed: {max_regressions})",
        ),
    ]
    rate = cmp.candidate.pass_rate
    if min_pass_rate is not None:
        checks.append(
            GateCheck(
                name="min_pass_rate",
                passed=rate >= min_pass_rate,
                detail=f"pass rate {rate:.0%} (minimum {min_pass_rate:.0%})",
            )
        )
    if max_pass_rate_drop is not None:
        checks.append(
            GateCheck(
                name="max_pass_rate_drop",
                passed=-cmp.pass_rate_delta <= max_pass_rate_drop + 1e-9,
                detail=f"pass rate changed by {cmp.pass_rate_delta:+.0%} "
                f"(largest allowed drop {max_pass_rate_drop:.0%})",
            )
        )
    for name, limit, delta, what in (
        ("max_cost_increase_pct", max_cost_increase_pct, cmp.cost_per_case_delta_pct, "cost/case"),
        (
            "max_p95_latency_increase_pct",
            max_p95_latency_increase_pct,
            cmp.latency_p95_delta_pct,
            "p95 latency",
        ),
    ):
        if limit is None:
            continue
        checks.append(
            GateCheck(
                name=name,
                passed=delta is not None and delta <= limit,
                detail=(
                    f"{what} changed by {delta:+.1f}% (allowed +{limit:g}%)"
                    if delta is not None
                    else f"{what} not available for both runs"
                ),
            )
        )
    return GateResult(passed=all(c.passed for c in checks), checks=checks, comparison=cmp)
