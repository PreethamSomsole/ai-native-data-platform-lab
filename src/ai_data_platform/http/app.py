"""FastAPI adapter around the reusable context capability layer."""

from __future__ import annotations

import os
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query, status
from pydantic import BaseModel, ConfigDict, Field, field_validator

from ai_data_platform.api import build_vector_index
from ai_data_platform.context import (
    ContextService,
    DatasetContext,
    DiscoveryContext,
    DiscoveryMode,
)
from ai_data_platform.embeddings import HashingEmbeddingProvider
from ai_data_platform.reasoning import (
    DatasetSelectionResult,
    OpenAIResponsesReasoningProvider,
    ReasoningPolicyError,
    ReasoningProviderError,
    ReasoningService,
)
from ai_data_platform.registry.loader import load_registry
from ai_data_platform.runtime import (
    DatasetRuntimeMetadata,
    DuckDBRuntimeHistoryStore,
    RuntimeMetadataRepository,
    SQLiteRuntimeMetadataStore,
)

DEFAULT_OPENAI_BASE_URL = "https://api.openai.com/v1"


class HttpModel(BaseModel):
    model_config = ConfigDict(extra="forbid")

    @field_validator("question", check_fields=False)
    @classmethod
    def question_must_not_be_blank(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("question must not be blank")
        return value


class DiscoveryRequest(HttpModel):
    question: str = Field(min_length=1)
    mode: DiscoveryMode = DiscoveryMode.DETERMINISTIC
    limit: int = Field(default=5, ge=1, le=100)
    min_similarity: float = Field(default=0.25, ge=-1.0, le=1.0)


class ReasoningRequest(HttpModel):
    question: str = Field(min_length=1)
    mode: DiscoveryMode = DiscoveryMode.HYBRID
    limit: int = Field(default=5, ge=1, le=5)
    min_similarity: float = Field(default=0.25, ge=-1.0, le=1.0)


class HealthResponse(HttpModel):
    status: str


def create_app(
    context_service: ContextService,
    reasoning_service: ReasoningService | None = None,
) -> FastAPI:
    app = FastAPI(
        title="AI-Native Data Platform Context Service",
        version="0.4.0",
    )

    @app.get("/health", response_model=HealthResponse)
    def health() -> HealthResponse:
        return HealthResponse(status="ok")

    @app.post("/v1/discovery", response_model=DiscoveryContext)
    def discover(request: DiscoveryRequest) -> DiscoveryContext:
        try:
            return context_service.discover(
                request.question,
                mode=request.mode,
                limit=request.limit,
                min_similarity=request.min_similarity,
            )
        except RuntimeError as error:
            raise HTTPException(status_code=503, detail=str(error)) from error

    @app.post(
        "/v1/reasoning/dataset-selection",
        response_model=DatasetSelectionResult,
    )
    def select_dataset(request: ReasoningRequest) -> DatasetSelectionResult:
        if reasoning_service is None:
            raise HTTPException(status_code=503, detail="reasoning provider is not configured")
        try:
            return reasoning_service.select_dataset(
                request.question,
                mode=request.mode,
                limit=request.limit,
                min_similarity=request.min_similarity,
            )
        except RuntimeError as error:
            if isinstance(error, (ReasoningPolicyError, ReasoningProviderError)):
                raise HTTPException(status_code=502, detail=str(error)) from error
            raise HTTPException(status_code=503, detail=str(error)) from error

    @app.get("/v1/datasets/{dataset_id}/context", response_model=DatasetContext)
    def dataset_context(
        dataset_id: str,
        trend_limit: int = Query(default=30, ge=1, le=1_000),
    ) -> DatasetContext:
        try:
            return context_service.get_dataset_context(dataset_id, trend_limit=trend_limit)
        except KeyError as error:
            raise HTTPException(status_code=404, detail="dataset not found") from error

    @app.post(
        "/v1/runtime/observations",
        response_model=DatasetRuntimeMetadata,
        status_code=status.HTTP_201_CREATED,
    )
    def record_runtime(metadata: DatasetRuntimeMetadata) -> DatasetRuntimeMetadata:
        try:
            return context_service.record_runtime(metadata)
        except KeyError as error:
            raise HTTPException(status_code=404, detail="dataset not found") from error

    @app.get(
        "/v1/datasets/{dataset_id}/runtime/history",
        response_model=list[DatasetRuntimeMetadata],
    )
    def runtime_history(
        dataset_id: str,
        limit: int = Query(default=100, ge=1, le=1_000),
    ) -> list[DatasetRuntimeMetadata]:
        if dataset_id not in context_service.registry.datasets:
            raise HTTPException(status_code=404, detail="dataset not found")
        return context_service.runtime_repository.list_history(dataset_id, limit=limit)

    return app


def create_reasoning_service_from_env(
    context_service: ContextService,
) -> ReasoningService | None:
    """Configure the optional reference provider without coupling it to the core service."""
    configured_base_url = os.getenv("AI_DATA_PLATFORM_LLM_BASE_URL")
    base_url = (
        configured_base_url.strip()
        if configured_base_url and configured_base_url.strip()
        else DEFAULT_OPENAI_BASE_URL
    )
    platform_api_key = (os.getenv("AI_DATA_PLATFORM_LLM_API_KEY") or "").strip() or None
    openai_api_key = (os.getenv("OPENAI_API_KEY") or "").strip() or None
    is_default_openai_url = base_url.rstrip("/") == DEFAULT_OPENAI_BASE_URL.rstrip("/")
    api_key = platform_api_key or (openai_api_key if is_default_openai_url else None)
    model = (os.getenv("AI_DATA_PLATFORM_LLM_MODEL") or "").strip() or None
    if not api_key or not model:
        return None
    provider = OpenAIResponsesReasoningProvider(
        api_key=api_key,
        model=model,
        base_url=base_url,
        timeout_seconds=float(os.getenv("AI_DATA_PLATFORM_LLM_TIMEOUT_SECONDS", "30")),
    )
    return ReasoningService(context_service, provider)


def create_default_app() -> FastAPI:
    """Build a configurable local reference service for Uvicorn's factory mode."""
    project_root = Path(__file__).resolve().parents[3]
    registry_path = Path(
        os.getenv("AI_DATA_PLATFORM_REGISTRY", str(project_root / "registry"))
    )
    state_directory = Path(os.getenv("AI_DATA_PLATFORM_STATE_DIR", "var"))
    state_directory.mkdir(parents=True, exist_ok=True)
    sqlite_path = os.getenv(
        "AI_DATA_PLATFORM_SQLITE_PATH", str(state_directory / "runtime-metadata.sqlite")
    )
    duckdb_path = os.getenv(
        "AI_DATA_PLATFORM_DUCKDB_PATH", str(state_directory / "runtime-history.duckdb")
    )
    registry = load_registry(registry_path)
    repository = RuntimeMetadataRepository(
        SQLiteRuntimeMetadataStore(sqlite_path),
        DuckDBRuntimeHistoryStore(duckdb_path),
    )
    vector_index = build_vector_index(registry, HashingEmbeddingProvider())
    context_service = ContextService(registry, repository, vector_index)
    return create_app(
        context_service,
        create_reasoning_service_from_env(context_service),
    )
