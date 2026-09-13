# Milestone 1 architecture

## Problem

Data consumers often use overlapping terms for different business definitions. This
baseline makes those definitions explicit, validates them, and deterministically
returns a small, explained dataset candidate set instead of guessing.

## Semantic registry

Git-tracked YAML is the authoritative source for concepts, entities, metrics, and
datasets. Canonical concepts and metrics are defined once. Dataset contracts reference
them and contain only physical or dataset-specific information: grain, certification,
ownership, intended use, and freshness.

## Capability layer

The Python package loads and validates the registry, resolves metrics, discovers
datasets, ranks candidates, and assesses semantic ambiguity. The CLI is a thin adapter
over those APIs, so a future REST, MCP, or platform adapter can reuse the same logic.

## Retrieval, ranking, and ambiguity

Retrieval is deterministic keyword matching over dataset and linked semantic metadata.
Centralized ranking rules combine textual evidence with metric compatibility, domain,
use-case/audience alignment, certification, and layer. Results include structured
reasons. When similarly relevant metrics explicitly declare themselves non-equivalent,
the result is `CLARIFICATION_REQUIRED` rather than a guessed answer.

## Why YAML in Git now

YAML is reviewable, versioned, portable, and simple enough for an initial semantic
registry. The loader and Pydantic contracts protect structural and referential
integrity without making a database a second source of truth.

## Deferred

Embeddings and hybrid retrieval, runtime freshness/quality services, LLM reasoning,
MCP/HTTP interfaces, agents, and Snowflake/Databricks mappings are intentionally
outside Milestone 1.
