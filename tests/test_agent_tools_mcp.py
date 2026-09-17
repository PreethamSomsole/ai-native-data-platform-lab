from __future__ import annotations

import os
import shutil
import sys
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path

from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client
from mcp.server.mcpserver.exceptions import ToolError

from ai_data_platform import build_vector_index, load_registry
from ai_data_platform.agent_tools import AgentToolError, AgentToolService
from ai_data_platform.context import ContextService, DiscoveryMode
from ai_data_platform.discovery import DiscoveryStatus
from ai_data_platform.embeddings import HashingEmbeddingProvider
from ai_data_platform.mcp import create_mcp_server, create_mcp_server_from_paths
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


class AgentToolServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        directory = Path(self.temporary_directory.name)
        self.current = SQLiteRuntimeMetadataStore(directory / "runtime.sqlite")
        self.history = DuckDBRuntimeHistoryStore(directory / "history.duckdb")
        repository = RuntimeMetadataRepository(self.current, self.history)
        self.registry = load_registry(REGISTRY_PATH)
        index = build_vector_index(self.registry, HashingEmbeddingProvider())
        self.context_service = ContextService(self.registry, repository, index)
        self.service = AgentToolService(self.context_service, REGISTRY_PATH)

    def tearDown(self) -> None:
        self.current.close()
        self.history.close()
        self.temporary_directory.cleanup()

    def test_discovery_preserves_ambiguity_and_candidate_bound(self) -> None:
        result = self.service.discover_datasets(
            "What was net revenue last quarter?",
            mode=DiscoveryMode.HYBRID,
            limit=3,
        )
        self.assertEqual(
            result.context.discovery.status,
            DiscoveryStatus.CLARIFICATION_REQUIRED,
        )
        self.assertLessEqual(len(result.context.candidates), 3)
        self.assertIsNotNone(result.context.discovery.ambiguity)

    def test_definition_tools_return_canonical_objects_and_stable_not_found(self) -> None:
        metric = self.service.get_metric_definition("metric.finance_net_revenue")
        self.assertEqual(metric.metric.owner, "finance analytics")
        dataset = self.service.get_dataset_contract("gold.finance_revenue")
        self.assertIn("metric.finance_net_revenue", dataset.dataset.metric_ids)
        entity = self.service.get_entity_definition("entity.customer")
        self.assertEqual(entity.entity.name, "Customer")
        concept = self.service.get_concept_definition("business_concept.revenue")
        self.assertEqual(concept.concept.name, "Revenue")

        with self.assertRaisesRegex(AgentToolError, "NOT_FOUND") as caught:
            self.service.get_dataset_contract("gold.unknown")
        self.assertEqual(caught.exception.code, "NOT_FOUND")

    def test_dataset_context_includes_runtime_state(self) -> None:
        self.context_service.record_runtime(
            DatasetRuntimeMetadata(
                dataset_id="gold.finance_revenue",
                observed_at=datetime(2026, 9, 17, 16, 0, tzinfo=UTC),
                source="mcp-test",
                freshness_status=FreshnessStatus.FRESH,
                quality_status=QualityStatus.PASSING,
                row_count=1_500,
                operational_health=OperationalHealth.HEALTHY,
            )
        )
        result = self.service.get_dataset_context("gold.finance_revenue")
        self.assertEqual(result.context.runtime.row_count, 1_500)
        self.assertEqual(result.context.row_count_trend[0].row_count, 1_500)

    def test_validation_reloads_configured_registry_and_reports_errors(self) -> None:
        self.assertTrue(self.service.validate_metadata().valid)

        invalid_root = Path(self.temporary_directory.name) / "invalid-registry"
        shutil.copytree(REGISTRY_PATH, invalid_root)
        dataset_path = invalid_root / "datasets" / "gold_finance_revenue.yaml"
        dataset_path.write_text(
            dataset_path.read_text(encoding="utf-8")
            + "\nunknown_milestone_5_field: true\n",
            encoding="utf-8",
        )
        invalid_service = AgentToolService(self.context_service, invalid_root)
        result = invalid_service.validate_metadata()
        self.assertFalse(result.valid)
        self.assertTrue(result.errors)


class McpAdapterTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        directory = Path(self.temporary_directory.name)
        self.current = SQLiteRuntimeMetadataStore(directory / "runtime.sqlite")
        self.history = DuckDBRuntimeHistoryStore(directory / "history.duckdb")
        repository = RuntimeMetadataRepository(self.current, self.history)
        registry = load_registry(REGISTRY_PATH)
        index = build_vector_index(registry, HashingEmbeddingProvider())
        context_service = ContextService(registry, repository, index)
        self.server = create_mcp_server(AgentToolService(context_service, REGISTRY_PATH))

    async def asyncTearDown(self) -> None:
        self.current.close()
        self.history.close()
        self.temporary_directory.cleanup()

    async def test_tool_catalog_is_bounded_read_only_and_has_strict_inputs(self) -> None:
        tools = {tool.name: tool for tool in await self.server.list_tools()}
        self.assertEqual(
            set(tools),
            {
                "discover_datasets",
                "get_dataset_contract",
                "get_dataset_context",
                "get_metric_definition",
                "get_entity_definition",
                "get_concept_definition",
                "validate_metadata",
            },
        )
        for tool in tools.values():
            self.assertTrue(tool.annotations.read_only_hint)
            self.assertFalse(tool.annotations.destructive_hint)
            self.assertFalse(tool.annotations.open_world_hint)
        discovery_schema = tools["discover_datasets"].input_schema
        self.assertEqual(discovery_schema["properties"]["limit"]["maximum"], 5)
        self.assertNotIn("registry_path", discovery_schema["properties"])
        self.assertEqual(
            tools["validate_metadata"].input_schema["properties"],
            {},
        )

    async def test_protocol_call_returns_structured_content_and_translates_errors(self) -> None:
        response = await self.server.call_tool(
            "get_metric_definition",
            {"metric_id": "metric.finance_net_revenue"},
        )
        self.assertFalse(response.is_error)
        self.assertEqual(
            response.structured_content["metric"]["id"],
            "metric.finance_net_revenue",
        )

        with self.assertRaisesRegex(ToolError, "NOT_FOUND"):
            await self.server.call_tool(
                "get_metric_definition",
                {"metric_id": "metric.unknown"},
            )

    async def test_protocol_validation_rejects_more_than_five_candidates(self) -> None:
        with self.assertRaises(ToolError):
            await self.server.call_tool(
                "discover_datasets",
                {"question": "revenue", "limit": 6},
            )


class McpLifecycleAndStdioTests(unittest.IsolatedAsyncioTestCase):
    async def test_path_factory_closes_owned_stores_and_allows_reopen(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            state_directory = Path(temporary_directory) / "state"
            server = create_mcp_server_from_paths(REGISTRY_PATH, state_directory)
            self.assertIsNotNone(server.settings.lifespan)
            async with server.settings.lifespan(server):
                response = await server.call_tool(
                    "get_dataset_contract",
                    {"dataset_id": "gold.finance_revenue"},
                )
                self.assertFalse(response.is_error)

            reopened = create_mcp_server_from_paths(REGISTRY_PATH, state_directory)
            self.assertIsNotNone(reopened.settings.lifespan)
            async with reopened.settings.lifespan(reopened):
                response = await reopened.call_tool("validate_metadata", {})
                self.assertTrue(response.structured_content["valid"])

    async def test_cli_stdio_initialize_list_success_and_wire_error(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            parameters = StdioServerParameters(
                command=sys.executable,
                args=[
                    "-m",
                    "ai_data_platform",
                    "--registry",
                    str(REGISTRY_PATH),
                    "mcp",
                    "--state-dir",
                    temporary_directory,
                ],
                cwd=PROJECT_ROOT,
                env=os.environ.copy(),
            )
            with tempfile.TemporaryFile(mode="w+") as stderr:
                async with stdio_client(parameters, errlog=stderr) as (
                    read_stream,
                    write_stream,
                ), ClientSession(read_stream, write_stream) as session:
                    initialized = await session.initialize()
                    self.assertEqual(
                        initialized.server_info.name,
                        "ai-native-data-platform",
                    )

                    catalog = await session.list_tools()
                    self.assertIn(
                        "discover_datasets",
                        {tool.name for tool in catalog.tools},
                    )

                    success = await session.call_tool(
                        "get_metric_definition",
                        {"metric_id": "metric.finance_net_revenue"},
                    )
                    self.assertFalse(success.is_error)
                    self.assertEqual(
                        success.structured_content["metric"]["id"],
                        "metric.finance_net_revenue",
                    )

                    failure = await session.call_tool(
                        "get_metric_definition",
                        {"metric_id": "metric.unknown"},
                    )
                    self.assertTrue(failure.is_error)
                    self.assertIn("NOT_FOUND", failure.content[0].text)
                stderr.seek(0)
                stderr_output = stderr.read()
                self.assertIn("NOT_FOUND", stderr_output)
                self.assertNotIn("Traceback", stderr_output)


if __name__ == "__main__":
    unittest.main()
