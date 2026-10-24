import pytest

from research_agent.corpus import DOCUMENTS
from research_agent.retrieval import ScoredDocument


class StubRetriever:
    """Ranks on-topic documents first; avoids loading an embedding model in unit tests."""

    device = "cpu"
    device_name = "cpu"
    model_name = "stub"

    def search(self, query, k=5, invert=False):
        ranked = [ScoredDocument(d, 0.8 if d.id.startswith("rag") else 0.05) for d in DOCUMENTS]
        ranked.sort(key=lambda d: d.score, reverse=not invert)
        return ranked[:k]


@pytest.fixture
def stub_retriever():
    return StubRetriever()
