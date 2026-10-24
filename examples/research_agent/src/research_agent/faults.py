"""Failure scenarios that can be injected into the demo agent."""

from enum import StrEnum


class Fault(StrEnum):
    NONE = "none"
    # The first two LLM attempts time out; the third succeeds.
    LLM_TIMEOUT = "llm-timeout"
    # Every LLM attempt times out; the run fails.
    LLM_OUTAGE = "llm-outage"
    # The retriever returns the least relevant documents (e.g. a stale/broken index).
    BAD_RETRIEVAL = "bad-retrieval"
    # The web search tool is down; the agent continues without search results.
    SEARCH_ERROR = "search-error"
