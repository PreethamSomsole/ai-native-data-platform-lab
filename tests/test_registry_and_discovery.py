from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path

from ai_data_platform import discover_datasets, load_registry, resolve_metric
from ai_data_platform.discovery import DiscoveryStatus
from ai_data_platform.validation import RegistryValidationError

PROJECT_ROOT = Path(__file__).resolve().parents[1]
REGISTRY_PATH = PROJECT_ROOT / "registry"


class RegistryAndDiscoveryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.registry = load_registry(REGISTRY_PATH)

    def test_ambiguous_revenue_requires_clarification(self) -> None:
        result = discover_datasets("What was net revenue last quarter?", self.registry)

        self.assertEqual(result.status, DiscoveryStatus.CLARIFICATION_REQUIRED)
        self.assertIsNotNone(result.ambiguity)
        conflicts = {match.metric_id for match in result.ambiguity.conflicting_metrics}
        self.assertEqual(
            conflicts,
            {"metric.finance_net_revenue", "metric.operations_net_revenue"},
        )
        self.assertLessEqual(len(result.candidates), 5)

    def test_finance_recognized_revenue_ranks_finance_dataset_first(self) -> None:
        result = discover_datasets("What was Finance recognized net revenue last quarter?", self.registry)

        self.assertEqual(result.status, DiscoveryStatus.RESOLVED)
        self.assertEqual(result.candidates[0].dataset_id, "gold.finance_revenue")
        signals = {reason.signal for reason in result.candidates[0].reasons}
        self.assertTrue(
            {"domain_match", "intended_use_case_match", "certification", "metric_compatibility"}
            <= signals
        )

    def test_operational_order_revenue_ranks_order_dataset_above_finance(self) -> None:
        result = discover_datasets(
            "Show operational order revenue by region.", self.registry, limit=10
        )

        ranks = {candidate.dataset_id: index for index, candidate in enumerate(result.candidates)}
        self.assertEqual(result.status, DiscoveryStatus.RESOLVED)
        self.assertLess(ranks["gold.order_revenue"], ranks["gold.finance_revenue"])

    def test_certified_gold_dataset_beats_similar_uncertified_silver_dataset(self) -> None:
        result = discover_datasets("What was Finance recognized net revenue?", self.registry)

        ranks = {candidate.dataset_id: index for index, candidate in enumerate(result.candidates)}
        self.assertLess(ranks["gold.finance_revenue"], ranks["silver.revenue_events"])

    def test_invalid_semantic_reference_fails_validation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            registry_copy = Path(temporary_directory) / "registry"
            shutil.copytree(REGISTRY_PATH, registry_copy)
            (registry_copy / "datasets" / "invalid_reference.yaml").write_text(
                """id: gold.invalid_reference\nname: Invalid reference\ndomain: finance\nlayer: gold\ndescription: Invalid test contract.\ngrain: day\ntarget_users: []\nsupported_use_cases: []\nprohibited_use_cases: []\ncertification: certified\nowner: test\nmetric_ids: [metric.does_not_exist]\nentity_ids: [entity.customer]\ndimension_concept_ids: [business_concept.region]\nfreshness_sla: daily\nrefresh_cadence: daily\ntrust_signals: []\n""",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(RegistryValidationError, "metric.does_not_exist"):
                load_registry(registry_copy)

    def test_duplicate_canonical_id_fails_validation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            registry_copy = Path(temporary_directory) / "registry"
            shutil.copytree(REGISTRY_PATH, registry_copy)
            (registry_copy / "metrics" / "duplicate.yaml").write_text(
                """id: metric.finance_net_revenue\nname: Duplicate Finance Revenue\ndomain: finance\ndefinition: Duplicate.\naliases: []\ntarget_users: []\nintended_use_cases: []\nprohibited_use_cases: []\nrelated_concept_ids: [business_concept.revenue]\nnot_equivalent_to_ids: []\n""",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(RegistryValidationError, "Duplicate metrics ID"):
                load_registry(registry_copy)

    def test_metric_resolution_is_deterministic(self) -> None:
        matches = resolve_metric("Finance recognized net revenue", self.registry)

        self.assertEqual(matches[0].metric_id, "metric.finance_net_revenue")

    def test_unrelated_question_returns_no_match(self) -> None:
        result = discover_datasets("What is the weather forecast for tomorrow?", self.registry)

        self.assertEqual(result.status, DiscoveryStatus.NO_MATCH)
        self.assertEqual(result.candidates, [])


if __name__ == "__main__":
    unittest.main()
