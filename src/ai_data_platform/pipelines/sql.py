"""Template rendering, SELECT-only validation, and sandboxed DuckDB connections.

Transform SQL is treated as untrusted input even when a human wrote it. Defense is layered:

1. dbt-style ``ref()``/``source()``/``this`` templates are the only way to name a relation,
   and only declared inputs render.
2. The rendered text must parse as exactly one SELECT statement with no table functions
   (``read_csv``, ``query_table``, ...), and every referenced table must be a declared input.
3. Execution happens on a connection whose filesystem access is disabled and locked, so even
   a parser gap cannot read files, attach databases, or re-enable access.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

import duckdb

WAREHOUSE_ALIAS = "wh"
_TEMPLATE = re.compile(r"\{\{(.*?)\}\}", re.DOTALL)
_REF = re.compile(r"^\s*ref\(\s*'([a-z][a-z0-9_]*\.[a-z][a-z0-9_]*)'\s*\)\s*$")
_SOURCE = re.compile(r"^\s*source\(\s*'([a-z][a-z0-9_]*)'\s*,\s*'([a-z][a-z0-9_]*)'\s*\)\s*$")
_THIS = re.compile(r"^\s*this\s*$")
_ALIAS = re.compile(r"\s+AS\s+.*$", re.IGNORECASE | re.DOTALL)


class PipelineSqlError(ValueError):
    """Raised when transform SQL is unsafe or references undeclared relations."""

    def __init__(self, messages: list[str]) -> None:
        self.messages = messages
        super().__init__("; ".join(messages))


def dataset_relation(dataset_id: str) -> str:
    """Physical table name for a registry dataset, e.g. silver.order_events -> silver__order_events."""
    return dataset_id.replace(".", "__")


def source_relation(source_id: str) -> str:
    """Physical raw table name for a source, e.g. tpch.lineitem -> raw__tpch__lineitem."""
    return "raw__" + source_id.replace(".", "__")


def qualified(relation: str, catalog: str = WAREHOUSE_ALIAS) -> str:
    return f'{catalog}.main."{relation}"'


@dataclass(frozen=True)
class RenderedSql:
    sql: str
    referenced_sources: frozenset[str]
    referenced_datasets: frozenset[str]
    uses_this: bool


def render(
    sql: str,
    *,
    sources: Iterable[str],
    datasets: Iterable[str],
    this: str | None = None,
) -> RenderedSql:
    """Replace templates with qualified relations; undeclared references are errors."""
    allowed_sources = set(sources)
    allowed_datasets = set(datasets)
    errors: list[str] = []
    used_sources: set[str] = set()
    used_datasets: set[str] = set()
    used_this = False

    if "{%" in sql or "{#" in sql:
        errors.append("only {{ ref(...) }}, {{ source(...) }}, and {{ this }} templates are supported")

    def replace(match: re.Match[str]) -> str:
        nonlocal used_this
        body = match.group(1)
        if ref := _REF.match(body):
            dataset_id = ref.group(1)
            if dataset_id not in allowed_datasets:
                errors.append(f"ref('{dataset_id}') is not a declared dataset input")
            used_datasets.add(dataset_id)
            return qualified(dataset_relation(dataset_id))
        if source := _SOURCE.match(body):
            source_id = f"{source.group(1)}.{source.group(2)}"
            if source_id not in allowed_sources:
                errors.append(f"source('{source.group(1)}', '{source.group(2)}') is not a declared source input")
            used_sources.add(source_id)
            return qualified(source_relation(source_id))
        if _THIS.match(body):
            if this is None:
                errors.append("{{ this }} is only available in reconciliation SQL")
                return "this"
            used_this = True
            return this
        errors.append(f"unsupported template expression: {{{{{body.strip()}}}}}")
        return "unsupported"

    rendered = _TEMPLATE.sub(replace, sql)
    if errors:
        raise PipelineSqlError(list(dict.fromkeys(errors)))
    return RenderedSql(rendered, frozenset(used_sources), frozenset(used_datasets), used_this)


def _table_functions(node: object) -> list[str]:
    found: list[str] = []
    if isinstance(node, dict):
        if node.get("type") == "TABLE_FUNCTION":
            function = node.get("function", {})
            found.append(str(function.get("function_name", "unknown")))
        for value in node.values():
            found.extend(_table_functions(value))
    elif isinstance(node, list):
        for value in node:
            found.extend(_table_functions(value))
    return found


def _base_tables(node: object) -> list[tuple[str, str, str]]:
    found: list[tuple[str, str, str]] = []
    if isinstance(node, dict):
        if node.get("type") == "BASE_TABLE":
            found.append(
                (
                    str(node.get("catalog_name") or ""),
                    str(node.get("schema_name") or ""),
                    str(node.get("table_name") or ""),
                )
            )
        for value in node.values():
            found.extend(_base_tables(value))
    elif isinstance(node, list):
        for value in node:
            found.extend(_base_tables(value))
    return found


def _cte_names(node: object) -> set[str]:
    names: set[str] = set()
    if isinstance(node, dict):
        cte_map = node.get("cte_map")
        if isinstance(cte_map, dict):
            names.update(str(entry.get("key", "")) for entry in cte_map.get("map", []))
        for value in node.values():
            names |= _cte_names(value)
    elif isinstance(node, list):
        for value in node:
            names |= _cte_names(value)
    return names


def _resolves(connection: duckdb.DuckDBPyConnection, name: str) -> bool:
    """True when an unqualified name already resolves to a relation (e.g. a system view)."""
    try:
        connection.execute('SELECT 1 FROM "' + name.replace('"', '""') + '" LIMIT 0')
    except duckdb.Error:
        return False
    return True


def validate_select(
    connection: duckdb.DuckDBPyConnection,
    sql: str,
    allowed_relations: Iterable[str],
) -> None:
    """Require one SELECT, no table functions, and only allowlisted table references."""
    try:
        statements = duckdb.extract_statements(sql)
    except duckdb.Error as error:
        raise PipelineSqlError([f"SQL does not parse: {error}"]) from error
    if len(statements) != 1:
        raise PipelineSqlError([f"expected exactly one statement, found {len(statements)}"])
    if statements[0].type.name != "SELECT":
        raise PipelineSqlError([f"only SELECT is allowed, found {statements[0].type.name}"])
    tree = json.loads(
        connection.execute("SELECT json_serialize_sql(?)", [sql]).fetchone()[0]
    )
    if tree.get("error"):
        raise PipelineSqlError([f"SQL does not parse: {tree.get('error_message', 'unknown error')}"])
    errors: list[str] = []
    functions = sorted(set(_table_functions(tree)))
    if functions:
        errors.append(f"table functions are not allowed: {', '.join(functions)}")
    allowed = {name.replace('"', "") for name in allowed_relations}
    # Qualified names come back as 'catalog.schema.table AS alias'; drop the alias and
    # compare exactly, so anything unexpected fails closed.
    referenced = {
        _ALIAS.sub("", name).replace('"', "")
        for name in connection.get_table_names(sql, qualified=True)
    }
    # get_table_names omits system views (information_schema, pg_catalog, sqlite_master), so
    # every base-table reference in the AST must also be a declared input or a CTE. CTE names
    # may not shadow a relation that already resolves, so an unqualified name can only ever
    # mean a CTE, whatever its scope or declaration order.
    ctes = _cte_names(tree)
    shadowing = sorted(name for name in ctes if _resolves(connection, name))
    if shadowing:
        errors.append(f"CTE names shadow existing relations: {', '.join(shadowing)}")
    lowered = {name.lower() for name in ctes}
    for catalog, schema, table in _base_tables(tree):
        if catalog or schema:
            referenced.add(".".join(part for part in (catalog, schema, table) if part))
        elif table.lower() not in lowered:
            referenced.add(table)
    undeclared = sorted(referenced - allowed)
    if undeclared:
        errors.append(
            "SQL references relations that are not declared inputs: "
            + ", ".join(undeclared)
            + " (use ref() or source())"
        )
    if errors:
        raise PipelineSqlError(errors)


def _literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def sandboxed_connection(
    warehouse_path: str | Path,
    *,
    read_only: bool,
    temp_directory: str | Path,
    memory_limit: str | None = None,
) -> duckdb.DuckDBPyConnection:
    """In-memory DuckDB with the warehouse attached as ``wh`` and file access locked off."""
    Path(temp_directory).mkdir(parents=True, exist_ok=True)
    connection = duckdb.connect(":memory:")
    try:
        mode = " (READ_ONLY)" if read_only else ""
        connection.execute(f"ATTACH {_literal(str(warehouse_path))} AS {WAREHOUSE_ALIAS}{mode}")
        connection.execute(f"SET temp_directory = {_literal(str(temp_directory))}")
        if memory_limit is not None:
            connection.execute(f"SET memory_limit = {_literal(memory_limit)}")
        connection.execute("SET enable_progress_bar = false")
        connection.execute("SET enable_external_access = false")
        connection.execute("SET lock_configuration = true")
    except Exception:
        connection.close()
        raise
    return connection
