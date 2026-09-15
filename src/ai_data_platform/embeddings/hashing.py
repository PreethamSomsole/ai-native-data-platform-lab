"""Dependency-free reference embedding provider for local development and tests."""

from __future__ import annotations

import hashlib
import math
import re
from collections.abc import Sequence
from itertools import pairwise

from ai_data_platform.embeddings.base import Embedding

_TOKEN_PATTERN = re.compile(r"[a-z0-9]+")


class HashingEmbeddingProvider:
    """Map tokens and adjacent token pairs into a stable, normalized vector.

    This provider makes the lab and CI fully reproducible. It is intentionally a
    reference implementation, not a claim of production semantic quality. A managed
    or local model can implement the same ``EmbeddingProvider`` protocol.
    """

    def __init__(self, dimension: int = 256) -> None:
        if dimension < 8:
            raise ValueError("dimension must be at least 8")
        self.dimension = dimension

    @property
    def provider_id(self) -> str:
        return f"hashing-v1-{self.dimension}"

    @staticmethod
    def _features(text: str) -> list[str]:
        tokens = _TOKEN_PATTERN.findall(text.lower())
        return tokens + [f"{left}::{right}" for left, right in pairwise(tokens)]

    def embed(self, texts: Sequence[str]) -> list[Embedding]:
        vectors: list[Embedding] = []
        for text in texts:
            vector = [0.0] * self.dimension
            for feature in self._features(text):
                digest = hashlib.blake2b(feature.encode("utf-8"), digest_size=8).digest()
                value = int.from_bytes(digest, byteorder="big", signed=False)
                bucket = value % self.dimension
                sign = 1.0 if value & 1 else -1.0
                vector[bucket] += sign
            norm = math.sqrt(sum(value * value for value in vector))
            if norm:
                vector = [value / norm for value in vector]
            vectors.append(tuple(vector))
        return vectors
