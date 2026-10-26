"""LLM backends for the demo agent.

* ``MockLLM``: a local, deterministic stand-in. It "generates" by extracting the
  context sentences that best overlap with the prompt, with simulated latency and
  estimated token usage, so tests run offline and repeatably.
* ``OllamaLLM``: a real model served by Ollama (e.g. ``qwen3:latest``), reporting
  the token counts and timings the server measured.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from typing import Any, Protocol

import httpx

MODEL_NAME = "obzerve-mock-llm-1"
PROVIDER = "local"

_WORD = re.compile(r"[a-z0-9]+")
_STOPWORDS = frozenset(
    "a an and are as at be by for from how in is it of on or that the to what when which with "
    "recent approaches compare".split()
)
_THINK_BLOCK = re.compile(r"<think>.*?</think>\s*", re.DOTALL)

SYSTEM_PROMPT = (
    "You are a careful research assistant. Answer using only the provided context. "
    "If the context does not contain the information needed, say so plainly instead of "
    "guessing. Be concise."
)


class LLMTimeoutError(TimeoutError):
    pass


class LLMUnavailableError(ConnectionError):
    """The model server could not be reached or rejected the request."""


@dataclass
class Completion:
    text: str
    input_tokens: int
    output_tokens: int
    # Backend-specific measurements, recorded as span attributes.
    metadata: dict[str, Any] = field(default_factory=dict)


class LLM(Protocol):
    model: str
    provider: str
    timeout_s: float

    def complete(
        self,
        instruction: str,
        context: list[str],
        simulate_timeout: bool = False,
        *,
        system: str | None = None,
        temperature: float | None = None,
        max_output_tokens: int | None = None,
    ) -> Completion: ...


def count_tokens(text: str) -> int:
    # Roughly 4 tokens per 3 words, like common BPE tokenizers on English text.
    return max(1, round(len(text.split()) * 4 / 3))


def _terms(text: str) -> set[str]:
    return {w for w in _WORD.findall(text.lower()) if w not in _STOPWORDS}


class MockLLM:
    model = MODEL_NAME
    provider = PROVIDER

    def __init__(
        self, latency_s: float = 0.25, timeout_s: float = 1.0, ms_per_output_token: float = 2.0
    ):
        self.latency_s = latency_s
        self.timeout_s = timeout_s
        self.ms_per_output_token = ms_per_output_token

    def complete(
        self,
        instruction: str,
        context: list[str],
        simulate_timeout: bool = False,
        *,
        system: str | None = None,
        temperature: float | None = None,
        max_output_tokens: int | None = None,
        max_sentences: int = 4,
    ) -> Completion:
        prompt = instruction + "\n\n" + "\n\n".join(context)
        if simulate_timeout:
            time.sleep(self.timeout_s)
            raise LLMTimeoutError(
                f"{self.model} did not respond within {self.timeout_s:.1f}s "
                "(simulated timeout, injected fault)"
            )

        if max_output_tokens is not None:
            # Roughly 30 tokens per extracted sentence.
            max_sentences = max(1, min(max_sentences, max_output_tokens // 30))
        wanted = _terms(instruction)
        sentences = [s.strip() for c in context for s in re.split(r"(?<=[.!?])\s+", c) if s.strip()]
        ranked = sorted(sentences, key=lambda s: len(wanted & _terms(s)), reverse=True)
        chosen = [s for s in ranked[:max_sentences] if wanted & _terms(s)] or ranked[:1]
        text = " ".join(chosen) if chosen else "I could not find relevant information."

        output_tokens = count_tokens(text)
        time.sleep(self.latency_s + output_tokens * self.ms_per_output_token / 1000)
        return Completion(
            text=text,
            input_tokens=count_tokens(prompt),
            output_tokens=output_tokens,
            metadata={"llm.token_counts": "estimated"},
        )


class OllamaLLM:
    """Chat completions from a local Ollama server (``POST /api/chat``)."""

    provider = "ollama"

    def __init__(
        self,
        model: str = "qwen3:latest",
        base_url: str = "http://localhost:11434",
        timeout_s: float = 120.0,
        temperature: float = 0.2,
        max_output_tokens: int = 512,
        think: bool = False,
        transport: httpx.BaseTransport | None = None,
    ):
        self.model = model
        self.timeout_s = timeout_s
        self.temperature = temperature
        self.max_output_tokens = max_output_tokens
        self.think = think
        self._client = httpx.Client(
            base_url=base_url.rstrip("/"),
            timeout=httpx.Timeout(timeout_s, connect=5.0),
            transport=transport,
        )

    def complete(
        self,
        instruction: str,
        context: list[str],
        simulate_timeout: bool = False,
        *,
        system: str | None = None,
        temperature: float | None = None,
        max_output_tokens: int | None = None,
    ) -> Completion:
        temperature = self.temperature if temperature is None else temperature
        max_output_tokens = max_output_tokens or self.max_output_tokens
        if simulate_timeout:
            # Injected fault: fail like a timed-out request, but immediately and without
            # calling the model, and say so, so the trace never misreports what happened.
            raise LLMTimeoutError(
                f"{self.model}: simulated timeout (injected fault; no request was sent)"
            )

        sources = "\n\n".join(f"[{i + 1}] {text}" for i, text in enumerate(context))
        body = {
            "model": self.model,
            "stream": False,
            "think": self.think,
            "messages": [
                {"role": "system", "content": system or SYSTEM_PROMPT},
                {"role": "user", "content": f"{instruction}\n\nContext:\n{sources}"},
            ],
            "options": {"temperature": temperature, "num_predict": max_output_tokens},
        }
        try:
            resp = self._client.post("/api/chat", json=body)
        except httpx.TimeoutException as exc:
            raise LLMTimeoutError(
                f"{self.model} did not respond within {self.timeout_s:.0f}s"
            ) from exc
        except httpx.TransportError as exc:
            raise LLMUnavailableError(
                f"Ollama unreachable at {self._client.base_url}: {exc}"
            ) from exc
        if resp.status_code != 200:
            detail = resp.text[:300]
            try:
                detail = resp.json().get("error", detail)
            except ValueError:
                pass
            raise LLMUnavailableError(f"Ollama returned {resp.status_code}: {detail}")

        data = resp.json()
        text = _THINK_BLOCK.sub("", data.get("message", {}).get("content", "")).strip()
        eval_ns = data.get("eval_duration") or 0
        output_tokens = data.get("eval_count") or 0
        metadata = {
            "llm.token_counts": "measured",
            "gen_ai.request.temperature": temperature,
            "gen_ai.request.max_tokens": max_output_tokens,
            "gen_ai.response.finish_reasons": [data.get("done_reason") or "unknown"],
            "ollama.load_ms": round((data.get("load_duration") or 0) / 1e6, 1),
            "ollama.prompt_eval_ms": round((data.get("prompt_eval_duration") or 0) / 1e6, 1),
            "ollama.eval_ms": round(eval_ns / 1e6, 1),
            "ollama.thinking": self.think,
        }
        if eval_ns and output_tokens:
            metadata["ollama.tokens_per_second"] = round(output_tokens / (eval_ns / 1e9), 1)
        return Completion(
            text=text,
            # Ollama omits prompt_eval_count when the whole prompt was cached.
            input_tokens=data.get("prompt_eval_count") or 0,
            output_tokens=output_tokens,
            metadata=metadata,
        )
