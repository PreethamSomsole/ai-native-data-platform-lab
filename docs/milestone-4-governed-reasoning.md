# Milestone 4 Governed Dataset Reasoning

## Purpose

Milestone 4 adds LLM reasoning only after semantic retrieval, runtime-aware reranking,
and ambiguity policy have completed. The capability selects and explains a dataset; it
does not answer the analytical question, generate SQL, or execute anything.

The authority order is:

1. Git/YAML defines business meaning and governance intent.
2. Deterministic and hybrid retrieval identify eligible candidates.
3. Deterministic policy establishes `RESOLVED`, `CLARIFICATION_REQUIRED`, or `NO_MATCH`.
4. The LLM reasons only within that curated boundary.
5. Deterministic post-generation validation checks every returned reference.

## Capability flow

```text
question
   ↓
ContextService.discover()
   ↓
top 1–5 eligible CandidateContext objects
   ↓
assemble_reasoning_context()
   ├── candidate summaries
   ├── canonical metric definitions
   ├── entities and concepts
   ├── ranking reasons
   ├── latest runtime observations
   └── stable evidence IDs
   ↓
DatasetReasoningProvider.generate()
   ↓
ReasoningDraft (untrusted)
   ↓
ReasoningService policy validation
   ↓
DatasetSelectionResult
```

## Provider boundary

`DatasetReasoningProvider` has two responsibilities:

- expose a stable `provider_id`
- return a typed `ReasoningDraft` for a `CuratedReasoningContext`

The reference `OpenAIResponsesReasoningProvider` calls a Responses API-compatible
`/responses` endpoint. It requests strict JSON Schema output containing:

- `selected_dataset_id`
- `explanation`
- `evidence_ids`
- `clarification_question`

The adapter uses the Python standard library, so the provider boundary does not force an
SDK dependency into the core package. Another implementation can replace it without
changing discovery, context assembly, policy validation, evaluation, or the HTTP model.

## Evidence contract

Every piece of source material exposed to the model has a stable ID. Evidence kinds are:

- dataset contract
- metric definition
- entity definition
- concept definition
- deterministic/runtime ranking reason
- latest runtime state

The model returns only evidence IDs. `ReasoningService` resolves them against the exact
catalog sent with the request. Unknown IDs fail closed with `ReasoningPolicyError` and map
to HTTP `502`; hallucinated sources are never echoed as valid evidence.

## Selection and abstention policy

| Discovery status | Provider call | Allowed result |
|---|---|---|
| `RESOLVED` | Yes | Exactly one curated candidate plus at least one evidence item |
| `CLARIFICATION_REQUIRED` | Yes | Explanation and one clarification question; no selection |
| `NO_MATCH` | No | Deterministic abstention |

If the provider selects a dataset during `CLARIFICATION_REQUIRED`, the selection and its
explanation are discarded. The service returns deterministic ambiguity language, a
clarification question derived from conflicting canonical metrics, and a
`SELECTION_BLOCKED_BY_AMBIGUITY` guardrail event.

## Configuration

The reasoning provider is optional. Configure it through environment variables:

| Variable | Required | Meaning |
|---|---|---|
| `AI_DATA_PLATFORM_LLM_API_KEY` or `OPENAI_API_KEY` | Yes | Bearer token |
| `AI_DATA_PLATFORM_LLM_MODEL` | Yes | Explicit model identifier |
| `AI_DATA_PLATFORM_LLM_BASE_URL` | No | Defaults to `https://api.openai.com/v1` |
| `AI_DATA_PLATFORM_LLM_TIMEOUT_SECONDS` | No | Defaults to `30` |

Both an API key and model must be present. Otherwise the existing context endpoints remain
available and the reasoning endpoint returns `503`.

## API

```bash
curl -X POST http://127.0.0.1:8000/v1/reasoning/dataset-selection \
  -H 'content-type: application/json' \
  -d '{
    "question": "Finance recognized net revenue",
    "mode": "hybrid",
    "limit": 5,
    "min_similarity": 0.25
  }'
```

`limit` is capped at five. This is a governance and context-bounding rule, not just a
default.

## Evaluation

`evaluation/reasoning_cases.yaml` is separate from retrieval evaluation because candidate
recall and reasoning correctness are different concerns. `evaluate_reasoning()` reports:

- discovery-status accuracy
- expected dataset selection accuracy
- safe-abstention rate
- grounded-response rate
- provider/policy error rate

Tests also exercise adversarial provider behavior: invented dataset IDs, invented evidence
IDs, ambiguity override attempts, malformed structured responses, and a valid but incorrect
source selection.

## Explicitly deferred

- analytical question answering
- SQL and transformation generation
- query execution
- MCP and agent exposure
- prompt/model version persistence and production telemetry
- authentication, authorization, rate limiting, retry policy, and secret management
- provider-specific model selection and cost/latency tuning
