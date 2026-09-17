"""Typed contracts for governed reasoning over curated dataset context."""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, ConfigDict, Field, field_validator

from ai_data_platform.discovery import Ambiguity, DiscoveryStatus, RankingReason
from ai_data_platform.models import Certification
from ai_data_platform.runtime import DatasetRuntimeMetadata


class ReasoningModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class EvidenceKind(str, Enum):
    DATASET_CONTRACT = "dataset_contract"
    METRIC_DEFINITION = "metric_definition"
    ENTITY_DEFINITION = "entity_definition"
    CONCEPT_DEFINITION = "concept_definition"
    RANKING_REASON = "ranking_reason"
    RUNTIME_STATE = "runtime_state"


class EvidenceItem(ReasoningModel):
    id: str = Field(min_length=1)
    kind: EvidenceKind
    source_id: str = Field(min_length=1)
    detail: str = Field(min_length=1)


class MetricReasoningContext(ReasoningModel):
    id: str
    name: str
    domain: str
    owner: str
    definition: str
    intended_use_cases: list[str] = Field(default_factory=list)
    prohibited_use_cases: list[str] = Field(default_factory=list)


class CandidateReasoningContext(ReasoningModel):
    dataset_id: str
    rank: int = Field(ge=1)
    score: int
    name: str
    domain: str
    layer: str
    certification: Certification
    owner: str
    grain: str
    description: str
    supported_use_cases: list[str] = Field(default_factory=list)
    prohibited_use_cases: list[str] = Field(default_factory=list)
    freshness_sla: str
    refresh_cadence: str
    metrics: list[MetricReasoningContext] = Field(default_factory=list)
    entity_ids: list[str] = Field(default_factory=list)
    concept_ids: list[str] = Field(default_factory=list)
    ranking_reasons: list[RankingReason] = Field(default_factory=list)
    runtime: DatasetRuntimeMetadata | None = None


class CuratedReasoningContext(ReasoningModel):
    question: str = Field(min_length=1)
    discovery_status: DiscoveryStatus
    selection_allowed: bool
    candidates: list[CandidateReasoningContext] = Field(default_factory=list)
    ambiguity: Ambiguity | None = None
    evidence_catalog: list[EvidenceItem] = Field(default_factory=list)


class ReasoningDraft(ReasoningModel):
    """Untrusted structured output returned by a reasoning provider."""

    selected_dataset_id: str | None = None
    explanation: str = Field(min_length=1)
    evidence_ids: list[str] = Field(default_factory=list)
    clarification_question: str | None = None

    @field_validator("evidence_ids")
    @classmethod
    def evidence_ids_must_be_unique(cls, value: list[str]) -> list[str]:
        if len(value) != len(set(value)):
            raise ValueError("evidence_ids must be unique")
        return value


class GuardrailEvent(ReasoningModel):
    code: str = Field(min_length=1)
    detail: str = Field(min_length=1)


class DatasetSelectionResult(ReasoningModel):
    status: DiscoveryStatus
    selected_dataset_id: str | None = None
    explanation: str
    evidence: list[EvidenceItem] = Field(default_factory=list)
    clarification_question: str | None = None
    candidate_ids: list[str] = Field(default_factory=list)
    provider_id: str | None = None
    guardrail_events: list[GuardrailEvent] = Field(default_factory=list)

