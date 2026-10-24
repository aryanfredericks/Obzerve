"""Replaying the research agent's traces (mock LLM, stub retriever)."""

import pytest
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from obzerve import Observer
from obzerve.replay import LLM_LIVE, RECORDED, bundle_from_spans
from research_agent.agent import ResearchAgent
from research_agent.faults import Fault
from research_agent.llm import LLMTimeoutError, MockLLM
from research_agent.prompts import CONCISE


@pytest.fixture
def observer():
    exporter = InMemorySpanExporter()
    obs = Observer(exporter=exporter)
    obs.exporter = exporter
    yield obs
    obs.shutdown()


def make_agent(obs, stub_retriever, **kwargs):
    llm = MockLLM(latency_s=0, timeout_s=0, ms_per_output_token=0)
    return ResearchAgent(obs, stub_retriever, llm=llm, sleep=lambda _: None, **kwargs)


def latest_bundle(obs):
    obs.flush()
    return bundle_from_spans(obs.exporter.get_finished_spans(), obs.last_trace_id)


class ExplodingLLM(MockLLM):
    def complete(self, *args, **kwargs):
        raise AssertionError("the model must not be called during a recorded replay")


class CountingRetriever:
    def __init__(self, inner):
        self.inner, self.calls, self.model_name = inner, 0, inner.model_name

    def search(self, *args, **kwargs):
        self.calls += 1
        return self.inner.search(*args, **kwargs)


def test_recorded_replay_reproduces_report_without_calling_anything(observer, stub_retriever):
    original = make_agent(observer, stub_retriever).run("RAG evaluation metrics")
    bundle = latest_bundle(observer)
    assert set(bundle.recordings) == {
        "search_web#1",
        "retrieve_documents#1",
        "llm.generate#1",
        "llm.generate#2",
    }

    retriever = CountingRetriever(stub_retriever)
    replayer = make_agent(observer, retriever)
    replayer.llm = ExplodingLLM()  # nothing may run live
    with observer.replay(bundle, RECORDED) as session:
        replayed = replayer.run(**bundle.root_input)
    assert replayed.report == original.report
    assert retriever.calls == 0 and session.live_calls == 0 and session.divergences == []


def test_llm_outage_replays_as_the_same_failure_and_the_fallback_fixes_it(observer, stub_retriever):
    with pytest.raises(LLMTimeoutError):
        make_agent(observer, stub_retriever).run("RAG evaluation metrics", fault=Fault.LLM_OUTAGE)
    bundle = latest_bundle(observer)
    assert [k for k in bundle.recordings if bundle.recordings[k].error_type] == [
        "llm.generate#1",
        "llm.generate#2",
        "llm.generate#3",
    ]

    for _ in range(2):  # repeatable
        with pytest.raises(LLMTimeoutError), observer.replay(bundle, RECORDED):
            make_agent(observer, stub_retriever).run(**bundle.root_input)

    fixed = make_agent(observer, stub_retriever, fallback_on_llm_failure=True)
    with observer.replay(bundle, RECORDED):
        result = fixed.run(**bundle.root_input)
    assert "currently unavailable" in result.report


def test_llm_live_replay_keeps_bad_retrieval_fixed(observer, stub_retriever):
    make_agent(observer, stub_retriever).run("RAG evaluation metrics", fault=Fault.BAD_RETRIEVAL)
    bundle = latest_bundle(observer)

    retriever = CountingRetriever(stub_retriever)
    candidate = make_agent(observer, retriever, prompt=CONCISE)
    with observer.replay(bundle, LLM_LIVE) as session:
        result = candidate.run(**bundle.root_input)  # no fault passed: retrieval is recorded
    assert retriever.calls == 0
    assert session.live_calls == 2 and session.divergences == []
    assert result.evidence_relevance < 0.35  # still the recorded (irrelevant) documents
