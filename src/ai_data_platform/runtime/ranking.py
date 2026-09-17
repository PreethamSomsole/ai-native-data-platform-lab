"""Transparent runtime-policy adjustments applied after semantic retrieval."""

from __future__ import annotations

from ai_data_platform.discovery import DatasetCandidate, DiscoveryResult, RankingReason
from ai_data_platform.policy import RUNTIME_RANKING_WEIGHTS
from ai_data_platform.runtime.models import DatasetRuntimeMetadata
from ai_data_platform.runtime.stores import RuntimeMetadataStore


def _usage_points(usage_count_30d: int) -> int:
    if usage_count_30d >= 100:
        return RUNTIME_RANKING_WEIGHTS["usage_high"]
    if usage_count_30d >= 20:
        return RUNTIME_RANKING_WEIGHTS["usage_medium"]
    if usage_count_30d > 0:
        return RUNTIME_RANKING_WEIGHTS["usage_low"]
    return 0


def runtime_ranking_reasons(metadata: DatasetRuntimeMetadata | None) -> list[RankingReason]:
    if metadata is None:
        return [
            RankingReason(
                signal="runtime_metadata_missing",
                points=0,
                detail="No runtime observation is available; semantic rank is unchanged",
            )
        ]

    reasons: list[RankingReason] = []
    values = (
        (
            "runtime_freshness",
            RUNTIME_RANKING_WEIGHTS["freshness"][metadata.freshness_status.value],
            f"Observed freshness is {metadata.freshness_status.value}",
        ),
        (
            "runtime_quality",
            RUNTIME_RANKING_WEIGHTS["quality"][metadata.quality_status.value],
            (
                f"Observed quality is {metadata.quality_status.value}; "
                f"checks passed={metadata.quality_checks_passed}, "
                f"failed={metadata.quality_checks_failed}"
            ),
        ),
        (
            "runtime_operational_health",
            RUNTIME_RANKING_WEIGHTS["health"][metadata.operational_health.value],
            f"Observed operational health is {metadata.operational_health.value}",
        ),
    )
    for signal, points, detail in values:
        if points:
            reasons.append(RankingReason(signal=signal, points=points, detail=detail))

    usage_points = _usage_points(metadata.usage_count_30d)
    if usage_points:
        reasons.append(
            RankingReason(
                signal="runtime_usage",
                points=usage_points,
                detail=f"Observed 30-day usage count is {metadata.usage_count_30d}",
            )
        )
    return reasons


def rerank_with_runtime(
    result: DiscoveryResult,
    runtime_store: RuntimeMetadataStore,
    *,
    limit: int = 5,
) -> DiscoveryResult:
    """Rerank candidates without changing semantic exclusions or ambiguity status."""
    if limit < 1:
        raise ValueError("limit must be at least 1")
    runtime = runtime_store.get_latest_many(
        [candidate.dataset_id for candidate in result.candidates]
    )
    candidates = []
    for candidate in result.candidates:
        reasons = candidate.reasons + runtime_ranking_reasons(runtime.get(candidate.dataset_id))
        candidates.append(
            DatasetCandidate(
                dataset_id=candidate.dataset_id,
                score=sum(reason.points for reason in reasons),
                reasons=reasons,
            )
        )
    candidates.sort(key=lambda candidate: (-candidate.score, candidate.dataset_id))
    return result.model_copy(update={"candidates": candidates[:limit]})
