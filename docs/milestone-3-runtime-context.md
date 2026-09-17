# Milestone 3 Runtime Metadata and Context Service

## Purpose

Milestone 3 answers a question that declarative contracts alone cannot answer: not only
*what does this dataset mean?*, but *is it currently fit to use?*

The implementation keeps these concerns separate:

| Concern | Authority | Reference implementation |
|---|---|---|
| Business meaning and governance intent | Versioned Git/YAML registry | Existing registry loader and validation |
| Latest observed operational state | Runtime current-state store | SQLite |
| Observation history and analytical trends | Runtime history store | DuckDB |
| Context orchestration | Python capability layer | `ContextService` |
| Network interface | Replaceable adapter | FastAPI |

SQLite and DuckDB are local reference implementations, not architectural requirements.
`RuntimeMetadataStore` and `RuntimeHistoryStore` define the replaceable boundaries.
DuckDB supplies chronological row-count trends and aggregate freshness, quality, health,
and volume summaries as part of dataset context.

## Observation contract

`DatasetRuntimeMetadata` records:

- dataset ID and observation source
- timezone-aware observation and last-successful-run timestamps
- freshness status
- quality status and passed/failed check counts
- row count
- 30-day usage count
- operational health

An observation must reference a dataset that exists in the validated registry. The same
observation is appended to DuckDB history and considered for the SQLite latest snapshot.
An older late-arriving observation remains in history but cannot replace a newer current
snapshot.

## Runtime ranking policy

Runtime-aware discovery first retrieves the full semantic candidate set, then applies
deterministic adjustments and finally truncates to the requested limit. Every non-zero
adjustment is included in the candidate's `reasons` list.

| Signal | Positive state | Negative state |
|---|---:|---:|
| Freshness | fresh `+8` | stale `-12` |
| Quality | passing `+10` | warning `-5`, failing `-20` |
| Operational health | healthy `+8` | degraded `-8`, failed `-20` |
| 30-day usage | tiered `+1` to `+5` | no penalty |

Unknown values make no adjustment. Missing runtime metadata adds a zero-point explanatory
reason and leaves semantic rank unchanged.

Runtime policy cannot:

- restore a candidate excluded by a prohibited use case
- change `CLARIFICATION_REQUIRED` into `RESOLVED`
- select one explicitly non-equivalent metric because its dataset is healthier or more used

## Run locally

```bash
python -m pip install -e ".[test]"
python -m ai_data_platform serve --state-dir var
```

The service listens on `127.0.0.1:8000` and publishes interactive OpenAPI documentation
at `/docs`. The state directory is ignored by Git.

The factory form supports environment-based startup:

```bash
python -m uvicorn ai_data_platform.http:create_default_app --factory
```

Configuration:

| Variable | Default |
|---|---|
| `AI_DATA_PLATFORM_REGISTRY` | repository `registry/` directory |
| `AI_DATA_PLATFORM_STATE_DIR` | `var/` |
| `AI_DATA_PLATFORM_SQLITE_PATH` | `<state-dir>/runtime-metadata.sqlite` |
| `AI_DATA_PLATFORM_DUCKDB_PATH` | `<state-dir>/runtime-history.duckdb` |

## API examples

Record an observation:

```bash
curl -X POST http://127.0.0.1:8000/v1/runtime/observations \
  -H 'content-type: application/json' \
  -d '{
    "dataset_id": "gold.finance_revenue",
    "observed_at": "2026-09-17T12:00:00Z",
    "source": "finance-revenue-pipeline",
    "last_successful_run_at": "2026-09-17T11:55:00Z",
    "freshness_status": "fresh",
    "quality_status": "passing",
    "quality_checks_passed": 12,
    "quality_checks_failed": 0,
    "row_count": 1250000,
    "usage_count_30d": 140,
    "operational_health": "healthy"
  }'
```

Discover with runtime-aware hybrid retrieval:

```bash
curl -X POST http://127.0.0.1:8000/v1/discovery \
  -H 'content-type: application/json' \
  -d '{
    "question": "Finance recognized net revenue",
    "mode": "hybrid",
    "limit": 5
  }'
```

Read combined context and history:

```bash
curl http://127.0.0.1:8000/v1/datasets/gold.finance_revenue/context
curl 'http://127.0.0.1:8000/v1/datasets/gold.finance_revenue/runtime/history?limit=20'
```

## Explicitly deferred

- authentication and authorization
- production database topology and migrations
- pipeline/catalog connectors and background scheduling
- production embedding and managed vector-store adapters
- LLM reasoning and MCP exposure
