import asyncio
import json
from dataclasses import asdict, dataclass

import pytest
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import StatusCode

from obzerve import Observer, SpanKind
from obzerve.replay import (
    LIVE,
    LLM_LIVE,
    RECORDED,
    ReplayBundle,
    ReplayedError,
    ReplayMissError,
    SideEffectBlocked,
    bundle_from_spans,
)


class ToolTimeout(Exception):
    pass


@dataclass
class Doc:
    id: str
    score: float


def bundle_from(exporter: InMemorySpanExporter, trace_id: str) -> ReplayBundle:
    return bundle_from_spans(exporter.get_finished_spans(), trace_id)


class Agent:
    """A small agent: search (tool) -> rank (retrieval) -> answer (llm) -> notify (side effect)."""

    def __init__(self, obs: Observer):
        self.obs = obs
        self.calls: list[str] = []
        self.search_fails = 0
        self.answer_style = "short"

        @obs.replayable(kind=SpanKind.TOOL)
        def search(query: str) -> list[str]:
            self.calls.append("search")
            if self.search_fails:
                self.search_fails -= 1
                raise ToolTimeout("search timed out")
            return [f"{query}-a", f"{query}-b"]

        @obs.replayable(
            kind=SpanKind.RETRIEVAL,
            encode=lambda docs: [asdict(d) for d in docs],
            decode=lambda data: [Doc(**d) for d in data],
        )
        def rank(hits: list[str]) -> list[Doc]:
            self.calls.append("rank")
            return [Doc(h, 1.0 / (i + 1)) for i, h in enumerate(hits)]

        @obs.replayable(kind=SpanKind.LLM)
        def answer(docs: list[str]) -> str:
            self.calls.append("answer")
            return f"{self.answer_style}: {', '.join(docs)}"

        @obs.replayable(kind=SpanKind.TOOL, side_effect=True)
        def notify(text: str) -> bool:
            self.calls.append("notify")
            return True

        self.search, self.rank, self.answer, self.notify = search, rank, answer, notify

    def run(self, query: str) -> str:
        with self.obs.trace("agent", input={"query": query}) as trace:
            for attempt in range(3):
                try:
                    hits = self.search(query)
                    break
                except ToolTimeout:
                    if attempt == 2:
                        raise
            docs = self.rank(hits)
            text = self.answer([d.id for d in docs])
            self.notify(text)
            trace.set_output(text)
            return text


@pytest.fixture
def setup():
    exporter = InMemorySpanExporter()
    obs = Observer(exporter=exporter, schedule_delay_ms=10)
    yield obs, exporter
    obs.shutdown()


def record(obs, exporter, agent, query="q"):
    with obs.trace("probe") as probe:  # capture the agent's trace id
        pass
    result = agent.run(query)
    obs.flush()
    trace_id = format(exporter.get_finished_spans()[-1].context.trace_id, "032x")
    assert trace_id != probe.trace_id
    return result, trace_id


def spans_by_key(exporter, trace_id):
    return {
        s.attributes["obzerve.replay.key"]: s
        for s in exporter.get_finished_spans()
        if format(s.context.trace_id, "032x") == trace_id and "obzerve.replay.key" in s.attributes
    }


def test_recording_captures_keys_outputs_and_root_input(setup):
    obs, exporter = setup
    agent = Agent(obs)
    agent.search_fails = 1
    _, trace_id = record(obs, exporter, agent)

    spans = spans_by_key(exporter, trace_id)
    assert set(spans) == {"search#1", "search#2", "rank#1", "answer#1", "notify#1"}
    failed = spans["search#1"]
    assert failed.status.status_code is StatusCode.ERROR
    assert failed.attributes["obzerve.replay.error_class"].endswith(":ToolTimeout")
    rank = spans["rank#1"]
    assert json.loads(rank.attributes["obzerve.output"]) == [
        {"id": "q-a", "score": 1.0},
        {"id": "q-b", "score": 0.5},
    ]
    assert rank.attributes["obzerve.replay.complete"] is True
    assert spans["notify#1"].attributes["obzerve.replay.side_effect"] is True
    root = next(s for s in exporter.get_finished_spans() if s.parent is None and s.name == "agent")
    assert root.attributes["obzerve.replay.complete"] is True


def test_recorded_replay_reproduces_the_run_without_calling_anything(setup):
    obs, exporter = setup
    agent = Agent(obs)
    agent.search_fails = 1  # the original run hit one tool timeout, then recovered
    original, trace_id = record(obs, exporter, agent)
    bundle = bundle_from(exporter, trace_id)

    replayer = Agent(obs)  # fresh agent: no failures configured, different style
    replayer.answer_style = "long"
    with obs.replay(bundle, RECORDED, label="debug") as session:
        result = replayer.run(**bundle.root_input)

    assert result == original  # the recorded LLM answer, not the new style
    assert replayer.calls == []  # nothing was executed, not even the side effect
    assert session.recorded_calls == 5 and session.live_calls == 0
    assert session.divergences == []
    obs.flush()
    replay_root = next(
        s
        for s in exporter.get_finished_spans()
        if s.attributes.get("obzerve.replay.of") == trace_id
    )
    assert replay_root.attributes["obzerve.replay.mode"] == "recorded"
    assert replay_root.attributes["obzerve.replay.label"] == "debug"
    replay_spans = spans_by_key(exporter, format(replay_root.context.trace_id, "032x"))
    # The recorded tool timeout was re-raised with its original class and retried.
    assert replay_spans["search#1"].attributes["error.type"] == "ToolTimeout"
    assert replay_spans["search#2"].attributes["obzerve.replay.source"] == "recorded"


def test_llm_live_replay_keeps_dependencies_fixed(setup):
    obs, exporter = setup
    agent = Agent(obs)
    _, trace_id = record(obs, exporter, agent)
    bundle = bundle_from(exporter, trace_id)

    candidate = Agent(obs)
    candidate.answer_style = "long"
    with obs.replay(bundle, LLM_LIVE) as session:
        result = candidate.run(**bundle.root_input)

    assert result == "long: q-a, q-b"
    assert candidate.calls == ["answer"]  # only the LLM ran; search, rank, notify recorded
    assert session.live_calls == 1


def test_failed_run_replays_as_the_same_failure(setup):
    obs, exporter = setup
    agent = Agent(obs)
    agent.search_fails = 3
    with pytest.raises(ToolTimeout):
        record(obs, exporter, agent)
    obs.flush()
    trace_id = format(exporter.get_finished_spans()[-1].context.trace_id, "032x")
    bundle = bundle_from(exporter, trace_id)

    for _ in range(3):  # repeatable: same failure every time, nothing called
        replayer = Agent(obs)
        with pytest.raises(ToolTimeout, match="search timed out"), obs.replay(bundle, RECORDED):
            replayer.run(**bundle.root_input)
        assert replayer.calls == []


def test_divergence_and_missing_recordings(setup):
    obs, exporter = setup
    _, trace_id = record(obs, exporter, Agent(obs), query="q")
    bundle = bundle_from(exporter, trace_id)

    # A different query: recorded calls are served but flagged as diverging.
    with obs.replay(bundle, RECORDED) as session:
        Agent(obs).run("other")
    assert "search#1" in session.divergences

    # A path the recording never took cannot be served.
    agent = Agent(obs)
    with obs.replay(bundle, RECORDED), obs.trace("agent", input={"query": "q"}):
        agent.search("q")
        with pytest.raises(ReplayMissError, match="search#2"):
            agent.search("q")


def test_side_effects_are_blocked_in_live_replay(setup):
    obs, exporter = setup
    _, trace_id = record(obs, exporter, Agent(obs))
    bundle = bundle_from(exporter, trace_id)
    without_notify = ReplayBundle(
        **{
            **bundle.__dict__,
            "recordings": {
                k: v for k, v in bundle.recordings.items() if not k.startswith("notify")
            },
        }
    )

    agent = Agent(obs)
    with obs.replay(bundle, LIVE):  # recorded side effect is reused, never re-run
        agent.run("q")
    assert "notify" not in agent.calls and "search" in agent.calls

    agent = Agent(obs)
    with pytest.raises(SideEffectBlocked), obs.replay(without_notify, LIVE):
        agent.run("q")
    assert "notify" not in agent.calls


def test_unknown_exception_class_is_replayed_generically(setup):
    obs, _ = setup
    bundle = ReplayBundle.from_json(
        {
            "trace_id": "0" * 31 + "1",
            "replayability": "replayable",
            "root_input": {"query": "q"},
            "recordings": [
                {
                    "key": "search#1",
                    "name": "search",
                    "kind": "tool",
                    "complete": True,
                    "error_type": "VendorError",
                    "error_class": "not_imported_module:VendorError",
                    "error_message": "boom",
                }
            ],
        }
    )
    with pytest.raises(ReplayedError, match="VendorError: boom"), obs.replay(bundle, RECORDED):
        Agent(obs).run("q")


def test_incomplete_recordings_are_flagged(setup):
    obs, exporter = setup

    @obs.replayable(kind=SpanKind.TOOL)
    def opaque() -> object:
        return object()

    @obs.replayable(kind=SpanKind.TOOL)
    def secret() -> dict:
        return {"api_key": "x", "value": 1}

    with obs.trace("agent", input={"q": 1}):
        opaque()
        secret()
    obs.flush()
    spans = {s.name: s.attributes for s in exporter.get_finished_spans()}
    assert spans["opaque"]["obzerve.replay.complete"] is False
    assert spans["opaque"]["obzerve.replay.incomplete_reason"] == "not JSON-serializable"
    assert spans["secret"]["obzerve.replay.incomplete_reason"] == "contains redacted values"


def test_async_replayable(setup):
    obs, exporter = setup

    @obs.replayable(kind=SpanKind.LLM)
    async def generate(prompt: str) -> str:
        return prompt.upper()

    async def run():
        with obs.trace("agent", input={"prompt": "hi"}):
            return await generate("hi")

    assert asyncio.run(run()) == "HI"
    obs.flush()
    trace_id = format(exporter.get_finished_spans()[-1].context.trace_id, "032x")
    with obs.replay(bundle_from(exporter, trace_id), RECORDED) as session:
        assert asyncio.run(run()) == "HI"
    assert session.recorded_calls == 1
