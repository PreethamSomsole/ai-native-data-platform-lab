"""Governed pipeline authoring for agents: inspect, validate, dry-run, and propose.

Agents never publish. The strongest action available is a reviewable proposal written to a
server-configured directory after a passing dry run. Promotion into ``pipelines/specs`` and
``registry/datasets`` remains a human-reviewed Git change.
"""

from __future__ import annotations

import hashlib
import json
import threading
from collections.abc import Callable
from datetime import UTC, date, datetime, time
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import uuid4

import duckdb
import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from ai_data_platform.models import Certification, Dataset, DatasetColumn, Registry
from ai_data_platform.validation import RegistryValidationError, validate_registry

from .execution import CheckOutcome, build_candidate, input_relations
from .loader import PipelineProject, PipelineValidationError, parse_pipeline_spec, spec_errors
from .models import PipelineSpec
from .sql import (
    PipelineSqlError,
    dataset_relation,
    qualified,
    render,
    sandboxed_connection,
    source_relation,
    validate_select,
)

MAX_YAML_CHARACTERS = 20_000
AGENT_REVIEW_REASON = "agent-authored pipelines require human review before promotion"


class AuthoringModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class InputColumn(AuthoringModel):
    name: str
    type: str


class SourceInput(AuthoringModel):
    id: str
    template: str
    description: str
    landed: bool
    columns: list[InputColumn] = Field(default_factory=list)


class DatasetInput(AuthoringModel):
    id: str
    template: str
    description: str
    certification: str
    published: bool
    columns: list[DatasetColumn] = Field(default_factory=list)


class PipelineInputCatalog(AuthoringModel):
    sources: list[SourceInput]
    datasets: list[DatasetInput]
    rules: list[str]


class ProposalValidation(AuthoringModel):
    valid: bool
    pipeline_id: str | None = None
    output: str | None = None
    new_contract: bool = False
    errors: list[str] = Field(default_factory=list)
    approval_reasons: list[str] = Field(default_factory=list)
    lineage_inputs: list[str] = Field(default_factory=list)


class DryRunResult(AuthoringModel):
    passed: bool
    validation: ProposalValidation
    row_count: int | None = None
    schema_columns: list[dict[str, Any]] = Field(default_factory=list)
    contract_errors: list[str] = Field(default_factory=list)
    checks: list[CheckOutcome] = Field(default_factory=list)
    preview: list[dict[str, Any]] = Field(default_factory=list)
    message: str = ""


class ProposalSubmission(AuthoringModel):
    status: str
    proposal_id: str | None = None
    proposal_path: str | None = None
    approval_reasons: list[str] = Field(default_factory=list)
    dry_run: DryRunResult
    message: str


class LineageNode(AuthoringModel):
    dataset_id: str
    pipeline_id: str | None
    upstream_sources: list[str]
    upstream_datasets: list[str]
    downstream_datasets: list[str]


class PipelineAuthoringError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(f"{code}: {message}")


def _json_safe(value: Any) -> Any:
    """Normalize preview values (including BLOB, LIST, STRUCT, and MAP) for JSON transport."""
    if value is None or isinstance(value, bool | int | float | str):
        return value
    if isinstance(value, bytes | bytearray | memoryview):
        return "0x" + bytes(value).hex()
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, datetime | date | time):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, list | tuple | set):
        return [_json_safe(item) for item in value]
    return str(value)


class PipelineAuthoringService:
    def __init__(
        self,
        registry: Registry,
        project: PipelineProject,
        warehouse_path: str | Path,
        proposals_directory: str | Path,
        *,
        temp_directory: str | Path,
        memory_limit: str | None = "2GB",
        max_temp_directory_size: str | None = "4GB",
        dry_run_timeout_seconds: float = 120.0,
        preview_rows: int = 5,
        audit: Callable[..., None] | None = None,
    ) -> None:
        self.registry = registry
        self.project = project
        self.warehouse_path = Path(warehouse_path)
        self.proposals_directory = Path(proposals_directory)
        self.temp_directory = Path(temp_directory)
        self.memory_limit = memory_limit
        self.max_temp_directory_size = max_temp_directory_size
        self.dry_run_timeout_seconds = dry_run_timeout_seconds
        self.preview_rows = preview_rows
        self.audit = audit

    def _warehouse(self) -> duckdb.DuckDBPyConnection:
        if not self.warehouse_path.is_file():
            raise PipelineAuthoringError(
                "CAPABILITY_UNAVAILABLE", "the DEV warehouse has not been built; run the pipelines first"
            )
        try:
            return sandboxed_connection(
                self.warehouse_path,
                read_only=True,
                temp_directory=self.temp_directory,
                memory_limit=self.memory_limit,
                max_temp_directory_size=self.max_temp_directory_size,
            )
        except duckdb.IOException as error:
            raise PipelineAuthoringError(
                "CAPABILITY_UNAVAILABLE", f"the DEV warehouse is busy or unreadable: {error}"
            ) from error

    @staticmethod
    def _relations(connection: duckdb.DuckDBPyConnection) -> set[str]:
        return {
            name
            for (name,) in connection.execute(
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_catalog = 'wh' AND table_schema = 'main'"
            ).fetchall()
        }

    def list_inputs(self) -> PipelineInputCatalog:
        try:
            connection = self._warehouse()
        except PipelineAuthoringError:
            connection = None
        try:
            relations = self._relations(connection) if connection else set()

            def columns(relation: str) -> list[InputColumn]:
                if connection is None or relation not in relations:
                    return []
                return [
                    InputColumn(name=row[0], type=row[1])
                    for row in connection.execute(f"DESCRIBE {qualified(relation)}").fetchall()
                ]

            sources = [
                SourceInput(
                    id=source.id,
                    template=f"{{{{ source('{source.system}', '{source.table}') }}}}",
                    description=source.description,
                    landed=source_relation(source.id) in relations,
                    columns=columns(source_relation(source.id)),
                )
                for source in sorted(self.project.sources.values(), key=lambda item: item.id)
            ]
            datasets = []
            for spec in sorted(self.project.pipelines.values(), key=lambda item: item.output):
                contract = self.registry.datasets[spec.output]
                datasets.append(
                    DatasetInput(
                        id=spec.output,
                        template=f"{{{{ ref('{spec.output}') }}}}",
                        description=contract.description,
                        certification=contract.certification.value,
                        published=dataset_relation(spec.output) in relations,
                        columns=contract.columns,
                    )
                )
        finally:
            if connection is not None:
                connection.close()
        return PipelineInputCatalog(
            sources=sources,
            datasets=datasets,
            rules=[
                "Write exactly one SELECT; name inputs only with the listed templates.",
                "Declare every source and dataset the SQL reads under inputs.",
                "Table functions (read_csv, read_parquet, query_table, ...) are rejected.",
                "A new output needs a proposed contract with columns; agents cannot certify it.",
                "Proposals are reviewed by a human before promotion; agents never publish.",
            ],
        )

    @staticmethod
    def _load_yaml(text: str, label: str) -> Any:
        if len(text) > MAX_YAML_CHARACTERS:
            raise PipelineAuthoringError("INVALID_ARGUMENT", f"{label} exceeds {MAX_YAML_CHARACTERS} characters")
        try:
            return yaml.safe_load(text)
        except yaml.YAMLError as error:
            raise PipelineAuthoringError("INVALID_ARGUMENT", f"{label} is not valid YAML: {error}") from error

    def _parse(
        self, spec_yaml: str, contract_yaml: str | None
    ) -> tuple[PipelineSpec | None, Dataset | None, ProposalValidation]:
        validation = ProposalValidation(valid=False)
        try:
            spec = parse_pipeline_spec(self._load_yaml(spec_yaml, "spec_yaml"), "proposal")
        except PipelineValidationError as error:
            validation.errors = error.messages
            return None, None, validation
        validation.pipeline_id = spec.id
        validation.output = spec.output
        validation.lineage_inputs = spec.inputs.sources + spec.inputs.datasets
        contract = self.registry.datasets.get(spec.output)
        if contract_yaml:
            if contract is not None:
                validation.errors.append(
                    f"'{spec.output}' already has a governed contract; contract changes go through registry review"
                )
                return spec, contract, validation
            try:
                contract = Dataset.model_validate(self._load_yaml(contract_yaml, "contract_yaml"))
            except ValidationError as error:
                validation.errors.append(f"proposed contract is invalid: {error}")
                return spec, None, validation
            validation.new_contract = True
        return spec, contract, validation

    def validate_proposal(self, spec_yaml: str, contract_yaml: str | None = None) -> ProposalValidation:
        spec, contract, validation = self._parse(spec_yaml, contract_yaml)
        if spec is None or validation.errors:
            return validation
        errors = validation.errors
        approval = [AGENT_REVIEW_REASON]
        if validation.new_contract and contract is not None:
            if contract.id != spec.output:
                errors.append(f"proposed contract id '{contract.id}' must equal the pipeline output '{spec.output}'")
            if contract.certification is Certification.CERTIFIED:
                errors.append("agents cannot certify a dataset; propose it as uncertified")
            layer = spec.output.split(".", 1)[0]
            if contract.layer != layer:
                errors.append(f"contract layer '{contract.layer}' must match the output id prefix '{layer}'")
            try:
                validate_registry(
                    self.registry.model_copy(
                        update={"datasets": {**self.registry.datasets, contract.id: contract}}
                    )
                )
            except RegistryValidationError as error:
                errors.extend(error.messages)
            approval.append("new dataset contracts require approval")
        existing = self.project.producer_of(spec.output)
        if existing is not None:
            approval.append(f"replaces repository pipeline {existing.id}")
        if spec.id in self.project.pipelines and (existing is None or existing.id != spec.id):
            errors.append(f"pipeline id '{spec.id}' is already used by another output")
        produced = {item.output for item in self.project.pipelines.values()}
        errors.extend(
            spec_errors(spec, sources=self.project.sources, contract=contract, available_datasets=produced)
        )
        # Validate the dependency graph as it would be after promotion, with this proposal
        # replacing any current producer of the same output.
        promoted = PipelineProject(
            sources=self.project.sources,
            pipelines={
                pipeline_id: item
                for pipeline_id, item in self.project.pipelines.items()
                if item.output != spec.output and pipeline_id != spec.id
            }
            | {spec.id: spec},
        )
        try:
            promoted.execution_order()
        except PipelineValidationError as error:
            errors.extend(error.messages)
        with duckdb.connect() as parser:
            try:
                rendered = render(spec.sql, sources=spec.inputs.sources, datasets=spec.inputs.datasets)
                validate_select(parser, rendered.sql, input_relations(spec))
            except PipelineSqlError as error:
                errors.extend(error.messages)
        validation.errors = list(dict.fromkeys(errors))
        validation.valid = not validation.errors
        validation.approval_reasons = approval
        return validation

    def dry_run(self, spec_yaml: str, contract_yaml: str | None = None) -> DryRunResult:
        validation = self.validate_proposal(spec_yaml, contract_yaml)
        if not validation.valid:
            return DryRunResult(passed=False, validation=validation, message="proposal is invalid; nothing was executed")
        spec, contract, _ = self._parse(spec_yaml, contract_yaml)
        assert spec is not None and contract is not None
        connection = self._warehouse()
        try:
            relations = self._relations(connection)
            missing = [
                item for item in spec.inputs.sources if source_relation(item) not in relations
            ] + [item for item in spec.inputs.datasets if dataset_relation(item) not in relations]
            if missing:
                return DryRunResult(
                    passed=False,
                    validation=validation,
                    message=f"inputs are not published in the DEV warehouse: {', '.join(missing)}",
                )
            candidate = qualified("agent_candidate", catalog="memory")
            # Untrusted SQL gets a wall-clock deadline (plus memory and temp-disk caps).
            deadline = threading.Timer(self.dry_run_timeout_seconds, connection.interrupt)
            deadline.start()
            try:
                outcome = build_candidate(connection, spec, contract, candidate)
                preview: list[dict[str, Any]] = []
                if not outcome.contract_errors and self.preview_rows:
                    cursor = connection.execute(f"SELECT * FROM {candidate} LIMIT {int(self.preview_rows)}")
                    names = [item[0] for item in cursor.description]
                    preview = [
                        {name: _json_safe(value) for name, value in zip(names, row, strict=True)}
                        for row in cursor.fetchall()
                    ]
            except duckdb.InterruptException:
                return DryRunResult(
                    passed=False,
                    validation=validation,
                    message=f"dry run exceeded the {self.dry_run_timeout_seconds:g}s time limit and was interrupted",
                )
            except (PipelineSqlError, ValueError, duckdb.Error) as error:
                return DryRunResult(passed=False, validation=validation, message=f"execution failed: {error}")
            finally:
                deadline.cancel()
        finally:
            connection.close()
        return DryRunResult(
            passed=outcome.passed,
            validation=validation,
            row_count=outcome.row_count,
            schema_columns=outcome.schema,
            contract_errors=outcome.contract_errors,
            checks=outcome.checks,
            preview=preview,
            message=(
                "dry run passed in an isolated, read-only sandbox; nothing was published"
                if outcome.passed
                else "dry run failed; see contract_errors and checks"
            ),
        )

    def submit_proposal(
        self, spec_yaml: str, contract_yaml: str | None = None, rationale: str = ""
    ) -> ProposalSubmission:
        if len(rationale) > 2_000:
            raise PipelineAuthoringError("INVALID_ARGUMENT", "rationale exceeds 2000 characters")
        dry_run = self.dry_run(spec_yaml, contract_yaml)
        if not dry_run.passed:
            return ProposalSubmission(
                status="rejected",
                dry_run=dry_run,
                message="only proposals with a passing dry run are queued for review",
            )
        proposal_id = f"proposal-{datetime.now(tz=UTC):%Y%m%d%H%M%S}-{uuid4().hex[:8]}"
        directory = self.proposals_directory / proposal_id
        directory.mkdir(parents=True)
        (directory / "pipeline.yaml").write_text(spec_yaml, encoding="utf-8")
        if contract_yaml:
            (directory / "contract.yaml").write_text(contract_yaml, encoding="utf-8")
        review = {
            "proposal_id": proposal_id,
            "pipeline_id": dry_run.validation.pipeline_id,
            "output": dry_run.validation.output,
            "submitted_at": datetime.now(tz=UTC).isoformat(),
            "rationale": rationale,
            "spec_sha256": hashlib.sha256(spec_yaml.encode()).hexdigest(),
            "approval_reasons": dry_run.validation.approval_reasons,
            "dry_run": dry_run.model_dump(mode="json", exclude={"preview"}),
            "promotion": (
                "After review, copy pipeline.yaml to pipelines/specs/ and any contract.yaml to "
                "registry/datasets/ in a pull request; CI validates both."
            ),
        }
        (directory / "review.json").write_text(json.dumps(review, indent=2) + "\n", encoding="utf-8")
        if self.audit is not None:
            self.audit(
                "submit_pipeline_proposal",
                proposal_id=proposal_id,
                pipeline_id=dry_run.validation.pipeline_id,
                output=dry_run.validation.output,
            )
        return ProposalSubmission(
            status="approval_required",
            proposal_id=proposal_id,
            proposal_path=str(directory),
            approval_reasons=dry_run.validation.approval_reasons,
            dry_run=dry_run,
            message="proposal queued for human review; nothing was published",
        )

    def lineage(self, dataset_id: str) -> LineageNode:
        produced = {spec.output: spec for spec in self.project.pipelines.values()}
        if dataset_id not in self.registry.datasets:
            raise PipelineAuthoringError("NOT_FOUND", f"dataset '{dataset_id}' was not found")
        spec = produced.get(dataset_id)
        return LineageNode(
            dataset_id=dataset_id,
            pipeline_id=spec.id if spec else None,
            upstream_sources=list(spec.inputs.sources) if spec else [],
            upstream_datasets=list(spec.inputs.datasets) if spec else [],
            downstream_datasets=sorted(
                item.output for item in self.project.pipelines.values() if dataset_id in item.inputs.datasets
            ),
        )
