import json
import os

import httpx
import pytest
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import StatusCode

from obzerve import Observer
from research_agent.agent import ResearchAgent
from research_agent.faults import Fault
from research_agent.llm import LLMTimeoutError, LLMUnavailableError, OllamaLLM

OLLAMA_URL = os.environ.get("OLLAMA_HOST", "http://localhost:11434")
OLLAMA_MODEL = os.environ.get("RESEARCH_AGENT_OLLAMA_MODEL", "qwen3:latest")

REPLY = {
    "model": "qwen3:latest",
    "message": {
        "role": "assistant",
        "content": "<think>\nhmm\n</think>\n\nFaithfulness checks claims.",
    },
    "done": True,
    "done_reason": "stop",
    "load_duration": 9_000_000,
    "prompt_eval_count": 120,
    "prompt_eval_duration": 50_000_000,
    "eval_count": 30,
    "eval_duration": 500_000_000,
}


def fake_ollama(handler) -> OllamaLLM:
    return OllamaLLM(transport=httpx.MockTransport(handler))


def test_request_and_measured_usage():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json=REPLY)

    completion = fake_ollama(handler).complete("Summarise X", ["doc one", "doc two"])

    body = seen["body"]
    assert seen["path"] == "/api/chat"
    assert body["model"] == "qwen3:latest" and body["stream"] is False and body["think"] is False
    assert body["messages"][0]["role"] == "system"
    assert "[1] doc one" in body["messages"][1]["content"]
    assert "[2] doc two" in body["messages"][1]["content"]
    # Thinking output is never returned as the answer.
    assert completion.text == "Faithfulness checks claims."
    assert (completion.input_tokens, completion.output_tokens) == (120, 30)
    assert completion.metadata["llm.token_counts"] == "measured"
    assert completion.metadata["ollama.tokens_per_second"] == 60.0
    assert completion.metadata["gen_ai.response.finish_reasons"] == ["stop"]


def test_errors_are_mapped():
    def timeout(request):
        raise httpx.ReadTimeout("slow", request=request)

    def refused(request):
        raise httpx.ConnectError("refused", request=request)

    with pytest.raises(LLMTimeoutError):
        fake_ollama(timeout).complete("q", ["c"])
    with pytest.raises(LLMUnavailableError):
        fake_ollama(refused).complete("q", ["c"])
    missing = fake_ollama(lambda r: httpx.Response(404, json={"error": "model 'x' not found"}))
    with pytest.raises(LLMUnavailableError, match="not found"):
        missing.complete("q", ["c"])


def test_injected_timeout_does_not_call_the_model():
    def handler(request):
        raise AssertionError("no request expected")

    with pytest.raises(LLMTimeoutError):
        fake_ollama(handler).complete("q", ["c"], simulate_timeout=True)


def _ollama_has_model() -> bool:
    try:
        tags = httpx.get(f"{OLLAMA_URL}/api/tags", timeout=2).json()
    except (httpx.HTTPError, ValueError):
        return False
    return any(m.get("name") == OLLAMA_MODEL for m in tags.get("models", []))


@pytest.mark.ollama
@pytest.mark.skipif(not _ollama_has_model(), reason=f"Ollama with {OLLAMA_MODEL} not available")
def test_agent_against_real_model(stub_retriever):
    exporter = InMemorySpanExporter()
    observer = Observer(exporter=exporter)
    agent = ResearchAgent(
        observer, stub_retriever, llm=OllamaLLM(model=OLLAMA_MODEL, base_url=OLLAMA_URL)
    )
    result = agent.run("What does faithfulness measure in RAG evaluation?", fault=Fault.LLM_TIMEOUT)
    observer.flush()
    spans = exporter.get_finished_spans()

    llm_spans = [s for s in spans if s.name == "llm.generate"]
    succeeded = [s for s in llm_spans if s.status.status_code is StatusCode.OK]
    assert len(llm_spans) == 4  # 2 injected timeouts, 1 successful retry, 1 report
    assert len(succeeded) == 2
    for span in succeeded:
        assert span.attributes["gen_ai.provider.name"] == "ollama"
        assert span.attributes["gen_ai.request.model"] == OLLAMA_MODEL
        assert span.attributes["gen_ai.usage.input_tokens"] > 0
        assert span.attributes["gen_ai.usage.output_tokens"] > 0
        assert span.attributes["llm.token_counts"] == "measured"
    assert "<think>" not in result.report
    assert len(result.report) > 50
    observer.shutdown()
