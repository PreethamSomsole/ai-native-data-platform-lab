from __future__ import annotations

import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path

from pydantic import ValidationError

from ai_data_platform import discover_datasets_with_runtime, load_registry
from ai_data_platform.context import ContextService, DiscoveryMode
from ai_data_platform.discovery import DiscoveryStatus
from ai_data_platform.runtime import (
    DatasetRuntimeMetadata,
    DuckDBRuntimeHistoryStore,
    FreshnessStatus,
    OperationalHealth,
    QualityStatus,
    RuntimeMetadataRepository,
    SQLiteRuntimeMetadataStore,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]
REGISTRY_PATH = PROJECT_ROOT / "registry"


def observation(
    dataset_id: str,
    observed_at: datetime,
    *,
    row_count: int = 100,
    freshness: FreshnessStatus = FreshnessStatus.FRESH,
    quality: QualityStatus = QualityStatus.PASSING,
    health: OperationalHealth = OperationalHealth.HEALTHY,
    usage_count_30d: int = 40,
) -> DatasetRuntimeMetadata:
    return DatasetRuntimeMetadata(
        dataset_id=dataset_id,
        observed_at=observed_at,
        source="test-pipeline",
        last_successful_run_at=observed_at - timedelta(minutes=5),
        freshness_status=freshness,
        quality_status=quality,
        quality_checks_passed=10,
        quality_checks_failed=0 if quality is not QualityStatus.FAILING else 2,
        row_count=row_count,
        usage_count_30d=usage_count_30d,
        operational_health=health,
    )


class RuntimeContextTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.directory = Path(self.temporary_directory.name)
        self.current = SQLiteRuntimeMetadataStore(self.directory / "runtime.sqlite")
        self.history = DuckDBRuntimeHistoryStore(self.directory / "history.duckdb")
        self.repository = RuntimeMetadataRepository(self.current, self.history)
        self.registry = load_registry(REGISTRY_PATH)

    def tearDown(self) -> None:
        self.current.close()
        self.history.close()
        self.temporary_directory.cleanup()

    def test_runtime_timestamps_must_be_timezone_aware(self) -> None:
        with self.assertRaisesRegex(ValidationError, "timezone"):
            observation("gold.finance_revenue", datetime(2026, 9, 17, 12, 0))  # noqa: DTZ001

    def test_sqlite_keeps_newest_snapshot_when_observations_arrive_out_of_order(self) -> None:
        newer = observation(
            "gold.finance_revenue", datetime(2026, 9, 17, 12, 0, tzinfo=UTC)
        )
        older = observation(
            "gold.finance_revenue",
            datetime(2026, 9, 17, 11, 0, tzinfo=UTC),
            row_count=50,
        )
        self.current.upsert_latest(newer)
        self.current.upsert_latest(older)

        latest = self.current.get_latest("gold.finance_revenue")
        self.assertIsNotNone(latest)
        self.assertEqual(latest.observed_at, newer.observed_at)
        self.assertEqual(latest.row_count, newer.row_count)

    def test_duckdb_retains_history_and_returns_ascending_row_count_trend(self) -> None:
        first = observation(
            "gold.order_revenue",
            datetime(2026, 9, 17, 10, 0, tzinfo=UTC),
            row_count=100,
        )
        second = observation(
            "gold.order_revenue",
            datetime(2026, 9, 17, 11, 0, tzinfo=UTC),
            row_count=125,
            freshness=FreshnessStatus.STALE,
            quality=QualityStatus.FAILING,
            health=OperationalHealth.FAILED,
        )
        self.history.append(first)
        self.history.append(second)

        history = self.history.list_history("gold.order_revenue")
        trend = self.history.row_count_trend("gold.order_revenue")
        summary = self.history.summarize("gold.order_revenue")
        self.assertEqual([item.observed_at for item in history], [second.observed_at, first.observed_at])
        self.assertEqual([item.row_count for item in trend], [100, 125])
        self.assertEqual(summary.observation_count, 2)
        self.assertEqual(summary.fresh_observation_count, 1)
        self.assertEqual(summary.stale_observation_count, 1)
        self.assertEqual(summary.failing_quality_count, 1)
        self.assertEqual(summary.average_row_count, 112.5)

    def test_sqlite_and_duckdb_state_survive_reopening(self) -> None:
        metadata = observation(
            "gold.finance_revenue",
            datetime(2026, 9, 17, 12, 0, tzinfo=UTC),
            row_count=750,
        )
        self.repository.record(metadata)
        self.current.close()
        self.history.close()

        self.current = SQLiteRuntimeMetadataStore(self.directory / "runtime.sqlite")
        self.history = DuckDBRuntimeHistoryStore(self.directory / "history.duckdb")
        self.repository = RuntimeMetadataRepository(self.current, self.history)

        self.assertEqual(self.repository.get_latest(metadata.dataset_id), metadata)
        self.assertEqual(self.repository.list_history(metadata.dataset_id), [metadata])

    def test_runtime_reranking_is_explained_and_preserves_semantic_ambiguity(self) -> None:
        observed_at = datetime(2026, 9, 17, 12, 0, tzinfo=UTC)
        self.repository.record(observation("gold.finance_revenue", observed_at))
        self.repository.record(
            observation(
                "gold.order_revenue",
                observed_at,
                freshness=FreshnessStatus.STALE,
                quality=QualityStatus.FAILING,
                health=OperationalHealth.FAILED,
                usage_count_30d=0,
            )
        )

        result = discover_datasets_with_runtime(
            "What was net revenue last quarter?",
            self.registry,
            self.repository,
            limit=10,
        )

        self.assertEqual(result.status, DiscoveryStatus.CLARIFICATION_REQUIRED)
        self.assertIsNotNone(result.ambiguity)
        self.assertEqual(result.candidates[0].dataset_id, "gold.finance_revenue")
        reasons = {
            candidate.dataset_id: {reason.signal: reason.points for reason in candidate.reasons}
            for candidate in result.candidates
        }
        self.assertEqual(reasons["gold.finance_revenue"]["runtime_quality"], 10)
        self.assertEqual(reasons["gold.order_revenue"]["runtime_quality"], -20)

    def test_context_combines_declarative_runtime_and_trend_data(self) -> None:
        observed_at = datetime(2026, 9, 17, 12, 0, tzinfo=UTC)
        self.repository.record(observation("gold.finance_revenue", observed_at, row_count=500))
        service = ContextService(self.registry, self.repository)

        context = service.get_dataset_context("gold.finance_revenue")
        discovery = service.discover(
            "Finance recognized net revenue", mode=DiscoveryMode.DETERMINISTIC
        )

        self.assertEqual(context.dataset.id, "gold.finance_revenue")
        self.assertEqual(context.runtime.row_count, 500)
        self.assertEqual(context.row_count_trend[0].row_count, 500)
        self.assertEqual(context.runtime_summary.observation_count, 1)
        self.assertEqual(context.runtime_summary.average_row_count, 500.0)
        self.assertEqual(context.metrics[0].id, "metric.finance_net_revenue")
        self.assertEqual(discovery.candidates[0].dataset.id, "gold.finance_revenue")
        self.assertIsNotNone(discovery.candidates[0].runtime)

    def test_context_rejects_runtime_for_unknown_dataset(self) -> None:
        service = ContextService(self.registry, self.repository)
        with self.assertRaises(KeyError):
            service.record_runtime(
                observation(
                    "gold.unknown",
                    datetime(2026, 9, 17, 12, 0, tzinfo=UTC),
                )
            )


if __name__ == "__main__":
    unittest.main()
