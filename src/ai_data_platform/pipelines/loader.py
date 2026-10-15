"""Load pipeline specs and validate them against the semantic registry."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from ai_data_platform.models import Dataset, Registry

from .models import LineageEdge, PipelineSpec, SourceSpec
from .sql import PipelineSqlError, render


class PipelineValidationError(ValueError):
    def __init__(self, messages: list[str]) -> None:
        self.messages = messages
        super().__init__("Pipeline validation failed:\n- " + "\n- ".join(messages))


@dataclass
class PipelineProject:
    sources: dict[str, SourceSpec] = field(default_factory=dict)
    pipelines: dict[str, PipelineSpec] = field(default_factory=dict)

    def producer_of(self, dataset_id: str) -> PipelineSpec | None:
        return next((spec for spec in self.pipelines.values() if spec.output == dataset_id), None)

    def lineage(self) -> list[LineageEdge]:
        edges: list[LineageEdge] = []
        for spec in self.pipelines.values():
            edges.extend(
                LineageEdge(pipeline_id=spec.id, output=spec.output, input_kind="source", input_id=item)
                for item in spec.inputs.sources
            )
            edges.extend(
                LineageEdge(pipeline_id=spec.id, output=spec.output, input_kind="dataset", input_id=item)
                for item in spec.inputs.datasets
            )
        return edges

    def execution_order(self) -> list[PipelineSpec]:
        """Deterministic topological order over dataset dependencies."""
        remaining = dict(self.pipelines)
        produced = {spec.output for spec in remaining.values()}
        done: set[str] = set()
        ordered: list[PipelineSpec] = []
        while remaining:
            ready = sorted(
                pipeline_id
                for pipeline_id, spec in remaining.items()
                if all(item in done or item not in produced for item in spec.inputs.datasets)
            )
            if not ready:
                raise PipelineValidationError(
                    ["pipeline dependencies contain a cycle: " + ", ".join(sorted(remaining))]
                )
            for pipeline_id in ready:
                spec = remaining.pop(pipeline_id)
                ordered.append(spec)
                done.add(spec.output)
        return ordered

    def downstream_of(self, pipeline_ids: set[str]) -> set[str]:
        selected = set(pipeline_ids)
        changed = True
        while changed:
            changed = False
            outputs = {self.pipelines[item].output for item in selected}
            for spec in self.pipelines.values():
                if spec.id not in selected and outputs & set(spec.inputs.datasets):
                    selected.add(spec.id)
                    changed = True
        return selected


def _read_yaml(path: Path) -> Any:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def parse_pipeline_spec(raw: Any, origin: str = "spec") -> PipelineSpec:
    if not isinstance(raw, dict):
        raise PipelineValidationError([f"{origin}: expected a YAML mapping"])
    try:
        return PipelineSpec.model_validate(raw)
    except ValidationError as error:
        raise PipelineValidationError([f"{origin}: {error}"]) from error


def load_pipeline_project(directory: str | Path) -> PipelineProject:
    root = Path(directory)
    errors: list[str] = []
    project = PipelineProject()
    sources_file = root / "sources.yaml"
    if not sources_file.is_file():
        raise PipelineValidationError([f"missing {sources_file}"])
    raw_sources = (_read_yaml(sources_file) or {}).get("sources", [])
    for index, raw in enumerate(raw_sources):
        try:
            source = SourceSpec.model_validate(raw)
        except ValidationError as error:
            errors.append(f"{sources_file} entry {index}: {error}")
            continue
        if source.id in project.sources:
            errors.append(f"duplicate source id '{source.id}'")
        project.sources[source.id] = source
    for path in sorted((root / "specs").glob("*.yaml")):
        try:
            spec = parse_pipeline_spec(_read_yaml(path), str(path))
        except (PipelineValidationError, yaml.YAMLError) as error:
            errors.extend(getattr(error, "messages", [str(error)]))
            continue
        if spec.id in project.pipelines:
            errors.append(f"duplicate pipeline id '{spec.id}' in {path}")
        project.pipelines[spec.id] = spec
    if errors:
        raise PipelineValidationError(errors)
    return project


def spec_errors(
    spec: PipelineSpec,
    *,
    sources: dict[str, SourceSpec],
    contract: Dataset | None,
    available_datasets: set[str],
) -> list[str]:
    """Static checks shared by repository pipelines and agent proposals."""
    errors: list[str] = []
    if contract is None:
        errors.append(f"{spec.id}: output '{spec.output}' has no dataset contract in the registry")
    elif not contract.columns:
        errors.append(f"{spec.id}: contract '{spec.output}' must declare columns before a pipeline can publish it")
    for source_id in spec.inputs.sources:
        if source_id not in sources:
            errors.append(f"{spec.id}: unknown source '{source_id}'")
    for dataset_id in spec.inputs.datasets:
        if dataset_id == spec.output:
            errors.append(f"{spec.id}: a pipeline cannot read its own output")
        elif dataset_id not in available_datasets:
            errors.append(f"{spec.id}: input dataset '{dataset_id}' is not produced by any pipeline")
    try:
        rendered = render(spec.sql, sources=spec.inputs.sources, datasets=spec.inputs.datasets)
    except PipelineSqlError as error:
        errors.extend(f"{spec.id}: {message}" for message in error.messages)
    else:
        unused = sorted(
            (set(spec.inputs.sources) - rendered.referenced_sources)
            | (set(spec.inputs.datasets) - rendered.referenced_datasets)
        )
        if unused:
            errors.append(f"{spec.id}: declared inputs are not used by the SQL: {', '.join(unused)}")
    for reconciliation in spec.reconciliations:
        for label, sql in (("expected_sql", reconciliation.expected_sql), ("actual_sql", reconciliation.actual_sql)):
            try:
                render(sql, sources=spec.inputs.sources, datasets=spec.inputs.datasets, this="this")
            except PipelineSqlError as error:
                errors.extend(
                    f"{spec.id}: reconciliation '{reconciliation.name}' {label}: {message}"
                    for message in error.messages
                )
    if contract is not None and contract.columns:
        known = {column.name for column in contract.columns}
        for check in spec.quality_checks:
            columns = getattr(check, "columns", None) or [getattr(check, "column", None)]
            for column in columns:
                if column is not None and column not in known:
                    errors.append(f"{spec.id}: {check.check} check uses unknown column '{column}'")
    return errors


def validate_pipeline_project(project: PipelineProject, registry: Registry) -> None:
    errors: list[str] = []
    produced: dict[str, str] = {}
    for spec in project.pipelines.values():
        if spec.output in produced:
            errors.append(f"{spec.output} is produced by both {produced[spec.output]} and {spec.id}")
        produced[spec.output] = spec.id
    for spec in project.pipelines.values():
        errors.extend(
            spec_errors(
                spec,
                sources=project.sources,
                contract=registry.datasets.get(spec.output),
                available_datasets=set(produced),
            )
        )
    if errors:
        raise PipelineValidationError(errors)
    project.execution_order()
