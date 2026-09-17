from __future__ import annotations

import json
import tempfile
import unittest
from collections.abc import Callable
from pathlib import Path

from httpx import ASGITransport, AsyncClient

from ai_data_platform import build_vector_index, load_registry
from ai_data_platform.context import ContextService, DiscoveryMode
from ai_data_platform.discovery import DiscoveryStatus
from ai_data_platform.embeddings import HashingEmbeddingProvider
from ai_data_platform.http import create_app
from ai_data_platform.reasoning import (
    CuratedReasoningContext,
    OpenAIResponsesReasoningProvider,
    ReasoningDraft,
    ReasoningEvaluationCase,
    ReasoningPolicyError,
    ReasoningProviderError,
    ReasoningService,
    evaluate_reasoning,
)
from ai_data_platform.runtime import (
    DuckDBRuntimeHistoryStore,
    RuntimeMetadataRepository,
    SQLiteRuntimeMetadataStore,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]
REGISTRY_PATH = PROJECT_ROOT / "registry"


class FakeProvider:
    provider_id = "fake:test"

    def __init__(
        self,
        response: ReasoningDraft | Callable[[CuratedReasoningContext], ReasoningDraft],
    ) -> None:
        self.response = response
        self.contexts: list[CuratedReasoningContext] = []

    def generate(self, context: CuratedReasoningContext) -> ReasoningDraft:
        self.contexts.append(context)
        if callable(self.response):
            return self.response(context)
        return self.response


def select_first(context: CuratedReasoningContext) -> ReasoningDraft:
    selected = context.candidates[0].dataset_id
    evidence_id = next(
        item.id
        for item in context.evidence_catalog
        if item.source_id == selected and item.kind.value == "dataset_contract"
    )
    return ReasoningDraft(
        selected_dataset_id=selected,
        explanation="The first governed candidate best fits the requested business context.",
        evidence_ids=[evidence_id],
    )


class ReasoningTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        directory = Path(self.temporary_directory.name)
        self.current = SQLiteRuntimeMetadataStore(directory / "runtime.sqlite")
        self.history = DuckDBRuntimeHistoryStore(directory / "history.duckdb")
        repository = RuntimeMetadataRepository(self.current, self.history)
        self.registry = load_registry(REGISTRY_PATH)
        index = build_vector_index(self.registry, HashingEmbeddingProvider())
        self.context_service = ContextService(self.registry, repository, index)

    def tearDown(self) -> None:
        self.current.close()
        self.history.close()
        self.temporary_directory.cleanup()

    def test_resolved_selection_is_bounded_and_grounded(self) -> None:
        provider = FakeProvider(select_first)
        service = ReasoningService(self.context_service, provider)

        result = service.select_dataset(
            "Finance recognized net revenue",
            mode=DiscoveryMode.DETERMINISTIC,
            limit=5,
        )

        self.assertEqual(result.status, DiscoveryStatus.RESOLVED)
        self.assertEqual(result.selected_dataset_id, "gold.finance_revenue")
        self.assertTrue(result.evidence)
        self.assertLessEqual(len(provider.contexts[0].candidates), 5)
        self.assertTrue(provider.contexts[0].selection_allowed)
        evidence_ids = {item.id for item in provider.contexts[0].evidence_catalog}
        self.assertTrue({item.id for item in result.evidence} <= evidence_ids)

    def test_reasoning_limit_cannot_exceed_governed_top_five(self) -> None:
        service = ReasoningService(self.context_service, FakeProvider(select_first))
        with self.assertRaisesRegex(ValueError, "between 1 and 5"):
            service.select_dataset("revenue", limit=6)

    def test_ambiguity_override_is_discarded_and_clarification_is_preserved(self) -> None:
        def malicious(context: CuratedReasoningContext) -> ReasoningDraft:
            return ReasoningDraft(
                selected_dataset_id=context.candidates[0].dataset_id,
                explanation="Select the healthier candidate.",
                evidence_ids=[context.evidence_catalog[0].id],
                clarification_question=None,
            )

        provider = FakeProvider(malicious)
        service = ReasoningService(self.context_service, provider)
        result = service.select_dataset(
            "What was net revenue last quarter?",
            mode=DiscoveryMode.DETERMINISTIC,
        )

        self.assertEqual(result.status, DiscoveryStatus.CLARIFICATION_REQUIRED)
        self.assertIsNone(result.selected_dataset_id)
        self.assertIsNotNone(result.clarification_question)
        self.assertEqual(
            result.guardrail_events[0].code,
            "SELECTION_BLOCKED_BY_AMBIGUITY",
        )
        self.assertNotIn("healthier", result.explanation)
        self.assertFalse(provider.contexts[0].selection_allowed)

    def test_no_match_abstains_without_calling_provider(self) -> None:
        provider = FakeProvider(select_first)
        service = ReasoningService(self.context_service, provider)
        result = service.select_dataset(
            "Which office has the best parking availability?",
            mode=DiscoveryMode.DETERMINISTIC,
        )
        self.assertEqual(result.status, DiscoveryStatus.NO_MATCH)
        self.assertIsNone(result.selected_dataset_id)
        self.assertEqual(provider.contexts, [])

    def test_hallucinated_dataset_id_is_rejected(self) -> None:
        def hallucinate_dataset(context: CuratedReasoningContext) -> ReasoningDraft:
            return ReasoningDraft(
                selected_dataset_id="gold.invented",
                explanation="Invented selection.",
                evidence_ids=[context.evidence_catalog[0].id],
            )

        provider = FakeProvider(hallucinate_dataset)
        service = ReasoningService(self.context_service, provider)
        with self.assertRaisesRegex(ReasoningPolicyError, "outside curated candidates"):
            service.select_dataset(
                "Finance recognized net revenue",
                mode=DiscoveryMode.DETERMINISTIC,
            )

    def test_ambiguity_with_hallucinated_evidence_falls_back_safely(self) -> None:
        provider = FakeProvider(
            ReasoningDraft(
                selected_dataset_id=None,
                explanation="Unsupported ambiguity explanation.",
                evidence_ids=["evidence:invented"],
                clarification_question="Which one?",
            )
        )
        service = ReasoningService(self.context_service, provider)
        result = service.select_dataset(
            "What was net revenue last quarter?",
            mode=DiscoveryMode.DETERMINISTIC,
        )
        self.assertIsNone(result.selected_dataset_id)
        self.assertEqual(result.guardrail_events[0].code, "UNGROUNDED_EVIDENCE_BLOCKED")
        self.assertNotIn("Unsupported", result.explanation)
        self.assertTrue(result.evidence)

    def test_hallucinated_evidence_id_is_rejected(self) -> None:
        def hallucinate(context: CuratedReasoningContext) -> ReasoningDraft:
            return ReasoningDraft(
                selected_dataset_id=context.candidates[0].dataset_id,
                explanation="Unsupported explanation.",
                evidence_ids=["evidence:invented"],
            )

        service = ReasoningService(self.context_service, FakeProvider(hallucinate))
        with self.assertRaisesRegex(ReasoningPolicyError, "evidence outside"):
            service.select_dataset(
                "Finance recognized net revenue",
                mode=DiscoveryMode.DETERMINISTIC,
            )

    def test_evaluation_detects_incorrect_allowed_source_selection(self) -> None:
        def select_wrong_candidate(context: CuratedReasoningContext) -> ReasoningDraft:
            selected = next(
                candidate.dataset_id
                for candidate in context.candidates
                if candidate.dataset_id != "gold.finance_revenue"
            )
            evidence = next(
                item.id
                for item in context.evidence_catalog
                if item.source_id == selected and item.kind.value == "dataset_contract"
            )
            return ReasoningDraft(
                selected_dataset_id=selected,
                explanation="A valid candidate, but the wrong expected source.",
                evidence_ids=[evidence],
            )

        service = ReasoningService(self.context_service, FakeProvider(select_wrong_candidate))
        report = evaluate_reasoning(
            service,
            [
                ReasoningEvaluationCase(
                    id="finance",
                    question="Finance recognized net revenue",
                    expected_status=DiscoveryStatus.RESOLVED,
                    expected_dataset_id="gold.finance_revenue",
                )
            ],
            mode=DiscoveryMode.DETERMINISTIC,
        )
        self.assertEqual(report.metrics.status_accuracy, 1.0)
        self.assertEqual(report.metrics.selection_accuracy, 0.0)
        self.assertEqual(report.metrics.grounded_response_rate, 1.0)

    def test_openai_compatible_adapter_uses_strict_schema_and_parses_response(self) -> None:
        captured: dict[str, object] = {}

        def transport(
            url: str,
            headers: dict[str, str],
            body: bytes,
            timeout: float,
        ) -> bytes:
            captured.update(
                url=url,
                headers=headers,
                payload=json.loads(body),
                timeout=timeout,
            )
            draft = {
                "selected_dataset_id": "gold.finance_revenue",
                "explanation": "Grounded selection.",
                "evidence_ids": ["dataset:gold.finance_revenue:contract"],
                "clarification_question": None,
            }
            return json.dumps(
                {
                    "output": [
                        {
                            "type": "message",
                            "content": [
                                {"type": "output_text", "text": json.dumps(draft)}
                            ],
                        }
                    ]
                }
            ).encode()

        provider = OpenAIResponsesReasoningProvider(
            api_key="test-key",
            model="test-model",
            base_url="https://llm.example/v1/",
            transport=transport,
        )
        discovery = self.context_service.discover(
            "Finance recognized net revenue",
            mode=DiscoveryMode.DETERMINISTIC,
        )
        from ai_data_platform.reasoning import assemble_reasoning_context

        context = assemble_reasoning_context(
            "Finance recognized net revenue", discovery, self.registry
        )
        draft = provider.generate(context)

        self.assertEqual(draft.selected_dataset_id, "gold.finance_revenue")
        self.assertEqual(captured["url"], "https://llm.example/v1/responses")
        payload = captured["payload"]
        self.assertEqual(payload["text"]["format"]["type"], "json_schema")
        self.assertTrue(payload["text"]["format"]["strict"])
        self.assertFalse(payload["text"]["format"]["schema"]["additionalProperties"])

    def test_openai_compatible_adapter_rejects_malformed_structured_output(self) -> None:
        provider = OpenAIResponsesReasoningProvider(
            api_key="test-key",
            model="test-model",
            transport=lambda *_: json.dumps(
                {
                    "output": [
                        {
                            "content": [
                                {"type": "output_text", "text": '{"explanation": 7}'}
                            ]
                        }
                    ]
                }
            ).encode(),
        )
        discovery = self.context_service.discover(
            "Finance recognized net revenue",
            mode=DiscoveryMode.DETERMINISTIC,
        )
        from ai_data_platform.reasoning import assemble_reasoning_context

        context = assemble_reasoning_context(
            "Finance recognized net revenue", discovery, self.registry
        )
        with self.assertRaisesRegex(ReasoningProviderError, "invalid structured"):
            provider.generate(context)


class ReasoningApiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        directory = Path(self.temporary_directory.name)
        self.current = SQLiteRuntimeMetadataStore(directory / "runtime.sqlite")
        self.history = DuckDBRuntimeHistoryStore(directory / "history.duckdb")
        repository = RuntimeMetadataRepository(self.current, self.history)
        registry = load_registry(REGISTRY_PATH)
        index = build_vector_index(registry, HashingEmbeddingProvider())
        context_service = ContextService(registry, repository, index)
        reasoning_service = ReasoningService(context_service, FakeProvider(select_first))
        app = create_app(context_service, reasoning_service)
        self.client = AsyncClient(transport=ASGITransport(app=app), base_url="http://test")

    async def asyncTearDown(self) -> None:
        await self.client.aclose()
        self.current.close()
        self.history.close()
        self.temporary_directory.cleanup()

    async def test_reasoning_endpoint_returns_grounded_selection(self) -> None:
        response = await self.client.post(
            "/v1/reasoning/dataset-selection",
            json={
                "question": "Finance recognized net revenue",
                "mode": "deterministic",
                "limit": 5,
            },
        )
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["status"], "RESOLVED")
        self.assertEqual(body["selected_dataset_id"], "gold.finance_revenue")
        self.assertTrue(body["evidence"])

    async def test_reasoning_endpoint_rejects_more_than_five_candidates(self) -> None:
        response = await self.client.post(
            "/v1/reasoning/dataset-selection",
            json={"question": "revenue", "limit": 6},
        )
        self.assertEqual(response.status_code, 422)


if __name__ == "__main__":
    unittest.main()
