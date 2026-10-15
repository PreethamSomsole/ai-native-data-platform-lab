"""Typed contracts for Git/YAML semantic metadata."""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, ConfigDict, Field, field_validator


class MetadataModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class Certification(str, Enum):
    CERTIFIED = "certified"
    UNCERTIFIED = "uncertified"


class BusinessConcept(MetadataModel):
    id: str
    name: str
    description: str
    synonyms: list[str] = Field(default_factory=list)


class BusinessEntity(MetadataModel):
    id: str
    name: str
    description: str
    related_concept_ids: list[str] = Field(default_factory=list)


class Metric(MetadataModel):
    id: str
    name: str
    domain: str
    owner: str
    definition: str
    aliases: list[str] = Field(default_factory=list)
    target_users: list[str] = Field(default_factory=list)
    intended_use_cases: list[str] = Field(default_factory=list)
    prohibited_use_cases: list[str] = Field(default_factory=list)
    related_concept_ids: list[str] = Field(default_factory=list)
    not_equivalent_to_ids: list[str] = Field(default_factory=list)


class DatasetColumn(MetadataModel):
    """Physical column promised by a dataset contract."""

    name: str = Field(pattern=r"^[a-z_][a-z0-9_]*$", max_length=63)
    type: str = Field(min_length=1, max_length=40)
    nullable: bool = True
    description: str = ""

    @field_validator("type")
    @classmethod
    def normalize_type(cls, value: str) -> str:
        return " ".join(value.upper().split()).replace(", ", ",")


class Dataset(MetadataModel):
    id: str
    name: str
    domain: str
    layer: str
    description: str
    grain: str
    target_users: list[str] = Field(default_factory=list)
    supported_use_cases: list[str] = Field(default_factory=list)
    prohibited_use_cases: list[str] = Field(default_factory=list)
    certification: Certification
    owner: str
    metric_ids: list[str] = Field(default_factory=list)
    entity_ids: list[str] = Field(default_factory=list)
    dimension_concept_ids: list[str] = Field(default_factory=list)
    freshness_sla: str
    refresh_cadence: str
    trust_signals: list[str] = Field(default_factory=list)
    columns: list[DatasetColumn] = Field(default_factory=list)

    @field_validator("columns")
    @classmethod
    def unique_column_names(cls, columns: list[DatasetColumn]) -> list[DatasetColumn]:
        names = [column.name for column in columns]
        duplicates = sorted({name for name in names if names.count(name) > 1})
        if duplicates:
            raise ValueError(f"duplicate column names: {', '.join(duplicates)}")
        return columns


class Registry(MetadataModel):
    concepts: dict[str, BusinessConcept] = Field(default_factory=dict)
    entities: dict[str, BusinessEntity] = Field(default_factory=dict)
    metrics: dict[str, Metric] = Field(default_factory=dict)
    datasets: dict[str, Dataset] = Field(default_factory=dict)
