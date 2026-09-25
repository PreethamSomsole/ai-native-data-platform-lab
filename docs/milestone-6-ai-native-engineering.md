# Milestone 6 — Governed AI-native engineering capabilities

Milestone 6 adds a capability layer for low-risk data-engineering work. It does **not** add a general-purpose autonomous agent, an arbitrary command runner, or a production deployment mechanism.

## Schema crawler onboarding

The crawler adds a manual, review-first onboarding path for local DuckDB sources. It is a
repository workflow, not an agent tool or an automatic registry publisher.

```text
DuckDB file + schema
        ↓
ai-data-platform crawl --source ... --schema ... --registry ... --output ...
        ↓
separate review area: draft YAML + crawl_report.yaml
        ↓
curator edits guessed and business metadata
        ↓
ai-data-platform validate-drafts --drafts ... --registry ...
        ↓
human-reviewed copy into registry/datasets/
        ↓
usual registry validation and discovery
```

Both the installed `ai-data-platform crawl` command and `test_crawler.py` require explicit
source, schema, registry, and output arguments. The demo runner can seed a deterministic
synthetic source with `--seed-demo`; it refuses to overwrite an existing source file or a
non-empty output directory. The regular crawler opens the source read-only. It rejects an
output directory inside the source registry. Crawling never writes into `registry/`.

The generated dataset YAML is the correction surface. Curators edit the guessed `name`
and `domain` directly and fill in business metadata such as grain, owner, audience, use cases,
freshness commitments, and canonical metric/entity/concept IDs. Layer is based on a table
prefix (or defaults to `raw`); domain uses recognized name tokens or a first-token fallback;
name is title-cased from the physical table name. These are conventions and heuristics, not
observed business facts. The report records their values and method under
`heuristic_assessments`. A fresh crawl should use a new empty review directory so it does not
replace curator edits.

`observed_facts` contains physical table identity, column names/types/nullability, declared
DuckDB constraints, and profile counts/ranges. Profiling is capped at 10,000 rows per table by
default. Tables at or below the cap are fully profiled. Larger tables use a repeatable
reservoir sample without replacement (default seed 42) of at most 10,000 rows. The report
labels partial coverage, records the sample size and seed, and warns that rare values and
anomalies may be missed. Sampling avoids a first-rows-only bias but scans the full table and
does not guarantee detection of every data issue. Change `--profile-rows` and `--sample-seed`
to make a different explicit profiling choice.

Warnings such as likely date/name drift, mixed epoch units, and out-of-range scores are
heuristic assessments. They do not state that a defect exists. The crawler reports no row
samples, does not assert candidate keys as constraints, and does not infer grain, semantic
references, owners, use cases, freshness commitments, or certification. A sample-based
unique-column candidate is only a review clue.

`validate-drafts` parses every dataset YAML in the review area, overlays those datasets on
the canonical registry in memory, and runs the existing structural and semantic-reference
validator. It catches unknown metric, entity, and concept IDs as well as invalid contracts.
It does not promote or write files. Promotion remains the curator's explicit file-copy/change
review step, followed by the regular `ai-data-platform validate` command. Discovery then uses
the platform's existing registry loader and discovery API.

### M6 crawler boundaries and acceptance criteria

Boundary: local DuckDB database files and named schemas only; no remote connectors, writes to
the source database, automatic metadata promotion, external filesystem scans, or agent/MCP
exposure. The 10,000-row cap controls in-memory profiling, not total scan cost for larger tables.
Review-area contracts remain uncertified unless they are preserved copies of already governed
contracts with the same canonical ID.

Acceptance criteria before any agent-tool connection:

1. A documented CLI requires explicit source, schema, canonical registry, and review output
   paths; only schema inspection and reading occur on the source.
2. The crawler keeps all generated contracts and the report outside `registry/`, labels observed
   facts separately from heuristic assessments, and makes profile completeness and sampling
   method visible.
3. Curators can correct inferred name/domain values and add canonical semantic references by
   editing review-area YAML.
4. A separate draft-validation command checks model fields and semantic references against the
   canonical registry without promoting or mutating either directory.
5. One corrected draft passes validation, is copied into an isolated registry fixture, loads
   through the existing registry API, and is discoverable through existing deterministic
   discovery.
6. Automated coverage exercises profiling, corrections/references, and safe review-area
   generation; no agent tool receives crawler, filesystem, SQL, or promotion access.

The reference implementation is deliberately local and repository-scoped:

```text
agent / planner
      ↓
typed EngineeringService request
      ↓
deterministic policy and configured DEV boundaries
      ↓
registry validation / fixed validation profile / local DuckDB action
      ↓
auditable result, manifest, and recovery artifact
```

## Capabilities

`EngineeringService` owns six high-level capabilities:

- `recommend_ingestion_pattern()` selects one deterministic pattern from source type and change-data availability: streaming ingestion, log-based CDC, incremental watermark batch, or file micro-batch.
- `generate_pipeline_plan()` creates a standard raw-to-curated implementation sequence and identifies contract, architecture, and environment approvals.
- `validate_dataset_contract()` validates a proposed `Dataset` contract against the canonical registry before it is committed to Git/YAML.
- `run_dev_validation()` executes only a server-defined validation profile. Callers can select `registry` or `unit`; they cannot supply a shell command.
- `run_reconciliation()` performs bounded, bag-aware row comparison between two validated table identifiers in the configured local DuckDB database.
- `deploy_pipeline_to_dev()` records a content-hashed deployment manifest only after a configured validation profile succeeds.

`safe_replace_table()` is the reference destructive operation. In a single DuckDB transaction it copies the current target to a uniquely named backup, replaces the target with a staging table, and retains the backup. `restore_table_from_backup()` provides the deterministic inverse operation and also preserves the pre-restore state.

## Safety and approval contract

| Action | Local DEV | QA / production |
| --- | --- | --- |
| Inspect, recommend, plan, validate, reconcile | Allowed | Plan is marked approval-required where applicable |
| DEV validation | Fixed `registry` / `unit` profiles only | Not an executor |
| Contract or architecture change | Approval-required | Approval-required |
| Table replacement / rollback | Allowed only with configured DEV writes and automatic backup | Not executable by this reference runner |
| Deploy | Content-hashed DEV manifest after validation | Not executable by this reference runner |

An `approval_granted` field does not bypass an environment boundary. QA and production need an environment-specific adapter that supplies authentication, authorization, change records, promotion policy, and production rollback semantics.

All configurable trust boundaries are constructor inputs, not tool-call inputs:

- repository root
- state directory and local DuckDB database
- registry object
- whether local DEV writes are enabled

The service rejects paths outside the configured repository, unsupported deployment file types, arbitrary shell commands, unknown registry references, and unsafe SQL identifiers. Every state-changing action appends a JSONL audit record in the configured state directory.

## Local usage

```python
from pathlib import Path

from ai_data_platform import load_registry
from ai_data_platform.engineering import (
    DeploymentRequest,
    EngineeringService,
    IngestionRequest,
    SafeReplaceRequest,
    SourceKind,
)

root = Path.cwd()
engineering = EngineeringService(
    load_registry(root / "registry"),
    repository_root=root,
    state_directory=root / "var" / "engineering",
)

recommendation = engineering.recommend_ingestion_pattern(
    IngestionRequest(
        source_kind=SourceKind.DATABASE,
        change_data_available=True,
        expected_latency_minutes=15,
    )
)
validation = engineering.run_dev_validation("unit")
deployment = engineering.deploy_pipeline_to_dev(
    DeploymentRequest(
        artifact_path="src/pipelines/customer_profitability.py",
        validation_profile="unit",
    )
)

# After a staging table has passed reconciliation:
replacement = engineering.safe_replace_table(
    SafeReplaceRequest(
        target_table="customer_profitability",
        replacement_table="customer_profitability_staging",
    )
)
```

The service intentionally does not write metadata to Git, generate production SQL, or merge branches. Those operations need explicit capability contracts and environment adapters rather than implicit agent authority.
