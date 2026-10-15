"""Build a candidate table and evaluate contract, quality, and reconciliation gates.

Shared by the DEV runner and agent dry-runs so both apply identical rules.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation

import duckdb

from ai_data_platform.models import Dataset

from .models import (
    AcceptedValuesCheck,
    CheckOutcome,
    NotNullCheck,
    PipelineSpec,
    RangeCheck,
    Reconciliation,
    RowCountCheck,
    UniqueCheck,
)
from .sql import dataset_relation, qualified, render, source_relation, validate_select


@dataclass
class BuildOutcome:
    row_count: int | None = None
    schema: list[dict[str, object]] = field(default_factory=list)
    contract_errors: list[str] = field(default_factory=list)
    checks: list[CheckOutcome] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return not self.contract_errors and all(check.passed for check in self.checks)


def input_relations(spec: PipelineSpec) -> set[str]:
    return {qualified(source_relation(item)) for item in spec.inputs.sources} | {
        qualified(dataset_relation(item)) for item in spec.inputs.datasets
    }


def _q(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'


def _contract_errors(schema: list[dict[str, object]], contract: Dataset) -> list[str]:
    actual = {str(column["name"]): str(column["type"]).replace(", ", ",") for column in schema}
    expected = {column.name: column.type for column in contract.columns}
    errors = [f"missing contract column '{name}'" for name in expected if name not in actual]
    errors.extend(
        f"column '{name}' is not in the contract (additive change needs a contract update)"
        for name in actual
        if name not in expected
    )
    errors.extend(
        f"column '{name}' has type {actual[name]}, contract requires {expected[name]}"
        for name in expected
        if name in actual and actual[name] != expected[name]
    )
    return errors


def _scalar(connection: duckdb.DuckDBPyConnection, sql: str, parameters: list | None = None):
    return connection.execute(sql, parameters or []).fetchone()[0]


def _quality_check(
    connection: duckdb.DuckDBPyConnection,
    table: str,
    check: object,
    row_count: int,
) -> CheckOutcome:
    if isinstance(check, NotNullCheck):
        expressions = ", ".join(f"COUNT(*) FILTER (WHERE {_q(column)} IS NULL)" for column in check.columns)
        counts = connection.execute(f"SELECT {expressions} FROM {table}").fetchone()
        failures = {column: count for column, count in zip(check.columns, counts, strict=True) if count}
        detail = (
            "no nulls"
            if not failures
            else ", ".join(f"{column}: {count} nulls" for column, count in failures.items())
        )
        return CheckOutcome(name=f"not_null({', '.join(check.columns)})", passed=not failures, detail=detail)
    if isinstance(check, UniqueCheck):
        columns = ", ".join(_q(column) for column in check.columns)
        duplicates = _scalar(
            connection,
            f"SELECT COUNT(*) FROM (SELECT {columns} FROM {table} GROUP BY ALL HAVING COUNT(*) > 1)",
        )
        return CheckOutcome(
            name=f"unique({', '.join(check.columns)})",
            passed=duplicates == 0,
            detail="no duplicate keys" if duplicates == 0 else f"{duplicates} duplicated key values",
        )
    if isinstance(check, AcceptedValuesCheck):
        placeholders = ", ".join("?" for _ in check.values)
        invalid = _scalar(
            connection,
            f"SELECT COUNT(*) FROM {table} WHERE {_q(check.column)} IS NOT NULL "
            f"AND CAST({_q(check.column)} AS VARCHAR) NOT IN ({placeholders})",
            list(check.values),
        )
        return CheckOutcome(
            name=f"accepted_values({check.column})",
            passed=invalid == 0,
            detail="all values accepted" if invalid == 0 else f"{invalid} rows outside {check.values}",
        )
    if isinstance(check, RangeCheck):
        conditions, parameters = [], []
        if check.min is not None:
            conditions.append(f"{_q(check.column)} < ?")
            parameters.append(check.min)
        if check.max is not None:
            conditions.append(f"{_q(check.column)} > ?")
            parameters.append(check.max)
        violations = _scalar(
            connection, f"SELECT COUNT(*) FROM {table} WHERE {' OR '.join(conditions)}", parameters
        )
        bounds = f"[{check.min if check.min is not None else '-inf'}, {check.max if check.max is not None else 'inf'}]"
        return CheckOutcome(
            name=f"range({check.column})",
            passed=violations == 0,
            detail=f"all values within {bounds}" if violations == 0 else f"{violations} rows outside {bounds}",
        )
    if isinstance(check, RowCountCheck):
        passed = row_count >= check.min and (check.max is None or row_count <= check.max)
        bounds = f">= {check.min}" + (f" and <= {check.max}" if check.max is not None else "")
        return CheckOutcome(name="row_count", passed=passed, detail=f"{row_count} rows (required {bounds})")
    raise TypeError(f"unsupported check {check!r}")


def _decimal(value: object) -> Decimal | None:
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except InvalidOperation as error:
        raise ValueError(f"reconciliation value {value!r} is not numeric") from error


def _reconcile(
    connection: duckdb.DuckDBPyConnection,
    spec: PipelineSpec,
    reconciliation: Reconciliation,
    candidate: str,
) -> CheckOutcome:
    allowed = input_relations(spec) | {candidate}
    values: list[Decimal | None] = []
    for sql in (reconciliation.expected_sql, reconciliation.actual_sql):
        rendered = render(sql, sources=spec.inputs.sources, datasets=spec.inputs.datasets, this=candidate)
        validate_select(connection, rendered.sql, allowed)
        rows = connection.execute(rendered.sql).fetchall()
        if len(rows) != 1 or len(rows[0]) != 1:
            raise ValueError(f"reconciliation '{reconciliation.name}' SQL must return one scalar")
        values.append(_decimal(rows[0][0]))
    expected, actual = values
    if expected is None or actual is None:
        passed = expected is None and actual is None
        difference = None
    else:
        difference = abs(expected - actual)
        passed = difference <= reconciliation.tolerance
    return CheckOutcome(
        name=f"reconcile: {reconciliation.name}",
        passed=passed,
        detail=f"expected={expected} actual={actual} difference={difference} tolerance={reconciliation.tolerance}",
    )


def build_candidate(
    connection: duckdb.DuckDBPyConnection,
    spec: PipelineSpec,
    contract: Dataset,
    candidate: str,
) -> BuildOutcome:
    """Create ``candidate`` from the spec SQL, then run every gate against it."""
    rendered = render(spec.sql, sources=spec.inputs.sources, datasets=spec.inputs.datasets)
    validate_select(connection, rendered.sql, input_relations(spec))
    connection.execute(f"CREATE TABLE {candidate} AS {rendered.sql}")
    outcome = BuildOutcome()
    outcome.schema = [
        {"name": row[0], "type": row[1], "nullable": row[2] == "YES"}
        for row in connection.execute(f"DESCRIBE {candidate}").fetchall()
    ]
    outcome.contract_errors = _contract_errors(outcome.schema, contract)
    if outcome.contract_errors:
        return outcome
    outcome.row_count = _scalar(connection, f"SELECT COUNT(*) FROM {candidate}")
    required = [column.name for column in contract.columns if not column.nullable]
    checks: list[object] = []
    if required:
        checks.append(NotNullCheck(check="not_null", columns=required))
    checks.extend(spec.quality_checks)
    outcome.checks = [_quality_check(connection, candidate, check, outcome.row_count) for check in checks]
    outcome.checks.extend(
        _reconcile(connection, spec, reconciliation, candidate) for reconciliation in spec.reconciliations
    )
    return outcome
