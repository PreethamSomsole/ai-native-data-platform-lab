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

Focus:
- vendor-neutral embedding provider interface
- embeddings for semantic registry content
- vector retrieval
- keyword + vector hybrid retrieval
- candidate fusion / re-ranking
- evaluation harness comparing M1 vs M2 retrieval quality

Important constraint:
- certification, business use case, domain, and semantic policy still influence final
  ranking; vector similarity must not become the sole authority.

## Milestone 3 — Runtime metadata and context service

Separate declarative semantics from observed runtime state.

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

## Milestone 4 — LLM reasoning over curated context

Introduce an LLM only after retrieval and policy are working independently.

Focus:
- provide the LLM only the top relevant candidates
- structured context assembly
- dataset selection reasoning
- explanation of selection
- explicit abstention when semantic policy says clarification is required
- evaluation for hallucination and incorrect source selection

Principle:

> Retrieval finds plausible context; deterministic policy enforces business constraints;
> the LLM reasons over the curated candidate set.

## Milestone 5 — Agent tool / MCP exposure

Expose existing core capabilities to AI agents without moving business logic into the
agent interface.

Potential tools:
- `discover_datasets()`
- `get_metric_definition()`
- `get_dataset_contract()`
- `get_entity()`
- `validate_metadata()`
- `get_lineage()` when available

MCP is an adapter around the capability layer, not the implementation of the platform.

## Milestone 6 — AI-native data engineering

Use the platform to help agents build and operate data systems.

Start with capabilities before multiple specialized agents.

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
