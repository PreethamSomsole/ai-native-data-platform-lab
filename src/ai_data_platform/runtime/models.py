"""Typed observations kept outside the declarative Git/YAML registry."""

from __future__ import annotations

from datetime import UTC, datetime
from enum import Enum

from pydantic import BaseModel, ConfigDict, Field, field_validator


class RuntimeModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class FreshnessStatus(str, Enum):
    FRESH = "fresh"
    STALE = "stale"
    UNKNOWN = "unknown"


class QualityStatus(str, Enum):
    PASSING = "passing"
    WARNING = "warning"
    FAILING = "failing"
    UNKNOWN = "unknown"


class OperationalHealth(str, Enum):
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    FAILED = "failed"
    UNKNOWN = "unknown"


class DatasetRuntimeMetadata(RuntimeModel):
    """Latest observed operational state for one registered dataset."""

    dataset_id: str = Field(min_length=1)
    observed_at: datetime
    source: str = Field(min_length=1)
    last_successful_run_at: datetime | None = None
    freshness_status: FreshnessStatus = FreshnessStatus.UNKNOWN
    quality_status: QualityStatus = QualityStatus.UNKNOWN
    quality_checks_passed: int = Field(default=0, ge=0)
    quality_checks_failed: int = Field(default=0, ge=0)
    row_count: int | None = Field(default=None, ge=0)
    usage_count_30d: int = Field(default=0, ge=0)
    operational_health: OperationalHealth = OperationalHealth.UNKNOWN

    @field_validator("observed_at", "last_successful_run_at")
    @classmethod
    def normalize_timestamp(cls, value: datetime | None) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("runtime timestamps must include a timezone")
        return value.astimezone(UTC)


class RowCountPoint(RuntimeModel):
    observed_at: datetime
    row_count: int = Field(ge=0)


class RuntimeAnalyticsSummary(RuntimeModel):
    dataset_id: str
    observation_count: int = Field(ge=0)
    fresh_observation_count: int = Field(ge=0)
    stale_observation_count: int = Field(ge=0)
    passing_quality_count: int = Field(ge=0)
    failing_quality_count: int = Field(ge=0)
    healthy_observation_count: int = Field(ge=0)
    failed_observation_count: int = Field(ge=0)
    minimum_row_count: int | None = Field(default=None, ge=0)
    maximum_row_count: int | None = Field(default=None, ge=0)
    average_row_count: float | None = Field(default=None, ge=0)
