"""Stable Python capability layer; future adapters should call these functions."""

from __future__ import annotations

from ai_data_platform.discovery import Ambiguity, DiscoveryResult, MetricMatch
from ai_data_platform.discovery import assess_ambiguity as _assess_ambiguity
from ai_data_platform.discovery import discover_datasets as _discover_datasets
from ai_data_platform.discovery import discover_datasets_hybrid as _discover_datasets_hybrid
from ai_data_platform.discovery import resolve_metric as _resolve_metric
from ai_data_platform.embeddings import (
    EmbeddingProvider,
    InMemoryVectorIndex,
    build_semantic_documents,
)
from ai_data_platform.models import BusinessConcept, BusinessEntity, Dataset, Metric, Registry
from ai_data_platform.runtime.ranking import rerank_with_runtime
from ai_data_platform.runtime.stores import RuntimeMetadataStore
from ai_data_platform.validation import validate_registry as _validate_registry


def get_concept(concept_id: str, registry: Registry) -> BusinessConcept:
    return registry.concepts[concept_id]


def get_metric(metric_id: str, registry: Registry) -> Metric:
    return registry.metrics[metric_id]


def get_entity(entity_id: str, registry: Registry) -> BusinessEntity:
    return registry.entities[entity_id]


def get_dataset(dataset_id: str, registry: Registry) -> Dataset:
    return registry.datasets[dataset_id]


def resolve_metric(query: str, registry: Registry) -> list[MetricMatch]:
    return _resolve_metric(query, registry)


def discover_datasets(question: str, registry: Registry, limit: int = 5) -> DiscoveryResult:
    return _discover_datasets(question, registry, limit)


def discover_datasets_with_runtime(
    question: str,
    registry: Registry,
    runtime_store: RuntimeMetadataStore,
    *,
    limit: int = 5,
) -> DiscoveryResult:
    result = _discover_datasets(question, registry, max(1, len(registry.datasets)))
    return rerank_with_runtime(result, runtime_store, limit=limit)


def build_vector_index(
    registry: Registry, embedding_provider: EmbeddingProvider
) -> InMemoryVectorIndex:
    return InMemoryVectorIndex(build_semantic_documents(registry), embedding_provider)


def discover_datasets_hybrid(
    question: str,
    registry: Registry,
    vector_index: InMemoryVectorIndex,
    *,
    limit: int = 5,
    vector_document_limit: int = 25,
    min_similarity: float = 0.25,
) -> DiscoveryResult:
    return _discover_datasets_hybrid(
        question,
        registry,
        vector_index,
        limit=limit,
        vector_document_limit=vector_document_limit,
        min_similarity=min_similarity,
    )


def discover_datasets_hybrid_with_runtime(
    question: str,
    registry: Registry,
    vector_index: InMemoryVectorIndex,
    runtime_store: RuntimeMetadataStore,
    *,
    limit: int = 5,
    vector_document_limit: int = 25,
    min_similarity: float = 0.25,
) -> DiscoveryResult:
    result = _discover_datasets_hybrid(
        question,
        registry,
        vector_index,
        limit=max(1, len(registry.datasets)),
        vector_document_limit=vector_document_limit,
        min_similarity=min_similarity,
    )
    return rerank_with_runtime(result, runtime_store, limit=limit)


def assess_ambiguity(metric_matches: list[MetricMatch], registry: Registry) -> Ambiguity | None:
    return _assess_ambiguity(metric_matches, registry)


def validate_registry(registry: Registry) -> None:
    _validate_registry(registry)
