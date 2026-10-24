"""A research agent: search → retrieve → synthesize → hand off to a report writer.

Every step is instrumented with Obzerve, so a run shows up as one trace whose
spans explain where time went and where things failed. Search, retrieval, and
each model call are *replayable*: their results are recorded exactly, so a run
can be replayed later with the same dependencies (see ``obzerve.replay``).
"""

from __future__ import annotations

import random
import re
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass
from typing import Any, Protocol

from obzerve import Observer, SpanKind
from obzerve import attributes as attrs
from research_agent.corpus import DOCUMENTS, Document
from research_agent.faults import Fault
from research_agent.llm import LLM, Completion, LLMTimeoutError, LLMUnavailableError, MockLLM
from research_agent.prompts import GROUNDED, PromptConfig
from research_agent.retrieval import ScoredDocument

# Below this cosine similarity the evidence is considered weak.
WEAK_EVIDENCE_THRESHOLD = 0.35


class Retriever(Protocol):
    model_name: str

    def search(self, query: str, k: int = 5, invert: bool = False) -> list[ScoredDocument]: ...


@dataclass
class ResearchResult:
    trace_id: str
    report: str
    evidence_relevance: float


# Codecs: how replayable results are stored as JSON and rebuilt on replay.
def _docs_to_json(docs: list[Document]) -> list[dict]:
    return [asdict(d) for d in docs]


def _docs_from_json(data: list[dict]) -> list[Document]:
    return [Document(**d) for d in data]


def _scored_to_json(docs: list[ScoredDocument]) -> list[dict]:
    return [{"document": asdict(d.document), "score": d.score} for d in docs]


def _scored_from_json(data: list[dict]) -> list[ScoredDocument]:
    return [ScoredDocument(Document(**d["document"]), d["score"]) for d in data]


def _completion_from_json(data: dict) -> Completion:
    return Completion(**data)


class ResearchAgent:
    def __init__(
        self,
        observer: Observer,
        retriever: Retriever,
        llm: LLM | None = None,
        *,
        prompt: PromptConfig = GROUNDED,
        prompt_version: dict[str, Any] | None = None,
        fallback_on_llm_failure: bool = False,
        max_llm_attempts: int = 3,
        retry_backoff_s: float = 0.2,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self.obs = observer
        self.retriever = retriever
        self.llm = llm or MockLLM()
        self.prompt = prompt
        self.prompt_version = prompt_version
        self.fallback_on_llm_failure = fallback_on_llm_failure
        self.max_llm_attempts = max_llm_attempts
        self.retry_backoff_s = retry_backoff_s
        self._sleep = sleep
        # Set as soon as a run starts, so callers can link to the trace of a failed run.
        self.last_trace_id: str | None = None

        obs = observer
        # `fault` and `simulate_timeout` are test-harness knobs, not real inputs: they are
        # excluded from the input hash so a replay without faults still matches.

        @obs.replayable(
            "search_web",
            kind=SpanKind.TOOL,
            encode=_docs_to_json,
            decode=_docs_from_json,
            exclude_args=("fault",),
        )
        def search_web(query: str, fault: Fault) -> list[Document]:
            """Keyword search over the local corpus, standing in for a web search API."""
            obs.current_span().set_attribute("tool.name", "web_search")
            self._sleep(random.uniform(0.15, 0.4))
            if fault is Fault.SEARCH_ERROR:
                raise ConnectionError("search API unreachable: connection refused (503)")
            stop = {"of", "the", "and", "to", "for"}
            terms = set(re.findall(r"[a-z]+", query.lower())) - stop
            hits = [d for d in DOCUMENTS if terms & set(re.findall(r"[a-z]+", d.title.lower()))]
            obs.current_span().set_attribute("tool.result_count", len(hits[:5]))
            return hits[:5]

        @obs.replayable(
            "retrieve_documents",
            kind=SpanKind.RETRIEVAL,
            encode=_scored_to_json,
            decode=_scored_from_json,
            exclude_args=("fault",),
        )
        def retrieve_documents(query: str, fault: Fault) -> list[ScoredDocument]:
            documents = self.retriever.search(query, k=5, invert=fault is Fault.BAD_RETRIEVAL)
            obs.current_span().set_attributes(
                {
                    "retrieval.top_k": 5,
                    "retrieval.embedding_model": self.retriever.model_name,
                    "retrieval.device": getattr(self.retriever, "device", "unknown"),
                    "retrieval.scores": [d.score for d in documents],
                }
            )
            return documents

        @obs.replayable(
            "llm.generate",
            kind=SpanKind.LLM,
            encode=asdict,
            decode=_completion_from_json,
            exclude_args=("simulate_timeout",),
        )
        def generate(
            instruction: str, context: list[str], attempt: int, simulate_timeout: bool = False
        ) -> Completion:
            span = obs.current_span()
            span.set_attributes(
                {"obzerve.retry.attempt": attempt, "gen_ai.request.timeout_s": self.llm.timeout_s}
            )
            span.set_llm(model=self.llm.model, provider=self.llm.provider)
            completion = self.llm.complete(
                instruction,
                context,
                simulate_timeout=simulate_timeout,
                system=self.prompt.system,
                temperature=self.prompt.temperature,
                max_output_tokens=self.prompt.max_output_tokens,
            )
            span.set_llm(
                input_tokens=completion.input_tokens,
                output_tokens=completion.output_tokens,
                response_model=self.llm.model,
            )
            span.set_attributes(completion.metadata)
            return completion

        self._search_web = search_web
        self._retrieve_documents = retrieve_documents
        self._generate = generate

    def run(self, question: str, fault: Fault = Fault.NONE) -> ResearchResult:
        root_attrs: dict[str, Any] = {
            "agent.name": "research_agent",
            "demo.fault": fault.value,
            "agent.fallback_on_llm_failure": self.fallback_on_llm_failure,
            attrs.GEN_AI_REQUEST_MODEL: self.llm.model,
            attrs.PROMPT_NAME: "research_agent",
        }
        if self.prompt_version:
            root_attrs[attrs.PROMPT_VERSION] = str(self.prompt_version["version"])
        with self.obs.trace(
            "research_agent", input={"question": question}, attributes=root_attrs
        ) as trace:
            self.last_trace_id = trace.trace_id
            try:
                results = self._search_web(question, fault)
            except ConnectionError:
                results = []  # search is optional: continue with retrieval alone
            query = " ".join([question, *(d.title for d in results)])
            documents = self._retrieve_documents(query, fault)
            relevance = documents[0].score if documents else 0.0
            if relevance < WEAK_EVIDENCE_THRESHOLD:
                trace.add_event(
                    "weak_evidence",
                    {"evidence.top_score": relevance, "threshold": WEAK_EVIDENCE_THRESHOLD},
                )
            try:
                evidence = self._synthesize(question, documents, fault)
                report = self._handoff_to_report_writer(question, evidence, documents)
            except (LLMTimeoutError, LLMUnavailableError) as exc:
                if not self.fallback_on_llm_failure:
                    raise
                trace.add_event("llm_fallback", {"error.type": type(exc).__name__})
                report = self._fallback_report(question, documents)
            trace.set_attribute("eval.evidence_relevance", relevance)
            trace.set_output(report)
            return ResearchResult(trace.trace_id, report, relevance)

    def _synthesize(self, question: str, documents: list[ScoredDocument], fault: Fault) -> str:
        context = [d.document.text for d in documents]
        instruction = self.prompt.synthesis.format(question=question)
        with self.obs.span("synthesize_evidence", kind=SpanKind.CHAIN) as chain:
            for attempt in range(1, self.max_llm_attempts + 1):
                simulate_timeout = fault is Fault.LLM_OUTAGE or (
                    fault is Fault.LLM_TIMEOUT and attempt < 3
                )
                try:
                    completion = self._generate(instruction, context, attempt, simulate_timeout)
                except LLMTimeoutError as exc:
                    if attempt == self.max_llm_attempts:
                        raise
                    chain.record_retry(attempt, exc)
                    self._sleep(self.retry_backoff_s * 2 ** (attempt - 1))
                    continue
                chain.set_output(completion.text)
                return completion.text
        raise AssertionError("unreachable")

    def _handoff_to_report_writer(
        self, question: str, evidence: str, documents: list[ScoredDocument]
    ) -> str:
        with self.obs.span(
            "handoff:report_writer",
            kind=SpanKind.HANDOFF,
            attributes={"handoff.from": "research_agent", "handoff.to": "report_writer"},
        ):
            with self.obs.span(
                "report_writer",
                kind=SpanKind.AGENT,
                input={"question": question, "evidence": evidence},
                attributes={"agent.name": "report_writer"},
            ) as span:
                completion = self._generate(
                    self.prompt.report.format(question=question), [evidence], attempt=1
                )
                report = f"# {question}\n\n{completion.text}\n\n{_sources(documents)}"
                span.set_output(report)
                return report

    def _fallback_report(self, question: str, documents: list[ScoredDocument]) -> str:
        """Degraded answer when the model is unavailable: say so, and list the evidence."""
        return (
            f"# {question}\n\nThe language model is currently unavailable, so no summary "
            f"could be written. The most relevant sources found are listed below.\n\n"
            f"{_sources(documents)}"
        )


def _sources(documents: list[ScoredDocument]) -> str:
    lines = "\n".join(
        f"- {d.document.title} ({d.document.url}) [score {d.score:.2f}]" for d in documents
    )
    return f"## Sources\n{lines}"
