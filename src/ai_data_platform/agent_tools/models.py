"""Typed, protocol-independent results exposed to agent adapters."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from ai_data_platform.context import DatasetContext, DiscoveryContext
from ai_data_platform.models import BusinessConcept, BusinessEntity, Dataset, Metric


class AgentToolModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class DiscoveryToolResult(AgentToolModel):
    context: DiscoveryContext


class DatasetContractToolResult(AgentToolModel):
    dataset: Dataset


class DatasetContextToolResult(AgentToolModel):
    context: DatasetContext


class MetricDefinitionToolResult(AgentToolModel):
    metric: Metric


class EntityDefinitionToolResult(AgentToolModel):
    entity: BusinessEntity


class ConceptDefinitionToolResult(AgentToolModel):
    concept: BusinessConcept


class RegistryObjectCounts(AgentToolModel):
    concepts: int = 0
    entities: int = 0
    metrics: int = 0
    datasets: int = 0


class MetadataValidationResult(AgentToolModel):
    valid: bool
    errors: list[str] = Field(default_factory=list)
    counts: RegistryObjectCounts | None = None
