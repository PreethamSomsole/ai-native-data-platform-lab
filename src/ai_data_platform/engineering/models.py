"""Typed contracts for governed AI-native engineering capabilities."""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, ConfigDict, Field

from ai_data_platform.models import Dataset


class EngineeringModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class Environment(str, Enum):
    DEVELOPMENT = "development"
    QA = "qa"
    PRODUCTION = "production"


class ApprovalRequirement(str, Enum):
    NOT_REQUIRED = "not_required"
    REQUIRED = "required"


class ToolStatus(str, Enum):
    PLANNED = "planned"
    APPROVAL_REQUIRED = "approval_required"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class SourceKind(str, Enum):
    DATABASE = "database"
    FILES = "files"
    STREAM = "stream"


class IngestionRequest(EngineeringModel):
    source_kind: SourceKind
    change_data_available: bool = False
    expected_latency_minutes: int = Field(ge=1, le=10080)
    historical_backfill: bool = False


class IngestionRecommendation(EngineeringModel):
    pattern: str
    reasons: list[str]
    approval_requirement: ApprovalRequirement = ApprovalRequirement.NOT_REQUIRED


class PipelinePlanRequest(EngineeringModel):
    pipeline_name: str = Field(min_length=1, max_length=100)
    environment: Environment = Environment.DEVELOPMENT
    source_kind: SourceKind
    dataset_id: str | None = None
    changes_dataset_contract: bool = False
    changes_architecture: bool = False


class PipelinePlan(EngineeringModel):
    pipeline_name: str
    environment: Environment
    dataset_id: str | None = None
    steps: list[str]
    approval_requirement: ApprovalRequirement
    approval_reasons: list[str] = Field(default_factory=list)


class ContractValidationResult(EngineeringModel):
    valid: bool
    dataset_id: str
    errors: list[str] = Field(default_factory=list)
    approval_requirement: ApprovalRequirement = ApprovalRequirement.REQUIRED


class DevValidationResult(EngineeringModel):
    profile: str
    status: ToolStatus
    command: list[str]
    exit_code: int
    output: str


class ReconciliationResult(EngineeringModel):
    status: ToolStatus
    source_table: str
    target_table: str
    source_row_count: int
    target_row_count: int
    source_only_row_count: int
    target_only_row_count: int
    matched: bool


class SafeReplaceRequest(EngineeringModel):
    target_table: str = Field(min_length=1, max_length=128)
    replacement_table: str = Field(min_length=1, max_length=128)
    environment: Environment = Environment.DEVELOPMENT
    approval_granted: bool = False


class SafeReplaceResult(EngineeringModel):
    status: ToolStatus
    target_table: str
    replacement_table: str
    backup_table: str | None = None
    rows_replaced: int | None = None
    approval_requirement: ApprovalRequirement
    message: str


class RestoreTableRequest(EngineeringModel):
    target_table: str = Field(min_length=1, max_length=128)
    backup_table: str = Field(min_length=1, max_length=160)
    environment: Environment = Environment.DEVELOPMENT
    approval_granted: bool = False


class DeploymentRequest(EngineeringModel):
    artifact_path: str = Field(min_length=1, max_length=500)
    validation_profile: str = Field(default="unit", min_length=1, max_length=30)
    environment: Environment = Environment.DEVELOPMENT
    approval_granted: bool = False


class DeploymentResult(EngineeringModel):
    status: ToolStatus
    environment: Environment
    artifact_path: str
    artifact_sha256: str | None = None
    manifest_path: str | None = None
    validation: DevValidationResult | None = None
    approval_requirement: ApprovalRequirement
    message: str


__all__ = [
    "ApprovalRequirement",
    "ContractValidationResult",
    "Dataset",
    "DeploymentRequest",
    "DeploymentResult",
    "DevValidationResult",
    "Environment",
    "IngestionRecommendation",
    "IngestionRequest",
    "PipelinePlan",
    "PipelinePlanRequest",
    "ReconciliationResult",
    "RestoreTableRequest",
    "SafeReplaceRequest",
    "SafeReplaceResult",
    "SourceKind",
    "ToolStatus",
]
