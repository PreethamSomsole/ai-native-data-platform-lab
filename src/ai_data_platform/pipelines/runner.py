"""Local DEV runner: land sources, build candidates, gate them, publish, and report."""

from __future__ import annotations

import hashlib
import json
import time
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import duckdb

from ai_data_platform.engineering import EngineeringService, SafeReplaceRequest, ToolStatus
from ai_data_platform.models import Registry
from ai_data_platform.runtime import (
    DatasetRuntimeMetadata,
    FreshnessStatus,
    OperationalHealth,
    QualityStatus,
    RuntimeMetadataRepository,
)

from .execution import build_candidate
from .loader import PipelineProject, PipelineValidationError, validate_pipeline_project
from .models import IngestionResult, PipelineRunResult, PipelineSpec, RunStatus, SourceFormat
from .sql import (
    PipelineSqlError,
    dataset_relation,
    qualified,
    sandboxed_connection,
    source_relation,
)

RUNS_TABLE = "_platform_pipeline_runs"
LINEAGE_TABLE = "_platform_lineage"


def _utc(value: datetime) -> datetime:
    """Platform tables store naive UTC timestamps (TIMESTAMPTZ reads would require pytz)."""
    return value.astimezone(UTC).replace(tzinfo=None)


class PipelineRunner:
    """Executes repository pipeline specs against the EngineeringService DEV warehouse."""

    def __init__(
        self,
        registry: Registry,
        project: PipelineProject,
        engineering: EngineeringService,
        data_directory: str | Path,
        *,
        runtime_repository: RuntimeMetadataRepository | None = None,
        memory_limit: str | None = None,
        keep_backups: int = 1,
    ) -> None:
        validate_pipeline_project(project, registry)
        self.registry = registry
        self.project = project
        self.engineering = engineering
        self.data_directory = Path(data_directory).resolve()
        self.runtime_repository = runtime_repository
        self.memory_limit = memory_limit
        self.keep_backups = keep_backups
        self.warehouse_path = engineering.database_path
        self.temp_directory = engineering.state_directory / "duckdb_tmp"
        self._ensure_platform_tables()

    def _connect(self) -> duckdb.DuckDBPyConnection:
        connection = duckdb.connect(str(self.warehouse_path))
        connection.execute("SET enable_progress_bar = false")
        return connection

    def _ensure_platform_tables(self) -> None:
        with self._connect() as connection:
            connection.execute(
                f"""
                CREATE TABLE IF NOT EXISTS {RUNS_TABLE} (
                    run_id VARCHAR, pipeline_id VARCHAR, output VARCHAR, status VARCHAR,
                    started_at TIMESTAMP, finished_at TIMESTAMP, duration_seconds DOUBLE,
                    row_count BIGINT, checks_passed INTEGER, checks_failed INTEGER,
                    spec_sha256 VARCHAR, message VARCHAR, checks VARCHAR
                )
                """
            )
            connection.execute(
                f"""
                CREATE TABLE IF NOT EXISTS {LINEAGE_TABLE} (
                    pipeline_id VARCHAR, output VARCHAR, input_kind VARCHAR, input_id VARCHAR,
                    verified_run_id VARCHAR, verified_at TIMESTAMP
                )
                """
            )

    def _source_path(self, relative: str) -> Path:
        path = (self.data_directory / relative).resolve()
        if not path.is_relative_to(self.data_directory):
            raise PipelineValidationError([f"source path '{relative}' escapes the data directory"])
        return path

    def ingest(self, source_ids: list[str] | None = None) -> list[IngestionResult]:
        """Land each source file set as a raw table (platform-controlled file access)."""
        selected = source_ids or sorted(self.project.sources)
        unknown = sorted(set(selected) - set(self.project.sources))
        if unknown:
            raise PipelineValidationError([f"unknown sources: {', '.join(unknown)}"])
        results = []
        with self._connect() as connection:
            for source_id in selected:
                source = self.project.sources[source_id]
                path = self._source_path(source.path)
                if not any(path.parent.glob(path.name)):
                    raise PipelineValidationError(
                        [f"no files match {path} for {source_id}; generate sample data first"]
                    )
                reader = (
                    "read_parquet(?, union_by_name = true)"
                    if source.format is SourceFormat.PARQUET
                    else "read_csv(?, header = true, auto_detect = true)"
                )
                table = source_relation(source_id)
                started = time.perf_counter()
                connection.execute(f'CREATE OR REPLACE TABLE "{table}" AS SELECT * FROM {reader}', [str(path)])
                rows = connection.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
                results.append(
                    IngestionResult(
                        source_id=source_id,
                        raw_table=table,
                        row_count=rows,
                        duration_seconds=round(time.perf_counter() - started, 3),
                    )
                )
        for result in results:
            self.engineering.record_audit_event(
                "ingest_source", source_id=result.source_id, raw_table=result.raw_table, row_count=result.row_count
            )
        return results

    def _relation_exists(self, relation: str) -> bool:
        with self._connect() as connection:
            return (
                connection.execute(
                    "SELECT 1 FROM information_schema.tables WHERE table_schema = 'main' AND table_name = ?",
                    [relation],
                ).fetchone()
                is not None
            )

    def _published_row_count(self, relation: str) -> int | None:
        if not self._relation_exists(relation):
            return None
        with self._connect() as connection:
            return connection.execute(f'SELECT COUNT(*) FROM "{relation}"').fetchone()[0]

    def _missing_inputs(self, spec: PipelineSpec) -> list[str]:
        missing = [item for item in spec.inputs.sources if not self._relation_exists(source_relation(item))]
        missing += [item for item in spec.inputs.datasets if not self._relation_exists(dataset_relation(item))]
        return missing

    def run(
        self,
        select: list[str] | None = None,
        *,
        include_downstream: bool = True,
    ) -> list[PipelineRunResult]:
        """Run pipelines in dependency order; failed upstreams skip their dependants."""
        order = self.project.execution_order()
        if select:
            unknown = sorted(set(select) - set(self.project.pipelines))
            if unknown:
                raise PipelineValidationError([f"unknown pipelines: {', '.join(unknown)}"])
            chosen = self.project.downstream_of(set(select)) if include_downstream else set(select)
            order = [spec for spec in order if spec.id in chosen]
        blocked: set[str] = set()
        results = []
        for spec in order:
            upstream_blocked = sorted(set(spec.inputs.datasets) & blocked)
            if upstream_blocked:
                result = self._result(
                    spec,
                    RunStatus.SKIPPED,
                    datetime.now(tz=UTC),
                    time.perf_counter(),
                    message=f"skipped because upstream did not publish: {', '.join(upstream_blocked)}",
                )
            else:
                result = self._run_one(spec)
            if result.status is not RunStatus.SUCCEEDED:
                blocked.add(spec.output)
            self._record(spec, result)
            results.append(result)
        return results

    def _result(self, spec: PipelineSpec, status: RunStatus, started_at: datetime, started: float, **values) -> PipelineRunResult:
        return PipelineRunResult(
            run_id=values.pop("run_id", f"run-{uuid4().hex[:12]}"),
            pipeline_id=spec.id,
            output=spec.output,
            status=status,
            started_at=started_at,
            finished_at=datetime.now(tz=UTC),
            duration_seconds=round(time.perf_counter() - started, 3),
            published_row_count=values.pop(
                "published_row_count", self._published_row_count(dataset_relation(spec.output))
            ),
            **values,
        )

    def _run_one(self, spec: PipelineSpec) -> PipelineRunResult:
        run_id = f"run-{uuid4().hex[:12]}"
        started_at, started = datetime.now(tz=UTC), time.perf_counter()
        target = dataset_relation(spec.output)
        staging = f"{target}__staging_{run_id[-8:]}"
        missing = self._missing_inputs(spec)
        if missing:
            return self._result(
                spec, RunStatus.FAILED_ERROR, started_at, started, run_id=run_id,
                message=f"inputs are not available yet: {', '.join(missing)}",
            )
        contract = self.registry.datasets[spec.output]
        connection = sandboxed_connection(
            self.warehouse_path, read_only=False, temp_directory=self.temp_directory, memory_limit=self.memory_limit
        )
        try:
            outcome = build_candidate(connection, spec, contract, qualified(staging))
        except (PipelineSqlError, ValueError, duckdb.Error) as error:
            self._drop(connection, staging)
            connection.close()
            return self._result(spec, RunStatus.FAILED_ERROR, started_at, started, run_id=run_id, message=str(error))
        if not outcome.passed:
            self._drop(connection, staging)
            connection.close()
            status = RunStatus.FAILED_CONTRACT if outcome.contract_errors else RunStatus.FAILED_QUALITY
            message = (
                "; ".join(outcome.contract_errors)
                if outcome.contract_errors
                else "candidate discarded; the published table was left unchanged"
            )
            return self._result(
                spec, status, started_at, started, run_id=run_id,
                row_count=outcome.row_count, checks=outcome.checks, message=message,
            )
        connection.close()
        request = SafeReplaceRequest(target_table=target, replacement_table=staging)
        if self._relation_exists(target):
            publication = self.engineering.safe_replace_table(request)
        else:
            publication = self.engineering.publish_new_table(request)
        if publication.status is not ToolStatus.SUCCEEDED:
            return self._result(
                spec, RunStatus.FAILED_ERROR, started_at, started, run_id=run_id,
                row_count=outcome.row_count, checks=outcome.checks, message=publication.message,
            )
        self.engineering.prune_backups(
            target, keep=self.keep_backups, preserve=publication.backup_table
        )
        return self._result(
            spec, RunStatus.SUCCEEDED, started_at, started, run_id=run_id,
            row_count=outcome.row_count, published_row_count=outcome.row_count,
            backup_table=publication.backup_table, checks=outcome.checks,
            message=publication.message,
        )

    @staticmethod
    def _drop(connection: duckdb.DuckDBPyConnection, staging: str) -> None:
        try:
            connection.execute(f"DROP TABLE IF EXISTS {qualified(staging)}")
        except duckdb.Error:
            pass

    def _record(self, spec: PipelineSpec, result: PipelineRunResult) -> None:
        spec_hash = hashlib.sha256(spec.model_dump_json().encode()).hexdigest()
        with self._connect() as connection:
            connection.execute(
                f"INSERT INTO {RUNS_TABLE} VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    result.run_id, spec.id, spec.output, result.status.value,
                    _utc(result.started_at), _utc(result.finished_at), result.duration_seconds,
                    result.row_count, result.checks_passed, result.checks_failed,
                    spec_hash, result.message,
                    json.dumps([check.model_dump() for check in result.checks]),
                ],
            )
            if result.status is RunStatus.SUCCEEDED:
                connection.execute(f"DELETE FROM {LINEAGE_TABLE} WHERE pipeline_id = ?", [spec.id])
                for edge in self.project.lineage():
                    if edge.pipeline_id == spec.id:
                        connection.execute(
                            f"INSERT INTO {LINEAGE_TABLE} VALUES (?, ?, ?, ?, ?, ?)",
                            [edge.pipeline_id, edge.output, edge.input_kind, edge.input_id, result.run_id, _utc(result.finished_at)],
                        )
        self.engineering.record_audit_event(
            "pipeline_run",
            pipeline_id=spec.id,
            output=spec.output,
            run_id=result.run_id,
            status=result.status.value,
            spec_sha256=spec_hash,
        )
        if self.runtime_repository is not None and result.status is not RunStatus.SKIPPED:
            self.runtime_repository.record(self._observation(spec, result))

    def _observation(self, spec: PipelineSpec, result: PipelineRunResult) -> DatasetRuntimeMetadata:
        previous = self.runtime_repository.get_latest(spec.output) if self.runtime_repository else None
        succeeded = result.status is RunStatus.SUCCEEDED
        if succeeded:
            quality = QualityStatus.PASSING
        elif result.status in (RunStatus.FAILED_QUALITY, RunStatus.FAILED_CONTRACT):
            quality = QualityStatus.FAILING
        else:
            quality = QualityStatus.UNKNOWN
        return DatasetRuntimeMetadata(
            dataset_id=spec.output,
            observed_at=result.finished_at,
            source=f"pipeline_runner:{spec.id}",
            last_successful_run_at=(
                result.finished_at if succeeded else (previous.last_successful_run_at if previous else None)
            ),
            freshness_status=FreshnessStatus.FRESH if succeeded else FreshnessStatus.STALE,
            quality_status=quality,
            quality_checks_passed=result.checks_passed,
            quality_checks_failed=result.checks_failed + (1 if result.status is RunStatus.FAILED_CONTRACT else 0),
            row_count=result.published_row_count,
            usage_count_30d=previous.usage_count_30d if previous else 0,
            operational_health=(
                OperationalHealth.HEALTHY
                if succeeded
                else OperationalHealth.FAILED
                if result.status is RunStatus.FAILED_ERROR
                else OperationalHealth.DEGRADED
            ),
        )

    def run_history(self, limit: int = 50) -> list[dict[str, object]]:
        with self._connect() as connection:
            rows = connection.execute(
                f"SELECT run_id, pipeline_id, output, status, finished_at, duration_seconds, row_count, "
                f"checks_passed, checks_failed, message FROM {RUNS_TABLE} ORDER BY finished_at DESC LIMIT ?",
                [limit],
            ).fetchall()
            columns = [item[0] for item in connection.description]
        return [dict(zip(columns, row, strict=True)) for row in rows]

    def verified_lineage(self) -> list[dict[str, object]]:
        with self._connect() as connection:
            rows = connection.execute(
                f"SELECT pipeline_id, output, input_kind, input_id, verified_run_id, verified_at "
                f"FROM {LINEAGE_TABLE} ORDER BY output, input_kind, input_id"
            ).fetchall()
            columns = [item[0] for item in connection.description]
        return [dict(zip(columns, row, strict=True)) for row in rows]
