"""Replaying a recorded trace.

Operations wrapped with ``Observer.replayable`` record their exact output (or
the exception they raised) under a key ``"<name>#<n>"`` — the n-th call of that
operation in the trace. A replay runs the agent again inside
``Observer.replay(bundle, policy)``: each replayable call either returns its
recording or runs live, as the policy decides.

Policies:

* ``RECORDED`` — every replayable operation returns its recording; nothing
  external is called. Re-executes the workflow logic deterministically,
  including recorded failures.
* ``LLM_LIVE`` — LLM calls run live, everything else (tools, retrieval) is
  recorded: test a prompt or model change against fixed dependencies.
* ``LIVE`` — everything runs live, except that side-effecting operations stay
  blocked unless ``allow_side_effects=True``.

Calls the recording does not contain (the replay took a different path) fail
with ``ReplayMissError`` unless the policy allows running them live.
"""

from __future__ import annotations

import hashlib
import json
import sys
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any

ALL_KINDS = frozenset({"agent", "llm", "tool", "retrieval", "chain", "handoff", "function"})


@dataclass(frozen=True)
class ReplayPolicy:
    name: str
    live_kinds: frozenset[str] = frozenset()
    allow_side_effects: bool = False
    # Run calls with no recording live instead of failing (never for side effects).
    live_on_missing: bool = False

    def is_live(self, kind: str) -> bool:
        return kind in self.live_kinds


RECORDED = ReplayPolicy("recorded")
LLM_LIVE = ReplayPolicy("llm-live", frozenset({"llm"}), live_on_missing=True)
LIVE = ReplayPolicy("live", ALL_KINDS, live_on_missing=True)
POLICIES = {p.name: p for p in (RECORDED, LLM_LIVE, LIVE)}


class ReplayError(Exception):
    pass


class ReplayMissError(ReplayError):
    """The replay made a call that the recording does not contain."""


class SideEffectBlocked(ReplayError):
    """A side-effecting operation was about to run live during a replay."""


class ReplayedError(Exception):
    """Stands in for a recorded exception whose class is not importable here."""

    def __init__(self, error_type: str, message: str):
        super().__init__(f"{error_type}: {message}" if message else error_type)
        self.error_type = error_type


@dataclass(frozen=True)
class Recording:
    key: str
    name: str
    kind: str
    input_hash: str | None
    output: str | None  # exact JSON
    complete: bool
    side_effect: bool = False
    error_type: str | None = None
    error_class: str | None = None
    error_message: str | None = None
    incomplete_reason: str | None = None


@dataclass(frozen=True)
class ReplayBundle:
    """Everything needed to replay one trace, as served by ``GET /api/v1/traces/{id}/replay``."""

    trace_id: str
    replayability: str  # replayable | partial | non_replayable
    reasons: tuple[str, ...]
    root_name: str | None
    root_input: Any
    recordings: dict[str, Recording]
    attributes: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> ReplayBundle:
        recordings = {
            r["key"]: Recording(
                key=r["key"],
                name=r["name"],
                kind=r["kind"],
                input_hash=r.get("input_hash"),
                output=r.get("output"),
                complete=r.get("complete", False),
                side_effect=r.get("side_effect", False),
                error_type=r.get("error_type"),
                error_class=r.get("error_class"),
                error_message=r.get("error_message"),
                incomplete_reason=r.get("incomplete_reason"),
            )
            for r in data.get("recordings", [])
        }
        return cls(
            trace_id=data["trace_id"],
            replayability=data["replayability"],
            reasons=tuple(data.get("reasons", [])),
            root_name=data.get("root_name"),
            root_input=data.get("root_input"),
            recordings=recordings,
            attributes=data.get("attributes", {}),
        )


class ReplaySession:
    def __init__(
        self,
        bundle: ReplayBundle,
        policy: ReplayPolicy,
        label: str | None = None,
        attributes: dict[str, Any] | None = None,
    ):
        self.bundle = bundle
        self.policy = policy
        self.label = label
        self.attributes = attributes or {}
        self.recorded_calls = 0
        self.live_calls = 0
        self.divergences: list[str] = []
        self.root_trace_id: str | None = None

    def recording_for(self, key: str) -> Recording | None:
        return self.bundle.recordings.get(key)


active_session: ContextVar[ReplaySession | None] = ContextVar("obzerve_replay", default=None)


def input_hash(value: Any) -> str:
    """Stable hash of a call's (encoded) input, to detect a replay diverging."""
    text = json.dumps(value, sort_keys=True, default=repr, ensure_ascii=False)
    return hashlib.sha256(text.encode()).hexdigest()[:16]


def class_path(exc: BaseException) -> str:
    cls = type(exc)
    return f"{cls.__module__}:{cls.__qualname__}"


def rebuild_exception(recording: Recording) -> BaseException:
    """Recreate a recorded exception so the agent takes the same code path.

    Only classes from modules that are *already imported* are used — data from
    the server never causes an import.
    """
    message = recording.error_message or ""
    if recording.error_class and ":" in recording.error_class:
        module_name, qualname = recording.error_class.split(":", 1)
        obj: Any = sys.modules.get(module_name)
        for part in qualname.split("."):
            obj = getattr(obj, part, None) if obj is not None else None
        if isinstance(obj, type) and issubclass(obj, Exception):
            try:
                return obj(message)
            except Exception:  # constructor needs other arguments
                pass
    return ReplayedError(recording.error_type or "Error", message)


def bundle_from_spans(spans: Any, trace_id: str | None = None) -> ReplayBundle:
    """Build a bundle from finished OpenTelemetry spans (e.g. an
    ``InMemorySpanExporter``), mirroring the server's replay endpoint — useful for
    testing replays without a server. Uses the latest trace if ``trace_id`` is None."""
    from opentelemetry.trace import StatusCode

    spans = list(spans)
    if trace_id is None:
        trace_id = format(spans[-1].context.trace_id, "032x")
    mine = [s for s in spans if format(s.context.trace_id, "032x") == trace_id]
    root = next(s for s in mine if s.parent is None)
    recordings = []
    for s in sorted(mine, key=lambda s: s.start_time or 0):
        a = s.attributes or {}
        if "obzerve.replay.key" not in a:
            continue
        failed = s.status.status_code is StatusCode.ERROR
        recordings.append(
            {
                "key": a["obzerve.replay.key"],
                "name": s.name,
                "kind": a.get("obzerve.span.kind", "function"),
                "input_hash": a.get("obzerve.replay.input_hash"),
                "output": None if failed else a.get("obzerve.output"),
                "complete": a.get("obzerve.replay.complete", False),
                "side_effect": a.get("obzerve.replay.side_effect", False),
                "error_type": a.get("error.type") if failed else None,
                "error_class": a.get("obzerve.replay.error_class") if failed else None,
                "error_message": s.status.description if failed else None,
            }
        )
    raw_input = (root.attributes or {}).get("obzerve.input")
    return ReplayBundle.from_json(
        {
            "trace_id": trace_id,
            "replayability": "replayable",
            "root_name": root.name,
            "root_input": json.loads(raw_input) if raw_input else None,
            "recordings": recordings,
        }
    )
