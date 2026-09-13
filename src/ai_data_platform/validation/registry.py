"""Deterministic structural relationships that an LLM must not decide."""

from __future__ import annotations

from ai_data_platform.models import Registry


class RegistryValidationError(ValueError):
    def __init__(self, messages: list[str]) -> None:
        self.messages = messages
        super().__init__("Registry validation failed:\n- " + "\n- ".join(messages))


def _missing_ids(actual_ids: list[str], known_ids: set[str]) -> list[str]:
    return [item_id for item_id in actual_ids if item_id not in known_ids]


def validate_registry(registry: Registry) -> None:
    """Validate cross-file semantic references in a loaded registry."""
    errors: list[str] = []
    concept_ids = set(registry.concepts)
    entity_ids = set(registry.entities)
    metric_ids = set(registry.metrics)

    for entity in registry.entities.values():
        for missing_id in _missing_ids(entity.related_concept_ids, concept_ids):
            errors.append(f"{entity.id} references unknown concept '{missing_id}'")

    for metric in registry.metrics.values():
        for missing_id in _missing_ids(metric.related_concept_ids, concept_ids):
            errors.append(f"{metric.id} references unknown concept '{missing_id}'")
        for missing_id in _missing_ids(metric.not_equivalent_to_ids, metric_ids):
            errors.append(f"{metric.id} references unknown metric '{missing_id}'")
        if metric.id in metric.not_equivalent_to_ids:
            errors.append(f"{metric.id} cannot be marked non-equivalent to itself")

    for dataset in registry.datasets.values():
        for missing_id in _missing_ids(dataset.metric_ids, metric_ids):
            errors.append(f"{dataset.id} references unknown metric '{missing_id}'")
        for missing_id in _missing_ids(dataset.entity_ids, entity_ids):
            errors.append(f"{dataset.id} references unknown entity '{missing_id}'")
        for missing_id in _missing_ids(dataset.dimension_concept_ids, concept_ids):
            errors.append(f"{dataset.id} references unknown concept '{missing_id}'")

    if errors:
        raise RegistryValidationError(errors)
