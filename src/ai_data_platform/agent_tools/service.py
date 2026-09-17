"""Read-only agent capabilities independent of MCP or any other protocol."""

from __future__ import annotations

from pathlib import Path

from ai_data_platform.api import get_concept, get_dataset, get_entity, get_metric
from ai_data_platform.context import ContextService, DiscoveryMode
from ai_data_platform.registry.loader import load_registry
from ai_data_platform.validation import RegistryValidationError

from .models import (
    ConceptDefinitionToolResult,
    DatasetContextToolResult,
    DatasetContractToolResult,
    DiscoveryToolResult,
    EntityDefinitionToolResult,
    MetadataValidationResult,
    MetricDefinitionToolResult,
    RegistryObjectCounts,
)


class AgentToolError(ValueError):
    """Stable agent-facing failure that protocol adapters may translate."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(f"{code}: {message}")


class AgentToolService:
    """Expose bounded, read-only platform capabilities to agent interfaces."""

    def __init__(self, context_service: ContextService, registry_path: str | Path) -> None:
        self.context_service = context_service
        self.registry_path = Path(registry_path)

    @staticmethod
    def _require_identifier(identifier: str, kind: str) -> str:
        normalized = identifier.strip()
        if not normalized:
            raise AgentToolError("INVALID_ARGUMENT", f"{kind}_id must not be empty")
        return normalized

    def discover_datasets(
        self,
        question: str,
        *,
        mode: DiscoveryMode = DiscoveryMode.DETERMINISTIC,
        limit: int = 5,
        min_similarity: float = 0.25,
    ) -> DiscoveryToolResult:
        normalized_question = question.strip()
        if not normalized_question:
            raise AgentToolError("INVALID_ARGUMENT", "question must not be empty")
        if not 1 <= limit <= 5:
            raise AgentToolError("INVALID_ARGUMENT", "limit must be between 1 and 5")
        if not -1.0 <= min_similarity <= 1.0:
            raise AgentToolError(
                "INVALID_ARGUMENT", "min_similarity must be between -1.0 and 1.0"
            )
        try:
            context = self.context_service.discover(
                normalized_question,
                mode=mode,
                limit=limit,
                min_similarity=min_similarity,
            )
        except RuntimeError as error:
            raise AgentToolError("CAPABILITY_UNAVAILABLE", str(error)) from error
        return DiscoveryToolResult(context=context)

    def get_dataset_contract(self, dataset_id: str) -> DatasetContractToolResult:
        dataset_id = self._require_identifier(dataset_id, "dataset")
        try:
            dataset = get_dataset(dataset_id, self.context_service.registry)
        except KeyError as error:
            raise AgentToolError("NOT_FOUND", f"dataset '{dataset_id}' was not found") from error
        return DatasetContractToolResult(dataset=dataset)

    def get_dataset_context(
        self, dataset_id: str, *, trend_limit: int = 30
    ) -> DatasetContextToolResult:
        dataset_id = self._require_identifier(dataset_id, "dataset")
        if not 1 <= trend_limit <= 1_000:
            raise AgentToolError(
                "INVALID_ARGUMENT", "trend_limit must be between 1 and 1000"
            )
        try:
            context = self.context_service.get_dataset_context(
                dataset_id, trend_limit=trend_limit
            )
        except KeyError as error:
            raise AgentToolError("NOT_FOUND", f"dataset '{dataset_id}' was not found") from error
        return DatasetContextToolResult(context=context)

    def get_metric_definition(self, metric_id: str) -> MetricDefinitionToolResult:
        metric_id = self._require_identifier(metric_id, "metric")
        try:
            metric = get_metric(metric_id, self.context_service.registry)
        except KeyError as error:
            raise AgentToolError("NOT_FOUND", f"metric '{metric_id}' was not found") from error
        return MetricDefinitionToolResult(metric=metric)

    def get_entity_definition(self, entity_id: str) -> EntityDefinitionToolResult:
        entity_id = self._require_identifier(entity_id, "entity")
        try:
            entity = get_entity(entity_id, self.context_service.registry)
        except KeyError as error:
            raise AgentToolError("NOT_FOUND", f"entity '{entity_id}' was not found") from error
        return EntityDefinitionToolResult(entity=entity)

    def get_concept_definition(self, concept_id: str) -> ConceptDefinitionToolResult:
        concept_id = self._require_identifier(concept_id, "concept")
        try:
            concept = get_concept(concept_id, self.context_service.registry)
        except KeyError as error:
            raise AgentToolError("NOT_FOUND", f"concept '{concept_id}' was not found") from error
        return ConceptDefinitionToolResult(concept=concept)

    def validate_metadata(self) -> MetadataValidationResult:
        """Reload configured YAML so agents can validate edits made outside the server."""
        try:
            registry = load_registry(self.registry_path)
        except RegistryValidationError as error:
            return MetadataValidationResult(valid=False, errors=error.messages)
        return MetadataValidationResult(
            valid=True,
            counts=RegistryObjectCounts(
                concepts=len(registry.concepts),
                entities=len(registry.entities),
                metrics=len(registry.metrics),
                datasets=len(registry.datasets),
            ),
        )
