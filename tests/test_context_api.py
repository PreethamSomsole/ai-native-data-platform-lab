from __future__ import annotations

import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path

from httpx import ASGITransport, AsyncClient

from ai_data_platform import build_vector_index, load_registry
from ai_data_platform.context import ContextService
from ai_data_platform.embeddings import HashingEmbeddingProvider
from ai_data_platform.http import create_app
from ai_data_platform.runtime import (
    DuckDBRuntimeHistoryStore,
    RuntimeMetadataRepository,
    SQLiteRuntimeMetadataStore,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]
REGISTRY_PATH = PROJECT_ROOT / "registry"


class ContextApiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        directory = Path(self.temporary_directory.name)
        self.current = SQLiteRuntimeMetadataStore(directory / "runtime.sqlite")
        self.history = DuckDBRuntimeHistoryStore(directory / "history.duckdb")
        repository = RuntimeMetadataRepository(self.current, self.history)
        registry = load_registry(REGISTRY_PATH)
        vector_index = build_vector_index(registry, HashingEmbeddingProvider())
        app = create_app(ContextService(registry, repository, vector_index))
        self.client = AsyncClient(transport=ASGITransport(app=app), base_url="http://test")

    async def asyncTearDown(self) -> None:
        await self.client.aclose()
        self.current.close()
        self.history.close()
        self.temporary_directory.cleanup()

    @staticmethod
    def _payload(dataset_id: str = "gold.finance_revenue") -> dict[str, object]:
        return {
            "dataset_id": dataset_id,
            "observed_at": datetime(2026, 9, 17, 12, 0, tzinfo=UTC).isoformat(),
            "source": "api-test",
            "last_successful_run_at": datetime(
                2026, 9, 17, 11, 55, tzinfo=UTC
            ).isoformat(),
            "freshness_status": "fresh",
            "quality_status": "passing",
            "quality_checks_passed": 12,
            "quality_checks_failed": 0,
            "row_count": 1_250,
            "usage_count_30d": 120,
            "operational_health": "healthy",
        }

    async def test_runtime_ingestion_context_discovery_and_history_flow(self) -> None:
        self.assertEqual((await self.client.get("/health")).json(), {"status": "ok"})

        created = await self.client.post("/v1/runtime/observations", json=self._payload())
        self.assertEqual(created.status_code, 201)

        context = await self.client.get("/v1/datasets/gold.finance_revenue/context")
        self.assertEqual(context.status_code, 200)
        self.assertEqual(context.json()["runtime"]["row_count"], 1_250)
        self.assertEqual(context.json()["row_count_trend"][0]["row_count"], 1_250)
        self.assertEqual(context.json()["runtime_summary"]["observation_count"], 1)

        discovery = await self.client.post(
            "/v1/discovery",
            json={
                "question": "Finance recognized net revenue",
                "mode": "hybrid",
                "limit": 5,
            },
        )
        self.assertEqual(discovery.status_code, 200)
        first = discovery.json()["candidates"][0]
        self.assertEqual(first["dataset"]["id"], "gold.finance_revenue")
        self.assertEqual(first["runtime"]["quality_status"], "passing")
        signals = {item["signal"] for item in first["candidate"]["reasons"]}
        self.assertIn("runtime_quality", signals)

        history = await self.client.get(
            "/v1/datasets/gold.finance_revenue/runtime/history?limit=10"
        )
        self.assertEqual(history.status_code, 200)
        self.assertEqual(len(history.json()), 1)

    async def test_unknown_dataset_and_invalid_runtime_payload_return_client_errors(self) -> None:
        unknown = await self.client.post(
            "/v1/runtime/observations", json=self._payload("gold.unknown")
        )
        self.assertEqual(unknown.status_code, 404)

        invalid = self._payload()
        invalid["observed_at"] = "2026-09-17T12:00:00"
        response = await self.client.post("/v1/runtime/observations", json=invalid)
        self.assertEqual(response.status_code, 422)

        missing = await self.client.get("/v1/datasets/gold.unknown/context")
        self.assertEqual(missing.status_code, 404)


if __name__ == "__main__":
    unittest.main()
