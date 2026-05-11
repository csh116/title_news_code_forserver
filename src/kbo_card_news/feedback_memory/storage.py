from __future__ import annotations

import logging
import os
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence

from kbo_card_news.feedback_memory.models import (
    FeedbackMemoryInitializationResult,
    FeedbackMemorySelectResult,
    FeedbackMemoryWriteResult,
)
from kbo_card_news.feedback_memory.serialization import deserialize_record, serialize_record

DEFAULT_DB_FILENAME = "feedback_memory.db"
ENV_DB_PATH_KEY = "KBO_FEEDBACK_MEMORY_DB_PATH"
ROOT_DIR = Path(__file__).resolve().parents[3]
SCHEMA_VERSION = 4
LOGGER = logging.getLogger(__name__)

_INITIAL_SCHEMA_STATEMENTS = (
    """
    CREATE TABLE IF NOT EXISTS feedback_memory_meta (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL
    )
    """.strip(),
    """
    CREATE TABLE IF NOT EXISTS headline_edit_memory (
        id TEXT PRIMARY KEY,
        created_at TEXT NOT NULL,
        topic_fingerprint TEXT,
        topic_type TEXT,
        entity_focus TEXT,
        event_type TEXT,
        angle_type TEXT,
        article_count INTEGER,
        asset_count INTEGER,
        has_notable_numbers INTEGER,
        recommended_focus TEXT,
        before_title_text TEXT,
        after_title_text TEXT,
        before_subheadline TEXT,
        after_subheadline TEXT,
        topic_id TEXT,
        topic_name TEXT,
        team_name TEXT,
        source_run_dir TEXT,
        source_spec_path TEXT,
        memory_context_used INTEGER,
        referenced_memory_ids TEXT
    )
    """.strip(),
    """
    CREATE INDEX IF NOT EXISTS idx_headline_edit_memory_created_at
    ON headline_edit_memory(created_at)
    """.strip(),
    """
    CREATE INDEX IF NOT EXISTS idx_headline_edit_memory_topic_fingerprint
    ON headline_edit_memory(topic_fingerprint)
    """.strip(),
    """
    CREATE INDEX IF NOT EXISTS idx_headline_edit_memory_topic_features
    ON headline_edit_memory(topic_type, entity_focus, event_type)
    """.strip(),
    """
    CREATE TABLE IF NOT EXISTS multimodal_edit_memory (
        id TEXT PRIMARY KEY,
        created_at TEXT NOT NULL,
        topic_fingerprint TEXT,
        asset_fingerprint TEXT,
        topic_type TEXT,
        entity_focus TEXT,
        event_type TEXT,
        angle_type TEXT,
        article_count INTEGER,
        asset_count INTEGER,
        has_notable_numbers INTEGER,
        recommended_focus TEXT,
        shot_type TEXT,
        subject_role TEXT,
        person_count_bucket TEXT,
        is_action_shot INTEGER,
        is_post_game INTEGER,
        width INTEGER,
        height INTEGER,
        aspect_ratio REAL,
        caption_signal TEXT,
        asset_reference TEXT,
        before_usage_recommendation TEXT,
        after_usage_recommendation TEXT,
        before_scene_description TEXT,
        after_scene_description TEXT,
        before_humor_point TEXT,
        after_humor_point TEXT,
        before_tag_summary TEXT,
        after_tag_summary TEXT,
        before_subject_tags TEXT,
        after_subject_tags TEXT,
        before_event_tags TEXT,
        after_event_tags TEXT,
        before_emotion_tags TEXT,
        after_emotion_tags TEXT,
        before_composition_tags TEXT,
        after_composition_tags TEXT,
        before_risk_tags TEXT,
        after_risk_tags TEXT,
        before_caution_note TEXT,
        after_caution_note TEXT,
        source_run_dir TEXT,
        source_report_path TEXT,
        memory_context_used INTEGER,
        referenced_memory_ids TEXT
    )
    """.strip(),
    """
    CREATE INDEX IF NOT EXISTS idx_multimodal_edit_memory_created_at
    ON multimodal_edit_memory(created_at)
    """.strip(),
    """
    CREATE INDEX IF NOT EXISTS idx_multimodal_edit_memory_asset_fingerprint
    ON multimodal_edit_memory(asset_fingerprint)
    """.strip(),
    """
    CREATE INDEX IF NOT EXISTS idx_multimodal_edit_memory_asset_features
    ON multimodal_edit_memory(shot_type, subject_role, is_action_shot)
    """.strip(),
    """
    CREATE INDEX IF NOT EXISTS idx_multimodal_edit_memory_topic_features
    ON multimodal_edit_memory(topic_type, entity_focus, event_type)
    """.strip(),
    """
    CREATE TABLE IF NOT EXISTS headline_correction_policies (
        id TEXT PRIMARY KEY,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        policy_key TEXT NOT NULL UNIQUE,
        scope_type TEXT NOT NULL,
        topic_type TEXT,
        entity_focus TEXT,
        event_type TEXT,
        angle_type TEXT,
        team_name TEXT,
        topic_fingerprint TEXT,
        rule_type TEXT NOT NULL,
        rule_payload TEXT NOT NULL,
        evidence_count INTEGER NOT NULL DEFAULT 0,
        success_count INTEGER NOT NULL DEFAULT 0,
        last_applied_at TEXT,
        priority INTEGER NOT NULL DEFAULT 0,
        active INTEGER NOT NULL DEFAULT 0
    )
    """.strip(),
    """
    CREATE INDEX IF NOT EXISTS idx_headline_correction_policies_active_priority
    ON headline_correction_policies(active, priority, evidence_count)
    """.strip(),
    """
    CREATE INDEX IF NOT EXISTS idx_headline_correction_policies_scope
    ON headline_correction_policies(scope_type, topic_type, entity_focus, event_type, angle_type, team_name)
    """.strip(),
    """
    CREATE TABLE IF NOT EXISTS headline_correction_policy_evidence (
        policy_id TEXT NOT NULL,
        headline_memory_id TEXT NOT NULL,
        evidence_role TEXT NOT NULL,
        PRIMARY KEY(policy_id, headline_memory_id)
    )
    """.strip(),
    """
    CREATE INDEX IF NOT EXISTS idx_headline_correction_policy_evidence_memory_id
    ON headline_correction_policy_evidence(headline_memory_id)
    """.strip(),
    """
    CREATE TABLE IF NOT EXISTS multimodal_correction_policies (
        id TEXT PRIMARY KEY,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        policy_key TEXT NOT NULL UNIQUE,
        scope_type TEXT NOT NULL,
        topic_type TEXT,
        entity_focus TEXT,
        event_type TEXT,
        angle_type TEXT,
        asset_fingerprint TEXT,
        topic_fingerprint TEXT,
        shot_type TEXT,
        subject_role TEXT,
        rule_type TEXT NOT NULL,
        rule_payload TEXT NOT NULL,
        evidence_count INTEGER NOT NULL DEFAULT 0,
        success_count INTEGER NOT NULL DEFAULT 0,
        last_applied_at TEXT,
        priority INTEGER NOT NULL DEFAULT 0,
        active INTEGER NOT NULL DEFAULT 0
    )
    """.strip(),
    """
    CREATE INDEX IF NOT EXISTS idx_multimodal_correction_policies_active_priority
    ON multimodal_correction_policies(active, priority, evidence_count)
    """.strip(),
    """
    CREATE INDEX IF NOT EXISTS idx_multimodal_correction_policies_scope
    ON multimodal_correction_policies(scope_type, topic_type, entity_focus, event_type, angle_type, shot_type, subject_role)
    """.strip(),
    """
    CREATE TABLE IF NOT EXISTS multimodal_correction_policy_evidence (
        policy_id TEXT NOT NULL,
        multimodal_memory_id TEXT NOT NULL,
        evidence_role TEXT NOT NULL,
        changed_fields TEXT NOT NULL,
        source_run_dir TEXT,
        before_snapshot TEXT,
        after_snapshot TEXT,
        PRIMARY KEY(policy_id, multimodal_memory_id)
    )
    """.strip(),
    """
    CREATE INDEX IF NOT EXISTS idx_multimodal_correction_policy_evidence_memory_id
    ON multimodal_correction_policy_evidence(multimodal_memory_id)
    """.strip(),
)


def resolve_feedback_memory_db_path(db_path: str | Path | None = None) -> Path:
    if db_path is not None:
        return Path(db_path).expanduser().resolve()

    env_value = os.getenv(ENV_DB_PATH_KEY, "").strip()
    if env_value:
        return Path(env_value).expanduser().resolve()

    return (ROOT_DIR / DEFAULT_DB_FILENAME).resolve()


def create_feedback_memory_connection(db_path: str | Path | None = None) -> sqlite3.Connection:
    resolved_path = resolve_feedback_memory_db_path(db_path)
    connection = sqlite3.connect(resolved_path)
    connection.row_factory = sqlite3.Row
    return connection


def _build_initialization_failure_result(
    *,
    db_path: str | Path | None,
    error_message: str,
) -> FeedbackMemoryInitializationResult:
    return FeedbackMemoryInitializationResult(
        db_path=resolve_feedback_memory_db_path(db_path),
        initialized=False,
        schema_version=SCHEMA_VERSION,
        error_message=error_message,
    )


def _normalize_rowcount(rowcount: int) -> int:
    return rowcount if rowcount >= 0 else 0


def _row_to_dict(row: sqlite3.Row | Any) -> dict[str, Any]:
    if isinstance(row, sqlite3.Row):
        return {key: row[key] for key in row.keys()}
    if isinstance(row, dict):
        return dict(row)
    raise TypeError(f"unsupported row type: {type(row).__name__}")


def initialize_feedback_memory_db(
    db_path: str | Path | None = None,
    *,
    connection: sqlite3.Connection | None = None,
    enable_wal: bool = True,
    suppress_errors: bool = False,
) -> "FeedbackMemoryInitializationResult":
    resolved_path = resolve_feedback_memory_db_path(db_path)
    owns_connection = connection is None
    active_connection = connection or create_feedback_memory_connection(resolved_path)
    try:
        if enable_wal:
            active_connection.execute("PRAGMA journal_mode=WAL")
        for statement in _INITIAL_SCHEMA_STATEMENTS:
            active_connection.execute(statement)
        active_connection.execute(
            """
            INSERT INTO feedback_memory_meta(key, value)
            VALUES (?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value
            """,
            ("schema_version", str(SCHEMA_VERSION)),
        )
        active_connection.commit()
        return FeedbackMemoryInitializationResult(
            db_path=resolved_path,
            initialized=True,
            schema_version=SCHEMA_VERSION,
        )
    except Exception as exc:
        if owns_connection:
            active_connection.close()
        if suppress_errors:
            LOGGER.warning(
                "feedback memory initialization failed for %s: %s",
                resolved_path,
                exc,
            )
            return FeedbackMemoryInitializationResult(
                db_path=resolved_path,
                initialized=False,
                schema_version=SCHEMA_VERSION,
                error_message=str(exc),
            )
        raise
    finally:
        if owns_connection and connection is None:
            try:
                active_connection.close()
            except Exception:
                pass


@dataclass(slots=True)
class FeedbackMemoryRepository:
    db_path: Path | None = None
    _connection: sqlite3.Connection | None = None

    def connect(self, *, initialize: bool = True, enable_wal: bool = True) -> sqlite3.Connection:
        if self._connection is None:
            self._connection = create_feedback_memory_connection(self.db_path)
        if initialize:
            self.initialize(enable_wal=enable_wal)
        return self._connection

    @property
    def resolved_db_path(self) -> Path:
        return resolve_feedback_memory_db_path(self.db_path)

    def initialize(self, *, enable_wal: bool = True) -> "FeedbackMemoryInitializationResult":
        connection = self.connect(initialize=False)
        return initialize_feedback_memory_db(
            self.db_path,
            connection=connection,
            enable_wal=enable_wal,
        )

    def safe_initialize(self, *, enable_wal: bool = True) -> "FeedbackMemoryInitializationResult":
        try:
            connection = self.connect(initialize=False)
        except Exception as exc:
            LOGGER.warning(
                "feedback memory connection failed for %s: %s",
                self.resolved_db_path,
                exc,
            )
            return _build_initialization_failure_result(
                db_path=self.db_path,
                error_message=str(exc),
            )

        return initialize_feedback_memory_db(
            self.db_path,
            connection=connection,
            enable_wal=enable_wal,
            suppress_errors=True,
        )

    def execute(
        self,
        query: str,
        params: Sequence[Any] | None = None,
        *,
        commit: bool = True,
    ) -> sqlite3.Cursor:
        connection = self.connect()
        cursor = connection.execute(query, tuple(params or ()))
        if commit:
            connection.commit()
        return cursor

    def safe_execute(
        self,
        query: str,
        params: Sequence[Any] | None = None,
        *,
        commit: bool = True,
        operation_name: str = "feedback_memory_execute",
    ) -> "FeedbackMemoryWriteResult":
        try:
            cursor = self.execute(query, params, commit=commit)
            return FeedbackMemoryWriteResult(
                ok=True,
                rowcount=_normalize_rowcount(cursor.rowcount),
            )
        except Exception as exc:
            LOGGER.warning("%s failed: %s", operation_name, exc)
            return FeedbackMemoryWriteResult(
                ok=False,
                rowcount=0,
                error_message=str(exc),
            )

    def insert_record(
        self,
        table_name: str,
        record: dict[str, Any],
        *,
        json_fields: set[str] | None = None,
        bool_fields: set[str] | None = None,
        plain_text_fields: set[str] | None = None,
    ) -> sqlite3.Cursor:
        serialized = serialize_record(
            record,
            json_fields=json_fields,
            bool_fields=bool_fields,
            plain_text_fields=plain_text_fields,
        )
        columns = list(serialized.keys())
        placeholders = ", ".join("?" for _ in columns)
        column_sql = ", ".join(columns)
        values = [serialized[column] for column in columns]
        return self.execute(
            f"INSERT INTO {table_name} ({column_sql}) VALUES ({placeholders})",
            values,
        )

    def safe_insert_record(
        self,
        table_name: str,
        record: dict[str, Any],
        *,
        json_fields: set[str] | None = None,
        bool_fields: set[str] | None = None,
        plain_text_fields: set[str] | None = None,
        operation_name: str = "feedback_memory_insert_record",
    ) -> "FeedbackMemoryWriteResult":
        try:
            cursor = self.insert_record(
                table_name,
                record,
                json_fields=json_fields,
                bool_fields=bool_fields,
                plain_text_fields=plain_text_fields,
            )
            return FeedbackMemoryWriteResult(
                ok=True,
                rowcount=_normalize_rowcount(cursor.rowcount),
            )
        except Exception as exc:
            LOGGER.warning("%s failed: %s", operation_name, exc)
            return FeedbackMemoryWriteResult(
                ok=False,
                rowcount=0,
                error_message=str(exc),
            )

    def select(
        self,
        query: str,
        params: Sequence[Any] | None = None,
    ) -> list[dict[str, Any]]:
        connection = self.connect()
        cursor = connection.execute(query, tuple(params or ()))
        return [_row_to_dict(row) for row in cursor.fetchall()]

    def safe_select(
        self,
        query: str,
        params: Sequence[Any] | None = None,
        *,
        operation_name: str = "feedback_memory_select",
    ) -> "FeedbackMemorySelectResult":
        try:
            return FeedbackMemorySelectResult(
                ok=True,
                rows=self.select(query, params),
            )
        except Exception as exc:
            LOGGER.warning("%s failed: %s", operation_name, exc)
            return FeedbackMemorySelectResult(
                ok=False,
                rows=[],
                error_message=str(exc),
            )

    def select_records(
        self,
        query: str,
        params: Sequence[Any] | None = None,
        *,
        json_fields: set[str] | None = None,
        bool_fields: set[str] | None = None,
    ) -> list[dict[str, Any]]:
        rows = self.select(query, params)
        return [
            deserialize_record(
                row,
                json_fields=json_fields,
                bool_fields=bool_fields,
            )
            for row in rows
        ]

    def safe_select_records(
        self,
        query: str,
        params: Sequence[Any] | None = None,
        *,
        json_fields: set[str] | None = None,
        bool_fields: set[str] | None = None,
        operation_name: str = "feedback_memory_select_records",
    ) -> "FeedbackMemorySelectResult":
        try:
            return FeedbackMemorySelectResult(
                ok=True,
                rows=self.select_records(
                    query,
                    params,
                    json_fields=json_fields,
                    bool_fields=bool_fields,
                ),
            )
        except Exception as exc:
            LOGGER.warning("%s failed: %s", operation_name, exc)
            return FeedbackMemorySelectResult(
                ok=False,
                rows=[],
                error_message=str(exc),
            )

    def close(self) -> None:
        if self._connection is not None:
            self._connection.close()
            self._connection = None

    def __enter__(self) -> "FeedbackMemoryRepository":
        self.safe_initialize()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()
