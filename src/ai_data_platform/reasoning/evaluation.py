"""Evaluation harness for selection accuracy, grounding, and safe abstention."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from ai_data_platform.context import DiscoveryMode
from ai_data_platform.discovery import DiscoveryStatus
from ai_data_platform.models import Registry
from ai_data_platform.reasoning.providers import ReasoningProviderError
from ai_data_platform.reasoning.service import ReasoningPolicyError, ReasoningService


class EvaluationModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class ReasoningEvaluationCase(EvaluationModel):
    id: str
    question: str
    expected_status: DiscoveryStatus
    expected_dataset_id: str | None = None

    @model_validator(mode="after")
    def validate_expected_selection(self) -> ReasoningEvaluationCase:
        if self.expected_status is DiscoveryStatus.RESOLVED and not self.expected_dataset_id:
            raise ValueError("resolved cases require expected_dataset_id")
        if self.expected_status is not DiscoveryStatus.RESOLVED and self.expected_dataset_id:
            raise ValueError("abstention cases cannot define expected_dataset_id")
        return self


class ReasoningCaseResult(EvaluationModel):
    case_id: str
    expected_status: DiscoveryStatus
    actual_status: DiscoveryStatus | None = None
    expected_dataset_id: str | None = None
    selected_dataset_id: str | None = None
    status_correct: bool = False
    selection_correct: bool | None = None
    safe_abstention: bool | None = None
    grounded: bool = False
    error: str | None = None


class ReasoningMetrics(EvaluationModel):
    case_count: int = Field(ge=1)
    status_accuracy: float = Field(ge=0.0, le=1.0)
    selection_accuracy: float = Field(ge=0.0, le=1.0)
    safe_abstention_rate: float = Field(ge=0.0, le=1.0)
    grounded_response_rate: float = Field(ge=0.0, le=1.0)
    error_rate: float = Field(ge=0.0, le=1.0)


class ReasoningEvaluationReport(EvaluationModel):
    provider_id: str
    metrics: ReasoningMetrics
    cases: list[ReasoningCaseResult]


def load_reasoning_evaluation_cases(
    path: str | Path,
    registry: Registry,
) -> list[ReasoningEvaluationCase]:
    try:
        raw: Any = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
        if not isinstance(raw, list):
            raise TypeError("expected a YAML list")
        cases = [ReasoningEvaluationCase.model_validate(item) for item in raw]
    except (OSError, TypeError, ValueError, ValidationError, yaml.YAMLError) as error:
        raise ValueError(f"invalid reasoning evaluation corpus {path}: {error}") from error
    if not cases:
        raise ValueError("reasoning evaluation corpus must contain at least one case")
    unknown = sorted(
        {
            case.expected_dataset_id
            for case in cases
            if case.expected_dataset_id and case.expected_dataset_id not in registry.datasets
        }
    )
    if unknown:
        raise ValueError(
            "reasoning evaluation cases reference unknown dataset IDs: " + ", ".join(unknown)
        )
    return cases


def evaluate_reasoning(
    service: ReasoningService,
    cases: list[ReasoningEvaluationCase],
    *,
    mode: DiscoveryMode = DiscoveryMode.HYBRID,
    limit: int = 5,
    min_similarity: float = 0.25,
) -> ReasoningEvaluationReport:
    if not cases:
        raise ValueError("cases must contain at least one reasoning evaluation case")
    results: list[ReasoningCaseResult] = []
    for case in cases:
        try:
            result = service.select_dataset(
                case.question,
                mode=mode,
                limit=limit,
                min_similarity=min_similarity,
            )
            selection_correct = (
                result.selected_dataset_id == case.expected_dataset_id
                if case.expected_dataset_id is not None
                else None
            )
            safe_abstention = (
                result.selected_dataset_id is None
                if case.expected_status is not DiscoveryStatus.RESOLVED
                else None
            )
            grounded = (
                bool(result.evidence)
                if result.status is not DiscoveryStatus.NO_MATCH
                else result.selected_dataset_id is None
            )
            results.append(
                ReasoningCaseResult(
                    case_id=case.id,
                    expected_status=case.expected_status,
                    actual_status=result.status,
                    expected_dataset_id=case.expected_dataset_id,
                    selected_dataset_id=result.selected_dataset_id,
                    status_correct=result.status is case.expected_status,
                    selection_correct=selection_correct,
                    safe_abstention=safe_abstention,
                    grounded=grounded,
                )
            )
        except (ReasoningPolicyError, ReasoningProviderError, ValueError) as error:
            results.append(
                ReasoningCaseResult(
                    case_id=case.id,
                    expected_status=case.expected_status,
                    expected_dataset_id=case.expected_dataset_id,
                    error=str(error),
                )
            )

    count = len(results)
    selection_results = [item for item in results if item.selection_correct is not None]
    abstention_results = [item for item in results if item.safe_abstention is not None]
    return ReasoningEvaluationReport(
        provider_id=service.provider.provider_id,
        metrics=ReasoningMetrics(
            case_count=count,
            status_accuracy=sum(item.status_correct for item in results) / count,
            selection_accuracy=(
                sum(bool(item.selection_correct) for item in selection_results)
                / len(selection_results)
                if selection_results
                else 1.0
            ),
            safe_abstention_rate=(
                sum(bool(item.safe_abstention) for item in abstention_results)
                / len(abstention_results)
                if abstention_results
                else 1.0
            ),
            grounded_response_rate=sum(item.grounded for item in results) / count,
            error_rate=sum(item.error is not None for item in results) / count,
        ),
        cases=results,
    )

