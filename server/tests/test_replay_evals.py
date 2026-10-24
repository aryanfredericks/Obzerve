"""End to end: SDK records over OTLP -> server -> replay bundle -> replay ->
regression case -> eval runs -> comparison and release gate."""

import time

import pytest
from sqlalchemy import select

from obzerve import Observer, SpanKind
from obzerve.client import ObzerveAPIError, ObzerveClient
from obzerve.evals import run_eval
from obzerve.replay import LLM_LIVE, RECORDED
from obzerve_server.models import AuditEvent


class SearchTimeout(Exception):
    pass


class Agent:
    """search (tool) -> answer (llm). ``search_fails`` makes the tool time out."""

    def __init__(
        self,
        obs: Observer,
        *,
        search_fails: int = 0,
        fallback: bool = False,
        style: str = "detailed",
    ):
        self.obs = obs
        self.search_fails = search_fails
        self.fallback = fallback
        self.style = style

        @obs.replayable(kind=SpanKind.TOOL)
        def search(query: str) -> list[str]:
            if self.search_fails:
                self.search_fails -= 1
                raise SearchTimeout("search API timed out")
            return [f"{query} is measured with faithfulness", f"{query} uses context recall"]

        @obs.replayable(kind=SpanKind.LLM)
        def answer(docs: list[str]) -> str:
            if self.style == "terse":
                return "It depends."
            return "Answer: " + "; ".join(docs)

        self.search, self.answer = search, answer

    def run(self, question: str) -> str:
        with self.obs.trace("qa_agent", input={"question": question}) as trace:
            try:
                docs = self.search(question)
            except SearchTimeout:
                if not self.fallback:
                    raise
                docs = []  # the fix: degrade gracefully instead of failing the request
            text = self.answer(docs) if docs else "Search is unavailable; please retry later."
            trace.set_output(text)
            return text


def wait_for_trace(client: ObzerveClient, trace_id: str, timeout: float = 10):
    deadline = time.time() + timeout
    while True:
        try:
            return client.replay_bundle(trace_id)
        except ObzerveAPIError as exc:
            if exc.status != 404 or time.time() > deadline:
                raise
            time.sleep(0.1)


@pytest.fixture
async def env(live_server, make_project):
    _, key = await make_project()
    obs = Observer(endpoint=live_server, api_key=key, service_name="qa", schedule_delay_ms=50)
    yield obs, ObzerveClient(live_server, key), live_server, key
    obs.shutdown()


def record(obs: Observer, agent: Agent, question: str) -> str:
    try:
        agent.run(question)
    except SearchTimeout:
        pass
    obs.flush()
    return obs.last_trace_id


async def test_replay_reproduces_a_failure_and_links_the_replay(env):
    obs, client, _, _ = env
    trace_id = record(obs, Agent(obs, search_fails=1), "RAG evaluation")
    bundle = wait_for_trace(client, trace_id)
    assert bundle.replayability == "replayable"
    assert bundle.root_input == {"question": "RAG evaluation"}
    assert list(bundle.recordings) == ["search#1"]
    assert bundle.recordings["search#1"].error_type == "SearchTimeout"

    # Recorded replay: the same failure, deterministically, without calling search.
    replayer = Agent(obs)  # this agent's search would succeed if it were called
    with pytest.raises(SearchTimeout, match="timed out"), obs.replay(bundle, RECORDED) as session:
        replayer.run(**bundle.root_input)
    assert session.recorded_calls == 1 and session.live_calls == 0
    obs.flush()

    replay_id = session.root_trace_id
    wait_for_trace(client, replay_id)
    replays = client._request("GET", f"/api/v1/traces/{trace_id}/replays")
    assert [r["trace_id"] for r in replays] == [replay_id]
    assert replays[0]["replay_of"] == trace_id and replays[0]["replay_mode"] == "recorded"
    assert replays[0]["status"] == "error"

    cmp = client.compare_traces(trace_id, replay_id)
    assert [s["key"] for s in cmp["steps"]] == ["search#1"]
    step = cmp["steps"][0]
    assert step["b"]["source"] == "recorded" and step["b"]["input_matched"] is True
    assert not step["output_changed"] and not step["input_changed"]


async def test_non_replayable_traces_are_explained_and_refused(env):
    obs, client, _, _ = env
    with obs.trace("plain_agent", input={"q": 1}):
        with obs.span("untracked_tool", kind=SpanKind.TOOL):
            pass
    obs.flush()
    bundle = wait_for_trace(client, obs.last_trace_id)
    assert bundle.replayability == "non_replayable"
    assert "Observer.replayable" in bundle.reasons[0]
    with pytest.raises(ObzerveAPIError) as err:
        client.create_case("regressions", obs.last_trace_id, "plain", [{"type": "no_error"}])
    assert err.value.status == 422


async def test_failed_trace_becomes_a_repeatable_regression_case(env, app):
    obs, client, _, _ = env
    failed = record(obs, Agent(obs, search_fails=1), "RAG evaluation")
    healthy = record(obs, Agent(obs), "context precision")
    wait_for_trace(client, failed)
    wait_for_trace(client, healthy)

    case = client.create_case(
        "regressions",
        failed,
        "search timeout is survivable",
        [{"type": "no_error"}, {"type": "contains_any", "values": ["unavailable", "answer"]}],
    )
    assert case["replayability"] == "replayable" and case["recordings"] == 1
    client.create_case(
        "regressions",
        healthy,
        "answers with evidence",
        [{"type": "no_error"}, {"type": "contains_all", "values": ["faithfulness"]}],
    )
    dataset = client.dataset("regressions")
    assert dataset["version"] == 2 and len(dataset["cases"]) == 2

    v1 = client.register_prompt("qa", {"style": "detailed", "model": "m"})
    assert client.register_prompt("qa", {"model": "m", "style": "detailed"})["id"] == v1["id"]
    v2 = client.register_prompt("qa", {"style": "terse", "model": "m"})
    assert (v1["version"], v2["version"]) == (1, 2)

    def target(agent):
        return lambda root_input: agent.run(**root_input)

    # Baseline: the agent without the fix. Recorded replay reproduces the failure,
    # run after run.
    baselines = [
        run_eval(
            obs,
            client,
            "regressions",
            target(Agent(obs)),
            label="baseline",
            policy=RECORDED,
            prompt_version=v1,
        )
        for _ in range(2)
    ]
    for b in baselines:
        assert (b["run"]["passed"], b["run"]["cases"]) == (1, 2)
    failing = next(r for r in baselines[0]["results"] if not r["passed"])
    assert failing["case_name"] == "search timeout is survivable"
    assert failing["error"].startswith("SearchTimeout")

    # Candidate: the fix (graceful fallback) passes both cases on the same recordings.
    fixed = run_eval(
        obs,
        client,
        "regressions",
        target(Agent(obs, fallback=True)),
        label="fallback fix",
        policy=RECORDED,
        prompt_version=v1,
    )
    assert fixed["run"]["pass_rate"] == 1.0

    base_id, fix_id = baselines[0]["run"]["id"], fixed["run"]["id"]
    cmp = client.compare_runs(base_id, fix_id)
    assert cmp["comparable"] and (cmp["fixes"], cmp["regressions"]) == (1, 0)
    assert client.gate(base_id, fix_id, min_pass_rate=1.0)["passed"] is True

    # A cheaper-looking prompt change that regresses quality is blocked.
    terse = run_eval(
        obs,
        client,
        "regressions",
        target(Agent(obs, fallback=True, style="terse")),
        label="terse prompt",
        policy=LLM_LIVE,
        prompt_version=v2,
    )
    assert terse["run"]["passed"] == 1  # the evidence case now fails
    gate = client.gate(fix_id, terse["run"]["id"])
    assert gate["passed"] is False
    checks = {c["name"]: c["passed"] for c in gate["checks"]}
    assert checks["comparable"] is False  # different replay policy is called out
    assert checks["regressions"] is False
    assert gate["comparison"]["regressions"] == 1

    # Eval traces are linked to the run, the case and the prompt version.
    obs.flush()
    trace_id = terse["results"][0]["trace_id"]
    trace = client._request("GET", f"/api/v1/traces/{trace_id}")["trace"]
    assert trace["eval_run_id"] == terse["run"]["id"]
    assert (trace["prompt_name"], trace["prompt_version"]) == ("qa", "2")
    assert trace["replay_label"] == "terse prompt"

    async with app.state.sessionmaker() as session:
        actions = set(await session.scalars(select(AuditEvent.action)))
    assert {"replay_bundle.read", "eval_case.create", "eval_cases.read"} <= actions


async def test_run_submission_must_match_the_dataset_version(env):
    obs, client, _, _ = env
    trace_id = record(obs, Agent(obs), "q")
    wait_for_trace(client, trace_id)
    case = client.create_case("ds", trace_id, "c1", [{"type": "no_error"}])
    base = {
        "dataset": "ds",
        "dataset_version": 1,
        "evaluator_version": "x",
        "label": "l",
        "replay_policy": "recorded",
        "started_at": "2026-01-01T00:00:00Z",
        "finished_at": "2026-01-01T00:00:01Z",
    }
    result = {"case_id": case["id"], "passed": True, "assertions": [], "latency_ms": 1}
    for bad in (
        {**base, "results": [result, result]},
        {**base, "dataset_version": 9, "results": [result]},
    ):
        with pytest.raises(ObzerveAPIError) as err:
            client.submit_run(bad)
        assert err.value.status == 422
    with pytest.raises(ObzerveAPIError) as err:
        client.create_case("ds", trace_id, "bad", [{"type": "regex", "pattern": "("}])
    assert err.value.status == 422


async def test_tenancy_and_scopes(env, make_project, live_server):
    obs, client, _, _ = env
    trace_id = record(obs, Agent(obs), "q")
    wait_for_trace(client, trace_id)
    client.create_case("ds", trace_id, "c1", [{"type": "no_error"}])

    _, other_key = await make_project()
    other = ObzerveClient(live_server, other_key)
    for call in (lambda: other.replay_bundle(trace_id), lambda: other.dataset("ds")):
        with pytest.raises(ObzerveAPIError) as err:
            call()
        assert err.value.status == 404

    _, read_only = await make_project(scopes=["read", "ingest"])
    with pytest.raises(ObzerveAPIError) as err:
        ObzerveClient(live_server, read_only).register_prompt("p", {"a": 1})
    assert err.value.status == 403
