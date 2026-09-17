"""Governed orchestration around untrusted model reasoning."""

from __future__ import annotations

from ai_data_platform.context import ContextService, DiscoveryMode
from ai_data_platform.discovery import DiscoveryStatus
from ai_data_platform.reasoning.assembly import assemble_reasoning_context
from ai_data_platform.reasoning.models import (
    CuratedReasoningContext,
    DatasetSelectionResult,
    EvidenceItem,
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
            explanation = draft.explanation
            clarification = draft.clarification_question
            unsafe_output = draft.selected_dataset_id is not None
            try:
                evidence = self._resolve_evidence(context, draft.evidence_ids)
            except ReasoningPolicyError as error:
                events.append(
                    GuardrailEvent(
                        code="UNGROUNDED_EVIDENCE_BLOCKED",
                        detail=str(error),
                    )
                )
                evidence = self._ambiguity_evidence(context)
                unsafe_output = True
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
            if unsafe_output:
                explanation = (
                    "Multiple eligible datasets use explicitly non-equivalent business "
                    "definitions, so no dataset can be selected without clarification."
                )
                clarification = self._default_clarification(context)
                evidence = self._ambiguity_evidence(context)
            elif not clarification:
                events.append(
                    GuardrailEvent(
                        code="CLARIFICATION_QUESTION_DEFAULTED",
                        detail="The provider omitted a clarification question; a governed default was used.",
                    )
                )
                clarification = self._default_clarification(context)
            if not evidence:
                evidence = self._ambiguity_evidence(context)
            return DatasetSelectionResult(
                status=status,
                selected_dataset_id=None,
                explanation=explanation,
                evidence=evidence,
                clarification_question=clarification,
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
        return DatasetSelectionResult(
            status=status,
            selected_dataset_id=draft.selected_dataset_id,
            explanation=draft.explanation,
            evidence=evidence,
            clarification_question=None,
            candidate_ids=candidate_ids,
            provider_id=self.provider.provider_id,
        )
