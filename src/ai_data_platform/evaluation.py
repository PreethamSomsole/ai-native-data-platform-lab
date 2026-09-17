"""Repeatable evaluation of deterministic and hybrid retrieval quality."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from ai_data_platform.api import discover_datasets, discover_datasets_hybrid
from ai_data_platform.discovery import DiscoveryResult, DiscoveryStatus
from ai_data_platform.embeddings import InMemoryVectorIndex
from ai_data_platform.models import Registry


class EvaluationCase(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    id: str
    question: str
    relevant_dataset_ids: list[str] = Field(min_length=1)
    expected_status: DiscoveryStatus


class RetrievalMetrics(BaseModel):
    case_count: int
    recall_at_k: float
    mean_reciprocal_rank: float
    status_accuracy: float


class EvaluationReport(BaseModel):
    k: int
    provider_id: str
    deterministic: RetrievalMetrics
    hybrid: RetrievalMetrics
    recall_delta: float
    mean_reciprocal_rank_delta: float
    status_accuracy_delta: float


def load_evaluation_cases(path: str | Path) -> list[EvaluationCase]:
    try:
        raw: Any = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
        if not isinstance(raw, list):
            raise TypeError("expected a YAML list")
        cases = [EvaluationCase.model_validate(item) for item in raw]
    except (OSError, TypeError, ValueError, ValidationError, yaml.YAMLError) as error:
        raise ValueError(f"invalid evaluation corpus {path}: {error}") from error
    if not cases:
        raise ValueError("evaluation corpus must contain at least one case")
    return cases


def _metrics(
    cases: list[EvaluationCase], results: list[DiscoveryResult], k: int
) -> RetrievalMetrics:
    recalls: list[float] = []
    reciprocal_ranks: list[float] = []
    correct_statuses = 0
    for case, result in zip(cases, results):
        ranked_ids = [candidate.dataset_id for candidate in result.candidates[:k]]
        relevant = set(case.relevant_dataset_ids)
        recalls.append(len(relevant & set(ranked_ids)) / len(relevant))
        first_rank = next(
            (rank for rank, dataset_id in enumerate(ranked_ids, start=1) if dataset_id in relevant),
            None,
        )
        reciprocal_ranks.append(0.0 if first_rank is None else 1.0 / first_rank)
        correct_statuses += result.status is case.expected_status
    count = len(cases)
    return RetrievalMetrics(
        case_count=count,
        recall_at_k=sum(recalls) / count,
        mean_reciprocal_rank=sum(reciprocal_ranks) / count,
        status_accuracy=correct_statuses / count,
    )


def evaluate_retrieval(
    registry: Registry,
    cases: list[EvaluationCase],
    vector_index: InMemoryVectorIndex,
    *,
    k: int = 5,
    min_similarity: float = 0.25,
) -> EvaluationReport:
    if k < 1:
        raise ValueError("k must be at least 1")
    if not cases:
        raise ValueError("cases must contain at least one evaluation case")

    unknown_dataset_ids = sorted(
        {
            dataset_id
            for case in cases
            for dataset_id in case.relevant_dataset_ids
            if dataset_id not in registry.datasets
        }
    )
    if unknown_dataset_ids:
        raise ValueError(
            "evaluation cases reference unknown dataset IDs: "
            + ", ".join(unknown_dataset_ids)
        )

    deterministic_results = [discover_datasets(case.question, registry, limit=k) for case in cases]
    hybrid_results = [
        discover_datasets_hybrid(
            case.question,
            registry,
            vector_index,
            limit=k,
            min_similarity=min_similarity,
        )
        for case in cases
    ]
    deterministic = _metrics(cases, deterministic_results, k)
    hybrid = _metrics(cases, hybrid_results, k)
    return EvaluationReport(
        k=k,
        provider_id=vector_index.provider_id,
        deterministic=deterministic,
        hybrid=hybrid,
        recall_delta=hybrid.recall_at_k - deterministic.recall_at_k,
        mean_reciprocal_rank_delta=(
            hybrid.mean_reciprocal_rank - deterministic.mean_reciprocal_rank
        ),
        status_accuracy_delta=hybrid.status_accuracy - deterministic.status_accuracy,
    )
