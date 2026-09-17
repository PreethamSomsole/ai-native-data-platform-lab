# AI-Native Data Platform Lab

A vendor-neutral, hands-on lab for learning and building **AI-ready data architecture** and **AI-native data engineering** together.

The project has two mutually reinforcing goals:

1. **Make enterprise data usable by AI efficiently and safely.**
   This means explicit semantics, business definitions, discoverability, trust signals,
   retrieval, ranking, lineage, policy, and context APIs.
2. **Use AI agents to build and operate the data platform itself.**
   Agents should reason, investigate, generate, validate, and automate low-risk work,
   while deterministic systems enforce policy, rollback, promotion, and human approval
   for higher-risk decisions.

The long-term feedback loop is:

```text
AI builds and operates the platform
              ↓
The platform accumulates semantics, metadata, lineage, quality, and policy
              ↓
AI uses that context to make better engineering decisions
              ↓
The platform becomes easier and safer for AI to operate
```

## Current milestone

Milestone 3 combines governed declarative semantics with observed runtime state and
exposes the result through a reusable context service:

```text
Git/YAML semantics                 Runtime observations
definitions / ownership           freshness / quality / health
grain / use cases                 volume / usage / pipeline runs
        │                                  │
        │                         ┌────────┴────────┐
        │                         │                 │
        │                  SQLite latest     DuckDB history
        │                         │                 │
        └──────────────┬──────────┴─────────────────┘
                       ↓
             context capability layer
                       ↓
       explained runtime-aware reranking
                       ↓
              FastAPI REST adapter
```

SQLite provides the request-time latest snapshot. DuckDB retains append-oriented history
for row-count trends and analytical inspection. Both sit behind replaceable interfaces;
neither becomes the source of truth for business semantics. Runtime freshness, quality,
health, and usage adjust candidate rank through explicit `RankingReason` entries. They do
not override prohibited-use exclusions or resolve semantic ambiguity.

FastAPI is only an adapter. Context assembly, runtime persistence, and ranking remain
callable Python capabilities so later MCP, Snowflake, or Databricks interfaces can reuse
the same behavior.

A key acceptance case is deliberately ambiguous:

```text
"What was net revenue last quarter?"
```

Finance recognized revenue and Operations completed-order revenue are different,
valid business definitions. The platform must return `CLARIFICATION_REQUIRED` rather
than silently choosing one.

## Architecture principles

The project is guided by a few important rules:

- **Metadata as code.** Declarative business semantics live in versioned YAML in Git.
- **Central semantic definitions.** Concepts and metrics are defined once; datasets
  reference them instead of redefining business meaning locally.
- **Different meanings stay different.** For example,
  `metric.finance_net_revenue` and `metric.operations_net_revenue` may both relate to
  revenue, but they are not interchangeable.
- **Retrieval before reasoning.** An AI should receive a small relevant candidate set,
  not the entire enterprise catalog.
- **Business fitness beats textual similarity.** Certification, intended use case,
  domain, audience, freshness, and trust matter when ranking datasets.
- **Ambiguity is a valid outcome.** When multiple certified definitions are valid, the
  system should ask the user instead of guessing.
- **AI reasons; deterministic systems control.** Validation, policy, authorization,
  rollback, artifact promotion, and safety boundaries should not depend only on an LLM.
- **Core capabilities before agent interfaces.** The current Python capability layer
  is deliberately independent of CLI, REST, MCP, Snowflake, or Databricks adapters.
- **High-level tools over raw infrastructure access.** Future agents should prefer
  intent-level capabilities such as `safe_replace_table()` over unrestricted
  `execute_sql()`.
- **Autonomy is contextual.** Environment, action risk, blast radius, recoverability,
  data layer, and contract impact determine whether an action can be autonomous.

See [architecture.md](architecture.md) for the full design narrative and
[roadmap.md](roadmap.md) for the implementation sequence.

## Current Python capability layer

`src/ai_data_platform/api.py` exposes the stable capability boundary used by the CLI
and intended for future adapters:

```python
get_metric(metric_id, registry)
get_entity(entity_id, registry)
get_dataset(dataset_id, registry)
resolve_metric(query, registry)
discover_datasets(question, registry, limit=5)
discover_datasets_with_runtime(question, registry, runtime_store, limit=5)
build_vector_index(registry, embedding_provider)
discover_datasets_hybrid(question, registry, vector_index, limit=5)
discover_datasets_hybrid_with_runtime(...)
validate_registry(registry)
```

The business logic should remain behind this layer so future REST or MCP interfaces do
not become the source of truth.

## Repository layout

```text
registry/
  concepts/       canonical business concepts
  metrics/        canonical metric definitions
  entities/       canonical business entities
  datasets/       dataset contracts referencing semantics

src/ai_data_platform/
  models/         Pydantic contracts
  registry/       YAML loading and registry construction
  validation/     structural and referential validation
  embeddings/     provider contract, semantic documents, and vector index
  discovery/      deterministic/hybrid retrieval, scoring, and ambiguity behavior
  runtime/        typed observations, SQLite latest state, and DuckDB history
  context/        semantic + runtime context assembly
  http/           thin FastAPI adapter
  policy/         centralized semantic, hybrid, and runtime ranking policy
  api.py          stable Python capability layer

evaluation/       versioned retrieval relevance cases
tests/            acceptance and unit tests
```

## Run Milestone 3

```bash
python -m pip install -e ".[test]"
python -m ai_data_platform validate
python -m ai_data_platform discover "What was net revenue last quarter?"
python -m ai_data_platform discover "What was net revenue last quarter?" --mode hybrid
python -m ai_data_platform evaluate
python -m unittest discover -s tests -v
python -m ai_data_platform serve --state-dir var
```

The service publishes OpenAPI documentation at `http://127.0.0.1:8000/docs`. Its main
endpoints are:

- `POST /v1/runtime/observations` — record current state in SQLite and history in DuckDB
- `POST /v1/discovery` — deterministic or hybrid discovery with explained runtime reranking
- `GET /v1/datasets/{dataset_id}/context` — combined contract, semantics, runtime state,
  and row-count trend
- `GET /v1/datasets/{dataset_id}/runtime/history` — recent observations

See [Milestone 3 runtime context design](docs/milestone-3-runtime-context.md) for the
storage boundary, ranking policy, API examples, and configuration.

## Where this is going

The planned progression is intentionally incremental:

```text
M1  Semantic registry + deterministic discovery
 ↓
M2  Embeddings + hybrid retrieval + evaluation
 ↓
M3  Runtime metadata + context service/API
 ↓
M4  LLM reasoning over curated context
 ↓
M5  MCP/tool exposure for agents
 ↓
M6  AI-native data engineering agents + safe high-level tools
 ↓
M7  Snowflake mapping
 ↓
M8  Databricks mapping
```

The goal is not to build a generic chatbot. The goal is to understand and implement the
architecture needed for AI to become a governed consumer **and** a governed engineering
actor in a modern data platform.
