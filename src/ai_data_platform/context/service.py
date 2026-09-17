"""Application service combining declarative semantics with observed runtime state."""

from __future__ import annotations

from ai_data_platform.api import (
    discover_datasets_hybrid_with_runtime,
    discover_datasets_with_runtime,
)
from ai_data_platform.context.models import (
    CandidateContext,
    DatasetContext,
    DiscoveryContext,
    DiscoveryMode,
)
from ai_data_platform.embeddings import InMemoryVectorIndex
from ai_data_platform.models import Registry
from ai_data_platform.runtime import DatasetRuntimeMetadata, RuntimeMetadataRepository


class ContextService:
    def __init__(
        self,
        registry: Registry,
        runtime_repository: RuntimeMetadataRepository,
        vector_index: InMemoryVectorIndex | None = None,
    ) -> None:
        self.registry = registry
        self.runtime_repository = runtime_repository
        self.vector_index = vector_index

    def _require_dataset(self, dataset_id: str) -> None:
        if dataset_id not in self.registry.datasets:
            raise KeyError(dataset_id)

    def record_runtime(self, metadata: DatasetRuntimeMetadata) -> DatasetRuntimeMetadata:
        self._require_dataset(metadata.dataset_id)
        self.runtime_repository.record(metadata)
        return metadata

    def get_dataset_context(self, dataset_id: str, *, trend_limit: int = 30) -> DatasetContext:
        self._require_dataset(dataset_id)
        dataset = self.registry.datasets[dataset_id]
        metrics = [self.registry.metrics[item] for item in dataset.metric_ids]
        entities = [self.registry.entities[item] for item in dataset.entity_ids]
        concept_ids = list(dataset.dimension_concept_ids)
        for metric in metrics:
            concept_ids.extend(metric.related_concept_ids)
        for entity in entities:
            concept_ids.extend(entity.related_concept_ids)
        unique_concept_ids = list(dict.fromkeys(concept_ids))
        return DatasetContext(
            dataset=dataset,
            metrics=metrics,
            entities=entities,
            concepts=[self.registry.concepts[item] for item in unique_concept_ids],
            runtime=self.runtime_repository.get_latest(dataset_id),
            row_count_trend=self.runtime_repository.row_count_trend(
                dataset_id, limit=trend_limit
            ),
            runtime_summary=self.runtime_repository.summarize(dataset_id),
        )

    def discover(
        self,
        question: str,
        *,
        mode: DiscoveryMode = DiscoveryMode.DETERMINISTIC,
        limit: int = 5,
        min_similarity: float = 0.25,
    ) -> DiscoveryContext:
        if mode is DiscoveryMode.HYBRID:
            if self.vector_index is None:
                raise RuntimeError("hybrid discovery requires a vector index")
            result = discover_datasets_hybrid_with_runtime(
                question,
                self.registry,
                self.vector_index,
                self.runtime_repository,
                limit=limit,
                min_similarity=min_similarity,
            )
        else:
            result = discover_datasets_with_runtime(
                question,
                self.registry,
                self.runtime_repository,
                limit=limit,
            )
        runtime = self.runtime_repository.get_latest_many(
            [candidate.dataset_id for candidate in result.candidates]
        )
        candidates = [
            CandidateContext(
                candidate=candidate,
                dataset=self.registry.datasets[candidate.dataset_id],
                runtime=runtime.get(candidate.dataset_id),
            )
            for candidate in result.candidates
        ]
        return DiscoveryContext(discovery=result, candidates=candidates)
