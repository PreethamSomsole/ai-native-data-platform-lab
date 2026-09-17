"""Thin MCP adapter over the protocol-independent agent tool service."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Annotated, Literal

from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import Field

from ai_data_platform.agent_tools import (
    AgentToolError,
    AgentToolService,
    ConceptDefinitionToolResult,
    DatasetContextToolResult,
    DatasetContractToolResult,
    DiscoveryToolResult,
    EntityDefinitionToolResult,
    MetadataValidationResult,
    MetricDefinitionToolResult,
)
from ai_data_platform.api import build_vector_index
from ai_data_platform.context import ContextService, DiscoveryMode
from ai_data_platform.embeddings import HashingEmbeddingProvider
from ai_data_platform.registry.loader import load_registry
from ai_data_platform.runtime import (
    DuckDBRuntimeHistoryStore,
    RuntimeMetadataRepository,
    SQLiteRuntimeMetadataStore,
)

READ_ONLY_ANNOTATIONS = ToolAnnotations(
    readOnlyHint=True,
    destructiveHint=False,
    idempotentHint=True,
    openWorldHint=False,
)


def _tool_call(function, *args, **kwargs):
    try:
        return function(*args, **kwargs)
    except AgentToolError as error:
        raise ToolError(str(error)) from error


def create_mcp_server(tool_service: AgentToolService) -> MCPServer:
    """Register the stable agent capabilities as read-only MCP tools."""
    server = MCPServer(
        name="ai-native-data-platform",
        title="AI-Native Data Platform Context Tools",
        description=(
            "Governed, read-only access to semantic metadata, dataset discovery, "
            "and runtime context."
        ),
        version="0.5.0",
    )

    @server.tool(
        name="discover_datasets",
        description=(
            "Find up to five governed dataset candidates. Deterministic semantic policy, "
            "prohibited-use exclusions, and clarification outcomes remain authoritative."
        ),
        annotations=READ_ONLY_ANNOTATIONS,
        structured_output=True,
    )
    def discover_datasets_tool(
        question: Annotated[str, Field(min_length=1)],
        mode: Literal["deterministic", "hybrid"] = "deterministic",
        limit: Annotated[int, Field(ge=1, le=5)] = 5,
        min_similarity: Annotated[float, Field(ge=-1.0, le=1.0)] = 0.25,
    ) -> DiscoveryToolResult:
        return _tool_call(
            tool_service.discover_datasets,
            question,
            mode=DiscoveryMode(mode),
            limit=limit,
            min_similarity=min_similarity,
        )

    @server.tool(
        name="get_dataset_contract",
        description="Return one canonical Git/YAML dataset contract by exact ID.",
        annotations=READ_ONLY_ANNOTATIONS,
        structured_output=True,
    )
    def get_dataset_contract_tool(
        dataset_id: Annotated[str, Field(min_length=1)],
    ) -> DatasetContractToolResult:
        return _tool_call(tool_service.get_dataset_contract, dataset_id)

    @server.tool(
        name="get_dataset_context",
        description=(
            "Return a dataset contract with referenced semantics, latest runtime state, "
            "runtime summary, and a bounded row-count trend."
        ),
        annotations=READ_ONLY_ANNOTATIONS,
        structured_output=True,
    )
    def get_dataset_context_tool(
        dataset_id: Annotated[str, Field(min_length=1)],
        trend_limit: Annotated[int, Field(ge=1, le=1_000)] = 30,
    ) -> DatasetContextToolResult:
        return _tool_call(
            tool_service.get_dataset_context,
            dataset_id,
            trend_limit=trend_limit,
        )

    @server.tool(
        name="get_metric_definition",
        description="Return one canonical metric definition by exact ID.",
        annotations=READ_ONLY_ANNOTATIONS,
        structured_output=True,
    )
    def get_metric_definition_tool(
        metric_id: Annotated[str, Field(min_length=1)],
    ) -> MetricDefinitionToolResult:
        return _tool_call(tool_service.get_metric_definition, metric_id)

    @server.tool(
        name="get_entity_definition",
        description="Return one canonical business entity definition by exact ID.",
        annotations=READ_ONLY_ANNOTATIONS,
        structured_output=True,
    )
    def get_entity_definition_tool(
        entity_id: Annotated[str, Field(min_length=1)],
    ) -> EntityDefinitionToolResult:
        return _tool_call(tool_service.get_entity_definition, entity_id)

    @server.tool(
        name="get_concept_definition",
        description="Return one canonical business concept definition by exact ID.",
        annotations=READ_ONLY_ANNOTATIONS,
        structured_output=True,
    )
    def get_concept_definition_tool(
        concept_id: Annotated[str, Field(min_length=1)],
    ) -> ConceptDefinitionToolResult:
        return _tool_call(tool_service.get_concept_definition, concept_id)

    @server.tool(
        name="validate_metadata",
        description=(
            "Reload and validate the server-configured Git/YAML registry. The registry "
            "path is controlled by the server and cannot be supplied by the caller."
        ),
        annotations=READ_ONLY_ANNOTATIONS,
        structured_output=True,
    )
    def validate_metadata_tool() -> MetadataValidationResult:
        return _tool_call(tool_service.validate_metadata)

    return server


def create_mcp_server_from_paths(
    registry_path: str | Path,
    state_directory: str | Path,
) -> MCPServer:
    registry_path = Path(registry_path)
    state_directory = Path(state_directory)
    state_directory.mkdir(parents=True, exist_ok=True)
    registry = load_registry(registry_path)
    repository = RuntimeMetadataRepository(
        SQLiteRuntimeMetadataStore(state_directory / "runtime-metadata.sqlite"),
        DuckDBRuntimeHistoryStore(state_directory / "runtime-history.duckdb"),
    )
    vector_index = build_vector_index(registry, HashingEmbeddingProvider())
    context_service = ContextService(registry, repository, vector_index)
    return create_mcp_server(AgentToolService(context_service, registry_path))


def create_default_mcp_server() -> MCPServer:
    project_root = Path(__file__).resolve().parents[3]
    registry_path = Path(
        os.getenv("AI_DATA_PLATFORM_REGISTRY", str(project_root / "registry"))
    )
    state_directory = Path(os.getenv("AI_DATA_PLATFORM_STATE_DIR", "var"))
    return create_mcp_server_from_paths(registry_path, state_directory)


def main() -> None:
    create_default_mcp_server().run(transport="stdio")


if __name__ == "__main__":
    main()
