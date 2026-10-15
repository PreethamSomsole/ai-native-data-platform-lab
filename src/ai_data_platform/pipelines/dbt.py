"""Compile pipeline specs into a runnable dbt project (dbt-duckdb by default).

The spec SQL already uses dbt's ``ref()``/``source()`` syntax, so compilation is a name
mapping plus generated YAML: dataset contracts become enforced dbt model contracts, column
checks become generic tests, and multi-column/range/row-count/reconciliation gates become
singular tests. Only built-in dbt tests are used, so no package download is required.
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

from ai_data_platform.models import Registry

from .loader import PipelineProject
from .models import AcceptedValuesCheck, NotNullCheck, RangeCheck, RowCountCheck, UniqueCheck
from .sql import dataset_relation, source_relation

PROJECT_NAME = "ai_native_platform"
_REF = re.compile(r"ref\(\s*'([a-z][a-z0-9_]*\.[a-z][a-z0-9_]*)'\s*\)")
_THIS = re.compile(r"\{\{\s*this\s*\}\}")


def _dbt_sql(sql: str, model: str | None = None) -> str:
    rendered = _REF.sub(lambda match: f"ref('{dataset_relation(match.group(1))}')", sql)
    if model is not None:
        rendered = _THIS.sub(f"{{{{ ref('{model}') }}}}", rendered)
    return rendered.strip()


def _dump(data: object) -> str:
    return yaml.safe_dump(data, sort_keys=False, width=100)


def export_dbt_project(
    project: PipelineProject,
    registry: Registry,
    output_directory: str | Path,
    *,
    warehouse_path: str | Path,
    target_database: str | Path | None = None,
) -> list[Path]:
    """Write the dbt project; raw sources are read from the attached DEV warehouse."""
    root = Path(output_directory).resolve()
    target = Path(target_database).resolve() if target_database else root / "dbt_dev.duckdb"
    written: list[Path] = []

    def write(relative: str, content: str) -> None:
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        written.append(path)

    write(
        "dbt_project.yml",
        _dump(
            {
                "name": PROJECT_NAME,
                "version": "1.0.0",
                "config-version": 2,
                "profile": PROJECT_NAME,
                "model-paths": ["models"],
                "test-paths": ["tests"],
                "models": {PROJECT_NAME: {"+materialized": "table"}},
                "flags": {"send_anonymous_usage_stats": False},
            }
        ),
    )
    write(
        "profiles.yml",
        _dump(
            {
                PROJECT_NAME: {
                    "target": "dev",
                    "outputs": {
                        "dev": {
                            "type": "duckdb",
                            "path": str(target),
                            "threads": 4,
                            "attach": [
                                {"path": str(Path(warehouse_path).resolve()), "alias": "wh", "read_only": True}
                            ],
                        }
                    },
                }
            }
        ),
    )
    systems: dict[str, list[dict[str, str]]] = {}
    for source in sorted(project.sources.values(), key=lambda item: item.id):
        systems.setdefault(source.system, []).append(
            {"name": source.table, "identifier": source_relation(source.id), "description": source.description}
        )
    write(
        "models/sources.yml",
        _dump(
            {
                "version": 2,
                "sources": [
                    {"name": system, "database": "wh", "schema": "main", "tables": tables}
                    for system, tables in systems.items()
                ],
            }
        ),
    )

    for spec in project.execution_order():
        contract = registry.datasets[spec.output]
        model = dataset_relation(spec.output)
        layer = spec.output.split(".", 1)[0]
        write(f"models/{layer}/{model}.sql", f"-- generated from {spec.id}; edit the pipeline spec instead\n{_dbt_sql(spec.sql)}\n")
        column_tests: dict[str, list[object]] = {column.name: [] for column in contract.columns}
        for check in spec.quality_checks:
            if isinstance(check, UniqueCheck) and len(check.columns) == 1:
                column_tests[check.columns[0]].append("unique")
            elif isinstance(check, AcceptedValuesCheck):
                column_tests[check.column].append({"accepted_values": {"arguments": {"values": check.values}}})
            elif isinstance(check, NotNullCheck):
                for column in check.columns:
                    column_tests[column].append("not_null")
        columns = []
        for column in contract.columns:
            entry: dict[str, object] = {
                "name": column.name,
                "data_type": column.type.lower(),
                "description": column.description,
            }
            if not column.nullable:
                entry["constraints"] = [{"type": "not_null"}]
            if column_tests[column.name]:
                entry["data_tests"] = column_tests[column.name]
            columns.append(entry)
        write(
            f"models/{layer}/{model}.yml",
            _dump(
                {
                    "version": 2,
                    "models": [
                        {
                            "name": model,
                            "description": contract.description,
                            "config": {
                                "contract": {"enforced": True},
                                "meta": {
                                    "dataset_id": spec.output,
                                    "pipeline_id": spec.id,
                                    "owner": contract.owner,
                                    "certification": contract.certification.value,
                                },
                            },
                            "columns": columns,
                        }
                    ],
                }
            ),
        )
        reference = f"{{{{ ref('{model}') }}}}"
        singular = 0
        for check in spec.quality_checks:
            sql = None
            if isinstance(check, UniqueCheck) and len(check.columns) > 1:
                keys = ", ".join(check.columns)
                sql = f"select {keys}, count(*) as rows_per_key\nfrom {reference}\ngroup by {keys}\nhaving count(*) > 1"
            elif isinstance(check, RangeCheck):
                conditions = []
                if check.min is not None:
                    conditions.append(f"{check.column} < {check.min}")
                if check.max is not None:
                    conditions.append(f"{check.column} > {check.max}")
                sql = f"select *\nfrom {reference}\nwhere {' or '.join(conditions)}"
            elif isinstance(check, RowCountCheck):
                bounds = [f"count(*) < {check.min}"] + ([f"count(*) > {check.max}"] if check.max is not None else [])
                sql = f"select count(*) as row_count\nfrom {reference}\nhaving {' or '.join(bounds)}"
            if sql is not None:
                singular += 1
                write(f"tests/{model}__{check.check}_{singular}.sql", f"-- {spec.id}: {check.check}\n{sql}\n")
        for index, reconciliation in enumerate(spec.reconciliations, start=1):
            expected = _dbt_sql(reconciliation.expected_sql, model)
            actual = _dbt_sql(reconciliation.actual_sql, model)
            write(
                f"tests/{model}__reconcile_{index}.sql",
                f"-- {spec.id}: {reconciliation.name}\n"
                f"with expected as (\n{expected}\n), actual as (\n{actual}\n), compared as (\n"
                "    select (select * from expected) as expected_value, (select * from actual) as actual_value\n)\n"
                "select * from compared\n"
                f"where abs(coalesce(expected_value, 0) - coalesce(actual_value, 0)) > {reconciliation.tolerance}\n"
                "   or (expected_value is null) <> (actual_value is null)\n",
            )
    return written
