# Governed pipelines (Milestone 6b)

Milestone 6 gave agents safe, high-level engineering capabilities. Milestone 6b makes pipelines
real: declarative specs that build the registry's dataset contracts from data at volume, behind
deterministic gates, and that report their state and lineage back to the context plane. This
closes the loop described in the architecture: the platform builds data, accumulates runtime
truth, and uses it for discovery and agent decisions.

```text
sample data (TPC-H Parquet, var/data)
        │  platform-controlled landing (only the runner touches files)
        ▼
raw__tpch__* tables ──► silver.order_events ──► silver.revenue_events ──► gold.finance_revenue
                                   │                       └────────────► gold.customer_financial_summary
                                   ├──► gold.order_revenue ──► gold.product_performance
                                   └──► gold.monthly_sales
        │
        ▼ per run: contract check · quality checks · reconciliations · atomic publish + backup
runtime observations (SQLite latest + DuckDB history) · verified lineage · audit log
        │
        ▼
runtime-aware discovery · get_dataset_context · MCP lineage and authoring tools
```

At TPC-H scale factor 1 (6,001,215 order lines), ingestion takes about 7 seconds and the seven
pipelines take about 12 seconds on a 4-core development container. Every reconciliation ties
out to the cent.

## Specs

`pipelines/sources.yaml` declares raw inputs (paths relative to the server-configured data
directory). `pipelines/specs/*.yaml` declares one pipeline per registry dataset:

```yaml
id: pipeline.gold_order_revenue
owner: sales operations analytics
output: gold.order_revenue            # must be a registry dataset with declared columns
inputs:
  datasets: [silver.order_events]     # every input must be declared and used
sql: |
  SELECT order_key, line_number, ..., net_amount AS net_revenue
  FROM {{ ref('silver.order_events') }}
  WHERE order_status = 'F'
quality_checks:
  - {check: unique, columns: [order_key, line_number]}
  - {check: range, column: net_revenue, min: 0}
reconciliations:
  - name: completed-order revenue ties to conformed order lines
    expected_sql: SELECT SUM(net_amount) FROM {{ ref('silver.order_events') }} WHERE order_status = 'F'
    actual_sql: SELECT SUM(net_revenue) FROM {{ this }}
```

Check types are `not_null`, `unique`, `accepted_values`, `range`, and `row_count`. Every
non-nullable contract column also gets an implicit `not_null` check. Reconciliations compare
two scalar SELECTs within a tolerance; `{{ this }}` is the candidate output.

Source and output IDs are `<segment>.<segment>`, where segments are lowercase words joined by
single underscores. Disallowing `__` keeps the physical table mapping (`silver.order_events` to
`silver__order_events`) unambiguous.

Dataset contracts now carry optional `columns` (`name`, `type`, `nullable`, `description`). A
pipeline cannot publish a contract without them.

The three revenue metrics are implemented according to their registry definitions:

| Dataset | Rule over TPC-H |
| --- | --- |
| `silver.revenue_events` (Finance recognized) | Shipped lines (`line_status = 'F'`) recognized on ship date; returned lines (`return_flag = 'R'`) reversed on receipt date |
| `gold.order_revenue` (Operations completed-order) | Lines of fully fulfilled orders (`order_status = 'F'`) |
| `gold.monthly_sales` (Sales bookings) | All order lines by order date, any status |

TPC-H has a fixed as-of date (1995-06-17) and flags about half of received lines as returned.
Both are properties of the generator; they make the three definitions diverge visibly near
the as-of date.

## Safety layers for transform SQL

Transform SQL is untrusted input, whether a person or an agent wrote it:

1. **Templates only.** Relations are named through `ref()`, `source()`, and `this`. Only
   declared inputs render; other template expressions are rejected.
2. **Static analysis.** The rendered SQL must parse as exactly one `SELECT`. The DuckDB AST
   is checked for table functions (`read_csv`, `read_parquet`, `query_table`, ...), and every
   base-table reference, including system views such as `information_schema` and
   `sqlite_master`, must be a declared input or a CTE. CTE names may not shadow any relation
   that already resolves, so an unqualified name can only ever mean a CTE, regardless of
   scope or declaration order. All violations are reported together.
3. **Sandboxed execution.** SQL runs on an in-memory DuckDB connection with the warehouse
   attached. Before user SQL runs, `enable_external_access` is turned off and
   `lock_configuration` is set, so files cannot be read, databases cannot be attached, and
   the settings cannot be re-enabled, even if a parser gap existed.

Landing source files is a separate step that only the runner performs. Glob matches are
expanded and resolved first, every file must lie inside the configured data directory (symlinks
included), and DuckDB receives that explicit file list.

## Runner lifecycle

For each pipeline in dependency order:

1. Skip the pipeline if an upstream pipeline did not publish in this invocation.
2. Build `<target>__staging_<run>` in the sandbox.
3. Compare the candidate schema with the contract: missing columns, unexpected columns
   (additive changes need a contract update), and type mismatches each fail with
   `failed_contract`.
4. Run the quality checks and reconciliations; any failure means `failed_quality`.
5. Discard failed candidates. The published table is never touched.
6. Publish passing candidates through `EngineeringService.safe_replace_table` (atomic, with a
   retained backup) or `publish_new_table` for the first build, then prune to one backup.
7. Record the run (`_platform_pipeline_runs`), replace verified lineage edges
   (`_platform_lineage`), append an audit event, and record a runtime observation.

| Outcome | freshness | quality | health |
| --- | --- | --- | --- |
| succeeded | fresh | passing | healthy |
| failed_quality / failed_contract | stale | failing | degraded |
| failed_error | stale | unknown | failed |
| skipped (upstream did not publish) | stale | unchanged (unknown if never built) | unchanged (unknown if never built) |

Failures and skips keep the previous `last_successful_run_at`. A skipped dataset's published
data was validated and has not changed, so only its freshness changes. Discovery then applies the existing,
explained runtime ranking: a failing run moves a dataset by -40 points relative to its
healthy state, without changing ambiguity outcomes.

## Agent authoring through MCP

When `pipelines/sources.yaml` is configured, the MCP server adds five tools:

| Tool | Effect |
| --- | --- |
| `list_pipeline_inputs` | Sources and published datasets, columns, templates, and rules (read-only) |
| `get_dataset_lineage` | Validated upstream and downstream lineage (read-only) |
| `validate_pipeline_proposal` | Static validation of spec and proposed contract YAML (read-only) |
| `dry_run_pipeline_proposal` | Full build and gates in a sandbox over a **read-only** warehouse attach; returns schema, checks, and a 5-row preview (read-only) |
| `submit_pipeline_proposal` | Re-runs the dry run and, only if it passes, writes a review package (`pipeline.yaml`, `contract.yaml`, `review.json`) to `var/pipeline_proposals/` (not read-only, not destructive) |

Agents cannot publish, change a governed contract, certify a dataset (proposed contracts must
be `uncertified`), or choose files, databases, or SQL execution modes. Promotion is a human
pull request that copies the reviewed files into `pipelines/specs/` and `registry/datasets/`;
CI validates both. DuckDB allows a single writer per database file, so dry runs report
`CAPABILITY_UNAVAILABLE` while a pipeline run holds the warehouse.

## Commands

```bash
python -m pip install -e ".[test,data,dbt]"
python -m ai_data_platform sample-data tpch --scale 1        # var/data/tpch, ~6M order lines
python -m ai_data_platform pipelines validate
python -m ai_data_platform pipelines list
python -m ai_data_platform pipelines run --ingest            # exit code 1 if any pipeline fails
python -m ai_data_platform pipelines run --select pipeline.silver_order_events
python -m ai_data_platform pipelines history
python -m ai_data_platform pipelines lineage
python -m ai_data_platform pipelines export-dbt --output var/dbt_project
python -m ai_data_platform sample-data nyc-taxi --months 2024-01 2024-02
```

## Boundaries and next steps

- Local DuckDB DEV only. QA and production still need environment adapters, as in Milestone 6.
- Full refresh only. Incremental models, CDC application, and late-arriving data windows are
  the next pipeline capability, and `recommend_ingestion_pattern()` already produces the pattern.
- NYC taxi data has a downloader, but no pipelines yet. Its specs should be written after
  inspecting the real files, because TLC column types have changed across years. That schema
  drift is itself a useful demonstration of the contract gate.
- Data quality checks are deterministic rules. Statistical anomaly detection (for example,
  row-count deviation from the DuckDB history store) would extend the same observation model.
