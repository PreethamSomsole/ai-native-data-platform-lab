# AI-Native Data Platform Architecture

## 1. Purpose

This project explores two related but distinct problems together:

1. **AI-ready data architecture** — how enterprise data, metadata, semantics, lineage,
   trust, and policy should be structured so AI applications can use them accurately
   and efficiently.
2. **AI-native data engineering** — how AI agents can become governed engineering
   actors that help build, validate, operate, and evolve the data platform itself.

The project is intentionally vendor-neutral first. Snowflake and Databricks are later
implementation mappings, not architectural starting points.

## 2. The two-plane model

The end-state has two mutually reinforcing planes.

```text
                AI-NATIVE DATA PLATFORM

        ┌──────────────────────────────┐
        │ Engineering / Agent Plane    │
        │                              │
        │ planner / coding / metadata  │
        │ validation / operations      │
        └──────────────┬───────────────┘
                       │ builds and operates
                       ▼
        ┌──────────────────────────────┐
        │ Data / Context Plane         │
        │                              │
        │ semantics / metadata         │
        │ retrieval / lineage / trust  │
        │ policy / context APIs        │
        └──────────────┬───────────────┘
                       │ provides context and tools
                       └───────────────► Agent Plane
```

The platform becomes more useful to AI as it accumulates explicit business semantics,
metadata, lineage, quality, and policy. The agents then use that context to make better
engineering decisions.

## 3. AI-ready data architecture

Traditional analytics often assumes that a human already understands which tables are
authoritative, what a metric means, and when a dataset should be used. AI cannot rely
on that tribal knowledge. The platform must make it machine-readable.

An AI context layer therefore needs to answer questions such as:

- What datasets and business concepts exist?
- What does each metric mean?
- Which definition is authoritative for a specific business use case?
- Who owns it?
- What is the grain?
- Which dimensions/entities are supported?
- Is the dataset certified and fresh?
- What is the lineage?
- What should this dataset not be used for?
- Is the current question semantically ambiguous?

### 3.1 Metadata as Code

Declarative semantic metadata lives in Git/YAML and is the authoritative source for:

- business concepts
- business entities
- metric definitions
- dataset contracts
- ownership
- intended and prohibited use cases
- semantic relationships
- certification
- freshness expectations

Git is preferred for declarative semantics because changes are versioned, reviewable,
auditable, and naturally governed through pull requests.

Dynamic observations such as current freshness, quality results, row counts, runtime
status, and usage statistics are expected to live in runtime stores in later
milestones rather than being committed repeatedly to Git.

### 3.2 Central semantics vs dataset-local metadata

Business meaning should be defined once when it is intended to be shared.

For example:

```text
business_concept.revenue
        ├── metric.finance_net_revenue
        └── metric.operations_net_revenue
```

Finance and Operations can both use the display term "Net Revenue" while owning
separate, non-equivalent definitions. Ownership, intended audience, and approved use
cases are part of the semantic identity.

Datasets reference these canonical definitions instead of redefining them locally.
Dataset-local metadata focuses on physical and implementation-specific details such as
physical name, grain, supported semantic references, refresh cadence, and ownership.

### 3.3 Retrieval before reasoning

An AI should not receive thousands of catalog definitions in one prompt. The platform
first retrieves a small relevant candidate set, then gives an LLM or agent enough
context to reason over those candidates.

```text
question
   ↓
candidate retrieval
   ↓
business-aware re-ranking
   ↓
top 5 candidates + reasons
   ↓
ambiguity check
   ↓
LLM/agent reasoning or user clarification
```

Milestone 1 uses deterministic keyword/metadata retrieval. Milestone 2 retains that path
as a baseline and adds provider-neutral embeddings, vector retrieval, and hybrid fusion
so retrieval quality can be measured rather than assumed.

### 3.4 Business-aware ranking

Text similarity only identifies plausible candidates; it does not decide which dataset
is best for the business question.

Ranking should consider signals such as:

- semantic/keyword relevance
- metric compatibility
- business domain
- intended use case
- target audience
- certification
- layer
- freshness and quality/trust signals when available

The caller receives the top candidate set and structured reasons rather than a hidden
single choice.

### 3.5 Semantic guardrails and abstention

Operational safety is not enough. An AI can leave every system unchanged and still
answer the wrong business question by choosing the wrong definition.

If multiple certified candidates are valid but represent different business meanings,
the platform should abstain and ask for clarification.

Example:

```text
"What was net revenue last quarter?"

→ Finance recognized revenue is valid
→ Operations completed-order revenue is valid
→ definitions are non-equivalent
→ CLARIFICATION_REQUIRED
```

But a question such as "Finance recognized net revenue" provides enough semantic intent
to prefer the Finance definition.

## 4. Core capability layer

The initial implementation is a reusable Python capability layer, not an HTTP service
or an MCP server.

Current public capabilities include:

```python
get_metric(...)
get_entity(...)
get_dataset(...)
resolve_metric(...)
discover_datasets(...)
build_vector_index(...)
discover_datasets_hybrid(...)
validate_registry(...)
```

The CLI is only an adapter. Future REST, MCP, Snowflake, and Databricks interfaces
should call the same underlying capability layer instead of reimplementing business
logic.

This keeps interfaces replaceable and avoids coupling the domain model to a specific
agent framework or vendor.

## 5. AI-native data engineering

The second plane treats AI agents as a new class of engineering actor alongside humans,
applications, schedulers, and services.

The goal is not to let an LLM become the deployment platform. The target separation is:

```text
Agent / LLM        → reason, investigate, propose, generate
Policy layer       → decide whether the proposed action is allowed
Execution platform → execute deterministically
Observability      → retain full operational detail
```

### 5.1 Autonomy model

Autonomy is not a global "trusted/untrusted" flag. It depends on:

- environment
- action risk
- blast radius
- recoverability
- reversibility
- data layer
- contract impact

Working direction:

```text
READ / INSPECT
→ generally autonomous

GENERATE / VALIDATE
→ autonomous

REVERSIBLE DEV WRITE
→ autonomous when policy allows

DESTRUCTIVE BUT RECOVERABLE DEV ACTION
→ autonomous only after deterministic safety checks

CONTRACT CHANGE
→ approval

PRODUCTION CHANGE / HIGH BLAST RADIUS / IRREVERSIBLE ACTION
→ approval or deny
```

For example, an additive nullable column in a schema-evolution-friendly raw layer can
be handled autonomously after validation, while changing a column from BIGINT to STRING
changes the contract and requires approval even if current values appear castable.

### 5.2 Investigate before remediation

Agents should not react to an error with the first technically plausible patch.

Preferred loop:

```text
observe
  ↓
investigate
  ↓
form hypothesis
  ↓
collect evidence
  ↓
recommend remediation
  ↓
policy decision
  ↓
execute if allowed
  ↓
validate
```

A schema mismatch should trigger inspection of schema history, source values, contracts,
and upstream changes before adding a cast.

### 5.3 High-level tools over raw primitives

Future agents should receive intent-level capabilities where possible.

Preferred:

```text
safe_replace_table()
deploy_pipeline_to_dev()
run_reconciliation()
validate_dataset_contract()
```

Restricted escape hatches:

```text
execute_sql()
shell_command()
raw_cloud_api()
```

High-level tools reduce privilege, encode repeatable safety behavior, improve auditability,
and prevent the agent from having to remember transactional or recovery mechanics.

### 5.4 Automated rollback as part of the tool contract

For autonomous destructive actions, recoverability should be a precondition rather than
an afterthought.

A capability such as `safe_replace_table()` should own its internal sequence:

```text
check environment / ownership / dependencies
        ↓
create recovery point
        ↓
perform replacement
        ↓
validate
        ├── success → commit outcome
        └── failure → rollback automatically
```

The agent should see the semantic outcome, for example:

```text
FAILED_AND_ROLLED_BACK
reason: schema validation failed
```

It does not need every internal SQL statement in its normal reasoning context.
Operational details remain available to observability and audit systems.

Principle:

> Hide operational complexity from the agent, but never hide it from observability.

### 5.5 AI reasons; deterministic systems control

Important controls should not rely on prompts alone.

Examples of deterministic responsibilities:

- structural and referential validation
- policy evaluation
- permission enforcement
- environment boundaries
- artifact promotion
- rollback behavior
- semantic ambiguity rules
- production approval gates

The agent expresses intent; the platform determines whether and how that intent can be
executed.

## 6. Promotion model

Agents may become highly autonomous in isolated or controlled DEV environments. That
does not mean regenerated code should independently flow through each environment.

The preferred pattern is artifact promotion:

```text
agent builds in DEV
      ↓
tests + data validation
      ↓
known artifact / commit
      ↓
promotion gate
      ↓
QA / UAT
      ↓
human or policy gate
      ↓
PROD
```

A known, validated artifact should be promoted rather than asking the model to recreate
production code from scratch.

## 7. Bootstrapping the platform

There is an intentional bootstrap sequence.

### Stage 1 — Bootstrap

Agent context comes primarily from Git, repository instructions, architecture docs, and
human guidance. The agent helps build the initial semantic/context platform.

### Stage 2 — Assisted

The platform exposes its first reusable capabilities such as semantic resolution,
dataset discovery, metadata validation, and context retrieval. Engineering agents start
using those capabilities while continuing to build the platform.

### Stage 3 — Native

Agents use the platform's own metadata, semantics, lineage, policy, validation, and
safe execution tools to build and operate data systems. New datasets and contracts are
registered back into the platform, closing the loop.

## 8. Milestone 1 implementation

Milestone 1 establishes the deterministic semantic baseline:

```text
registry/ YAML
     ↓
Pydantic models
     ↓
registry loader
     ↓
structural + referential validation
     ↓
keyword / metadata retrieval
     ↓
centralized business ranking policy
     ↓
top 5 explained candidates
     ↓
ambiguity detection
```

This milestone deliberately excludes LLMs and embeddings. The purpose is to make
semantics and policy explicit and testable before adding probabilistic retrieval or
reasoning.

## 9. Milestone 2 implementation

Milestone 2 adds a probabilistic retrieval signal without allowing it to bypass semantic
or governance rules:

```text
validated registry
      ↓
canonical documents for concepts / entities / metrics / datasets
      ↓
EmbeddingProvider
      ↓
in-memory cosine index
      ↓
M1 candidate rank + vector candidate rank
      ↓
weighted reciprocal-rank fusion
      ↓
prohibited-use exclusion + certification/layer policy
      ↓
ambiguity assessment + explained top candidates
```

The interfaces are intentionally small. A production-quality local model or managed
embedding service can implement `EmbeddingProvider`; storage can later replace the
in-memory index without changing registry semantics or discovery policy. The built-in
hashing provider exists for deterministic development and CI, not as a production model.

Reciprocal-rank fusion is used because deterministic scores and cosine similarity are not
directly comparable. The deterministic path receives a higher fusion weight so adding a
weak vector signal cannot silently degrade the known baseline. Prohibited-use conflicts
remain hard exclusions, and explicitly non-equivalent metrics can still force
`CLARIFICATION_REQUIRED`.

Evaluation cases live in versioned YAML. The harness compares Recall@K, mean reciprocal
rank, and expected discovery status for M1 and M2 using the same questions. This makes
provider and threshold changes reviewable engineering decisions.

## 10. Roadmap

The planned sequence is documented in [roadmap.md](roadmap.md). At a high level:

1. deterministic semantic registry and discovery
2. embeddings and hybrid retrieval
3. runtime metadata and context service/API
4. LLM reasoning over curated context
5. MCP/tool exposure
6. AI-native engineering agents and safe execution tools
7. Snowflake mapping
8. Databricks mapping

The implementation should remain incremental: build reusable platform capabilities
first and compose them into agents later.
