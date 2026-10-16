# Live demo: governed AI-native data pipelines

A 15–20 minute walkthrough on real data volume. Everything is computed live; nothing is mocked.

## Setup (once, about 1 minute)

```bash
python -m pip install -e ".[test,data,dbt]"
python -m ai_data_platform sample-data tpch --scale 1    # ~6M order lines, ~350 MB Parquet in var/data
```

Rehearse once with `python demo/showcase.py`. The full run takes about one minute on a
4-core laptop or container.

## Run it

```bash
python demo/showcase.py --pause          # Enter advances each step
python demo/showcase.py --only 6 --pause # replay one step (needs the earlier state in var/)
python demo/showcase.py --scale 10       # ~60M lines; regenerate var/data first
```

## Talking points

| Step | What the audience sees | The point |
| --- | --- | --- |
| 1. Data | Row counts and Parquet sizes | Realistic volume from a deterministic generator, never committed to Git |
| 2. Semantics | Three "revenue" definitions with owners | Meaning is defined once, before any pipeline exists |
| 3. Pipelines | Seven pipelines, about 12s, every check passing | Specs are YAML + one SELECT; gates are declarative; publishing is atomic with a backup |
| 4. Analytics | Three definitions diverge by billions near TPC-H's as-of date; DuckDB answers in milliseconds | "Net revenue" really is ambiguous, so the platform must not guess |
| 5. Discovery | `CLARIFICATION_REQUIRED`, then `RESOLVED` with runtime reasons; verified lineage tree | Pipelines feed freshness, quality, and lineage back into AI context |
| 6. Bad batch | An orphan line and a sign-flipped price: range and reconciliation gates fail, downstream is skipped and marked stale, the failing dataset drops 40 points in discovery, then everything recovers | Bad data never reaches consumers, and AI consumers can see the degradation |
| 7. Agent | Unsafe SQL and self-certification rejected; a valid proposal dry-runs and is queued for review | Agents get real engineering capability, with deterministic limits |
| 8. dbt | The same specs compile to a contract-enforced dbt project; `dbt build` passes 39 nodes | The governance model carries over to dbt, warehouses, and Databricks |

## Live agent variant (step 7)

Register the MCP server with Claude Code (or any MCP client) from the repository root:

```bash
claude mcp add ai-native-data-platform -- ai-data-platform-mcp
# or copy demo/mcp.json into .mcp.json
```

Then ask: *"Using the data platform tools, propose a pipeline that reports recognized revenue
by customer market segment and fiscal quarter. Dry-run it before submitting."* The agent
discovers inputs with `list_pipeline_inputs`, iterates on `dry_run_pipeline_proposal`, and
ends with `submit_pipeline_proposal`. Review the package in `var/pipeline_proposals/`.
Close other processes using `var/engineering.duckdb` first, because DuckDB allows one writer.

## Likely questions

- **Is this real data?** TPC-H is synthetic but standard and realistically shaped. Its quirks
  (a fixed 1995-06-17 as-of date and about 50% returns on received lines) are called out on
  screen. NYC taxi trip data can be downloaded with `sample-data nyc-taxi` where the network
  allows `d37ci6vzurychx.cloudfront.net`.
- **Why not let the agent run SQL directly?** It can, but only one SELECT over declared
  inputs, in a sandbox with file access locked off, and never to publish.
- **Does this replace dbt or DLT?** No. Specs compile to dbt today, and the Databricks mapping
  is in `docs/pipeline-portability.md`. The contribution is the contract, gate, observation, and
  agent-permission model around the engine.
