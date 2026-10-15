# Pipeline portability: DuckDB, dbt, and Databricks

The governance model is the product; DuckDB is the reference engine. Specs deliberately use
dbt's `ref()`/`source()` syntax and plain SELECTs so the same contracts, gates, and lineage
move to other stacks.

## dbt (runnable today)

`python -m ai_data_platform pipelines export-dbt --output var/dbt_project` writes a complete
dbt project. With `dbt-duckdb` installed (`pip install -e ".[dbt]"`):

```bash
dbt build --project-dir var/dbt_project --profiles-dir var/dbt_project
```

The generated profile attaches the DEV warehouse **read-only** as `wh` for raw sources and
builds models into a separate `dbt_dev.duckdb`. On TPC-H scale factor 1 this builds 7
contract-enforced models and runs 32 tests (39 nodes, about 10 seconds). Row counts and
totals match the native runner exactly.

| Platform concept | dbt equivalent |
| --- | --- |
| `pipelines/sources.yaml` | `models/sources.yml` (`identifier: raw__tpch__*`) |
| Pipeline spec SQL | Model SQL; `ref('silver.order_events')` becomes `ref('silver__order_events')` |
| Dataset contract `columns` | `contract: {enforced: true}` with `data_type` and `not_null` constraints |
| `not_null`, single-column `unique`, `accepted_values` | Built-in generic tests |
| Multi-column `unique`, `range`, `row_count`, reconciliations | Singular tests in `tests/` |
| Owner, certification, dataset ID | `config.meta` |
| Verified lineage | dbt's DAG (`dbt docs`) |
| Safe replace + backup | Table materialization (atomic swap); dbt keeps no backup, so rely on warehouse time travel |

To target a warehouse, replace `profiles.yml` with the adapter profile (Snowflake, BigQuery,
Databricks, ...). Generic tests and contracts are adapter-neutral. Review the singular tests
and type names for the target dialect: `DECIMAL(18,2)`, `strftime`, `date_trunc`, and
`GROUP BY ALL` are DuckDB-friendly, and `GROUP BY ALL` is also supported by Snowflake and
Databricks SQL.

Runtime observations do not come from dbt itself. Map `run_results.json` (status, timing, and
test failures) to `DatasetRuntimeMetadata` in an adapter, so the context plane keeps one
observation contract regardless of the engine.

## Databricks (design mapping, Milestone 8)

| Platform concept | Databricks capability |
| --- | --- |
| Source landing | Auto Loader / `read_files` into bronze Delta tables |
| Pipeline spec | Lakeflow Declarative Pipelines (formerly DLT) dataset, or a dbt-databricks model |
| `quality_checks` | Expectations: `EXPECT ... ON VIOLATION FAIL UPDATE` to block publication, `DROP ROW` only where a contract permits it |
| Reconciliations | An expectation over an aggregate view, or a dbt singular test |
| Dataset contract columns | Unity Catalog table schema, `NOT NULL` and informational constraints, column comments |
| Owner, certification | Unity Catalog owner, tags (for example `certification=certified`), and governed tags |
| Verified lineage | Unity Catalog lineage (captured automatically) cross-checked against declared inputs |
| Runtime observations | Pipeline event log and `system.lakeflow` / `system.access` tables mapped to `DatasetRuntimeMetadata` |
| Safe replace + rollback | Atomic `CREATE OR REPLACE TABLE`, Delta time travel, `RESTORE TABLE ... TO VERSION AS OF` |
| SQL sandbox for agents | A service principal with `SELECT` only on declared inputs, a dedicated dry-run schema, and SQL warehouse or serverless isolation |
| Proposal review | Git folders plus Databricks Asset Bundles promoted through CI |

The important difference is where enforcement lives. In the DuckDB reference, the runner
enforces input allowlists by parsing SQL and locking file access. On Databricks the stronger
control is Unity Catalog privileges on the identity that runs the SQL. Keep the static checks
anyway: they give agents fast, explained feedback before anything runs.

## Snowflake (brief, Milestone 7)

The same mapping applies with dbt-snowflake or Dynamic Tables. Contracts map to table DDL and
Data Metric Functions, quality gates to dbt tests or DMFs, lineage to `ACCESS_HISTORY` and
object dependencies, rollback to Time Travel and zero-copy clones, and the agent sandbox to a
role with `SELECT` on declared inputs plus a dry-run schema.
