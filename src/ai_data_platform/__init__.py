"""Vendor-neutral semantic registry and deterministic dataset discovery."""

from ai_data_platform.api import (
    assess_ambiguity,
    discover_datasets,
    get_concept,
    get_dataset,
    get_entity,
    get_metric,
    resolve_metric,
    validate_registry,
)
from ai_data_platform.registry.loader import load_registry

__all__ = [
    "assess_ambiguity",
    "discover_datasets",
    "get_concept",
    "get_dataset",
    "get_entity",
    "get_metric",
    "load_registry",
    "resolve_metric",
    "validate_registry",
]
