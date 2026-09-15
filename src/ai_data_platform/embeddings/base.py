"""Vendor-neutral embedding contracts."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol, runtime_checkable

Embedding = tuple[float, ...]


@runtime_checkable
class EmbeddingProvider(Protocol):
    """Small capability boundary implemented by local or managed embedding providers."""

    @property
    def provider_id(self) -> str:
        """Return a stable identifier suitable for logs and index metadata."""

    def embed(self, texts: Sequence[str]) -> list[Embedding]:
        """Embed texts in order, returning one finite, non-empty vector per input."""
