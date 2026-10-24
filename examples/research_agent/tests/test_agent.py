import pytest
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import StatusCode

from obzerve import Observer
from research_agent.agent import ResearchAgent
from research_agent.faults import Fault
from research_agent.llm import LLMTimeoutError, MockLLM


@pytest.fixture
def exporter():
    return InMemorySpanExporter()


@pytest.fixture
def agent(exporter, stub_retriever):
    observer = Observer(exporter=exporter)
    llm = MockLLM(latency_s=0, timeout_s=0, ms_per_output_token=0)
    yield ResearchAgent(observer, stub_retriever, llm=llm, sleep=lambda _: None)
    observer.shutdown()


def spans_of(agent, exporter):
    agent.obs.flush()
    return list(exporter.get_finished_spans())


def test_happy_path_produces_one_linked_trace(agent, exporter):
    result = agent.run("Compare approaches to RAG evaluation")
    spans = spans_of(agent, exporter)

    names = [s.name for s in spans]
    for expected in (
        "research_agent",
        "search_web",
        "retrieve_documents",
        "synthesize_evidence",
        "llm.generate",
        "handoff:report_writer",
        "report_writer",
    ):
        assert expected in names
    assert {format(s.context.trace_id, "032x") for s in spans} == {result.trace_id}
    assert all(s.status.status_code is StatusCode.OK for s in spans)
    assert "## Sources" in result.report
    llm = next(s for s in spans if s.name == "llm.generate")
    assert llm.attributes["gen_ai.usage.output_tokens"] > 0


def test_llm_timeout_is_retried_then_succeeds(agent, exporter):
    agent.run("RAG evaluation metrics", fault=Fault.LLM_TIMEOUT)
    spans = spans_of(agent, exporter)

    chain = next(s for s in spans if s.name == "synthesize_evidence")
    assert chain.attributes["obzerve.retry.count"] == 2
    attempts = [
        s for s in spans if s.name == "llm.generate" and s.parent.span_id == chain.context.span_id
    ]
    assert [s.status.status_code for s in attempts] == [
        StatusCode.ERROR,
        StatusCode.ERROR,
        StatusCode.OK,
    ]
    assert attempts[0].attributes["error.type"] == "LLMTimeoutError"


def test_llm_outage_fails_the_run(agent, exporter):
    with pytest.raises(LLMTimeoutError):
        agent.run("RAG evaluation metrics", fault=Fault.LLM_OUTAGE)
    root = next(s for s in spans_of(agent, exporter) if s.name == "research_agent")
    assert root.status.status_code is StatusCode.ERROR
    assert format(root.context.trace_id, "032x") == agent.last_trace_id


def test_search_error_is_recorded_but_run_continues(agent, exporter):
    result = agent.run("RAG evaluation metrics", fault=Fault.SEARCH_ERROR)
    spans = {s.name: s for s in spans_of(agent, exporter)}
    assert spans["search_web"].status.status_code is StatusCode.ERROR
    assert spans["search_web"].attributes["error.type"] == "ConnectionError"
    assert spans["research_agent"].status.status_code is StatusCode.OK
    assert result.report


def test_bad_retrieval_flags_weak_evidence(agent, exporter):
    result = agent.run("RAG evaluation metrics", fault=Fault.BAD_RETRIEVAL)
    root = next(s for s in spans_of(agent, exporter) if s.name == "research_agent")
    assert result.evidence_relevance < 0.35
    assert [e.name for e in root.events] == ["weak_evidence"]


@pytest.mark.gpu
def test_dense_retriever_uses_gpu_and_ranks_relevant_documents():
    torch = pytest.importorskip("torch")
    if not torch.cuda.is_available():
        pytest.skip("no GPU visible to PyTorch")
    from research_agent.retrieval import DenseRetriever

    retriever = DenseRetriever()
    assert retriever.device == "cuda"
    top = retriever.search("How do I measure faithfulness of RAG answers?", k=3)
    assert top[0].document.id == "rag-01"
    worst = retriever.search("How do I measure faithfulness of RAG answers?", k=3, invert=True)
    assert all(d.document.id.startswith("off") for d in worst)
