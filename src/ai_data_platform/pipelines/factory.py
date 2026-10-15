"""Assemble the pipeline runner from server-controlled paths (CLI, MCP, and demo)."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from ai_data_platform.engineering import EngineeringService
from ai_data_platform.models import Registry
from ai_data_platform.registry.loader import load_registry
from ai_data_platform.runtime import (
    DuckDBRuntimeHistoryStore,
    RuntimeMetadataRepository,
    SQLiteRuntimeMetadataStore,
)

from .loader import PipelineProject, load_pipeline_project
from .runner import PipelineRunner

PROJECT_ROOT = Path(__file__).resolve().parents[3]


@dataclass(frozen=True)
class PlatformPaths:
    registry: Path = PROJECT_ROOT / "registry"
    pipelines: Path = PROJECT_ROOT / "pipelines"
    state: Path = Path("var")
    data: Path = Path("var/data")
    repository_root: Path = PROJECT_ROOT


@dataclass
class PipelineEnvironment:
    registry: Registry
    project: PipelineProject
    engineering: EngineeringService
    runtime: RuntimeMetadataRepository
    runner: PipelineRunner


def open_runtime_repository(
    state_directory: Path,
) -> tuple[RuntimeMetadataRepository, SQLiteRuntimeMetadataStore, DuckDBRuntimeHistoryStore]:
    """Runtime stores at the same paths used by the REST and MCP adapters."""
    state_directory.mkdir(parents=True, exist_ok=True)
    current = SQLiteRuntimeMetadataStore(state_directory / "runtime-metadata.sqlite")
    history = DuckDBRuntimeHistoryStore(state_directory / "runtime-history.duckdb")
    return RuntimeMetadataRepository(current, history), current, history


@contextmanager
def open_pipeline_environment(
    paths: PlatformPaths,
    *,
    memory_limit: str | None = None,
) -> Iterator[PipelineEnvironment]:
    registry = load_registry(paths.registry)
    project = load_pipeline_project(paths.pipelines)
    engineering = EngineeringService(registry, paths.repository_root, paths.state)
    runtime, current, history = open_runtime_repository(Path(paths.state))
    try:
        runner = PipelineRunner(
            registry,
            project,
            engineering,
            paths.data,
            runtime_repository=runtime,
            memory_limit=memory_limit,
        )
        yield PipelineEnvironment(registry, project, engineering, runtime, runner)
    finally:
        history.close()
        current.close()
