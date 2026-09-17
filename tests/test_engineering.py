"""Acceptance tests for the repository-scoped Milestone 6 DEV reference runner."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from ai_data_platform import load_registry
from ai_data_platform.engineering import (
    ApprovalRequirement,
    DeploymentRequest,
    EngineeringService,
    Environment,
    IngestionRequest,
    RestoreTableRequest,
    SafeReplaceRequest,
    SourceKind,
    ToolStatus,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]


class EngineeringServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.service = EngineeringService(
            load_registry(PROJECT_ROOT / "registry"),
            PROJECT_ROOT,
            Path(self.temporary_directory.name) / "state",
        )

    def test_recommendation_and_plan_apply_deterministic_governance(self) -> None:
        recommendation = self.service.recommend_ingestion_pattern(
            IngestionRequest(
                source_kind=SourceKind.DATABASE,
                change_data_available=True,
                expected_latency_minutes=15,
            )
        )
        self.assertEqual(recommendation.pattern, "log_based_cdc")

        from ai_data_platform.engineering import PipelinePlanRequest

        plan = self.service.generate_pipeline_plan(
            PipelinePlanRequest(
                pipeline_name="finance-revenue",
                source_kind=SourceKind.DATABASE,
                dataset_id="gold.finance_revenue",
                changes_dataset_contract=True,
            )
        )
        self.assertEqual(plan.approval_requirement, ApprovalRequirement.REQUIRED)
        self.assertIn("dataset contract changes require approval", plan.approval_reasons)

    def test_contract_validation_rejects_unknown_semantic_reference(self) -> None:
        dataset = self.service.registry.datasets["gold.finance_revenue"].model_copy(
            update={
                "id": "gold.proposed_finance_revenue",
                "metric_ids": ["metric.unknown"],
            }
        )
        result = self.service.validate_dataset_contract(dataset)
        self.assertFalse(result.valid)
        self.assertIn(
            "gold.proposed_finance_revenue references unknown metric 'metric.unknown'",
            result.errors,
        )

    def test_reconciliation_detects_difference(self) -> None:
        connection = self.service._connect()
        try:
            connection.execute("CREATE TABLE source_data (id INTEGER, amount INTEGER)")
            connection.execute("CREATE TABLE target_data (id INTEGER, amount INTEGER)")
            connection.execute("INSERT INTO source_data VALUES (1, 10), (2, 20)")
            connection.execute("INSERT INTO target_data VALUES (1, 10), (2, 25)")
        finally:
            connection.close()
        result = self.service.run_reconciliation("source_data", "target_data")
        self.assertFalse(result.matched)
        self.assertEqual(result.status, ToolStatus.FAILED)
        self.assertEqual(result.source_only_row_count, 1)
        self.assertEqual(result.target_only_row_count, 1)

    def test_safe_replace_is_atomic_and_has_recoverable_rollback(self) -> None:
        connection = self.service._connect()
        try:
            connection.execute("CREATE TABLE gold_table (id INTEGER)")
            connection.execute("CREATE TABLE replacement_table (id INTEGER)")
            connection.execute("INSERT INTO gold_table VALUES (1)")
            connection.execute("INSERT INTO replacement_table VALUES (2), (3)")
        finally:
            connection.close()

        replaced = self.service.safe_replace_table(
            SafeReplaceRequest(target_table="gold_table", replacement_table="replacement_table")
        )
        self.assertEqual(replaced.status, ToolStatus.SUCCEEDED)
        self.assertIsNotNone(replaced.backup_table)
        self.assertEqual(replaced.rows_replaced, 2)

        restored = self.service.restore_table_from_backup(
            RestoreTableRequest(target_table="gold_table", backup_table=replaced.backup_table or "")
        )
        self.assertEqual(restored.status, ToolStatus.SUCCEEDED)
        connection = self.service._connect()
        try:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM gold_table").fetchone()[0], 1)
            self.assertEqual(connection.execute("SELECT id FROM gold_table").fetchone()[0], 1)
        finally:
            connection.close()

    def test_non_dev_table_replacement_is_not_executable_even_with_approval(self) -> None:
        result = self.service.safe_replace_table(
            SafeReplaceRequest(
                target_table="gold_table",
                replacement_table="replacement_table",
                environment=Environment.PRODUCTION,
                approval_granted=True,
            )
        )
        self.assertEqual(result.status, ToolStatus.APPROVAL_REQUIRED)
        self.assertEqual(result.approval_requirement, ApprovalRequirement.REQUIRED)

    def test_server_configuration_can_disable_development_writes(self) -> None:
        disabled_service = EngineeringService(
            self.service.registry,
            PROJECT_ROOT,
            Path(self.temporary_directory.name) / "disabled-state",
            allow_development_writes=False,
        )
        result = disabled_service.safe_replace_table(
            SafeReplaceRequest(
                target_table="gold_table",
                replacement_table="replacement_table",
                approval_granted=True,
            )
        )
        self.assertEqual(result.status, ToolStatus.APPROVAL_REQUIRED)

    def test_registry_validation_and_repository_scoped_deployment(self) -> None:
        validation = self.service.run_dev_validation("registry")
        self.assertEqual(validation.status, ToolStatus.SUCCEEDED, validation.output)
        deployment = self.service.deploy_pipeline_to_dev(
            DeploymentRequest(
                artifact_path="src/ai_data_platform/api.py",
                validation_profile="registry",
            )
        )
        self.assertEqual(deployment.status, ToolStatus.SUCCEEDED, deployment.message)
        self.assertTrue(Path(deployment.manifest_path or "").is_file())

    def test_non_dev_deployment_cannot_bypass_the_environment_boundary(self) -> None:
        deployment = self.service.deploy_pipeline_to_dev(
            DeploymentRequest(
                artifact_path="src/ai_data_platform/api.py",
                environment=Environment.PRODUCTION,
                approval_granted=True,
            )
        )
        self.assertEqual(deployment.status, ToolStatus.APPROVAL_REQUIRED)


if __name__ == "__main__":
    unittest.main()
