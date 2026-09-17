# Milestone 5 Governed MCP Tools

## Purpose

Milestone 5 makes the platform's existing semantic and runtime context capabilities
available to AI agents. It does not make the MCP server a new source of business logic.

The authority order remains:

1. Git/YAML defines business meaning and governance intent.
2. Runtime stores provide observed operational state.
3. Capability and context services apply retrieval, exclusion, ambiguity, and reranking.
4. `AgentToolService` presents bounded, read-only operations.
5. MCP validates protocol inputs and serializes typed results.

## Layers

```text
MCP client
   ↓
MCPServer tool registration and schema validation
   ↓
AgentToolService
   ↓
existing api.py and ContextService capabilities
   ↓
registry, policy, discovery, embedding index, and runtime stores
```

`AgentToolService` contains no MCP types. It can therefore support another agent protocol
without duplicating the platform's domain behavior. The MCP module contains descriptions,
input constraints, read-only annotations, and error translation only.

## Tool contract

| Tool | Purpose | Important bounds |
|---|---|---|
| `discover_datasets` | Governed deterministic or hybrid discovery with runtime reranking | 1–5 candidates; similarity -1.0–1.0 |
| `get_dataset_contract` | Exact canonical dataset contract lookup | Exact server-loaded ID |
| `get_dataset_context` | Contract, semantics, latest runtime state, summary, and row-count trend | 1–1,000 trend points |
| `get_metric_definition` | Exact canonical metric lookup | Exact server-loaded ID |
| `get_entity_definition` | Exact canonical entity lookup | Exact server-loaded ID |
| `get_concept_definition` | Exact canonical concept lookup | Exact server-loaded ID |
| `validate_metadata` | Reload and validate configured YAML | No caller-supplied path |

Every tool is marked read-only, non-destructive, idempotent, and closed-world. These MCP
annotations help clients plan safely but are not treated as enforcement by themselves.
Actual enforcement comes from the absence of write/execution operations and from strict
server-controlled inputs.

## Discovery behavior

The discovery tool delegates to `ContextService.discover()`. Consequently it preserves:

- prohibited-use exclusions
- deterministic semantic non-equivalence checks
- `CLARIFICATION_REQUIRED` rather than guessed metric selection
- `NO_MATCH` for unrelated questions
- certification, layer, declared freshness, and runtime ranking reasons
- deterministic or hybrid retrieval selected explicitly by the caller

The tool returns the structured `DiscoveryContext`, including the authoritative discovery
status, ranked candidates, dataset contracts, explanations, and available runtime state.
There is no LLM call in this path.

## Validation and errors

`validate_metadata` reloads the registry from the path configured when the server starts.
This allows a local engineering agent to edit repository YAML and validate the current
files without restarting the server. The tool returns `valid`, all validation errors, and
object counts when valid.

Known lookup and argument failures use stable codes:

- `NOT_FOUND`
- `INVALID_ARGUMENT`
- `CAPABILITY_UNAVAILABLE`

The MCP adapter converts these failures to tool errors while retaining the code in the
message. Pydantic and MCP schema validation reject malformed protocol arguments before a
capability runs.

## Running the server

Install the package and start the stdio server:

```bash
python -m pip install -e ".[test]"
python -m ai_data_platform mcp --state-dir var
```

Or use the installed entry point:

```bash
AI_DATA_PLATFORM_REGISTRY=/absolute/path/to/registry \
AI_DATA_PLATFORM_STATE_DIR=/absolute/path/to/var \
ai-data-platform-mcp
```

The second form is convenient for MCP client configuration. The stdio transport is local
and does not add a network listener.

## Verification

Tests cover the protocol-independent service and the MCP registration layer:

- canonical definition and runtime-context results
- preserved ambiguity and bounded candidates
- registry reload and invalid-YAML reporting
- exact published tool catalog
- read-only/non-destructive annotations
- generated input bounds and absence of a caller-controlled registry path
- structured MCP results and stable tool-error translation
- real CLI/stdio initialization, tool listing, success, and wire-level error results
- deterministic SQLite/DuckDB cleanup through the MCP server lifespan

## Explicitly deferred

- metadata writes and registry mutation
- SQL generation or query execution
- pipeline, deployment, and infrastructure actions
- remote MCP transports, authentication, authorization, and rate limiting
- agent autonomy, approvals, rollback, and engineering actions (Milestone 6)
- lineage until the registry has a first-class lineage model
- Snowflake- or Databricks-specific tool implementations
