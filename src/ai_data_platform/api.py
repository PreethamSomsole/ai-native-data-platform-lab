"""Stable Python capability layer; future adapters should call these functions."""

from __future__ import annotations

from ai_data_platform.discovery import DiscoveryResult, discover_datasets as _discover_datasets
from ai_data_platform.discovery import resolve_metric as _resolve_metric
from ai_data_platform.models import BusinessEntity, Dataset, Metric, Registry
from ai_data_platform.validation import validate_registry as _validate_registry


def get_metric(metric_id: str, registry: Registry) -> Metric:
    return registry.metrics[metric_id]


def get_entity(entity_id: str, registry: Registry) -> BusinessEntity:
    return registry.entities[entity_id]


def get_dataset(dataset_id: str, registry: Registry) -> Dataset:
    return registry.datasets[dataset_id]


def resolve_metric(query: str, registry: Registry):
    return _resolve_metric(query, registry)


def discover_datasets(question: str, registry: Registry, limit: int = 5) -> DiscoveryResult:
    return _discover_datasets(question, registry, limit)


def validate_registry(registry: Registry) -> None:
    _validate_registry(registry)
