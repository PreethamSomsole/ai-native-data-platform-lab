# AI-Native Data Platform Lab

Milestone 1 is a vendor-neutral semantic registry and deterministic dataset-discovery
baseline. Metadata lives in Git/YAML; Python validates it and returns an explained,
small candidate set without LLMs or vector search.

## Run

```bash
python -m pip install -e .
python -m ai_data_platform validate
python -m ai_data_platform discover "What was net revenue last quarter?"
python -m unittest discover -s tests -v
```

The first discovery query intentionally returns `CLARIFICATION_REQUIRED`: Finance
recognized revenue and Operations completed-order revenue are distinct metrics.
