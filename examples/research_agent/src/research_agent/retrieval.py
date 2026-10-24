"""Dense retrieval over the local corpus with sentence-transformers.

Runs on the GPU when PyTorch can see one (ROCm builds expose AMD GPUs through
the ``torch.cuda`` API), otherwise on the CPU.
"""

from __future__ import annotations

from dataclasses import dataclass

from research_agent.corpus import DOCUMENTS, Document

EMBEDDING_MODEL = "sentence-transformers/all-MiniLM-L6-v2"


@dataclass
class ScoredDocument:
    document: Document
    score: float


class DenseRetriever:
    def __init__(self, model_name: str = EMBEDDING_MODEL, device: str | None = None):
        import torch
        from sentence_transformers import SentenceTransformer

        self.device = device or ("cuda" if torch.cuda.is_available() else "cpu")
        self.device_name = (
            torch.cuda.get_device_name(0) if self.device.startswith("cuda") else "cpu"
        )
        self.model_name = model_name
        self._model = SentenceTransformer(model_name, device=self.device)
        self._docs = list(DOCUMENTS)
        self._embeddings = self._model.encode(
            [f"{d.title}. {d.text}" for d in self._docs],
            convert_to_tensor=True,
            normalize_embeddings=True,
        )

    def search(self, query: str, k: int = 5, invert: bool = False) -> list[ScoredDocument]:
        """Top-``k`` documents by cosine similarity; ``invert`` returns the worst matches."""
        query_embedding = self._model.encode(
            query, convert_to_tensor=True, normalize_embeddings=True
        )
        scores = (self._embeddings @ query_embedding).tolist()
        ranked = sorted(
            zip(self._docs, scores, strict=True), key=lambda p: p[1], reverse=not invert
        )
        return [ScoredDocument(doc, round(score, 4)) for doc, score in ranked[:k]]


class LazyDenseRetriever:
    """Loads the embedding model on first use, so a recorded replay (which never
    calls retrieval) does not touch the GPU."""

    model_name = EMBEDDING_MODEL

    def __init__(self) -> None:
        self._retriever: DenseRetriever | None = None

    @property
    def loaded(self) -> DenseRetriever:
        if self._retriever is None:
            self._retriever = DenseRetriever(self.model_name)
        return self._retriever

    @property
    def device(self) -> str:
        return self.loaded.device

    def search(self, query: str, k: int = 5, invert: bool = False) -> list[ScoredDocument]:
        return self.loaded.search(query, k=k, invert=invert)
