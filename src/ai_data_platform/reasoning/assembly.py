"""Build a bounded, typed reasoning context from governed discovery results."""

from __future__ import annotations

from ai_data_platform.context import DiscoveryContext
from ai_data_platform.discovery import DiscoveryStatus
from ai_data_platform.models import Registry
from ai_data_platform.reasoning.models import (
    CandidateReasoningContext,
    CuratedReasoningContext,
    EvidenceItem,
    EvidenceKind,
    MetricReasoningContext,
)


def _dataset_contract_detail(candidate: CandidateReasoningContext) -> str:
    return (
        f"name={candidate.name}; domain={candidate.domain}; layer={candidate.layer}; "
        f"certification={candidate.certification.value}; owner={candidate.owner}; "
        f"grain={candidate.grain}; supported_use_cases={candidate.supported_use_cases}; "
        f"prohibited_use_cases={candidate.prohibited_use_cases}; "
        f"freshness_sla={candidate.freshness_sla}; refresh_cadence={candidate.refresh_cadence}"
    )


def assemble_reasoning_context(
    question: str,
    discovery_context: DiscoveryContext,
    registry: Registry,
) -> CuratedReasoningContext:
    """Expose only retrieved candidates and addressable evidence to the model."""
    evidence_by_id: dict[str, EvidenceItem] = {}
    candidates: list[CandidateReasoningContext] = []

    def add_evidence(item: EvidenceItem) -> None:
        evidence_by_id.setdefault(item.id, item)

    for rank, item in enumerate(discovery_context.candidates, start=1):
        dataset = item.dataset
        metrics = []
        for metric_id in dataset.metric_ids:
            metric = registry.metrics[metric_id]
            metrics.append(
                MetricReasoningContext(
                    id=metric.id,
                    name=metric.name,
                    domain=metric.domain,
                    owner=metric.owner,
                    definition=metric.definition,
                    intended_use_cases=metric.intended_use_cases,
                    prohibited_use_cases=metric.prohibited_use_cases,
                )
            )
        concept_ids = list(dataset.dimension_concept_ids)
        for metric_id in dataset.metric_ids:
            concept_ids.extend(registry.metrics[metric_id].related_concept_ids)
        for entity_id in dataset.entity_ids:
            concept_ids.extend(registry.entities[entity_id].related_concept_ids)
        concept_ids = list(dict.fromkeys(concept_ids))
        candidate = CandidateReasoningContext(
            dataset_id=dataset.id,
            rank=rank,
            score=item.candidate.score,
            name=dataset.name,
            domain=dataset.domain,
            layer=dataset.layer,
            certification=dataset.certification,
            owner=dataset.owner,
            grain=dataset.grain,
            description=dataset.description,
            supported_use_cases=dataset.supported_use_cases,
            prohibited_use_cases=dataset.prohibited_use_cases,
            freshness_sla=dataset.freshness_sla,
            refresh_cadence=dataset.refresh_cadence,
            metrics=metrics,
            entity_ids=dataset.entity_ids,
            concept_ids=concept_ids,
            ranking_reasons=item.candidate.reasons,
            runtime=item.runtime,
        )
        candidates.append(candidate)
        add_evidence(
            EvidenceItem(
                id=f"dataset:{dataset.id}:contract",
                kind=EvidenceKind.DATASET_CONTRACT,
                source_id=dataset.id,
                detail=_dataset_contract_detail(candidate),
            )
        )
        for metric in metrics:
            add_evidence(
                EvidenceItem(
                    id=f"metric:{metric.id}:definition",
                    kind=EvidenceKind.METRIC_DEFINITION,
                    source_id=metric.id,
                    detail=(
                        f"name={metric.name}; domain={metric.domain}; owner={metric.owner}; "
                        f"definition={metric.definition}; "
                        f"intended_use_cases={metric.intended_use_cases}; "
                        f"prohibited_use_cases={metric.prohibited_use_cases}"
                    ),
                )
            )
        for entity_id in dataset.entity_ids:
            entity = registry.entities[entity_id]
            add_evidence(
                EvidenceItem(
                    id=f"entity:{entity.id}:definition",
                    kind=EvidenceKind.ENTITY_DEFINITION,
                    source_id=entity.id,
                    detail=f"name={entity.name}; description={entity.description}",
                )
            )
        for concept_id in concept_ids:
            concept = registry.concepts[concept_id]
            add_evidence(
                EvidenceItem(
                    id=f"concept:{concept.id}:definition",
                    kind=EvidenceKind.CONCEPT_DEFINITION,
                    source_id=concept.id,
                    detail=f"name={concept.name}; description={concept.description}",
                )
            )
        for index, reason in enumerate(item.candidate.reasons, start=1):
            add_evidence(
                EvidenceItem(
                    id=f"ranking:{dataset.id}:{index}",
                    kind=EvidenceKind.RANKING_REASON,
                    source_id=dataset.id,
                    detail=f"signal={reason.signal}; points={reason.points}; {reason.detail}",
                )
            )
        if item.runtime is not None:
            runtime = item.runtime
            add_evidence(
                EvidenceItem(
                    id=f"runtime:{dataset.id}:latest",
                    kind=EvidenceKind.RUNTIME_STATE,
                    source_id=dataset.id,
                    detail=(
                        f"observed_at={runtime.observed_at.isoformat()}; "
                        f"freshness={runtime.freshness_status.value}; "
                        f"quality={runtime.quality_status.value}; "
                        f"health={runtime.operational_health.value}; "
                        f"usage_count_30d={runtime.usage_count_30d}; row_count={runtime.row_count}"
                    ),
                )
            )

    status = discovery_context.discovery.status
    return CuratedReasoningContext(
        question=question,
        discovery_status=status,
        selection_allowed=status is DiscoveryStatus.RESOLVED,
        candidates=candidates,
        ambiguity=discovery_context.discovery.ambiguity,
        evidence_catalog=list(evidence_by_id.values()),
    )
