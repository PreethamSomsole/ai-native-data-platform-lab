"""Crawl DuckDB schemas into conservative dataset drafts and review reports."""

from __future__ import annotations

import json
import re
from datetime import date, datetime, time
from enum import Enum
from pathlib import Path
from typing import Any

import duckdb
import yaml

from ai_data_platform.models import Certification, Dataset
from ai_data_platform.registry import load_registry
from ai_data_platform.validation import RegistryValidationError, validate_registry


_LAYERS = ("gold", "silver", "bronze", "raw")
_DOMAIN_ALIASES = {
    "customer": "customer",
    "finance": "finance",
    "order": "orders",
    "orders": "orders",
    "payment": "payments",
    "payments": "payments",
    "product": "product",
    "sales": "sales",
    "stripe": "payments",
    "revenue": "finance",
}
_GRAIN_TERMS = {
    "account": {"account"},
    "customer": {"customer"},
    "date": {"date", "timestamp", "time", "period", "month"},
    "entity": {"entity"},
    "event": {"event"},
    "legal": {"legal"},
    "month": {"month", "period", "date", "timestamp"},
    "order": {"order"},
    "period": {"period", "month", "date", "timestamp"},
    "product": {"product"},
    "region": {"region"},
}
_NON_SEMANTIC_GRAIN_WORDS = {
    "and",
    "by",
    "calendar",
    "fiscal",
    "for",
    "of",
    "per",
    "sales",
    "the",
    "with",
}
_BUSINESS_FIELDS_NOT_INFERRED = (
    "target_users",
    "supported_use_cases",
    "prohibited_use_cases",
    "metric_ids",
    "entity_ids",
    "dimension_concept_ids",
    "trust_signals",
    "owner",
    "freshness_sla",
    "refresh_cadence",
    "certification",
)


class SchemaCrawler:
    """Inspect DuckDB tables and draft contracts without asserting business meaning."""

    def __init__(
        self,
        connection: duckdb.DuckDBPyConnection,
        *,
        max_profile_rows: int = 10_000,
        profile_seed: int = 42,
    ) -> None:
        if max_profile_rows < 1:
            raise ValueError("max_profile_rows must be at least 1")
        if profile_seed < 0:
            raise ValueError("profile_seed must be non-negative")
        self.connection = connection
        self.max_profile_rows = max_profile_rows
        self.profile_seed = profile_seed

    @staticmethod
    def _quote_identifier(identifier: str) -> str:
        return '"' + identifier.replace('"', '""') + '"'

    @staticmethod
    def _table_identity(table: str) -> tuple[str, str]:
        layer = "raw"
        stem = table
        for candidate in _LAYERS:
            prefix = f"{candidate}_"
            if table.startswith(prefix):
                layer = candidate
                stem = table.removeprefix(prefix)
                break
        return layer, stem

    @staticmethod
    def _humanize_table_name(table: str) -> str:
        readable = re.sub(r"^tbl_", "", table, flags=re.IGNORECASE)
        return re.sub(r"_+", " ", readable).strip().title()

    @staticmethod
    def _infer_domain(layer: str, stem: str) -> tuple[str, str]:
        tokens = re.findall(r"[a-z0-9]+", stem.lower())
        for token in tokens:
            if token in _DOMAIN_ALIASES:
                return _DOMAIN_ALIASES[token], f"table-name token '{token}'"
        if layer in {"gold", "silver", "bronze"} and tokens:
            return tokens[0], f"first token '{tokens[0]}' after layer prefix"
        return "unknown", "no recognized domain token in table name"

    @staticmethod
    def _json_safe(value: Any) -> Any:
        if isinstance(value, Enum):
            return SchemaCrawler._json_safe(value.value)
        if isinstance(value, (datetime, date, time)):
            return value.isoformat()
        if isinstance(value, (bytes, bytearray)):
            return f"<binary:{len(value)} bytes>"
        if isinstance(value, dict):
            return {str(key): SchemaCrawler._json_safe(item) for key, item in value.items()}
        if isinstance(value, (list, tuple)):
            return [SchemaCrawler._json_safe(item) for item in value]
        if value is None or isinstance(value, (str, int, float, bool)):
            return value
        return str(value)

    @staticmethod
    def _distinct_key(value: Any) -> str:
        try:
            return json.dumps(
                SchemaCrawler._json_safe(value), sort_keys=True, separators=(",", ":")
            )
        except (TypeError, ValueError):
            return repr(value)

    def _profile_table(
        self,
        quoted_table: str,
        columns: list[dict[str, Any]],
    ) -> dict[str, Any]:
        total_rows = int(
            self.connection.execute(f"SELECT COUNT(*) FROM {quoted_table}").fetchone()[0]
        )
        if total_rows <= self.max_profile_rows:
            sample_method = "full_table"
            profile_rows = self.connection.execute(f"SELECT * FROM {quoted_table}").fetchall()
        else:
            sample_method = "reservoir_sample_without_replacement"
            profile_rows = self.connection.execute(
                f"SELECT * FROM {quoted_table} USING SAMPLE reservoir "
                f"({self.max_profile_rows} ROWS) REPEATABLE ({self.profile_seed})"
            ).fetchall()
        profile_count = len(profile_rows)
        by_column: list[dict[str, Any]] = []
        unique_candidates: list[str] = []

        for index, column in enumerate(columns):
            values = [row[index] for row in profile_rows]
            non_null = [value for value in values if value is not None]
            distinct_values = {self._distinct_key(value) for value in non_null}
            record: dict[str, Any] = {
                "column": column["name"],
                "profiled_null_count": profile_count - len(non_null),
                "profiled_null_rate": (
                    round((profile_count - len(non_null)) / profile_count, 4)
                    if profile_count
                    else None
                ),
                "profiled_distinct_count": len(distinct_values),
                "profiled_duplicate_value_count": len(non_null) - len(distinct_values),
            }
            if re.search(r"(^|_)(id|key|code)(_|$)", column["name"].lower()):
                record["identifier_like_name"] = True
            identifier_like = bool(re.search(r"(^|_)(id|key|code)(_|$)", column["name"].lower()))
            if (
                identifier_like
                and profile_count
                and len(non_null) == profile_count
                and len(distinct_values) == profile_count
            ):
                unique_candidates.append(column["name"])

            data_type = column["data_type"].upper()
            supports_range = any(
                marker in data_type
                for marker in (
                    "INT",
                    "DECIMAL",
                    "NUMERIC",
                    "DOUBLE",
                    "FLOAT",
                    "REAL",
                    "DATE",
                    "TIME",
                    "TIMESTAMP",
                )
            )
            if supports_range and non_null:
                try:
                    record["profiled_min"] = self._json_safe(min(non_null))
                    record["profiled_max"] = self._json_safe(max(non_null))
                except (TypeError, ValueError):
                    record["range_status"] = "unavailable_for_observed_values"
            by_column.append(record)

        return {
            "total_rows": total_rows,
            "profiled_rows": profile_count,
            "profile_limit": self.max_profile_rows,
            "profile_complete": total_rows <= self.max_profile_rows,
            "profile_method": sample_method,
            "sampling_seed": self.profile_seed if sample_method != "full_table" else None,
            "profile_scope": "complete_table"
            if total_rows <= self.max_profile_rows
            else "sampled_subset",
            "sampling_limitations": (
                None
                if total_rows <= self.max_profile_rows
                else "A repeatable reservoir sample covers the full table scan, but rare values or anomalies may be missed."
            ),
            "columns": by_column,
            "sample_unique_column_candidates": unique_candidates,
        }

    def _constraints(self, schema_name: str, table: str) -> dict[str, Any]:
        try:
            rows = self.connection.execute(
                """
                SELECT constraint_type, constraint_column_names, constraint_text
                FROM duckdb_constraints()
                WHERE schema_name = ? AND table_name = ?
                ORDER BY constraint_type, constraint_text
                """,
                [schema_name, table],
            ).fetchall()
        except duckdb.Error as error:
            return {"status": "unavailable", "reason": str(error)}
        return {
            "status": "available",
            "items": [
                {
                    "type": kind,
                    "columns": list(column_names or []),
                    "definition": definition,
                }
                for kind, column_names, definition in rows
            ],
        }

    @staticmethod
    def _missing_grain_columns(grain: str, column_names: list[str]) -> list[str]:
        observed_words = {
            word for column in column_names for word in re.findall(r"[a-z0-9]+", column.lower())
        }
        missing: list[str] = []
        for term in re.findall(r"[a-z0-9]+", grain.lower()):
            if term in _NON_SEMANTIC_GRAIN_WORDS or term not in _GRAIN_TERMS:
                continue
            if not (_GRAIN_TERMS[term] & observed_words) and term not in missing:
                missing.append(term)
        return missing

    def _crawl_table(self, schema_name: str, table: str) -> tuple[Dataset, dict[str, Any]]:
        raw_columns = self.connection.execute(
            """
            SELECT column_name, data_type, is_nullable, ordinal_position
            FROM information_schema.columns
            WHERE table_schema = ? AND table_name = ?
            ORDER BY ordinal_position
            """,
            [schema_name, table],
        ).fetchall()
        columns = [
            {
                "name": name,
                "data_type": data_type,
                "nullable": nullable == "YES",
                "ordinal_position": position,
            }
            for name, data_type, nullable, position in raw_columns
        ]
        layer, stem = self._table_identity(table)
        domain, domain_source = self._infer_domain(layer, stem)
        dataset_id = f"{layer}.{stem}"
        draft = Dataset(
            id=dataset_id,
            name=self._humanize_table_name(table),
            domain=domain,
            layer=layer,
            description=(
                f"Physical schema for {schema_name}.{table}. Observed columns: "
                + ", ".join(f"{column['name']} ({column['data_type']})" for column in columns)
            ),
            grain="unknown",
            certification=Certification.UNCERTIFIED,
            owner="unknown",
            freshness_sla="unknown",
            refresh_cadence="unknown",
        )
        quoted_table = f"{self._quote_identifier(schema_name)}.{self._quote_identifier(table)}"
        observed_facts = {
            "physical_table": f"{schema_name}.{table}",
            "columns": columns,
            "constraints": self._constraints(schema_name, table),
            "profile": self._profile_table(quoted_table, columns),
        }
        warnings: list[dict[str, Any]] = []
        table_years = set(re.findall(r"20\d{2}", table))
        observed_years = {
            year
            for column_profile in observed_facts["profile"]["columns"]
            for bound in (column_profile.get("profiled_min"), column_profile.get("profiled_max"))
            if isinstance(bound, str)
            for year in re.findall(r"20\d{2}", bound)
        }
        if table_years and observed_years and table_years.isdisjoint(observed_years):
            warnings.append(
                {
                    "kind": "table_name_year_differs_from_profiled_dates",
                    "table_name_years": sorted(table_years),
                    "profiled_date_years": sorted(observed_years),
                    "interpretation": "possible legacy naming drift; review before renaming",
                }
            )
        for column_profile in observed_facts["profile"]["columns"]:
            column_name = column_profile["column"].lower()
            lower = column_profile.get("profiled_min")
            upper = column_profile.get("profiled_max")
            if (
                re.search(r"(^|_)(created|epoch|timestamp|ts|time)(_|$)", column_name)
                and isinstance(lower, (int, float))
                and isinstance(upper, (int, float))
                and lower > 0
                and lower < 100_000_000_000 <= upper
                and upper / lower > 50
            ):
                warnings.append(
                    {
                        "kind": "possible_mixed_epoch_units",
                        "column": column_profile["column"],
                        "profiled_min": lower,
                        "profiled_max": upper,
                        "interpretation": "values span likely seconds and milliseconds; confirm source convention",
                    }
                )
            if (
                column_name.endswith("score")
                and isinstance(lower, (int, float))
                and isinstance(upper, (int, float))
                and (lower < 0 or upper > 1)
            ):
                warnings.append(
                    {
                        "kind": "score_outside_zero_to_one",
                        "column": column_profile["column"],
                        "profiled_min": lower,
                        "profiled_max": upper,
                        "interpretation": "name suggests a normalized score; validate the expected range",
                    }
                )
        heuristic_assessments = {
            "inference": {
                "layer": {
                    "value": layer,
                    "method": "table_name_convention",
                    "source": "recognized table-name prefix"
                    if layer != "raw" or table.startswith("raw_")
                    else "default for unprefixed table",
                },
                "domain": {
                    "value": domain,
                    "method": "table_name_heuristic",
                    "source": domain_source,
                },
                "name": {
                    "value": draft.name,
                    "method": "table_name_humanization",
                    "source": "table name converted to title case",
                },
                "grain": {"value": "unknown", "method": "not_inferred"},
            },
            "warnings": warnings,
        }
        observation = {
            "dataset_id": dataset_id,
            "observed_facts": observed_facts,
            "heuristic_assessments": heuristic_assessments,
        }
        return draft, observation

    def crawl_schema(self, schema_name: str = "main") -> list[Dataset]:
        """Crawl a schema and return draft Dataset contracts for every base table."""
        result = self.connection.execute(
            """
            SELECT table_name
            FROM information_schema.tables
            WHERE table_schema = ? AND table_type = 'BASE TABLE'
            ORDER BY table_name
            """,
            [schema_name],
        ).fetchall()
        drafts: list[Dataset] = []
        seen_ids: set[str] = set()
        for (table,) in result:
            draft, _ = self._crawl_table(schema_name, table)
            if draft.id in seen_ids:
                raise ValueError(
                    f"multiple physical tables map to generated dataset id '{draft.id}'; "
                    "rename a table or create the contracts manually"
                )
            seen_ids.add(draft.id)
            drafts.append(draft)
        return drafts

    def _crawl_schema_with_observations(
        self, schema_name: str
    ) -> tuple[list[Dataset], list[dict[str, Any]]]:
        names = self.connection.execute(
            """
            SELECT table_name
            FROM information_schema.tables
            WHERE table_schema = ? AND table_type = 'BASE TABLE'
            ORDER BY table_name
            """,
            [schema_name],
        ).fetchall()
        drafts: list[Dataset] = []
        observations: list[dict[str, Any]] = []
        seen_ids: set[str] = set()
        for (table,) in names:
            draft, observation = self._crawl_table(schema_name, table)
            if draft.id in seen_ids:
                raise ValueError(
                    f"multiple physical tables map to generated dataset id '{draft.id}'; "
                    "rename a table or create the contracts manually"
                )
            seen_ids.add(draft.id)
            drafts.append(draft)
            observations.append(observation)
        return drafts, observations

    @staticmethod
    def _file_name(dataset_id: str) -> str:
        return f"{re.sub(r'[^A-Za-z0-9_-]+', '_', dataset_id.replace('.', '_'))}.yaml"

    @staticmethod
    def _write_dataset(path: Path, dataset: Dataset) -> None:
        data = dataset.model_dump(mode="json")
        ordered = {
            key: data[key]
            for key in (
                "id",
                "name",
                "domain",
                "layer",
                "description",
                "grain",
                "target_users",
                "supported_use_cases",
                "prohibited_use_cases",
                "certification",
                "owner",
                "metric_ids",
                "entity_ids",
                "dimension_concept_ids",
                "freshness_sla",
                "refresh_cadence",
                "trust_signals",
            )
        }
        path.write_text(
            yaml.safe_dump(ordered, sort_keys=False, allow_unicode=True), encoding="utf-8"
        )

    def generate_registry(
        self,
        output_dir: Path,
        schema_name: str = "main",
        *,
        existing_registry_directory: Path | None = None,
    ) -> list[str]:
        """Write draft contracts and a comparison/profile report without editing the source registry.

        When a crawled dataset ID already exists in ``existing_registry_directory``, its existing
        business contract is written unchanged to the output; the report captures the
        physical-versus-contract differences for review.
        """
        existing_registry = None
        if existing_registry_directory is not None:
            registry_path = Path(existing_registry_directory).resolve()
            resolved_output = Path(output_dir).resolve()
            if resolved_output == registry_path or registry_path in resolved_output.parents:
                raise ValueError(
                    "output_dir cannot be the existing registry or a directory inside it"
                )
            existing_registry = load_registry(registry_path)
        drafts, observations = self._crawl_schema_with_observations(schema_name)
        datasets_dir = output_dir / "datasets"
        datasets_dir.mkdir(parents=True, exist_ok=True)

        generated_files: list[str] = []
        used_names: set[str] = set()
        reviews: list[dict[str, Any]] = []
        matched_ids: set[str] = set()
        observation_by_id = {item["dataset_id"]: item for item in observations}

        for draft in drafts:
            name = self._file_name(draft.id)
            if name in used_names:
                raise ValueError(f"multiple dataset IDs map to generated file '{name}'")
            used_names.add(name)
            existing = existing_registry.datasets.get(draft.id) if existing_registry else None
            output_dataset = existing or draft
            if existing is not None:
                matched_ids.add(draft.id)
                differences = [
                    {
                        "field": field,
                        "existing": self._json_safe(getattr(existing, field)),
                        "crawler_draft": self._json_safe(getattr(draft, field)),
                    }
                    for field in Dataset.model_fields
                    if getattr(existing, field) != getattr(draft, field)
                ]
                observation = observation_by_id[draft.id]
                observed = observation["observed_facts"]
                reviews.append(
                    {
                        "dataset_id": draft.id,
                        "physical_table": observed["physical_table"],
                        "status": "existing_contract_preserved",
                        "differences": differences,
                        "business_fields_requiring_curator_input": list(
                            _BUSINESS_FIELDS_NOT_INFERRED
                        ),
                        "grain_terms_without_matching_columns": self._missing_grain_columns(
                            existing.grain, [column["name"] for column in observed["columns"]]
                        ),
                    }
                )
            else:
                reviews.append(
                    {
                        "dataset_id": draft.id,
                        "physical_table": observation_by_id[draft.id]["observed_facts"][
                            "physical_table"
                        ],
                        "status": "new_uncertified_draft",
                        "business_fields_requiring_curator_input": list(
                            _BUSINESS_FIELDS_NOT_INFERRED
                        ),
                    }
                )
            file_path = datasets_dir / name
            self._write_dataset(file_path, output_dataset)
            generated_files.append(str(file_path))

        report = {
            "report_version": 1,
            "schema": schema_name,
            "profile_row_limit": self.max_profile_rows,
            "profile_sampling_method": "full census up to the limit; repeatable reservoir sample above the limit",
            "profile_sampling_seed": self.profile_seed,
            "existing_contracts_compared": existing_registry is not None,
            "existing_registry_only_dataset_ids": (
                sorted(set(existing_registry.datasets) - matched_ids)
                if existing_registry is not None
                else []
            ),
            "dataset_reviews": reviews,
            "table_observations": observations,
            "generated_contract_files": [Path(item).name for item in generated_files],
        }
        report_path = output_dir / "crawl_report.yaml"
        report_path.write_text(
            yaml.safe_dump(report, sort_keys=False, allow_unicode=True), encoding="utf-8"
        )
        return generated_files


def validate_crawler_drafts(
    draft_directory: str | Path,
    existing_registry_directory: str | Path,
) -> list[str]:
    """Validate edited crawler contracts against the existing semantic registry.

    The registry is loaded first, then draft dataset contracts replace matching dataset IDs
    in-memory. No metadata is written or promoted by this function.
    """
    base_registry = load_registry(existing_registry_directory)
    dataset_directory = Path(draft_directory) / "datasets"
    if not dataset_directory.is_dir():
        raise RegistryValidationError([f"Missing crawler draft directory: {dataset_directory}"])

    draft_files = sorted((*dataset_directory.glob("*.yaml"), *dataset_directory.glob("*.yml")))
    if not draft_files:
        raise RegistryValidationError([f"No crawler dataset drafts found in {dataset_directory}"])

    contracts: dict[str, Dataset] = {}
    errors: list[str] = []
    for file_path in draft_files:
        try:
            raw = yaml.safe_load(file_path.read_text(encoding="utf-8"))
            if not isinstance(raw, dict):
                raise TypeError("expected a YAML mapping")
            dataset = Dataset.model_validate(raw)
        except (OSError, TypeError, ValueError, yaml.YAMLError) as error:
            errors.append(f"{file_path}: invalid dataset draft: {error}")
            continue
        if dataset.id in contracts:
            errors.append(f"Duplicate dataset ID '{dataset.id}' in {file_path}")
        else:
            contracts[dataset.id] = dataset

    if errors:
        raise RegistryValidationError(errors)

    datasets = dict(base_registry.datasets)
    datasets.update(contracts)
    validate_registry(base_registry.model_copy(update={"datasets": datasets}))
    return sorted(contracts)
