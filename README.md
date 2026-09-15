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

Milestone 2 adds vector retrieval while retaining Milestone 1 as a measurable,
deterministic baseline:

```text
Git/YAML semantic registry
        ↓
canonical semantic documents
        ↓
provider-neutral embeddings
        ↓
deterministic retrieval + in-memory vector retrieval
        ↓
weighted reciprocal-rank fusion
        ↓
certification / layer / prohibited-use policy
        ↓
top candidates + explanations + semantic ambiguity detection
```

The core owns no model-vendor SDK and requires no external vector database. The
`EmbeddingProvider` protocol accepts a local or managed model, while the dependency-free
`HashingEmbeddingProvider` keeps tests, examples, and CI reproducible. It is a reference
provider rather than a claim of production semantic quality.

The Milestone 1 path remains available as `discover_datasets()`. The new
`discover_datasets_hybrid()` path fuses that baseline with vector results and applies
deterministic exclusions and ambiguity policy after retrieval. Vector similarity is never
the sole authority.

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
build_vector_index(registry, embedding_provider)
discover_datasets_hybrid(question, registry, vector_index, limit=5)
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
  policy/         centralized ranking policy
  api.py          stable Python capability layer

evaluation/       versioned retrieval relevance cases
tests/            acceptance and unit tests
```

## Run Milestone 2

```bash
python -m pip install -e .
python -m ai_data_platform validate
python -m ai_data_platform discover "What was net revenue last quarter?"
python -m ai_data_platform discover "What was net revenue last quarter?" --mode hybrid
python -m ai_data_platform evaluate
python -m unittest discover -s tests -v
```

The evaluation command reports Recall@K, mean reciprocal rank, and discovery-status
accuracy for both the deterministic baseline and hybrid retrieval. The checked-in corpus
is deliberately small; expanding it with domain-owner judgments is part of making the
comparison representative rather than optimizing for a demo.

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
