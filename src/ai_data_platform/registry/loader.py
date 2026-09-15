"""Loading YAML metadata into a validated in-memory registry."""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path
from typing import Any, TypeVar

import yaml
from pydantic import BaseModel, ValidationError

from ai_data_platform.models import BusinessConcept, BusinessEntity, Dataset, Metric, Registry
from ai_data_platform.validation import RegistryValidationError, validate_registry

T = TypeVar("T", bound=BaseModel)

_REGISTRY_TYPES: tuple[tuple[str, type[BaseModel], str], ...] = (
    ("concepts", BusinessConcept, "concepts"),
    ("entities", BusinessEntity, "entities"),
    ("metrics", Metric, "metrics"),
    ("datasets", Dataset, "datasets"),
)


def _yaml_files(directory: Path) -> Iterable[Path]:
    return sorted((*directory.glob("*.yaml"), *directory.glob("*.yml")))


def _load_models(directory: Path, model_type: type[T], kind: str) -> dict[str, T]:
    records: dict[str, T] = {}
    errors: list[str] = []
    if not directory.is_dir():
        errors.append(f"Missing registry directory: {directory}")
    else:
        for file_path in _yaml_files(directory):
            try:
                raw: Any = yaml.safe_load(file_path.read_text(encoding="utf-8"))
                if not isinstance(raw, dict):
                    raise TypeError("expected a YAML mapping")
                record = model_type.model_validate(raw)
            except (OSError, TypeError, ValueError, ValidationError, yaml.YAMLError) as error:
                errors.append(f"{file_path}: invalid {kind} metadata: {error}")
                continue
            record_id = record.id
            if record_id in records:
                errors.append(f"Duplicate {kind} ID '{record_id}' in {file_path}")
            else:
                records[record_id] = record
    if errors:
        raise RegistryValidationError(errors)
    return records


def load_registry(registry_path: str | Path) -> Registry:
    """Load YAML records and fail when structural or referential rules are violated."""
    root = Path(registry_path)
    loaded: dict[str, dict[str, BaseModel]] = {}
    errors: list[str] = []
    for directory_name, model_type, field_name in _REGISTRY_TYPES:
        try:
            loaded[field_name] = _load_models(root / directory_name, model_type, directory_name)
        except RegistryValidationError as error:
            errors.extend(error.messages)
    if errors:
        raise RegistryValidationError(errors)

    registry = Registry(
        concepts=loaded["concepts"],  # type: ignore[arg-type]
        entities=loaded["entities"],  # type: ignore[arg-type]
        metrics=loaded["metrics"],  # type: ignore[arg-type]
        datasets=loaded["datasets"],  # type: ignore[arg-type]
    )
    validate_registry(registry)
    return registry
