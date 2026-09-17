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

    def _registry_copy(self, temporary_directory: str) -> Path:
        registry_copy = Path(temporary_directory) / "registry"
        shutil.copytree(REGISTRY_PATH, registry_copy)
        return registry_copy

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
        result = discover_datasets(
            "What was Finance recognized net revenue last quarter?", self.registry
        )

        self.assertEqual(result.status, DiscoveryStatus.RESOLVED)
        self.assertEqual(result.candidates[0].dataset_id, "gold.finance_revenue")
        signals = {reason.signal for reason in result.candidates[0].reasons}
        self.assertTrue(
            {"domain_match", "intended_use_case_match", "certification", "metric_compatibility"}
            <= signals
        )

    def test_operational_order_revenue_excludes_finance_dataset(self) -> None:
        result = discover_datasets(
            "Show operational order revenue by region.", self.registry, limit=10
        )

        candidate_ids = {candidate.dataset_id for candidate in result.candidates}
        excluded = {item.dataset_id: item.reasons for item in result.excluded_candidates}
        self.assertEqual(result.status, DiscoveryStatus.RESOLVED)
        self.assertIn("gold.order_revenue", candidate_ids)
        self.assertNotIn("gold.finance_revenue", candidate_ids)
        self.assertIn("gold.finance_revenue", excluded)

    def test_certified_gold_dataset_beats_similar_uncertified_silver_dataset(self) -> None:
        result = discover_datasets("What was Finance recognized net revenue?", self.registry)

        ranks = {candidate.dataset_id: index for index, candidate in enumerate(result.candidates)}
        self.assertLess(ranks["gold.finance_revenue"], ranks["silver.revenue_events"])

    def test_prohibited_use_case_excludes_operations_for_external_reporting(self) -> None:
        result = discover_datasets(
            "Show net revenue for external financial reporting", self.registry, limit=10
        )

        candidate_ids = {candidate.dataset_id for candidate in result.candidates}
        excluded = {item.dataset_id: item.reasons for item in result.excluded_candidates}
        self.assertIn("gold.finance_revenue", candidate_ids)
        self.assertNotIn("gold.order_revenue", candidate_ids)
        self.assertIn("gold.order_revenue", excluded)
        self.assertTrue(
            any(
                "external financial reporting" in reason
                for reason in excluded["gold.order_revenue"]
            )
        )

    def test_excluded_metric_does_not_create_false_ambiguity(self) -> None:
        result = discover_datasets(
            "Show net revenue for accounting recognition", self.registry, limit=10
        )

        excluded_ids = {item.dataset_id for item in result.excluded_candidates}
        self.assertEqual(result.status, DiscoveryStatus.RESOLVED)
        self.assertIsNone(result.ambiguity)
        self.assertIsNotNone(result.resolved_metric)
        self.assertEqual(result.resolved_metric.metric_id, "metric.finance_net_revenue")
        self.assertIn("gold.order_revenue", excluded_ids)

    def test_weak_incidental_metric_terms_do_not_create_false_ambiguity(self) -> None:
        result = discover_datasets("Find the certified customer financial summary", self.registry)

        self.assertEqual(result.status, DiscoveryStatus.RESOLVED)
        self.assertEqual(result.candidates[0].dataset_id, "gold.customer_financial_summary")
        self.assertIsNone(result.ambiguity)

    def test_broad_revenue_query_keeps_non_equivalent_metrics_ambiguous(self) -> None:
        result = discover_datasets("revenue", self.registry)

        self.assertEqual(result.status, DiscoveryStatus.CLARIFICATION_REQUIRED)
        self.assertIsNotNone(result.ambiguity)
        conflicts = {match.metric_id for match in result.ambiguity.conflicting_metrics}
        self.assertEqual(
            conflicts,
            {
                "metric.finance_net_revenue",
                "metric.operations_net_revenue",
                "metric.sales_bookings",
            },
        )

    def test_single_term_prohibited_use_case_is_enforced(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            registry_copy = self._registry_copy(temporary_directory)
            dataset = registry_copy / "datasets" / "gold_order_revenue.yaml"
            dataset.write_text(
                dataset.read_text(encoding="utf-8").replace(
                    "prohibited_use_cases: [external financial reporting, accounting recognition]",
                    "prohibited_use_cases: [external financial reporting, accounting recognition, forecasting]",
                ),
                encoding="utf-8",
            )
            registry = load_registry(registry_copy)

            result = discover_datasets(
                "Show forecasting for operational order revenue", registry, limit=10
            )

            candidate_ids = {candidate.dataset_id for candidate in result.candidates}
            excluded = {item.dataset_id: item.reasons for item in result.excluded_candidates}
            self.assertNotIn("gold.order_revenue", candidate_ids)
            self.assertIn("gold.order_revenue", excluded)
            self.assertTrue(
                any("forecasting" in reason for reason in excluded["gold.order_revenue"])
            )

    def test_declared_freshness_can_contribute_to_ranking(self) -> None:
        result = discover_datasets("Show hourly operational order revenue", self.registry, limit=10)

        order_candidate = next(
            candidate
            for candidate in result.candidates
            if candidate.dataset_id == "gold.order_revenue"
        )
        signals = {reason.signal for reason in order_candidate.reasons}
        self.assertIn("freshness_expectation_match", signals)

    def test_invalid_semantic_reference_fails_validation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            registry_copy = self._registry_copy(temporary_directory)
            (registry_copy / "datasets" / "invalid_reference.yaml").write_text(
                """id: gold.invalid_reference\nname: Invalid reference\ndomain: finance\nlayer: gold\ndescription: Invalid test contract.\ngrain: day\ntarget_users: []\nsupported_use_cases: []\nprohibited_use_cases: []\ncertification: certified\nowner: test\nmetric_ids: [metric.does_not_exist]\nentity_ids: [entity.customer]\ndimension_concept_ids: [business_concept.region]\nfreshness_sla: daily\nrefresh_cadence: daily\ntrust_signals: []\n""",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(RegistryValidationError, "metric.does_not_exist"):
                load_registry(registry_copy)

    def test_unknown_entity_reference_fails_validation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            registry_copy = self._registry_copy(temporary_directory)
            dataset = registry_copy / "datasets" / "gold_finance_revenue.yaml"
            dataset.write_text(
                dataset.read_text(encoding="utf-8").replace(
                    "entity.customer, entity.product", "entity.unknown, entity.product"
                ),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(RegistryValidationError, "entity.unknown"):
                load_registry(registry_copy)

    def test_unknown_concept_reference_fails_validation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            registry_copy = self._registry_copy(temporary_directory)
            metric = registry_copy / "metrics" / "finance_net_revenue.yaml"
            metric.write_text(
                metric.read_text(encoding="utf-8").replace(
                    "business_concept.revenue", "business_concept.unknown"
                ),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(RegistryValidationError, "business_concept.unknown"):
                load_registry(registry_copy)

    def test_unknown_non_equivalent_metric_fails_validation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            registry_copy = self._registry_copy(temporary_directory)
            metric = registry_copy / "metrics" / "finance_net_revenue.yaml"
            metric.write_text(
                metric.read_text(encoding="utf-8").replace(
                    "metric.operations_net_revenue", "metric.unknown"
                ),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(RegistryValidationError, "metric.unknown"):
                load_registry(registry_copy)

    def test_self_non_equivalence_fails_validation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            registry_copy = self._registry_copy(temporary_directory)
            metric = registry_copy / "metrics" / "finance_net_revenue.yaml"
            text = metric.read_text(encoding="utf-8")
            text = text.replace(
                "not_equivalent_to_ids: [metric.operations_net_revenue, metric.sales_bookings]",
                "not_equivalent_to_ids: [metric.finance_net_revenue, metric.operations_net_revenue, metric.sales_bookings]",
            )
            metric.write_text(text, encoding="utf-8")
            with self.assertRaisesRegex(
                RegistryValidationError, "cannot be marked non-equivalent to itself"
            ):
                load_registry(registry_copy)

    def test_asymmetric_non_equivalence_fails_validation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            registry_copy = self._registry_copy(temporary_directory)
            metric = registry_copy / "metrics" / "operations_net_revenue.yaml"
            text = metric.read_text(encoding="utf-8")
            text = text.replace(
                "not_equivalent_to_ids: [metric.finance_net_revenue, metric.sales_bookings]",
                "not_equivalent_to_ids: [metric.sales_bookings]",
            )
            metric.write_text(text, encoding="utf-8")
            with self.assertRaisesRegex(
                RegistryValidationError, "Non-equivalence must be symmetric"
            ):
                load_registry(registry_copy)

    def test_duplicate_canonical_id_fails_validation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            registry_copy = self._registry_copy(temporary_directory)
            (registry_copy / "metrics" / "duplicate.yaml").write_text(
                """id: metric.finance_net_revenue\nname: Duplicate Finance Revenue\ndomain: finance\nowner: test\ndefinition: Duplicate.\naliases: []\ntarget_users: []\nintended_use_cases: []\nprohibited_use_cases: []\nrelated_concept_ids: [business_concept.revenue]\nnot_equivalent_to_ids: []\n""",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(RegistryValidationError, "Duplicate metrics ID"):
                load_registry(registry_copy)

    def test_extra_yaml_field_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            registry_copy = self._registry_copy(temporary_directory)
            metric = registry_copy / "metrics" / "finance_net_revenue.yaml"
            metric.write_text(
                metric.read_text(encoding="utf-8") + "unexpected_field: true\n",
                encoding="utf-8",
            )
            with self.assertRaises(RegistryValidationError):
                load_registry(registry_copy)

    def test_malformed_yaml_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            registry_copy = self._registry_copy(temporary_directory)
            (registry_copy / "metrics" / "malformed.yaml").write_text(
                "id: [unterminated\n", encoding="utf-8"
            )
            with self.assertRaises(RegistryValidationError):
                load_registry(registry_copy)

    def test_missing_registry_directory_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            registry_copy = self._registry_copy(temporary_directory)
            shutil.rmtree(registry_copy / "entities")
            with self.assertRaisesRegex(RegistryValidationError, "Missing registry directory"):
                load_registry(registry_copy)

    def test_metric_resolution_is_deterministic(self) -> None:
        matches = resolve_metric("Finance recognized net revenue", self.registry)
        self.assertEqual(matches[0].metric_id, "metric.finance_net_revenue")

    def test_unrelated_question_returns_no_match(self) -> None:
        result = discover_datasets("What is the weather forecast for tomorrow?", self.registry)
        self.assertEqual(result.status, DiscoveryStatus.NO_MATCH)
        self.assertEqual(result.candidates, [])

    def test_limit_must_be_positive(self) -> None:
        with self.assertRaisesRegex(ValueError, "limit must be at least 1"):
            discover_datasets("finance revenue", self.registry, limit=0)

    def test_default_result_is_capped_at_five_candidates(self) -> None:
        result = discover_datasets("revenue customer product region", self.registry)
        self.assertLessEqual(len(result.candidates), 5)

    def test_candidate_ordering_is_deterministic(self) -> None:
        first = discover_datasets("revenue customer product region", self.registry, limit=10)
        second = discover_datasets("revenue customer product region", self.registry, limit=10)
        self.assertEqual(first.candidates, second.candidates)


if __name__ == "__main__":
    unittest.main()
