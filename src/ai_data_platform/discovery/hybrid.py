"""Hybrid keyword/vector retrieval with deterministic business guardrails."""

from __future__ import annotations

from collections import defaultdict

from ai_data_platform.discovery.service import (
    DatasetCandidate,
    DiscoveryResult,
    DiscoveryStatus,
    MetricMatch,
    RankingReason,
    assess_ambiguity,
    discover_datasets,
    resolve_metric,
)
from ai_data_platform.embeddings import (
    InMemoryVectorIndex,
    SemanticDocument,
    SemanticDocumentKind,
    VectorMatch,
)
from ai_data_platform.models import Certification, Registry
from ai_data_platform.policy import HYBRID_RANKING_WEIGHTS, RANKING_WEIGHTS


def _dataset_ids_for_document(document: SemanticDocument, registry: Registry) -> set[str]:
    if document.kind is SemanticDocumentKind.DATASET:
        return {document.record_id}
    if document.kind is SemanticDocumentKind.METRIC:
        return {
            dataset.id
            for dataset in registry.datasets.values()
            if document.record_id in dataset.metric_ids
        }
    if document.kind is SemanticDocumentKind.ENTITY:
        return {
            dataset.id
            for dataset in registry.datasets.values()
            if document.record_id in dataset.entity_ids
        }

    dataset_ids: set[str] = set()
    for dataset in registry.datasets.values():
        concept_ids = set(dataset.dimension_concept_ids)
        for metric_id in dataset.metric_ids:
            concept_ids.update(registry.metrics[metric_id].related_concept_ids)
        for entity_id in dataset.entity_ids:
            concept_ids.update(registry.entities[entity_id].related_concept_ids)
        if document.record_id in concept_ids:
            dataset_ids.add(dataset.id)
    return dataset_ids


def _vector_dataset_ranks(
    matches: list[VectorMatch], registry: Registry, excluded_ids: set[str]
) -> list[tuple[str, float, list[str]]]:
    similarities: dict[str, float] = {}
    sources: dict[str, list[str]] = defaultdict(list)
    for match in matches:
        for dataset_id in _dataset_ids_for_document(match.document, registry):
            if dataset_id in excluded_ids:
                continue
            similarities[dataset_id] = max(similarities.get(dataset_id, -1.0), match.similarity)
            sources[dataset_id].append(match.document.document_id)
    return sorted(
        (
            (dataset_id, similarity, sorted(set(sources[dataset_id])))
            for dataset_id, similarity in similarities.items()
        ),
        key=lambda item: (-item[1], item[0]),
    )


def _rrf_points(rank: int, weight_key: str) -> int:
    return round(
        HYBRID_RANKING_WEIGHTS["rrf_scale"] * HYBRID_RANKING_WEIGHTS[weight_key]
        / (HYBRID_RANKING_WEIGHTS["rrf_k"] + rank)
    )


def _hybrid_metric_matches(
    question: str,
    registry: Registry,
    vector_matches: list[VectorMatch],
) -> list[MetricMatch]:
    matches = {item.metric_id: item for item in resolve_metric(question, registry)}
    for vector_match in vector_matches:
        document = vector_match.document
        if document.kind is not SemanticDocumentKind.METRIC:
            continue
        metric = registry.metrics[document.record_id]
        vector_score = max(1, round(vector_match.similarity * 10))
        existing = matches.get(metric.id)
        if existing is None or vector_score > existing.score:
            matches[metric.id] = MetricMatch(
                metric_id=metric.id,
                name=metric.name,
                domain=metric.domain,
                definition=metric.definition,
                score=vector_score,
                matched_terms=(existing.matched_terms if existing else []),
            )
    return sorted(matches.values(), key=lambda item: (-item.score, item.metric_id))


def discover_datasets_hybrid(
    question: str,
    registry: Registry,
    vector_index: InMemoryVectorIndex,
    *,
    limit: int = 5,
    vector_document_limit: int = 25,
    min_similarity: float = 0.25,
) -> DiscoveryResult:
    """Fuse M1 ranking with vector retrieval, then apply deterministic guardrails."""
    if limit < 1:
        raise ValueError("limit must be at least 1")
    if vector_document_limit < 1:
        raise ValueError("vector_document_limit must be at least 1")

    baseline = discover_datasets(question, registry, limit=max(1, len(registry.datasets)))
    excluded_ids = {item.dataset_id for item in baseline.excluded_candidates}
    vector_matches = vector_index.search(
        question,
        limit=vector_document_limit,
        min_similarity=min_similarity,
    )
    vector_ranks = _vector_dataset_ranks(vector_matches, registry, excluded_ids)

    reasons: dict[str, list[RankingReason]] = defaultdict(list)
    for rank, candidate in enumerate(baseline.candidates, start=1):
        contributing_signals = ", ".join(reason.signal for reason in candidate.reasons)
        reasons[candidate.dataset_id].append(
            RankingReason(
                signal="deterministic_rrf",
                points=_rrf_points(rank, "deterministic_rrf_weight"),
                detail=(
                    f"Milestone 1 rank {rank}; contributing signals: {contributing_signals}"
                ),
            )
        )
    for rank, (dataset_id, similarity, sources) in enumerate(vector_ranks, start=1):
        reasons[dataset_id].append(
            RankingReason(
                signal="vector_rrf",
                points=_rrf_points(rank, "vector_rrf_weight"),
                detail=(
                    f"Vector rank {rank} with best cosine similarity {similarity:.4f}; "
                    f"source documents: {', '.join(sources[:5])}"
                ),
            )
        )

    candidates: list[DatasetCandidate] = []
    for dataset_id, retrieval_reasons in reasons.items():
        dataset = registry.datasets[dataset_id]
        policy_reasons = list(retrieval_reasons)
        if dataset.certification is Certification.CERTIFIED:
            policy_reasons.append(
                RankingReason(
                    signal="certification",
                    points=RANKING_WEIGHTS["certified"],
                    detail="Dataset is certified for governed use",
                )
            )
        if dataset.layer.lower() == "gold":
            policy_reasons.append(
                RankingReason(
                    signal="curated_layer",
                    points=RANKING_WEIGHTS["gold_layer"],
                    detail="Dataset is in the Gold curated layer",
                )
            )
        candidates.append(
            DatasetCandidate(
                dataset_id=dataset_id,
                score=sum(reason.points for reason in policy_reasons),
                reasons=policy_reasons,
            )
        )
    candidates.sort(key=lambda item: (-item.score, item.dataset_id))

    if not candidates:
        return DiscoveryResult(
            status=DiscoveryStatus.NO_MATCH,
            excluded_candidates=baseline.excluded_candidates,
        )

    surviving_metric_ids = {
        metric_id
        for candidate in candidates
        for metric_id in registry.datasets[candidate.dataset_id].metric_ids
    }
    metric_matches = [
        match
        for match in _hybrid_metric_matches(question, registry, vector_matches)
        if match.metric_id in surviving_metric_ids
    ]
    ambiguity = assess_ambiguity(metric_matches, registry)
    return DiscoveryResult(
        status=(DiscoveryStatus.CLARIFICATION_REQUIRED if ambiguity else DiscoveryStatus.RESOLVED),
        candidates=candidates[:limit],
        excluded_candidates=baseline.excluded_candidates,
        resolved_metric=(None if ambiguity else metric_matches[0] if metric_matches else None),
        ambiguity=ambiguity,
    )
