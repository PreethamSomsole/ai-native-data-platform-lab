"""Structured context returned to API consumers and future reasoning layers."""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, ConfigDict, Field

from ai_data_platform.discovery import DatasetCandidate, DiscoveryResult
from ai_data_platform.models import BusinessConcept, BusinessEntity, Dataset, Metric
from ai_data_platform.runtime import (
    DatasetRuntimeMetadata,
    RowCountPoint,
    RuntimeAnalyticsSummary,
)


class ContextModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class DiscoveryMode(str, Enum):
    DETERMINISTIC = "deterministic"
    HYBRID = "hybrid"


class DatasetContext(ContextModel):
    dataset: Dataset
    metrics: list[Metric] = Field(default_factory=list)
    entities: list[BusinessEntity] = Field(default_factory=list)
    concepts: list[BusinessConcept] = Field(default_factory=list)
    runtime: DatasetRuntimeMetadata | None = None
    row_count_trend: list[RowCountPoint] = Field(default_factory=list)
    runtime_summary: RuntimeAnalyticsSummary


class CandidateContext(ContextModel):
    candidate: DatasetCandidate
    dataset: Dataset
    runtime: DatasetRuntimeMetadata | None = None


class DiscoveryContext(ContextModel):
    discovery: DiscoveryResult
    candidates: list[CandidateContext] = Field(default_factory=list)
