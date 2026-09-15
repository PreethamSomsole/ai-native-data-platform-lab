"""Small in-memory vector index used behind a replaceable capability boundary."""

from __future__ import annotations

import math
from collections.abc import Iterable, Sequence

from pydantic import BaseModel, ConfigDict

from ai_data_platform.embeddings.base import Embedding, EmbeddingProvider
from ai_data_platform.embeddings.documents import SemanticDocument


class VectorMatch(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    document: SemanticDocument
    similarity: float
    rank: int


def _validated_vectors(vectors: Sequence[Sequence[float]], expected_count: int) -> list[Embedding]:
    if len(vectors) != expected_count:
        raise ValueError(
            f"embedding provider returned {len(vectors)} vectors for {expected_count} texts"
        )
    normalized: list[Embedding] = []
    dimension: int | None = None
    for vector in vectors:
        values = tuple(float(value) for value in vector)
        if not values:
            raise ValueError("embedding vectors must not be empty")
        if not all(math.isfinite(value) for value in values):
            raise ValueError("embedding vectors must contain only finite values")
        if dimension is None:
            dimension = len(values)
        elif len(values) != dimension:
            raise ValueError("embedding vectors must have a consistent dimension")
        normalized.append(values)
    return normalized


def _cosine(left: Embedding, right: Embedding) -> float:
    if len(left) != len(right):
        raise ValueError("query and document embeddings must have the same dimension")
    left_norm = math.sqrt(sum(value * value for value in left))
    right_norm = math.sqrt(sum(value * value for value in right))
    if not left_norm or not right_norm:
        return 0.0
    return sum(a * b for a, b in zip(left, right)) / (left_norm * right_norm)


class InMemoryVectorIndex:
    """Immutable document/vector snapshot suitable for a small registry lab."""

    def __init__(
        self,
        documents: Iterable[SemanticDocument],
        provider: EmbeddingProvider,
    ) -> None:
        self.documents = tuple(documents)
        self.provider = provider
        self.vectors = tuple(
            _validated_vectors(
                provider.embed([document.text for document in self.documents]),
                len(self.documents),
            )
        )

    @property
    def provider_id(self) -> str:
        return self.provider.provider_id

    def search(
        self,
        query: str,
        *,
        limit: int = 10,
        min_similarity: float = 0.25,
    ) -> list[VectorMatch]:
        if limit < 1:
            raise ValueError("limit must be at least 1")
        query_vectors = _validated_vectors(self.provider.embed([query]), 1)
        scored = [
            (document, _cosine(query_vectors[0], vector))
            for document, vector in zip(self.documents, self.vectors)
        ]
        scored.sort(key=lambda item: (-item[1], item[0].document_id))
        return [
            VectorMatch(document=document, similarity=similarity, rank=rank)
            for rank, (document, similarity) in enumerate(
                (item for item in scored if item[1] >= min_similarity), start=1
            )
        ][:limit]
