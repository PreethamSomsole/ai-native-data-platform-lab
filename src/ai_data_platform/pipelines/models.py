"""Typed contracts for declarative, governed pipeline specifications."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from enum import Enum
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

# Segments cannot contain "__" (or end in "_"), so the "." -> "__" physical-name mapping is
# injective and cannot collide with runner names such as raw__*, *__backup__*, *__staging_*.
_SEGMENT = r"[a-z][a-z0-9]*(?:_[a-z0-9]+)*"
_DATASET_ID = rf"^{_SEGMENT}\.{_SEGMENT}$"
_COLUMN = r"^[a-z_][a-z0-9_]*$"


class PipelineModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class SourceFormat(str, Enum):
    PARQUET = "parquet"
    CSV = "csv"


class SourceSpec(PipelineModel):
    """A raw input file set landed by the platform, never by transform SQL."""

    id: str = Field(pattern=_DATASET_ID, description="<system>.<table>, for example tpch.lineitem")
    description: str = Field(min_length=1)
    format: SourceFormat
    path: str = Field(
        min_length=1,
        max_length=300,
        description="Path or glob relative to the server-configured data directory.",
    )
    owner: str = Field(min_length=1)

    @field_validator("path")
    @classmethod
    def relative_path(cls, value: str) -> str:
        if value.startswith(("/", "\\")) or ".." in value.replace("\\", "/").split("/"):
            raise ValueError("source paths must stay inside the configured data directory")
        return value

    @property
    def system(self) -> str:
        return self.id.split(".", 1)[0]

    @property
    def table(self) -> str:
        return self.id.split(".", 1)[1]


class NotNullCheck(PipelineModel):
    check: Literal["not_null"]
    columns: list[Annotated[str, Field(pattern=_COLUMN)]] = Field(min_length=1)


class UniqueCheck(PipelineModel):
    check: Literal["unique"]
    columns: list[Annotated[str, Field(pattern=_COLUMN)]] = Field(min_length=1)


class AcceptedValuesCheck(PipelineModel):
    check: Literal["accepted_values"]
    column: str = Field(pattern=_COLUMN)
    values: list[str] = Field(min_length=1)


class RangeCheck(PipelineModel):
    check: Literal["range"]
    column: str = Field(pattern=_COLUMN)
    min: Decimal | None = None
    max: Decimal | None = None

    @model_validator(mode="after")
    def has_bound(self) -> RangeCheck:
        if self.min is None and self.max is None:
            raise ValueError("range checks need min, max, or both")
        return self


class RowCountCheck(PipelineModel):
    check: Literal["row_count"]
    min: int = Field(default=1, ge=0)
    max: int | None = Field(default=None, ge=0)


QualityCheck = Annotated[
    NotNullCheck | UniqueCheck | AcceptedValuesCheck | RangeCheck | RowCountCheck,
    Field(discriminator="check"),
]


class Reconciliation(PipelineModel):
    """Two scalar SELECTs that must agree; ``{{ this }}`` is the candidate output."""

    name: str = Field(min_length=1, max_length=120)
    expected_sql: str = Field(min_length=1, max_length=5_000)
    actual_sql: str = Field(min_length=1, max_length=5_000)
    tolerance: Decimal = Field(default=Decimal(0), ge=0)


class PipelineInputs(PipelineModel):
    sources: list[Annotated[str, Field(pattern=_DATASET_ID)]] = Field(default_factory=list)
    datasets: list[Annotated[str, Field(pattern=_DATASET_ID)]] = Field(default_factory=list)

    @model_validator(mode="after")
    def at_least_one(self) -> PipelineInputs:
        if not self.sources and not self.datasets:
            raise ValueError("a pipeline needs at least one source or dataset input")
        return self


class PipelineSpec(PipelineModel):
    """One SELECT that builds one registry dataset from declared inputs."""

    id: str = Field(pattern=r"^pipeline\.[a-z][a-z0-9_]*$")
    description: str = Field(min_length=1)
    owner: str = Field(min_length=1)
    output: str = Field(pattern=_DATASET_ID)
    inputs: PipelineInputs
    sql: str = Field(min_length=1, max_length=20_000)
    quality_checks: list[QualityCheck] = Field(default_factory=list)
    reconciliations: list[Reconciliation] = Field(default_factory=list)


class CheckOutcome(PipelineModel):
    name: str
    passed: bool
    detail: str


class RunStatus(str, Enum):
    SUCCEEDED = "succeeded"
    FAILED_QUALITY = "failed_quality"
    FAILED_CONTRACT = "failed_contract"
    FAILED_ERROR = "failed_error"
    SKIPPED = "skipped"


class PipelineRunResult(PipelineModel):
    run_id: str
    pipeline_id: str
    output: str
    status: RunStatus
    started_at: datetime
    finished_at: datetime
    duration_seconds: float
    row_count: int | None = None
    published_row_count: int | None = None
    backup_table: str | None = None
    checks: list[CheckOutcome] = Field(default_factory=list)
    message: str = ""

    @property
    def checks_passed(self) -> int:
        return sum(1 for check in self.checks if check.passed)

    @property
    def checks_failed(self) -> int:
        return sum(1 for check in self.checks if not check.passed)


class IngestionResult(PipelineModel):
    source_id: str
    raw_table: str
    row_count: int
    duration_seconds: float


class LineageEdge(PipelineModel):
    pipeline_id: str
    output: str
    input_kind: Literal["source", "dataset"]
    input_id: str
