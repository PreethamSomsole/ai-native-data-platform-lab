from __future__ import annotations

import tempfile
import unittest
from collections.abc import Sequence
from pathlib import Path

from ai_data_platform import (
    build_vector_index,
    discover_datasets_hybrid,
    load_registry,
)
from ai_data_platform.discovery import DiscoveryStatus
from ai_data_platform.embeddings import (
    Embedding,
    HashingEmbeddingProvider,
    SemanticDocumentKind,
    build_semantic_documents,
)
from ai_data_platform.evaluation import evaluate_retrieval, load_evaluation_cases

PROJECT_ROOT = Path(__file__).resolve().parents[1]
REGISTRY_PATH = PROJECT_ROOT / "registry"


class ControlledEmbeddingProvider:
    """Make one vector-only query resolve to the product-performance dataset."""

    @property
    def provider_id(self) -> str:
        return "controlled-test-v1"

    def embed(self, texts: Sequence[str]) -> list[Embedding]:
        return [
            (1.0, 0.0)
            if text == "north star signal" or "id: gold.product_performance" in text
            else (0.0, 1.0)
            for text in texts
        ]


class InvalidEmbeddingProvider:
    @property
    def provider_id(self) -> str:
        return "invalid-test-v1"

    def embed(self, texts: Sequence[str]) -> list[Embedding]:
        return [(1.0,)] if texts else []


class HybridRetrievalTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.registry = load_registry(REGISTRY_PATH)
        cls.provider = HashingEmbeddingProvider(dimension=256)
        cls.index = build_vector_index(cls.registry, cls.provider)

    def test_semantic_documents_cover_entire_registry(self) -> None:
        documents = build_semantic_documents(self.registry)
        expected_count = sum(
            len(records)
            for records in (
                self.registry.concepts,
                self.registry.entities,
                self.registry.metrics,
                self.registry.datasets,
            )
        )
        self.assertEqual(len(documents), expected_count)
        self.assertEqual(
            {document.kind for document in documents},
            set(SemanticDocumentKind),
        )
        self.assertEqual(len({document.document_id for document in documents}), len(documents))

    def test_hashing_provider_is_reproducible(self) -> None:
        first = self.provider.embed(["recognized net revenue"])
        second = self.provider.embed(["recognized net revenue"])
        self.assertEqual(first, second)
        self.assertEqual(len(first[0]), 256)

    def test_vector_index_returns_ranked_semantic_documents(self) -> None:
        matches = self.index.search("finance recognized revenue", limit=5)
        self.assertTrue(matches)
        self.assertEqual([match.rank for match in matches], list(range(1, len(matches) + 1)))
        self.assertGreaterEqual(matches[0].similarity, matches[-1].similarity)

    def test_hybrid_preserves_ambiguous_revenue_guardrail(self) -> None:
        result = discover_datasets_hybrid(
            "What was net revenue last quarter?", self.registry, self.index
        )
        self.assertEqual(result.status, DiscoveryStatus.CLARIFICATION_REQUIRED)
        self.assertIsNotNone(result.ambiguity)

    def test_hybrid_preserves_prohibited_use_exclusion(self) -> None:
        result = discover_datasets_hybrid(
            "Show net revenue for external financial reporting",
            self.registry,
            self.index,
            limit=10,
        )
        candidate_ids = {candidate.dataset_id for candidate in result.candidates}
        excluded_ids = {item.dataset_id for item in result.excluded_candidates}
        self.assertNotIn("gold.order_revenue", candidate_ids)
        self.assertIn("gold.order_revenue", excluded_ids)

    def test_hybrid_unrelated_question_returns_no_match(self) -> None:
        result = discover_datasets_hybrid(
            "What is the weather forecast for tomorrow?", self.registry, self.index
        )
        self.assertEqual(result.status, DiscoveryStatus.NO_MATCH)
        self.assertEqual(result.candidates, [])

    def test_vector_only_candidate_can_enter_hybrid_result(self) -> None:
        index = build_vector_index(self.registry, ControlledEmbeddingProvider())
        result = discover_datasets_hybrid(
            "north star signal",
            self.registry,
            index,
            min_similarity=0.9,
        )
        self.assertEqual(result.status, DiscoveryStatus.RESOLVED)
        self.assertEqual(result.candidates[0].dataset_id, "gold.product_performance")
        signals = {reason.signal for reason in result.candidates[0].reasons}
        self.assertIn("vector_rrf", signals)

    def test_vector_index_rejects_wrong_embedding_count(self) -> None:
        with self.assertRaisesRegex(ValueError, "vectors"):
            build_vector_index(self.registry, InvalidEmbeddingProvider())

    def test_evaluation_compares_baseline_and_hybrid(self) -> None:
        cases = load_evaluation_cases(PROJECT_ROOT / "evaluation" / "cases.yaml")
        report = evaluate_retrieval(self.registry, cases, self.index, k=5)
        self.assertEqual(report.deterministic.case_count, len(cases))
        self.assertEqual(report.hybrid.case_count, len(cases))
        self.assertEqual(report.provider_id, self.provider.provider_id)
        self.assertGreaterEqual(report.deterministic.recall_at_k, 0.0)
        self.assertLessEqual(report.hybrid.recall_at_k, 1.0)

    def test_invalid_evaluation_corpus_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "cases.yaml"
            path.write_text("not: a-list\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "invalid evaluation corpus"):
                load_evaluation_cases(path)


if __name__ == "__main__":
    unittest.main()
