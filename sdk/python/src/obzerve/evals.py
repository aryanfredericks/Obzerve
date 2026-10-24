"""Evaluation runs: replay every case of a dataset against a candidate and
check its assertions.

Each case carries the recorded input and dependencies of the trace it was
made from, and its own replay policy: ``recorded`` to re-run a recorded failure
exactly, or ``llm-live`` to test the model/prompt against recorded tools and
retrieval. ``run_eval`` replays each case, applies its deterministic
assertions, and submits the results with everything needed to judge whether
two runs are comparable: dataset version, evaluator version, prompt version,
replay policy, and configuration.
"""

from __future__ import annotations

import json
import re
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from obzerve import attributes as attrs
from obzerve.client import ObzerveClient
from obzerve.observer import Observer
from obzerve.replay import POLICIES, ReplayBundle, ReplayPolicy

# Bump whenever an assertion's semantics change: runs with different evaluator
# versions are not compared.
EVALUATOR_VERSION = "obzerve-assertions/1"


@dataclass
class Outcome:
    output: str | None
    error: str | None
    latency_ms: float


def output_text(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, ensure_ascii=False, default=repr)
    except (TypeError, ValueError):
        return repr(value)


def evaluate(assertion: dict[str, Any], outcome: Outcome) -> tuple[bool, str]:
    """Apply one assertion; returns ``(passed, explanation)``."""
    kind = assertion["type"]
    text = (outcome.output or "").lower()
    if kind == "no_error":
        return outcome.error is None, "completed without error" if outcome.error is None else (
            f"raised {outcome.error}"
        )
    if kind == "max_latency_ms":
        limit = assertion["value"]
        return outcome.latency_ms <= limit, f"took {outcome.latency_ms:.0f} ms (limit {limit:g})"
    if outcome.error is not None:
        return False, f"no output: raised {outcome.error}"
    if kind == "contains_all":
        missing = [v for v in assertion["values"] if v.lower() not in text]
        return not missing, "contains all terms" if not missing else f"missing {missing}"
    if kind == "contains_any":
        found = [v for v in assertion["values"] if v.lower() in text]
        need = assertion.get("min_matches", 1)
        return len(found) >= need, f"found {len(found)} of required {need}: {found}"
    if kind == "not_contains":
        present = [v for v in assertion["values"] if v.lower() in text]
        return not present, "none present" if not present else f"contains {present}"
    if kind == "regex":
        matched = re.search(assertion["pattern"], outcome.output or "", re.IGNORECASE) is not None
        want = assertion.get("must_match", True)
        return matched == want, f"pattern {'matched' if matched else 'did not match'}"
    if kind == "max_output_chars":
        n, limit = len(outcome.output or ""), assertion["value"]
        return n <= limit, f"{n} chars (limit {limit})"
    return False, f"unknown assertion type {kind!r} for {EVALUATOR_VERSION}"


def run_eval(
    observer: Observer,
    client: ObzerveClient,
    dataset: str,
    target: Callable[[Any], Any],
    *,
    label: str,
    policy: ReplayPolicy | None = None,
    prompt_version: dict[str, Any] | None = None,
    config: dict[str, Any] | None = None,
    on_case: Callable[[dict[str, Any], dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    """Run every active case of ``dataset`` through ``target(root_input)`` and
    submit the results. ``policy`` overrides every case's own replay policy
    (default: per case). ``prompt_version`` is the dict returned by
    ``client.register_prompt``. Returns the stored run (with its summary)."""
    data = client.dataset(dataset, include_bundles=True)
    if not data["cases"]:
        raise ValueError(f"dataset {dataset!r} has no active cases")
    started = datetime.now(UTC)
    run_id = str(uuid.uuid4())
    replay_attrs: dict[str, Any] = {attrs.EVAL_RUN_ID: run_id}
    if prompt_version:
        replay_attrs[attrs.PROMPT_NAME] = prompt_version["name"]
        replay_attrs[attrs.PROMPT_VERSION] = str(prompt_version["version"])

    results = []
    for case in data["cases"]:
        bundle = ReplayBundle.from_json(case["bundle"])
        case_attrs = {**replay_attrs, attrs.EVAL_CASE_ID: case["id"]}
        t0 = time.perf_counter()
        output = error = None
        case_policy = policy or POLICIES[case.get("replay_policy", "llm-live")]
        with observer.replay(bundle, case_policy, label=label, attributes=case_attrs) as session:
            try:
                output = output_text(target(bundle.root_input))
            except Exception as exc:  # a failing case is a result, not a crash
                error = f"{type(exc).__name__}: {exc}"
        outcome = Outcome(output, error, (time.perf_counter() - t0) * 1000)
        verdicts = []
        for assertion in case["assertions"]:
            passed, detail = evaluate(assertion, outcome)
            verdicts.append({"type": assertion["type"], "passed": passed, "detail": detail})
        result = {
            "case_id": case["id"],
            "trace_id": session.root_trace_id,
            "passed": all(v["passed"] for v in verdicts),
            "assertions": verdicts,
            "output": (output or "")[:200_000] or None,
            "error": error,
            "latency_ms": outcome.latency_ms,
        }
        results.append(result)
        if on_case:
            on_case(case, result)

    observer.flush()
    return client.submit_run(
        {
            "id": run_id,
            "dataset": dataset,
            "dataset_version": data["version"],
            "evaluator_version": EVALUATOR_VERSION,
            "label": label,
            "replay_policy": policy.name if policy else "per-case",
            "prompt_version_id": prompt_version["id"] if prompt_version else None,
            "config": config or {},
            "started_at": started.isoformat(),
            "finished_at": datetime.now(UTC).isoformat(),
            "results": results,
        }
    )
