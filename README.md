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

Milestone 6 adds the first governed, repository-scoped **AI-native data engineering**
capabilities. They sit on the Milestone 5 read-only Model Context Protocol (MCP) adapter:

```text
agent / MCP client
       ↓
validated, bounded tool arguments
       ↓
protocol-independent AgentToolService
       ↓
existing capability + ContextService layers
       ↓
Git/YAML semantics + runtime observations
       ↓
structured tool result with governed status and reasons
```

MCP contains no ranking, validation, or business policy of its own. It registers typed tool
schemas and delegates every operation to the same capability and context services used by
the Python and REST adapters. Discovery is capped at five candidates, registry paths remain
server-controlled, and all published tools are annotated as read-only and non-destructive.

A key acceptance case is deliberately ambiguous:

```text
"What was net revenue last quarter?"
```

Finance recognized revenue and Operations completed-order revenue are different, valid
business definitions. `discover_datasets` still returns `CLARIFICATION_REQUIRED`; the
agent interface cannot bypass that status or restore excluded candidates.

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
ReasoningService(context_service, reasoning_provider).select_dataset(...)
evaluate_reasoning(reasoning_service, cases)
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
  reasoning/      curated evidence, provider contract, guardrails, and evaluation
  agent_tools/    protocol-independent, read-only agent capability service
  mcp/            thin MCP server and stdio entry point
  engineering/    deterministic, repository-scoped DEV engineering capabilities
  http/           thin FastAPI adapter
  policy/         centralized semantic, hybrid, and runtime ranking policy
  api.py          stable Python capability layer

evaluation/       versioned retrieval and reasoning evaluation cases
tests/            acceptance and unit tests
```

## Run Milestone 6

```bash
python -m pip install -e ".[test]"
python -m ai_data_platform validate
python -m ai_data_platform discover "What was net revenue last quarter?"
python -m ai_data_platform discover "What was net revenue last quarter?" --mode hybrid
python -m ai_data_platform evaluate
python -m unittest discover -s tests -v
python -m ai_data_platform serve --state-dir var
python -m ai_data_platform mcp --state-dir var
```

### Crawl and review a DuckDB schema

The crawler reads a local DuckDB database and writes candidate dataset contracts and a
report to a separate review directory. Supply each path and the schema explicitly:

```bash
python -m ai_data_platform crawl \
  --source /path/to/source.duckdb \
  --schema main \
  --registry registry \
  --output var/crawler_review
```

For a synthetic source, use this demo runner instead; it creates the source and crawls it
in one invocation. The runner refuses to overwrite an existing source database or a
non-empty review directory:

```bash
python test_crawler.py \
  --source var/crawler_onboarding_demo.duckdb \
  --schema main \
  --registry registry \
  --output var/crawler_review \
  --seed-demo
```

Review `var/crawler_review/crawl_report.yaml`, then edit the generated YAML under
`var/crawler_review/datasets/`. Correct the guessed `name` and `domain` there and supply
business fields such as grain, owner, use cases, freshness, and canonical semantic IDs.
Observed schema/profile facts and heuristic assessments are separate report sections. The
default profile fully reads tables up to 10,000 rows; larger tables use a repeatable
reservoir sample of at most 10,000 rows (seed 42), so rare anomalies can still be missed.

Validate the edited drafts against the canonical semantic registry before promoting any
contract:

```bash
python -m ai_data_platform validate-drafts \
  --drafts var/crawler_review \
  --registry registry
```

This checks contract fields and semantic references without writing to `registry/`. After
review and a successful validation, copy only the approved contract files into
`registry/datasets/`, then run the usual registry validation and discovery commands. See
[the M6 crawler boundaries and acceptance criteria](docs/milestone-6-ai-native-engineering.md#schema-crawler-onboarding).

`EngineeringService` is a protocol-independent local DEV capability boundary. It
recommends ingestion patterns, produces approval-aware pipeline plans, validates proposed
dataset contracts, runs only configured validation profiles, reconciles bounded DuckDB
tables, records content-hashed DEV deployment manifests, and performs transactional table
replacement with deterministic rollback. It does not accept arbitrary commands or paths,
write Git metadata, or execute QA/production changes. See [Milestone 6 governed
engineering capabilities](docs/milestone-6-ai-native-engineering.md).

The MCP server uses standard input/output and publishes seven structured, read-only tools:

- `discover_datasets`
- `get_dataset_contract`
- `get_dataset_context`
- `get_metric_definition`
- `get_entity_definition`
- `get_concept_definition`
- `validate_metadata`

An MCP client can launch the installed entry point with configuration like:

```json
{
  "mcpServers": {
    "ai-native-data-platform": {
      "command": "ai-data-platform-mcp",
      "env": {
        "AI_DATA_PLATFORM_REGISTRY": "/absolute/path/to/registry",
        "AI_DATA_PLATFORM_STATE_DIR": "/absolute/path/to/var"
      }
    }
  }
}
```

The registry and state paths are server configuration, never tool-call parameters. This
prevents an agent from using the metadata tools as arbitrary filesystem readers. The MCP
server does not expose metadata writes, SQL/query execution, deployment actions, or hidden
LLM calls.

The reference reasoning adapter uses an OpenAI-compatible Responses API with strict
structured output. Configure it before starting the service:

```bash
export AI_DATA_PLATFORM_LLM_API_KEY="..."
export AI_DATA_PLATFORM_LLM_MODEL="..."
# Optional for another Responses API-compatible endpoint:
export AI_DATA_PLATFORM_LLM_BASE_URL="https://api.openai.com/v1"
python -m ai_data_platform serve --state-dir var
```

If the API key or model is absent, the context endpoints still run and the reasoning
endpoint returns `503` rather than silently substituting a non-LLM implementation.
For a custom `AI_DATA_PLATFORM_LLM_BASE_URL`, set
`AI_DATA_PLATFORM_LLM_API_KEY` explicitly; the server never sends `OPENAI_API_KEY` to a
custom endpoint.

The service publishes OpenAPI documentation at `http://127.0.0.1:8000/docs`. Its main
endpoints are:

- `POST /v1/runtime/observations` — record current state in SQLite and history in DuckDB
- `POST /v1/discovery` — deterministic or hybrid discovery with explained runtime reranking
- `POST /v1/reasoning/dataset-selection` — governed selection, explanation, and evidence
- `GET /v1/datasets/{dataset_id}/context` — combined contract, semantics, runtime state,
  and row-count trend
- `GET /v1/datasets/{dataset_id}/runtime/history` — recent observations

See [Milestone 3 runtime context design](docs/milestone-3-runtime-context.md) for the
storage boundary and ranking policy. See
[Milestone 4 governed reasoning design](docs/milestone-4-governed-reasoning.md) for the
provider boundary, evidence contract, guardrails, API, and evaluation approach. See
[Milestone 5 governed MCP tools](docs/milestone-5-governed-mcp-tools.md) for the tool
contract, security boundary, configuration, and deferred capabilities.

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
