"""Thin MCP adapter over the protocol-independent agent tool service."""

from __future__ import annotations

import os
from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from functools import partial
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
from ai_data_platform.engineering.service import append_audit_record
from ai_data_platform.pipelines import (
    PipelineValidationError,
    load_pipeline_project,
    validate_pipeline_project,
)
from ai_data_platform.pipelines.authoring import (
    DryRunResult,
    LineageNode,
    PipelineAuthoringError,
    PipelineAuthoringService,
    PipelineInputCatalog,
    ProposalSubmission,
    ProposalValidation,
)
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

PROPOSAL_ANNOTATIONS = ToolAnnotations(
    readOnlyHint=False,
    destructiveHint=False,
    idempotentHint=False,
    openWorldHint=False,
)

ServerLifespan = Callable[[MCPServer], AbstractAsyncContextManager[object]]


def _tool_call(function, *args, **kwargs):
    try:
        return function(*args, **kwargs)
    except (AgentToolError, PipelineAuthoringError) as error:
        raise ToolError(str(error)) from error


def create_mcp_server(
    tool_service: AgentToolService,
    *,
    pipeline_service: PipelineAuthoringService | None = None,
    lifespan: ServerLifespan | None = None,
) -> MCPServer:
    """Register read-only context tools and, when configured, governed pipeline authoring."""
    server = MCPServer(
        name="ai-native-data-platform",
        title="AI-Native Data Platform Context Tools",
        description=(
            "Governed access to semantic metadata, dataset discovery, runtime context, "
            "and review-only pipeline authoring."
        ),
        version="0.7.0",
        lifespan=lifespan,
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

    if pipeline_service is not None:
        _register_pipeline_tools(server, pipeline_service)
    return server


SpecYaml = Annotated[str, Field(min_length=1, max_length=20_000)]
ContractYaml = Annotated[str | None, Field(default=None, max_length=20_000)]


def _register_pipeline_tools(server: MCPServer, service: PipelineAuthoringService) -> None:
    @server.tool(
        name="list_pipeline_inputs",
        description=(
            "List the sources and published datasets a pipeline may read, their columns and "
            "template syntax, and the authoring rules."
        ),
        annotations=READ_ONLY_ANNOTATIONS,
        structured_output=True,
    )
    def list_pipeline_inputs_tool() -> PipelineInputCatalog:
        return _tool_call(service.list_inputs)

    @server.tool(
        name="get_dataset_lineage",
        description="Return validated upstream and downstream lineage for one dataset.",
        annotations=READ_ONLY_ANNOTATIONS,
        structured_output=True,
    )
    def get_dataset_lineage_tool(dataset_id: Annotated[str, Field(min_length=1)]) -> LineageNode:
        return _tool_call(service.lineage, dataset_id.strip())

    @server.tool(
        name="validate_pipeline_proposal",
        description=(
            "Statically validate a pipeline spec (YAML) and, for a new output, its proposed "
            "dataset contract (YAML). Nothing is executed."
        ),
        annotations=READ_ONLY_ANNOTATIONS,
        structured_output=True,
    )
    def validate_pipeline_proposal_tool(spec_yaml: SpecYaml, contract_yaml: ContractYaml = None) -> ProposalValidation:
        return _tool_call(service.validate_proposal, spec_yaml, contract_yaml)

    @server.tool(
        name="dry_run_pipeline_proposal",
        description=(
            "Build the proposal in an isolated sandbox over a read-only DEV warehouse and run "
            "contract, quality, and reconciliation gates. Nothing is published."
        ),
        annotations=READ_ONLY_ANNOTATIONS,
        structured_output=True,
    )
    def dry_run_pipeline_proposal_tool(spec_yaml: SpecYaml, contract_yaml: ContractYaml = None) -> DryRunResult:
        return _tool_call(service.dry_run, spec_yaml, contract_yaml)

    @server.tool(
        name="submit_pipeline_proposal",
        description=(
            "Queue a proposal for human review after a passing dry run. This never publishes "
            "data or edits the repository; promotion is a reviewed pull request."
        ),
        annotations=PROPOSAL_ANNOTATIONS,
        structured_output=True,
    )
    def submit_pipeline_proposal_tool(
        spec_yaml: SpecYaml,
        contract_yaml: ContractYaml = None,
        rationale: Annotated[str, Field(max_length=2_000)] = "",
    ) -> ProposalSubmission:
        return _tool_call(service.submit_proposal, spec_yaml, contract_yaml, rationale)


def resolve_pipelines_path(registry_path: str | Path, explicit: str | Path | None) -> Path | None:
    """Pipelines directory for the MCP server, or None to omit pipeline tools.

    Order: an explicit path, then AI_DATA_PLATFORM_PIPELINES, then the bundled pipelines
    only when the bundled registry is in use (bundled specs target bundled contracts).
    """
    if explicit is not None:
        return Path(explicit)
    configured = os.getenv("AI_DATA_PLATFORM_PIPELINES")
    if configured:
        return Path(configured)
    project_root = Path(__file__).resolve().parents[3]
    bundled = project_root / "pipelines"
    if (
        Path(registry_path).resolve() == (project_root / "registry").resolve()
        and (bundled / "sources.yaml").is_file()
    ):
        return bundled
    return None


def create_pipeline_authoring_service(
    registry, pipelines_path: Path, state_directory: Path
) -> PipelineAuthoringService:
    if not (pipelines_path / "sources.yaml").is_file():
        raise PipelineValidationError(
            [f"configured pipelines path {pipelines_path} has no sources.yaml"]
        )
    project = load_pipeline_project(pipelines_path)
    validate_pipeline_project(project, registry)
    state_directory.mkdir(parents=True, exist_ok=True)
    return PipelineAuthoringService(
        registry,
        project,
        state_directory / "engineering.duckdb",
        state_directory / "pipeline_proposals",
        temp_directory=state_directory / "duckdb_tmp",
        audit=partial(append_audit_record, state_directory / "engineering-audit.jsonl"),
    )


def create_mcp_server_from_paths(
    registry_path: str | Path,
    state_directory: str | Path,
    pipelines_path: str | Path | None = None,
) -> MCPServer:
    registry_path = Path(registry_path)
    state_directory = Path(state_directory)
    state_directory.mkdir(parents=True, exist_ok=True)
    registry = load_registry(registry_path)
    vector_index = build_vector_index(registry, HashingEmbeddingProvider())
    current_store = SQLiteRuntimeMetadataStore(
        state_directory / "runtime-metadata.sqlite"
    )
    history_store = DuckDBRuntimeHistoryStore(state_directory / "runtime-history.duckdb")
    repository = RuntimeMetadataRepository(
        current_store,
        history_store,
    )
    context_service = ContextService(registry, repository, vector_index)

    @asynccontextmanager
    async def lifespan(_: MCPServer) -> AsyncIterator[None]:
        try:
            yield None
        finally:
            history_store.close()
            current_store.close()

    pipeline_service = (
        create_pipeline_authoring_service(registry, Path(pipelines_path), state_directory)
        if pipelines_path is not None
        else None
    )
    return create_mcp_server(
        AgentToolService(context_service, registry_path),
        pipeline_service=pipeline_service,
        lifespan=lifespan,
    )


def create_default_mcp_server() -> MCPServer:
    project_root = Path(__file__).resolve().parents[3]
    registry_path = Path(
        os.getenv("AI_DATA_PLATFORM_REGISTRY", str(project_root / "registry"))
    )
    state_directory = Path(os.getenv("AI_DATA_PLATFORM_STATE_DIR", "var"))
    return create_mcp_server_from_paths(
        registry_path, state_directory, resolve_pipelines_path(registry_path, None)
    )


def main() -> None:
    create_default_mcp_server().run(transport="stdio")


if __name__ == "__main__":
    main()
