# Milestone 6 — Governed AI-native engineering capabilities

Milestone 6 adds a capability layer for low-risk data-engineering work. It does **not** add a general-purpose autonomous agent, an arbitrary command runner, or a production deployment mechanism.

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
