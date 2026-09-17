"""Governed orchestration around untrusted model reasoning."""

from __future__ import annotations

from ai_data_platform.context import ContextService, DiscoveryMode
from ai_data_platform.discovery import DiscoveryStatus
from ai_data_platform.reasoning.assembly import assemble_reasoning_context
from ai_data_platform.reasoning.models import (
    CuratedReasoningContext,
    DatasetSelectionResult,
    EvidenceItem,
    EvidenceKind,
    GuardrailEvent,
)
from ai_data_platform.reasoning.providers import DatasetReasoningProvider


class ReasoningPolicyError(RuntimeError):
    """Raised when model output references context it was not given."""


class ReasoningService:
    def __init__(
        self,
        context_service: ContextService,
        provider: DatasetReasoningProvider,
    ) -> None:
        self.context_service = context_service
        self.provider = provider

    @staticmethod
    def _resolve_evidence(
        context: CuratedReasoningContext,
        evidence_ids: list[str],
    ) -> list[EvidenceItem]:
        catalog = {item.id: item for item in context.evidence_catalog}
        unknown = sorted(set(evidence_ids) - set(catalog))
        if unknown:
            raise ReasoningPolicyError(
                "provider referenced evidence outside curated context: " + ", ".join(unknown)
            )
        return [catalog[evidence_id] for evidence_id in evidence_ids]

    @staticmethod
    def _evidence_ids_for_candidate(
        context: CuratedReasoningContext,
        dataset_id: str,
    ) -> set[str]:
        """Return evidence that is directly associated with one curated candidate."""
        candidate = next(
            (item for item in context.candidates if item.dataset_id == dataset_id),
            None,
        )
        if candidate is None:
            return set()
        metric_ids = {metric.id for metric in candidate.metrics}
        entity_ids = set(candidate.entity_ids)
        concept_ids = set(candidate.concept_ids)
        semantic_source_ids = {
            EvidenceKind.METRIC_DEFINITION: metric_ids,
            EvidenceKind.ENTITY_DEFINITION: entity_ids,
            EvidenceKind.CONCEPT_DEFINITION: concept_ids,
        }
        supported: set[str] = set()
        for evidence in context.evidence_catalog:
            if evidence.kind in {
                EvidenceKind.DATASET_CONTRACT,
                EvidenceKind.RANKING_REASON,
                EvidenceKind.RUNTIME_STATE,
            }:
                if evidence.source_id == dataset_id:
                    supported.add(evidence.id)
            elif evidence.source_id in semantic_source_ids.get(evidence.kind, set()):
                supported.add(evidence.id)
        return supported

    @staticmethod
    def _ambiguity_evidence(context: CuratedReasoningContext) -> list[EvidenceItem]:
        if context.ambiguity is None:
            return []
        wanted = {
            f"metric:{match.metric_id}:definition"
            for match in context.ambiguity.conflicting_metrics
        }
        return [item for item in context.evidence_catalog if item.id in wanted]

    @staticmethod
    def _default_clarification(context: CuratedReasoningContext) -> str:
        if context.ambiguity and context.ambiguity.conflicting_metrics:
            choices = ", ".join(
                f"{match.name} ({match.domain})"
                for match in context.ambiguity.conflicting_metrics
            )
            return f"Which business definition should be used: {choices}?"
        return "Which business definition should be used?"

    def select_dataset(
        self,
        question: str,
        *,
        mode: DiscoveryMode = DiscoveryMode.HYBRID,
        limit: int = 5,
        min_similarity: float = 0.25,
    ) -> DatasetSelectionResult:
        if not 1 <= limit <= 5:
            raise ValueError("reasoning limit must be between 1 and 5")
        discovery_context = self.context_service.discover(
            question,
            mode=mode,
            limit=limit,
            min_similarity=min_similarity,
        )
        context = assemble_reasoning_context(
            question,
            discovery_context,
            self.context_service.registry,
        )
        candidate_ids = [candidate.dataset_id for candidate in context.candidates]
        status = context.discovery_status

        if status is DiscoveryStatus.NO_MATCH:
            return DatasetSelectionResult(
                status=status,
                explanation="No eligible registered dataset matched the question.",
                candidate_ids=[],
            )

        draft = self.provider.generate(context)

        if status is DiscoveryStatus.CLARIFICATION_REQUIRED:
            events: list[GuardrailEvent] = []
            if draft.selected_dataset_id is not None:
                events.append(
                    GuardrailEvent(
                        code="SELECTION_BLOCKED_BY_AMBIGUITY",
                        detail=(
                            "The provider attempted to select a dataset while deterministic "
                            "semantic policy required clarification. The selection and model "
                            "explanation were discarded."
                        ),
                    )
                )
            events.append(
                GuardrailEvent(
                    code="AMBIGUITY_RESPONSE_REPLACED",
                    detail=(
                        "Provider prose and citations were replaced with deterministic ambiguity "
                        "guidance so a tentative recommendation cannot be returned."
                    ),
                )
            )
            return DatasetSelectionResult(
                status=status,
                selected_dataset_id=None,
                explanation=(
                    "Multiple eligible datasets use explicitly non-equivalent business "
                    "definitions, so no dataset can be selected without clarification."
                ),
                evidence=self._ambiguity_evidence(context),
                clarification_question=self._default_clarification(context),
                candidate_ids=candidate_ids,
                provider_id=self.provider.provider_id,
                guardrail_events=events,
            )

        if draft.selected_dataset_id is None:
            raise ReasoningPolicyError("provider did not select a dataset for a resolved question")
        if draft.selected_dataset_id not in candidate_ids:
            raise ReasoningPolicyError(
                "provider selected a dataset outside curated candidates: "
                f"{draft.selected_dataset_id}"
            )
        evidence = self._resolve_evidence(context, draft.evidence_ids)
        if not evidence:
            raise ReasoningPolicyError("provider selection must cite at least one evidence item")
        allowed_evidence_ids = self._evidence_ids_for_candidate(
            context,
            draft.selected_dataset_id,
        )
        if not any(item.id in allowed_evidence_ids for item in evidence):
            raise ReasoningPolicyError(
                "provider selection must cite evidence tied to the selected candidate"
            )
        return DatasetSelectionResult(
            status=status,
            selected_dataset_id=draft.selected_dataset_id,
            explanation=draft.explanation,
            evidence=evidence,
            clarification_question=None,
            candidate_ids=candidate_ids,
            provider_id=self.provider.provider_id,
        )
