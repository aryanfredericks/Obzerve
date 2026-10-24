"""Versioned prompt configurations for the research agent.

A configuration is registered with Obzerve (``client.register_prompt``), which
assigns it a version number; runs and eval runs record which version they used.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass


@dataclass(frozen=True)
class PromptConfig:
    label: str
    system: str
    synthesis: str  # formatted with {question}
    report: str  # formatted with {question}
    temperature: float = 0.2
    max_output_tokens: int = 512

    def as_config(self, model: str) -> dict:
        """The content that defines a version: prompts, sampling, and model."""
        return {**asdict(self), "model": model}


GROUNDED = PromptConfig(
    label="grounded",
    system=(
        "You are a careful research assistant. Answer using only the provided context. "
        "If the context does not contain the information needed, say so plainly instead of "
        "guessing. Be concise."
    ),
    synthesis="Using only the context, summarise the evidence for: {question}",
    report="Write a short report answering: {question}",
)

# A "cost optimisation" that drops the instruction to admit missing context and
# caps output length: fewer tokens, but it answers confidently from weak evidence.
CONCISE = PromptConfig(
    label="concise",
    system="You are an expert. Answer confidently in at most two sentences. Never hedge.",
    synthesis="Answer: {question}",
    report="Give a confident two-sentence answer to: {question}",
    max_output_tokens=96,
)

PROMPTS = {p.label: p for p in (GROUNDED, CONCISE)}
