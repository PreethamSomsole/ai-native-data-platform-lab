# AI-Native Data Platform Roadmap

This roadmap preserves the learning and implementation sequence for the lab. The goal is
to add complexity only when the previous layer is understood, testable, and useful.

## Milestone 1 — Semantic registry and deterministic discovery

Status: implementation baseline plus semantic-governance hardening.

Focus:
- Git/YAML metadata as the declarative source of truth
- canonical concepts, entities, metrics, and dataset contracts
- explicit metric ownership
- Pydantic domain models
- structural and referential validation
- symmetric semantic non-equivalence relationships
- deterministic keyword/metadata retrieval
- business-aware ranking
- hard exclusion when a requested use case conflicts with an explicit prohibited use case
- declared freshness/cadence as a ranking expectation signal
- top-5 candidates with ranking explanations
- semantic ambiguity detection and `CLARIFICATION_REQUIRED`
- CI validation of registry integrity, tests, and linting

Milestone 1 freshness is **declarative**: the registry can describe and rank against an
expected cadence or SLA. It does not claim that a dataset is actually fresh at runtime.
Observed freshness, pipeline health, row-count signals, and quality status belong to
Milestone 3 runtime metadata.

Key learning:

> Good AI answers begin with explicit semantics and governance, not with an LLM.

## Milestone 2 — Embeddings and hybrid retrieval

Add semantic retrieval without replacing the deterministic baseline.

Status: reference implementation complete; expand the labeled corpus and evaluate a
production-quality embedding adapter before treating retrieval quality as production-ready.

Focus:
- vendor-neutral embedding provider interface
- embeddings for semantic registry content
- vector retrieval
- keyword + vector hybrid retrieval
- candidate fusion / re-ranking
- evaluation harness comparing M1 vs M2 retrieval quality

Reference implementation:
- `EmbeddingProvider` is the vendor-neutral model boundary
- canonical documents cover concepts, entities, metrics, and datasets
- `InMemoryVectorIndex` provides validated cosine retrieval for the lab
- weighted reciprocal-rank fusion combines the full M1 rank with vector rank
- deterministic ranking receives the higher fusion weight to protect the known baseline
- prohibited-use exclusions and non-equivalence ambiguity rules run after retrieval
- a dependency-free hashing provider makes local development and CI repeatable
- versioned YAML cases compare Recall@K, mean reciprocal rank, and status accuracy

The hashing provider validates architecture and integration, but it is not a substitute for
evaluating a production semantic model. Similarity thresholds are provider- and corpus-specific
and must be tuned against labeled cases.

Important constraint:
- certification, business use case, domain, and semantic policy still influence final
  ranking; vector similarity must not become the sole authority.

## Milestone 3 — Runtime metadata and context service

Separate declarative semantics from observed runtime state.

Status: reference implementation complete; production connectors, authentication, and
managed deployment remain future work.

Declarative metadata remains in Git:
- definitions
- ownership
- grain
- use cases
- semantic relationships
- certification intent

Runtime metadata is added separately:
- freshness
- quality status
- last successful pipeline run
- row-count or volume signals
- usage/popularity
- operational health

Expose a reusable context service/API that combines both sources.

Reference implementation:
- typed, timezone-aware runtime observations
- SQLite latest-state store for request-time context
- DuckDB append history for trends and analytical inspection
- replaceable current-state and history store interfaces
- deterministic runtime ranking policy for freshness, quality, health, and usage
- an explained adjustment for every runtime signal that changes rank
- runtime-aware deterministic and hybrid discovery capabilities
- context assembly joining dataset contracts, canonical semantics, runtime state, and
  row-count history
- FastAPI health, observation ingestion, discovery, dataset context, and history endpoints
- configurable local persistence and a CLI service runner

Important constraints:
- Git/YAML remains authoritative for business meaning and governance intent.
- Runtime signals may rerank candidates but do not bypass prohibited-use exclusions.
- Runtime signals do not choose between explicitly non-equivalent business definitions;
  `CLARIFICATION_REQUIRED` remains authoritative.
- FastAPI is an adapter around the capability layer rather than a second implementation.

## Milestone 4 — LLM reasoning over curated context

Introduce an LLM only after retrieval and policy are working independently.

Status: reference implementation complete; production model evaluation, prompt/model
version tracking, authentication, rate limiting, and operational telemetry remain future
hardening work.

Focus:
- provide the LLM only the top relevant candidates
- structured context assembly
- dataset selection reasoning
- explanation of selection
- explicit abstention when semantic policy says clarification is required
- evaluation for hallucination and incorrect source selection

Reference implementation:
- `DatasetReasoningProvider` defines a vendor-neutral model boundary
- an OpenAI-compatible Responses API adapter requests strict structured output
- structured assembly exposes at most five candidates and an enumerated evidence catalog
- the reasoning scope is dataset selection, explanation, and evidence only
- returned dataset and evidence IDs are validated against the supplied context
- `NO_MATCH` abstains without calling the model
- `CLARIFICATION_REQUIRED` permits explanation and a clarification question but never
  dataset selection or a tentative recommendation
- attempted ambiguity overrides are discarded and surfaced through guardrail events
- versioned evaluation cases measure status accuracy, selection accuracy, safe abstention,
  grounded responses, and provider/policy errors

Important constraints:
- the LLM cannot restore excluded candidates or introduce registry objects
- deterministic discovery status remains authoritative after generation
- the reference provider is optional configuration; missing credentials never trigger a
  hidden fallback
- analytical answers, SQL generation, query execution, MCP, and agent actions remain out
  of scope

Principle:

> Retrieval finds plausible context; deterministic policy enforces business constraints;
> the LLM reasons over the curated candidate set.

## Milestone 5 — Agent tool / MCP exposure

Expose existing core capabilities to AI agents without moving business logic into the
agent interface.

Status: reference read-only stdio implementation complete; authentication, authorization,
remote transport, write tools, and production deployment remain future work.

Reference implementation:
- `discover_datasets()`
- `get_dataset_contract()`
- `get_dataset_context()`
- `get_metric_definition()`
- `get_entity()`
- `get_concept_definition()`
- `validate_metadata()` reloads only the server-configured registry

The protocol-independent `AgentToolService` is the agent capability boundary. The MCP
server registers strict input/output schemas, translates stable capability errors, caps
discovery at five candidates, and marks every tool read-only, non-destructive, idempotent,
and closed-world.

MCP is an adapter around the capability layer, not the implementation of the platform.
Git/YAML, deterministic semantic policy, runtime ranking, and context assembly remain in
their existing layers. There is no raw filesystem, SQL, deployment, or metadata-write tool.

Lineage is deliberately deferred until the registry has an explicit, validated lineage
model. The interface must not imply lineage knowledge the platform does not yet possess.

## Milestone 6 — AI-native data engineering

Use the platform to help agents build and operate data systems.

Start with capabilities before multiple specialized agents.

Status: local DEV reference implementation complete; environment-specific adapters,
Git workflow execution, and specialized agent orchestration remain future work.

Reference implementation:
- deterministic ingestion-pattern recommendation
- approval-aware pipeline planning
- proposed dataset-contract validation against canonical semantics
- repository-scoped, fixed-profile DEV validation
- bounded DuckDB reconciliation with safe identifiers
- content-hashed DEV deployment manifests after validation
- transactional DuckDB replacement with a retained backup and deterministic rollback
- append-only local audit records for state-changing actions

The local runner is deliberately not a universal executor: callers cannot pass shell
commands, database URLs, external filesystem paths, or arbitrary SQL. QA and production
writes remain unavailable even if a caller supplies an approval flag; those environments
need an explicit adapter with their own authorization, change-control, and rollback
contracts.

Potential high-level tools:
- `recommend_ingestion_pattern()`
- `generate_pipeline_plan()`
- `validate_dataset_contract()`
- `run_dev_validation()`
- `run_reconciliation()`
- `deploy_pipeline_to_dev()`
- `safe_replace_table()`

Introduce specialized agents only when responsibilities are clear, for example:
- planner / engineering agent
- metadata agent
- validation agent
- operations agent

### Autonomy rules

Autonomous by default where low-risk:
- read Git
- inspect metadata
- generate code
- run tests
- isolated DEV/sandbox execution
- validate and iterate
- merge DEV-only work where the environment boundary is safe

Policy/approval dependent:
- destructive actions even in DEV unless recoverability and blast radius checks pass
- source or downstream contract changes
- ingestion-pattern / architecture changes
- QA/UAT promotion depending on policy
- production changes
- IAM/security changes
- irreversible or high-blast-radius operations

### Rollback

Autonomous destructive actions should require an automated recovery path. Rollback is
part of the deterministic tool contract, not something the LLM is expected to remember.

## Milestone 7 — Snowflake mapping

Map the vendor-neutral architecture onto Snowflake capabilities.

Evaluate which components Snowflake can provide or replace, including:
- semantic models / semantic views
- Cortex / agent capabilities
- search and retrieval
- metadata and lineage
- Snowflake-native tool execution
- governance and security

The purpose is to understand the mapping from architecture to product capability rather
than rebuild the architecture around Snowflake terminology.

## Milestone 8 — Databricks mapping

Repeat the same exercise for Databricks.

Evaluate mappings such as:
- Unity Catalog
- semantic / Genie capabilities
- vector search
- Lakeflow
- agent tooling
- model serving / AI Gateway where appropriate
- governance, lineage, and quality integration

Compare Snowflake and Databricks against the same vendor-neutral architectural model.

## End-state

The target workflow is something like:

```text
"Onboard a customer profitability dataset."

Engineering agent
      ↓
queries platform semantics and contracts
      ↓
finds approved revenue/customer/cost definitions
      ↓
inspects existing implementation patterns
      ↓
recommends ingestion / transformation architecture
      ↓
human approves architecture or contract changes
      ↓
agent implements in DEV
      ↓
runs tests, reconciliation, and validation
      ↓
uses safe rollback when needed
      ↓
merges/promotes validated artifact according to policy
      ↓
registers new semantic and dataset metadata
      ↓
context platform immediately knows about the new dataset
```

This closes the loop between **AI-ready data** and **AI-native data engineering**.
