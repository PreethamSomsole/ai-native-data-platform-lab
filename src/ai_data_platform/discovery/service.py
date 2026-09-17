"""Deterministic semantic retrieval, ranking, and ambiguity assessment."""

from __future__ import annotations

import re
from enum import Enum

from pydantic import BaseModel, Field

from ai_data_platform.models import Certification, Dataset, Metric, Registry
from ai_data_platform.policy import RANKING_WEIGHTS

_STOP_WORDS = {
    "a", "an", "and", "are", "by", "for", "from", "how", "in", "is", "last", "me",
    "of", "on", "quarter", "show", "the", "to", "was", "what", "with",
}


class DiscoveryStatus(str, Enum):
    RESOLVED = "RESOLVED"
    CLARIFICATION_REQUIRED = "CLARIFICATION_REQUIRED"
    NO_MATCH = "NO_MATCH"


class RankingReason(BaseModel):
    signal: str
    points: int
    detail: str


class MetricMatch(BaseModel):
    metric_id: str
    name: str
    domain: str
    definition: str
    score: int
    matched_terms: list[str]
    retrieval_sources: list[str] = Field(default_factory=list)


class DatasetCandidate(BaseModel):
    dataset_id: str
    score: int
    reasons: list[RankingReason]


class DatasetExclusion(BaseModel):
    dataset_id: str
    reasons: list[str]


class Ambiguity(BaseModel):
    reason: str
    conflicting_metrics: list[MetricMatch]


class DiscoveryResult(BaseModel):
    status: DiscoveryStatus
    candidates: list[DatasetCandidate] = Field(default_factory=list)
    excluded_candidates: list[DatasetExclusion] = Field(default_factory=list)
    resolved_metric: MetricMatch | None = None
    ambiguity: Ambiguity | None = None


def _terms(text: str) -> set[str]:
    return {
        term
        for term in re.findall(r"[a-z0-9]+", text.lower())
        if len(term) > 1 and term not in _STOP_WORDS
    }


def _joined(values: list[str]) -> str:
    return " ".join(values)


def _metric_document(metric: Metric) -> str:
    return " ".join(
        [
            metric.id,
            metric.name,
            metric.domain,
            metric.owner,
            metric.definition,
            _joined(metric.aliases),
            _joined(metric.target_users),
            _joined(metric.intended_use_cases),
        ]
    )


def _match_metric(query: str, metric: Metric) -> MetricMatch:
    query_terms = _terms(query)
    document = _metric_document(metric).lower()
    matched_terms = sorted(term for term in query_terms if term in _terms(document))
    score = len(matched_terms) * 2
    normalized_query = " ".join(re.findall(r"[a-z0-9]+", query.lower()))
    normalized_name = " ".join(re.findall(r"[a-z0-9]+", metric.name.lower()))
    if "net revenue" in normalized_query and "net revenue" in normalized_name:
        score += 6
    if metric.domain.lower() in query_terms:
        score += 3
    return MetricMatch(
        metric_id=metric.id,
        name=metric.name,
        domain=metric.domain,
        definition=metric.definition,
        score=score,
        matched_terms=matched_terms,
        retrieval_sources=["deterministic"],
    )


def resolve_metric(query: str, registry: Registry) -> list[MetricMatch]:
    """Return all deterministically relevant canonical metric definitions."""
    matches = [_match_metric(query, metric) for metric in registry.metrics.values()]
    return sorted(
        (match for match in matches if match.score > 0),
        key=lambda match: (-match.score, match.metric_id),
    )


def _dataset_document(dataset: Dataset, registry: Registry) -> str:
    metrics = [registry.metrics[metric_id] for metric_id in dataset.metric_ids]
    entities = [registry.entities[entity_id] for entity_id in dataset.entity_ids]
    concepts = [registry.concepts[concept_id] for concept_id in dataset.dimension_concept_ids]
    return " ".join(
        [
            dataset.id,
            dataset.name,
            dataset.domain,
            dataset.description,
            dataset.grain,
            dataset.owner,
            _joined(dataset.target_users),
            _joined(dataset.supported_use_cases),
            _joined(dataset.trust_signals),
            _joined([_metric_document(metric) for metric in metrics]),
            _joined([f"{entity.name} {entity.description}" for entity in entities]),
            _joined([f"{concept.name} {concept.description}" for concept in concepts]),
        ]
    )


def _prohibited_use_conflicts(query: str, dataset: Dataset, registry: Registry) -> list[str]:
    query_terms = _terms(query)
    prohibited: list[tuple[str, str]] = [
        ("dataset", use_case) for use_case in dataset.prohibited_use_cases
    ]
    for metric_id in dataset.metric_ids:
        prohibited.extend(
            (metric_id, use_case)
            for use_case in registry.metrics[metric_id].prohibited_use_cases
        )

    conflicts: list[str] = []
    for source, use_case in prohibited:
        use_case_terms = _terms(use_case)
        if not use_case_terms:
            continue
        overlap = query_terms & use_case_terms
        required_overlap = min(2, len(use_case_terms))
        if len(overlap) >= required_overlap:
            conflicts.append(
                f"{source} prohibits use case '{use_case}' "
                f"(matched: {', '.join(sorted(overlap))})"
            )
    return conflicts


def _points_for_term_match(
    matched_terms: set[str], per_term_key: str, cap_key: str
) -> int:
    return min(
        len(matched_terms) * RANKING_WEIGHTS[per_term_key],
        RANKING_WEIGHTS[cap_key],
    )


def _rank_dataset(
    query: str,
    dataset: Dataset,
    registry: Registry,
    metric_matches: dict[str, MetricMatch],
) -> DatasetCandidate | None:
    query_terms = _terms(query)
    reasons: list[RankingReason] = []
    dataset_terms = _terms(_dataset_document(dataset, registry))
    keyword_terms = query_terms & dataset_terms
    keyword_points = _points_for_term_match(
        keyword_terms, "keyword_match_per_term", "keyword_match_cap"
    )
    if keyword_points:
        reasons.append(
            RankingReason(
                signal="keyword_retrieval",
                points=keyword_points,
                detail=f"Matched metadata terms: {', '.join(sorted(keyword_terms))}",
            )
        )

    supporting_matches = [
        metric_matches[metric_id]
        for metric_id in dataset.metric_ids
        if metric_id in metric_matches
    ]
    if supporting_matches:
        best_match = max(supporting_matches, key=lambda match: match.score)
        metric_points = min(
            best_match.score * RANKING_WEIGHTS["metric_match_multiplier"],
            RANKING_WEIGHTS["metric_match_cap"],
        )
        reasons.append(
            RankingReason(
                signal="metric_compatibility",
                points=metric_points,
                detail=f"Supports {best_match.metric_id} matched to the question",
            )
        )

    if dataset.domain.lower() in query_terms:
        reasons.append(
            RankingReason(
                signal="domain_match",
                points=RANKING_WEIGHTS["domain_match"],
                detail=f"Dataset domain matches '{dataset.domain}'",
            )
        )

    use_case_terms = query_terms & _terms(_joined(dataset.supported_use_cases))
    use_case_points = _points_for_term_match(
        use_case_terms, "use_case_match_per_term", "use_case_match_cap"
    )
    if use_case_points:
        reasons.append(
            RankingReason(
                signal="intended_use_case_match",
                points=use_case_points,
                detail=f"Matched intended-use terms: {', '.join(sorted(use_case_terms))}",
            )
        )

    audience_terms = query_terms & _terms(_joined(dataset.target_users))
    audience_points = _points_for_term_match(
        audience_terms, "audience_match_per_term", "audience_match_cap"
    )
    if audience_points:
        reasons.append(
            RankingReason(
                signal="target_audience_match",
                points=audience_points,
                detail=f"Matched audience terms: {', '.join(sorted(audience_terms))}",
            )
        )

    freshness_terms = query_terms & _terms(f"{dataset.freshness_sla} {dataset.refresh_cadence}")
    freshness_points = _points_for_term_match(
        freshness_terms, "freshness_match_per_term", "freshness_match_cap"
    )
    if freshness_points:
        reasons.append(
            RankingReason(
                signal="freshness_expectation_match",
                points=freshness_points,
                detail=f"Matched declared freshness terms: {', '.join(sorted(freshness_terms))}",
            )
        )

    # Governance signals improve the rank of a retrieved candidate; they must
    # never make unrelated metadata look like a search result.
    if not reasons:
        return None

    if dataset.certification is Certification.CERTIFIED:
        reasons.append(
            RankingReason(
                signal="certification",
                points=RANKING_WEIGHTS["certified"],
                detail="Dataset is certified for governed use",
            )
        )
    if dataset.layer.lower() == "gold":
        reasons.append(
            RankingReason(
                signal="curated_layer",
                points=RANKING_WEIGHTS["gold_layer"],
                detail="Dataset is in the Gold curated layer",
            )
        )

    score = sum(reason.points for reason in reasons)
    return DatasetCandidate(dataset_id=dataset.id, score=score, reasons=reasons)


def assess_ambiguity(metric_matches: list[MetricMatch], registry: Registry) -> Ambiguity | None:
    """Require clarification for similarly relevant, explicitly incompatible metrics."""
    if len(metric_matches) < 2:
        return None
    leading_score = metric_matches[0].score
    comparable = [
        match
        for match in metric_matches
        if match.score >= leading_score * RANKING_WEIGHTS["ambiguity_similarity_ratio"]
    ]
    conflicts: list[MetricMatch] = []
    for match in comparable:
        metric = registry.metrics[match.metric_id]
        if any(
            other.metric_id in metric.not_equivalent_to_ids
            and (
                set(match.matched_terms) & set(other.matched_terms)
                or (
                    any(source.startswith("vector") for source in match.retrieval_sources)
                    and any(source.startswith("vector") for source in other.retrieval_sources)
                )
            )
            for other in comparable
            if other != match
        ):
            conflicts.append(match)
    if len(conflicts) < 2:
        return None
    return Ambiguity(
        reason=(
            "Multiple similarly relevant metrics are explicitly non-equivalent; "
            "choose the intended business definition."
        ),
        conflicting_metrics=conflicts,
    )


def discover_datasets(question: str, registry: Registry, limit: int = 5) -> DiscoveryResult:
    """Return an explained, deterministic top candidate set without guessing semantics."""
    if limit < 1:
        raise ValueError("limit must be at least 1")

    metric_match_list = resolve_metric(question, registry)
    metric_matches = {match.metric_id: match for match in metric_match_list}
    candidates: list[DatasetCandidate] = []
    exclusions: list[DatasetExclusion] = []

    for dataset in registry.datasets.values():
        conflicts = _prohibited_use_conflicts(question, dataset, registry)
        if conflicts:
            exclusions.append(DatasetExclusion(dataset_id=dataset.id, reasons=conflicts))
            continue
        candidate = _rank_dataset(question, dataset, registry, metric_matches)
        if candidate is not None:
            candidates.append(candidate)

    candidates.sort(key=lambda candidate: (-candidate.score, candidate.dataset_id))
    exclusions.sort(key=lambda exclusion: exclusion.dataset_id)

    surviving_metric_ids = {
        metric_id
        for candidate in candidates
        for metric_id in registry.datasets[candidate.dataset_id].metric_ids
    }
    eligible_metric_matches = [
        match for match in metric_match_list if match.metric_id in surviving_metric_ids
    ]
    ambiguity = assess_ambiguity(eligible_metric_matches, registry)

    if not candidates:
        return DiscoveryResult(
            status=DiscoveryStatus.NO_MATCH,
            excluded_candidates=exclusions,
        )

    return DiscoveryResult(
        status=(DiscoveryStatus.CLARIFICATION_REQUIRED if ambiguity else DiscoveryStatus.RESOLVED),
        candidates=candidates[:limit],
        excluded_candidates=exclusions,
        resolved_metric=(
            None if ambiguity else eligible_metric_matches[0] if eligible_metric_matches else None
        ),
        ambiguity=ambiguity,
    )
