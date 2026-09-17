"""Repository-scoped, deterministic engineering capabilities for the local DEV lab."""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import duckdb

from ai_data_platform.models import Dataset, Registry
from ai_data_platform.validation import RegistryValidationError, validate_registry

from .models import (
    ApprovalRequirement,
    ContractValidationResult,
    DeploymentRequest,
    DeploymentResult,
    DevValidationResult,
    Environment,
    IngestionRecommendation,
    IngestionRequest,
    PipelinePlan,
    PipelinePlanRequest,
    ReconciliationResult,
    RestoreTableRequest,
    SafeReplaceRequest,
    SafeReplaceResult,
    SourceKind,
    ToolStatus,
)

_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_ALLOWED_ARTIFACT_SUFFIXES = {".py", ".sql", ".yaml", ".yml"}


class EngineeringToolError(ValueError):
    """Stable failure for callers and future protocol adapters."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(f"{code}: {message}")


class EngineeringService:
    """Deterministic DEV tools; server configuration owns all executable boundaries."""

    def __init__(
        self,
        registry: Registry,
        repository_root: str | Path,
        state_directory: str | Path,
        *,
        allow_development_writes: bool = True,
    ) -> None:
        self.registry = registry
        self.repository_root = Path(repository_root).resolve()
        self.state_directory = Path(state_directory).resolve()
        if not self.repository_root.is_dir():
            raise ValueError("repository_root must be an existing directory")
        self.state_directory.mkdir(parents=True, exist_ok=True)
        self.database_path = self.state_directory / "engineering.duckdb"
        self.audit_path = self.state_directory / "engineering-audit.jsonl"
        self.allow_development_writes = allow_development_writes

    @staticmethod
    def _approval(
        environment: Environment,
        *,
        contract_change: bool = False,
        architecture_change: bool = False,
    ) -> tuple[ApprovalRequirement, list[str]]:
        reasons: list[str] = []
        if environment is not Environment.DEVELOPMENT:
            reasons.append(f"{environment.value} actions require approval")
        if contract_change:
            reasons.append("dataset contract changes require approval")
        if architecture_change:
            reasons.append("architecture changes require approval")
        requirement = ApprovalRequirement.REQUIRED if reasons else ApprovalRequirement.NOT_REQUIRED
        return requirement, reasons

    @staticmethod
    def _quote_identifier(identifier: str) -> str:
        if not _IDENTIFIER.fullmatch(identifier):
            raise EngineeringToolError(
                "INVALID_ARGUMENT",
                "table names must use only letters, numbers, and underscores and cannot start with a number",
            )
        return f'"{identifier}"'

    @staticmethod
    def _table_exists(connection: duckdb.DuckDBPyConnection, table_name: str) -> bool:
        return (
            connection.execute(
                "SELECT 1 FROM information_schema.tables WHERE table_schema = 'main' "
                "AND table_name = ? LIMIT 1",
                [table_name],
            ).fetchone()
            is not None
        )

    @staticmethod
    def _backup_name(table_name: str, purpose: str) -> str:
        timestamp = datetime.now(tz=UTC).strftime("%Y%m%d%H%M%S")
        return f"{table_name}__{purpose}__{timestamp}_{uuid4().hex[:8]}"

    def _connect(self) -> duckdb.DuckDBPyConnection:
        return duckdb.connect(str(self.database_path))

    def _copy_table_with_schema(
        self,
        connection: duckdb.DuckDBPyConnection,
        source_name: str,
        destination_name: str,
    ) -> None:
        """Copy a DuckDB table's definition, data, and indexes for rollback safety."""
        source = self._quote_identifier(source_name)
        destination = self._quote_identifier(destination_name)
        definition_row = connection.execute(
            "SELECT sql FROM duckdb_tables() WHERE database_name = current_database() "
            "AND schema_name = 'main' AND table_name = ?",
            [source_name],
        ).fetchone()
        if definition_row is None or not definition_row[0]:
            raise EngineeringToolError(
                "NOT_FOUND", f"table definition for '{source_name}' was not found"
            )
        create_table_sql = re.sub(
            r'^(CREATE\s+TABLE\s+)(?:"(?:[^"]|"")*"|[A-Za-z_][A-Za-z0-9_]*)',
            lambda match: f"{match.group(1)}{destination}",
            definition_row[0],
            count=1,
        )
        if create_table_sql == definition_row[0]:
            raise EngineeringToolError(
                "CAPABILITY_UNAVAILABLE",
                f"could not safely clone the definition for table '{source_name}'",
            )
        connection.execute(create_table_sql)
        connection.execute(f"INSERT INTO {destination} SELECT * FROM {source}")
        indexes = connection.execute(
            "SELECT sql FROM duckdb_indexes() WHERE database_name = current_database() "
            "AND schema_name = 'main' AND table_name = ? ORDER BY index_name",
            [source_name],
        ).fetchall()
        for (index_sql,) in indexes:
            new_index_name = self._backup_name("idx", "copy")
            rewritten_index_sql = re.sub(
                r'^(CREATE\s+(?:UNIQUE\s+)?INDEX\s+)(?:"(?:[^"]|"")*"|[A-Za-z_][A-Za-z0-9_]*)',
                lambda match, index_name=new_index_name: (
                    f"{match.group(1)}{self._quote_identifier(index_name)}"
                ),
                index_sql,
                count=1,
            )
            rewritten_index_sql = re.sub(
                r'(\s+ON\s+)(?:"(?:[^"]|"")*"|[A-Za-z_][A-Za-z0-9_]*)',
                lambda match: f"{match.group(1)}{destination}",
                rewritten_index_sql,
                count=1,
            )
            if rewritten_index_sql == index_sql:
                raise EngineeringToolError(
                    "CAPABILITY_UNAVAILABLE",
                    f"could not safely clone an index for table '{source_name}'",
                )
            connection.execute(rewritten_index_sql)

    def _audit(self, action: str, **details: object) -> None:
        entry = {"observed_at": datetime.now(tz=UTC).isoformat(), "action": action, **details}
        with self.audit_path.open("a", encoding="utf-8") as audit_file:
            audit_file.write(json.dumps(entry, sort_keys=True) + "\n")

    def recommend_ingestion_pattern(self, request: IngestionRequest) -> IngestionRecommendation:
        if request.source_kind is SourceKind.STREAM:
            return IngestionRecommendation(
                pattern="streaming_ingestion",
                reasons=[
                    "The source emits events and should retain ordered, replayable event handling."
                ],
            )
        if request.source_kind is SourceKind.DATABASE and request.change_data_available:
            return IngestionRecommendation(
                pattern="log_based_cdc",
                reasons=[
                    "The database exposes change data, avoiding repeated full extracts.",
                    "CDC supports incremental history and lower source-system load.",
                ],
            )
        if request.source_kind is SourceKind.DATABASE:
            return IngestionRecommendation(
                pattern="incremental_watermark_batch",
                reasons=[
                    "No change feed is available, so a stable updated-at or surrogate-key watermark is required.",
                    "The design should retain a bounded overlap window for late updates.",
                ],
            )
        reasons = ["Files should be processed idempotently from immutable, partitioned arrivals."]
        if request.historical_backfill:
            reasons.append("Backfill should be isolated from the normal incremental checkpoint.")
        return IngestionRecommendation(pattern="file_microbatch", reasons=reasons)

    def generate_pipeline_plan(self, request: PipelinePlanRequest) -> PipelinePlan:
        if request.dataset_id is not None and request.dataset_id not in self.registry.datasets:
            raise EngineeringToolError("NOT_FOUND", f"dataset '{request.dataset_id}' was not found")
        recommendation = self.recommend_ingestion_pattern(
            IngestionRequest(
                source_kind=request.source_kind,
                expected_latency_minutes=60,
            )
        )
        requirement, reasons = self._approval(
            request.environment,
            contract_change=request.changes_dataset_contract,
            architecture_change=request.changes_architecture,
        )
        steps = [
            f"Confirm source access and use the {recommendation.pattern} ingestion pattern.",
            "Land immutable raw data with run identifiers and source checkpoints.",
            "Validate schema, required fields, and dataset contract references before publishing.",
            "Transform through the curated layer with deterministic data-quality checks.",
            "Run reconciliation against the source or prior trusted output.",
            "Run the configured DEV validation profile and record the deployment manifest.",
        ]
        return PipelinePlan(
            pipeline_name=request.pipeline_name,
            environment=request.environment,
            dataset_id=request.dataset_id,
            steps=steps,
            approval_requirement=requirement,
            approval_reasons=reasons,
        )

    def validate_dataset_contract(self, dataset: Dataset) -> ContractValidationResult:
        errors: list[str] = []
        if dataset.id in self.registry.datasets:
            errors.append(
                f"dataset '{dataset.id}' already exists; contract replacement requires an approved change"
            )
        for field_name in (
            "name",
            "description",
            "grain",
            "owner",
            "freshness_sla",
            "refresh_cadence",
        ):
            if not getattr(dataset, field_name).strip():
                errors.append(f"{field_name} must not be empty")
        candidate_registry = self.registry.model_copy(
            update={"datasets": {**self.registry.datasets, dataset.id: dataset}}
        )
        try:
            validate_registry(candidate_registry)
        except RegistryValidationError as error:
            errors.extend(error.messages)
        return ContractValidationResult(
            valid=not errors,
            dataset_id=dataset.id,
            errors=list(dict.fromkeys(errors)),
        )

    def run_dev_validation(self, profile: str = "unit") -> DevValidationResult:
        if profile == "registry":
            command = ["in-process", "validate_registry", "configured-service-registry"]
            try:
                validate_registry(self.registry)
            except RegistryValidationError as error:
                output = "\n".join(error.messages)
                status = ToolStatus.FAILED
                exit_code = 1
            else:
                output = "Configured service registry is valid."
                status = ToolStatus.SUCCEEDED
                exit_code = 0
            self._audit("run_dev_validation", profile=profile, status=status.value)
            return DevValidationResult(
                profile=profile,
                status=status,
                command=command,
                exit_code=exit_code,
                output=output,
            )
        commands = {
            "unit": [sys.executable, "-m", "unittest", "discover", "-s", "tests", "-v"],
        }
        if profile not in commands:
            raise EngineeringToolError("INVALID_ARGUMENT", "profile must be one of: registry, unit")
        command = commands[profile]
        completed = subprocess.run(
            command,
            cwd=self.repository_root,
            check=False,
            text=True,
            capture_output=True,
            timeout=120,
        )
        output = (completed.stdout + completed.stderr)[-8_000:]
        status = ToolStatus.SUCCEEDED if completed.returncode == 0 else ToolStatus.FAILED
        self._audit("run_dev_validation", profile=profile, status=status.value)
        return DevValidationResult(
            profile=profile,
            status=status,
            command=command,
            exit_code=completed.returncode,
            output=output,
        )

    def run_reconciliation(self, source_table: str, target_table: str) -> ReconciliationResult:
        source = self._quote_identifier(source_table)
        target = self._quote_identifier(target_table)
        connection = self._connect()
        try:
            for table_name in (source_table, target_table):
                if not self._table_exists(connection, table_name):
                    raise EngineeringToolError("NOT_FOUND", f"table '{table_name}' was not found")
            source_count = connection.execute(f"SELECT COUNT(*) FROM {source}").fetchone()[0]
            target_count = connection.execute(f"SELECT COUNT(*) FROM {target}").fetchone()[0]
            source_only = connection.execute(
                f"SELECT COUNT(*) FROM (SELECT * FROM {source} EXCEPT ALL SELECT * FROM {target})"
            ).fetchone()[0]
            target_only = connection.execute(
                f"SELECT COUNT(*) FROM (SELECT * FROM {target} EXCEPT ALL SELECT * FROM {source})"
            ).fetchone()[0]
        finally:
            connection.close()
        matched = source_only == 0 and target_only == 0
        status = ToolStatus.SUCCEEDED if matched else ToolStatus.FAILED
        self._audit(
            "run_reconciliation",
            source_table=source_table,
            target_table=target_table,
            status=status.value,
        )
        return ReconciliationResult(
            status=status,
            source_table=source_table,
            target_table=target_table,
            source_row_count=source_count,
            target_row_count=target_count,
            source_only_row_count=source_only,
            target_only_row_count=target_only,
            matched=matched,
        )

    def safe_replace_table(self, request: SafeReplaceRequest) -> SafeReplaceResult:
        target = self._quote_identifier(request.target_table)
        replacement = self._quote_identifier(request.replacement_table)
        requirement, reasons = self._approval(request.environment)
        if request.environment is not Environment.DEVELOPMENT:
            return SafeReplaceResult(
                status=ToolStatus.APPROVAL_REQUIRED,
                target_table=request.target_table,
                replacement_table=request.replacement_table,
                approval_requirement=ApprovalRequirement.REQUIRED,
                message=(
                    "only the configured local DEVELOPMENT database is executable; "
                    f"{request.environment.value} execution requires a future environment adapter"
                ),
            )
        if request.environment is Environment.DEVELOPMENT and not self.allow_development_writes:
            return SafeReplaceResult(
                status=ToolStatus.APPROVAL_REQUIRED,
                target_table=request.target_table,
                replacement_table=request.replacement_table,
                approval_requirement=ApprovalRequirement.REQUIRED,
                message="development writes are disabled by server configuration",
            )
        if requirement is ApprovalRequirement.REQUIRED and not request.approval_granted:
            return SafeReplaceResult(
                status=ToolStatus.APPROVAL_REQUIRED,
                target_table=request.target_table,
                replacement_table=request.replacement_table,
                approval_requirement=requirement,
                message="; ".join(reasons),
            )
        connection = self._connect()
        backup_name = self._backup_name(request.target_table, "backup")
        try:
            if not self._table_exists(connection, request.target_table):
                raise EngineeringToolError(
                    "NOT_FOUND", f"table '{request.target_table}' was not found"
                )
            if not self._table_exists(connection, request.replacement_table):
                raise EngineeringToolError(
                    "NOT_FOUND", f"table '{request.replacement_table}' was not found"
                )
            replacement_rows = connection.execute(f"SELECT COUNT(*) FROM {replacement}").fetchone()[
                0
            ]
            connection.execute("BEGIN TRANSACTION")
            self._copy_table_with_schema(connection, request.target_table, backup_name)
            connection.execute(f"DROP TABLE {target}")
            connection.execute(f"ALTER TABLE {replacement} RENAME TO {target}")
            connection.execute("COMMIT")
        except Exception:
            try:
                connection.execute("ROLLBACK")
            except duckdb.Error:
                pass
            raise
        finally:
            connection.close()
        self._audit(
            "safe_replace_table",
            target_table=request.target_table,
            replacement_table=request.replacement_table,
            backup_table=backup_name,
            status=ToolStatus.SUCCEEDED.value,
        )
        return SafeReplaceResult(
            status=ToolStatus.SUCCEEDED,
            target_table=request.target_table,
            replacement_table=request.replacement_table,
            backup_table=backup_name,
            rows_replaced=replacement_rows,
            approval_requirement=requirement,
            message="Replacement completed atomically; use the backup table for deterministic rollback.",
        )

    def restore_table_from_backup(self, request: RestoreTableRequest) -> SafeReplaceResult:
        target = self._quote_identifier(request.target_table)
        backup = self._quote_identifier(request.backup_table)
        requirement, reasons = self._approval(request.environment)
        if request.environment is not Environment.DEVELOPMENT:
            return SafeReplaceResult(
                status=ToolStatus.APPROVAL_REQUIRED,
                target_table=request.target_table,
                replacement_table=request.backup_table,
                approval_requirement=ApprovalRequirement.REQUIRED,
                message=(
                    "only the configured local DEVELOPMENT database is executable; "
                    f"{request.environment.value} execution requires a future environment adapter"
                ),
            )
        if not self.allow_development_writes:
            return SafeReplaceResult(
                status=ToolStatus.APPROVAL_REQUIRED,
                target_table=request.target_table,
                replacement_table=request.backup_table,
                approval_requirement=ApprovalRequirement.REQUIRED,
                message="development writes are disabled by server configuration",
            )
        if requirement is ApprovalRequirement.REQUIRED and not request.approval_granted:
            return SafeReplaceResult(
                status=ToolStatus.APPROVAL_REQUIRED,
                target_table=request.target_table,
                replacement_table=request.backup_table,
                approval_requirement=requirement,
                message="; ".join(reasons),
            )
        connection = self._connect()
        prior_name = self._backup_name(request.target_table, "before_restore")
        try:
            if not self._table_exists(connection, request.target_table):
                raise EngineeringToolError(
                    "NOT_FOUND", f"table '{request.target_table}' was not found"
                )
            if not self._table_exists(connection, request.backup_table):
                raise EngineeringToolError(
                    "NOT_FOUND", f"table '{request.backup_table}' was not found"
                )
            restored_rows = connection.execute(f"SELECT COUNT(*) FROM {backup}").fetchone()[0]
            connection.execute("BEGIN TRANSACTION")
            self._copy_table_with_schema(connection, request.target_table, prior_name)
            connection.execute(f"DROP TABLE {target}")
            self._copy_table_with_schema(connection, request.backup_table, request.target_table)
            connection.execute(f"DROP TABLE {backup}")
            connection.execute("COMMIT")
        except Exception:
            try:
                connection.execute("ROLLBACK")
            except duckdb.Error:
                pass
            raise
        finally:
            connection.close()
        self._audit(
            "restore_table_from_backup",
            target_table=request.target_table,
            backup_table=request.backup_table,
            prior_table=prior_name,
            status=ToolStatus.SUCCEEDED.value,
        )
        return SafeReplaceResult(
            status=ToolStatus.SUCCEEDED,
            target_table=request.target_table,
            replacement_table=request.backup_table,
            backup_table=prior_name,
            rows_replaced=restored_rows,
            approval_requirement=requirement,
            message="Rollback completed atomically; the pre-rollback table was retained as a backup.",
        )

    def deploy_pipeline_to_dev(self, request: DeploymentRequest) -> DeploymentResult:
        requirement, reasons = self._approval(request.environment)
        if request.environment is not Environment.DEVELOPMENT:
            return DeploymentResult(
                status=ToolStatus.APPROVAL_REQUIRED,
                environment=request.environment,
                artifact_path=request.artifact_path,
                approval_requirement=ApprovalRequirement.REQUIRED,
                message=(
                    "only DEVELOPMENT deployment is implemented; an approved environment-specific "
                    "adapter is required for QA or production"
                ),
            )
        if requirement is ApprovalRequirement.REQUIRED and not request.approval_granted:
            return DeploymentResult(
                status=ToolStatus.APPROVAL_REQUIRED,
                environment=request.environment,
                artifact_path=request.artifact_path,
                approval_requirement=requirement,
                message="; ".join(reasons),
            )
        artifact = (self.repository_root / request.artifact_path).resolve()
        try:
            relative_path = artifact.relative_to(self.repository_root)
        except ValueError as error:
            raise EngineeringToolError(
                "INVALID_ARGUMENT", "artifact_path must stay within the configured repository"
            ) from error
        if not artifact.is_file():
            raise EngineeringToolError("NOT_FOUND", f"artifact '{relative_path}' was not found")
        if artifact.suffix.lower() not in _ALLOWED_ARTIFACT_SUFFIXES:
            raise EngineeringToolError(
                "INVALID_ARGUMENT", "artifact type is not approved for DEV deployment"
            )
        validation = self.run_dev_validation(request.validation_profile)
        if validation.status is not ToolStatus.SUCCEEDED:
            return DeploymentResult(
                status=ToolStatus.FAILED,
                environment=request.environment,
                artifact_path=str(relative_path),
                validation=validation,
                approval_requirement=requirement,
                message="DEV deployment was blocked because validation failed.",
            )
        digest = hashlib.sha256(artifact.read_bytes()).hexdigest()
        deployment_id = (
            f"deployment-{datetime.now(tz=UTC).strftime('%Y%m%d%H%M%S')}-{uuid4().hex[:8]}"
        )
        manifest = self.state_directory / f"{deployment_id}.json"
        manifest.write_text(
            json.dumps(
                {
                    "deployment_id": deployment_id,
                    "environment": request.environment.value,
                    "artifact_path": str(relative_path),
                    "artifact_sha256": digest,
                    "validation_profile": request.validation_profile,
                    "validated_at": datetime.now(tz=UTC).isoformat(),
                },
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        self._audit(
            "deploy_pipeline_to_dev",
            artifact_path=str(relative_path),
            artifact_sha256=digest,
            manifest_path=str(manifest),
            status=ToolStatus.SUCCEEDED.value,
        )
        return DeploymentResult(
            status=ToolStatus.SUCCEEDED,
            environment=request.environment,
            artifact_path=str(relative_path),
            artifact_sha256=digest,
            manifest_path=str(manifest),
            validation=validation,
            approval_requirement=requirement,
            message="DEV deployment manifest recorded after configured validation.",
        )
