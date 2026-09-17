"""Replaceable current-state and analytical-history stores for runtime metadata."""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence
from pathlib import Path
from threading import RLock
from typing import Protocol

import duckdb

from ai_data_platform.runtime.models import (
    DatasetRuntimeMetadata,
    RowCountPoint,
    RuntimeAnalyticsSummary,
)


class RuntimeMetadataStore(Protocol):
    def upsert_latest(self, metadata: DatasetRuntimeMetadata) -> None: ...

    def get_latest(self, dataset_id: str) -> DatasetRuntimeMetadata | None: ...

    def get_latest_many(
        self, dataset_ids: Sequence[str]
    ) -> dict[str, DatasetRuntimeMetadata]: ...


class RuntimeHistoryStore(Protocol):
    def append(self, metadata: DatasetRuntimeMetadata) -> None: ...

    def list_history(
        self, dataset_id: str, *, limit: int = 100
    ) -> list[DatasetRuntimeMetadata]: ...

    def row_count_trend(self, dataset_id: str, *, limit: int = 30) -> list[RowCountPoint]: ...

    def summarize(self, dataset_id: str) -> RuntimeAnalyticsSummary: ...


class SQLiteRuntimeMetadataStore:
    """SQLite-backed latest snapshot used on the request path."""

    def __init__(self, path: str | Path = ":memory:") -> None:
        self._connection = sqlite3.connect(str(path), check_same_thread=False)
        self._lock = RLock()
        with self._connection:
            self._connection.execute(
                """
                CREATE TABLE IF NOT EXISTS latest_dataset_runtime (
                    dataset_id TEXT PRIMARY KEY,
                    observed_at TEXT NOT NULL,
                    payload TEXT NOT NULL
                )
                """
            )

    def upsert_latest(self, metadata: DatasetRuntimeMetadata) -> None:
        observed_at = metadata.observed_at.isoformat()
        payload = metadata.model_dump_json()
        with self._lock, self._connection:
            self._connection.execute(
                """
                INSERT INTO latest_dataset_runtime(dataset_id, observed_at, payload)
                VALUES (?, ?, ?)
                ON CONFLICT(dataset_id) DO UPDATE SET
                    observed_at = excluded.observed_at,
                    payload = excluded.payload
                WHERE excluded.observed_at >= latest_dataset_runtime.observed_at
                """,
                (metadata.dataset_id, observed_at, payload),
            )

    def get_latest(self, dataset_id: str) -> DatasetRuntimeMetadata | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT payload FROM latest_dataset_runtime WHERE dataset_id = ?",
                (dataset_id,),
            ).fetchone()
        return None if row is None else DatasetRuntimeMetadata.model_validate_json(row[0])

    def get_latest_many(
        self, dataset_ids: Sequence[str]
    ) -> dict[str, DatasetRuntimeMetadata]:
        unique_ids = tuple(dict.fromkeys(dataset_ids))
        if not unique_ids:
            return {}
        placeholders = ", ".join("?" for _ in unique_ids)
        with self._lock:
            rows = self._connection.execute(
                f"SELECT payload FROM latest_dataset_runtime "
                f"WHERE dataset_id IN ({placeholders})",
                unique_ids,
            ).fetchall()
        items = [DatasetRuntimeMetadata.model_validate_json(row[0]) for row in rows]
        return {item.dataset_id: item for item in items}

    def close(self) -> None:
        with self._lock:
            self._connection.close()


class DuckDBRuntimeHistoryStore:
    """DuckDB-backed append history for trends and analytical inspection."""

    def __init__(self, path: str | Path = ":memory:") -> None:
        self._connection = duckdb.connect(str(path))
        self._lock = RLock()
        self._connection.execute(
            """
            CREATE TABLE IF NOT EXISTS dataset_runtime_history (
                dataset_id VARCHAR NOT NULL,
                observed_at TIMESTAMPTZ NOT NULL,
                row_count BIGINT,
                freshness_status VARCHAR NOT NULL,
                quality_status VARCHAR NOT NULL,
                operational_health VARCHAR NOT NULL,
                usage_count_30d BIGINT NOT NULL,
                payload VARCHAR NOT NULL
            )
            """
        )

    def append(self, metadata: DatasetRuntimeMetadata) -> None:
        with self._lock:
            self._connection.execute(
                """
                INSERT INTO dataset_runtime_history VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    metadata.dataset_id,
                    metadata.observed_at,
                    metadata.row_count,
                    metadata.freshness_status.value,
                    metadata.quality_status.value,
                    metadata.operational_health.value,
                    metadata.usage_count_30d,
                    metadata.model_dump_json(),
                ],
            )

    @staticmethod
    def _validate_limit(limit: int) -> None:
        if limit < 1:
            raise ValueError("limit must be at least 1")

    def list_history(
        self, dataset_id: str, *, limit: int = 100
    ) -> list[DatasetRuntimeMetadata]:
        self._validate_limit(limit)
        with self._lock:
            rows = self._connection.execute(
                """
                SELECT payload
                FROM dataset_runtime_history
                WHERE dataset_id = ?
                ORDER BY observed_at DESC
                LIMIT ?
                """,
                [dataset_id, limit],
            ).fetchall()
        return [DatasetRuntimeMetadata.model_validate_json(row[0]) for row in rows]

    def row_count_trend(self, dataset_id: str, *, limit: int = 30) -> list[RowCountPoint]:
        self._validate_limit(limit)
        with self._lock:
            rows = self._connection.execute(
                """
                SELECT observed_at, row_count
                FROM dataset_runtime_history
                WHERE dataset_id = ? AND row_count IS NOT NULL
                ORDER BY observed_at DESC
                LIMIT ?
                """,
                [dataset_id, limit],
            ).fetchall()
        return [
            RowCountPoint(observed_at=observed_at, row_count=row_count)
            for observed_at, row_count in reversed(rows)
        ]

    def summarize(self, dataset_id: str) -> RuntimeAnalyticsSummary:
        with self._lock:
            row = self._connection.execute(
                """
                SELECT
                    COUNT(*),
                    SUM(CASE WHEN freshness_status = 'fresh' THEN 1 ELSE 0 END),
                    SUM(CASE WHEN freshness_status = 'stale' THEN 1 ELSE 0 END),
                    SUM(CASE WHEN quality_status = 'passing' THEN 1 ELSE 0 END),
                    SUM(CASE WHEN quality_status = 'failing' THEN 1 ELSE 0 END),
                    SUM(CASE WHEN operational_health = 'healthy' THEN 1 ELSE 0 END),
                    SUM(CASE WHEN operational_health = 'failed' THEN 1 ELSE 0 END),
                    MIN(row_count),
                    MAX(row_count),
                    AVG(row_count)
                FROM dataset_runtime_history
                WHERE dataset_id = ?
                """,
                [dataset_id],
            ).fetchone()
        return RuntimeAnalyticsSummary(
            dataset_id=dataset_id,
            observation_count=row[0],
            fresh_observation_count=row[1] or 0,
            stale_observation_count=row[2] or 0,
            passing_quality_count=row[3] or 0,
            failing_quality_count=row[4] or 0,
            healthy_observation_count=row[5] or 0,
            failed_observation_count=row[6] or 0,
            minimum_row_count=row[7],
            maximum_row_count=row[8],
            average_row_count=row[9],
        )

    def close(self) -> None:
        with self._lock:
            self._connection.close()


class RuntimeMetadataRepository:
    """Coordinate the latest operational snapshot and append-only analytical history."""

    def __init__(
        self,
        current: RuntimeMetadataStore,
        history: RuntimeHistoryStore,
    ) -> None:
        self.current = current
        self.history = history

    def record(self, metadata: DatasetRuntimeMetadata) -> None:
        self.history.append(metadata)
        self.current.upsert_latest(metadata)

    def get_latest(self, dataset_id: str) -> DatasetRuntimeMetadata | None:
        return self.current.get_latest(dataset_id)

    def get_latest_many(
        self, dataset_ids: Sequence[str]
    ) -> dict[str, DatasetRuntimeMetadata]:
        return self.current.get_latest_many(dataset_ids)

    def list_history(
        self, dataset_id: str, *, limit: int = 100
    ) -> list[DatasetRuntimeMetadata]:
        return self.history.list_history(dataset_id, limit=limit)

    def row_count_trend(self, dataset_id: str, *, limit: int = 30) -> list[RowCountPoint]:
        return self.history.row_count_trend(dataset_id, limit=limit)

    def summarize(self, dataset_id: str) -> RuntimeAnalyticsSummary:
        return self.history.summarize(dataset_id)
