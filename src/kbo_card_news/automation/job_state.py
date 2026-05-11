from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Literal

ROOT_DIR = Path(__file__).resolve().parents[3]
OUTPUT_ROOT = ROOT_DIR / "outputs"
AUTOMATION_OUTPUT_DIR = OUTPUT_ROOT / "automation"
AUTOMATION_STATE_DB_PATH = AUTOMATION_OUTPUT_DIR / "automation_state.db"
KST = timezone(timedelta(hours=9))

AutomationJobStatus = Literal[
    "detected",
    "pending_approval",
    "notified",
    "approved",
    "pipeline_running",
    "editor_ready",
    "render_ready",
    "publish_approved",
    "published",
    "skipped",
    "expired",
    "failed",
]

VALID_JOB_STATUSES: set[str] = {
    "detected",
    "pending_approval",
    "notified",
    "approved",
    "pipeline_running",
    "editor_ready",
    "render_ready",
    "publish_approved",
    "published",
    "skipped",
    "expired",
    "failed",
}

CLEAR_FAILURE_MESSAGE_STATUSES: set[str] = {
    "approved",
    "pipeline_running",
    "editor_ready",
    "render_ready",
    "publish_approved",
    "published",
}


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def build_job_id(*, timestamp: datetime | None = None, sequence: int = 1) -> str:
    value = timestamp or datetime.now(KST)
    return f"{value.astimezone(KST).strftime('%Y%m%d-%H%M%S')}-{sequence:03d}"


@dataclass(slots=True)
class AutomationJobArticle:
    title: str = ""
    source_type: str = ""
    source_url: str = ""
    published_at: str | None = None
    article_id: str | None = None


@dataclass(slots=True)
class AutomationJob:
    job_id: str
    topic_id: str
    topic_name: str
    status: AutomationJobStatus = "detected"
    notification_level: str = "watch"
    virality_potential_score: float = 0.0
    account_fit_score: float = 0.0
    recommendation_summary: str = ""
    approval_run_dir: str | None = None
    editor_url: str | None = None
    render_png_path: str | None = None
    social_copy_md_path: str | None = None
    failure_message: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    articles: list[AutomationJobArticle] = field(default_factory=list)
    created_at: datetime = field(default_factory=utc_now)
    updated_at: datetime = field(default_factory=utc_now)


@dataclass(slots=True)
class AutomationJobEvent:
    id: int
    job_id: str
    event_type: str
    message: str
    metadata: dict[str, Any]
    created_at: datetime


class AutomationJobRepository:
    def __init__(self, db_path: str | Path = AUTOMATION_STATE_DB_PATH) -> None:
        self.db_path = Path(db_path).expanduser()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(str(self.db_path))
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA foreign_keys = ON")
        self.initialize()

    def close(self) -> None:
        self._connection.close()

    def __enter__(self) -> AutomationJobRepository:
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        self.close()

    def initialize(self) -> None:
        self._connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS automation_jobs (
                job_id TEXT PRIMARY KEY,
                topic_id TEXT NOT NULL,
                topic_name TEXT NOT NULL,
                status TEXT NOT NULL,
                notification_level TEXT NOT NULL,
                virality_potential_score REAL NOT NULL DEFAULT 0,
                account_fit_score REAL NOT NULL DEFAULT 0,
                recommendation_summary TEXT NOT NULL DEFAULT '',
                approval_run_dir TEXT,
                editor_url TEXT,
                render_png_path TEXT,
                social_copy_md_path TEXT,
                failure_message TEXT,
                metadata_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_automation_jobs_status
            ON automation_jobs(status, updated_at);

            CREATE INDEX IF NOT EXISTS idx_automation_jobs_topic
            ON automation_jobs(topic_id, topic_name);

            CREATE TABLE IF NOT EXISTS automation_job_articles (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                job_id TEXT NOT NULL,
                article_id TEXT,
                title TEXT NOT NULL DEFAULT '',
                source_type TEXT NOT NULL DEFAULT '',
                source_url TEXT NOT NULL DEFAULT '',
                published_at TEXT,
                sort_order INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                FOREIGN KEY (job_id) REFERENCES automation_jobs(job_id) ON DELETE CASCADE
            );

            CREATE INDEX IF NOT EXISTS idx_automation_job_articles_job_id
            ON automation_job_articles(job_id, sort_order);

            CREATE UNIQUE INDEX IF NOT EXISTS idx_automation_job_articles_url
            ON automation_job_articles(job_id, source_url)
            WHERE source_url != '';

            CREATE TABLE IF NOT EXISTS automation_job_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                job_id TEXT NOT NULL,
                event_type TEXT NOT NULL,
                message TEXT NOT NULL DEFAULT '',
                metadata_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL,
                FOREIGN KEY (job_id) REFERENCES automation_jobs(job_id) ON DELETE CASCADE
            );

            CREATE INDEX IF NOT EXISTS idx_automation_job_events_job_id
            ON automation_job_events(job_id, id);
            """
        )
        self._connection.commit()

    def create_job(self, job: AutomationJob) -> AutomationJob:
        self._validate_status(job.status)
        now = utc_now()
        created_at = job.created_at or now
        updated_at = job.updated_at or now
        with self._connection:
            self._connection.execute(
                """
                INSERT INTO automation_jobs (
                    job_id, topic_id, topic_name, status, notification_level,
                    virality_potential_score, account_fit_score, recommendation_summary,
                    approval_run_dir, editor_url, render_png_path, social_copy_md_path,
                    failure_message, metadata_json, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    job.job_id,
                    job.topic_id,
                    job.topic_name,
                    job.status,
                    job.notification_level,
                    float(job.virality_potential_score),
                    float(job.account_fit_score),
                    job.recommendation_summary,
                    job.approval_run_dir,
                    job.editor_url,
                    job.render_png_path,
                    job.social_copy_md_path,
                    job.failure_message,
                    _json_dumps(job.metadata),
                    _format_datetime(created_at),
                    _format_datetime(updated_at),
                ),
            )
            self._replace_articles(job.job_id, job.articles)
            self._insert_event(
                job.job_id,
                "created",
                f"job created with status={job.status}",
                {"status": job.status},
            )
        return self.get_job(job.job_id) or job

    def get_job(self, job_id: str) -> AutomationJob | None:
        row = self._connection.execute(
            "SELECT * FROM automation_jobs WHERE job_id = ?",
            (job_id,),
        ).fetchone()
        if row is None:
            return None
        return self._job_from_row(row)

    def get_job_by_topic_id(self, topic_id: str) -> AutomationJob | None:
        row = self._connection.execute(
            """
            SELECT *
            FROM automation_jobs
            WHERE topic_id = ?
            ORDER BY updated_at DESC, created_at DESC
            LIMIT 1
            """,
            (topic_id,),
        ).fetchone()
        if row is None:
            return None
        return self._job_from_row(row)

    def list_recent_jobs(self, *, hours: int = 24, limit: int = 300) -> list[AutomationJob]:
        cutoff = utc_now() - timedelta(hours=max(1, int(hours)))
        rows = self._connection.execute(
            """
            SELECT *
            FROM automation_jobs
            WHERE created_at >= ?
            ORDER BY updated_at DESC, created_at DESC
            LIMIT ?
            """,
            (_format_datetime(cutoff), max(1, int(limit))),
        ).fetchall()
        return [self._job_from_row(row) for row in rows]

    def list_jobs(
        self,
        *,
        status: str | None = None,
        limit: int = 20,
    ) -> list[AutomationJob]:
        params: list[Any] = []
        where = ""
        if status:
            self._validate_status(status)
            where = "WHERE status = ?"
            params.append(status)
        params.append(max(1, int(limit)))
        rows = self._connection.execute(
            f"""
            SELECT *
            FROM automation_jobs
            {where}
            ORDER BY updated_at DESC, created_at DESC
            LIMIT ?
            """,
            params,
        ).fetchall()
        return [self._job_from_row(row) for row in rows]

    def update_status(
        self,
        job_id: str,
        status: AutomationJobStatus,
        *,
        message: str = "",
        failure_message: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> AutomationJob:
        self._validate_status(status)
        existing = self.get_job(job_id)
        if existing is None:
            raise KeyError(f"automation job not found: {job_id}")
        now = utc_now()
        merged_metadata = dict(existing.metadata)
        if metadata:
            merged_metadata.update(metadata)
        next_failure_message = failure_message
        if next_failure_message is None and status in CLEAR_FAILURE_MESSAGE_STATUSES:
            next_failure_message = ""
        with self._connection:
            self._connection.execute(
                """
                UPDATE automation_jobs
                SET status = ?,
                    failure_message = CASE
                        WHEN ? = '' THEN NULL
                        ELSE COALESCE(?, failure_message)
                    END,
                    metadata_json = ?,
                    updated_at = ?
                WHERE job_id = ?
                """,
                (
                    status,
                    next_failure_message,
                    next_failure_message,
                    _json_dumps(merged_metadata),
                    _format_datetime(now),
                    job_id,
                ),
            )
            self._insert_event(
                job_id,
                "status_changed",
                message or f"status changed to {status}",
                {"status": status, **(metadata or {})},
            )
        updated = self.get_job(job_id)
        if updated is None:
            raise KeyError(f"automation job not found after update: {job_id}")
        return updated

    def update_job_paths(
        self,
        job_id: str,
        *,
        approval_run_dir: str | None = None,
        editor_url: str | None = None,
        render_png_path: str | None = None,
        social_copy_md_path: str | None = None,
        message: str = "",
    ) -> AutomationJob:
        existing = self.get_job(job_id)
        if existing is None:
            raise KeyError(f"automation job not found: {job_id}")
        now = utc_now()
        with self._connection:
            self._connection.execute(
                """
                UPDATE automation_jobs
                SET approval_run_dir = COALESCE(?, approval_run_dir),
                    editor_url = COALESCE(?, editor_url),
                    render_png_path = COALESCE(?, render_png_path),
                    social_copy_md_path = COALESCE(?, social_copy_md_path),
                    updated_at = ?
                WHERE job_id = ?
                """,
                (
                    approval_run_dir,
                    editor_url,
                    render_png_path,
                    social_copy_md_path,
                    _format_datetime(now),
                    job_id,
                ),
            )
            self._insert_event(
                job_id,
                "paths_updated",
                message or "job paths updated",
                {
                    "approval_run_dir": approval_run_dir,
                    "editor_url": editor_url,
                    "render_png_path": render_png_path,
                    "social_copy_md_path": social_copy_md_path,
                },
            )
        updated = self.get_job(job_id)
        if updated is None:
            raise KeyError(f"automation job not found after path update: {job_id}")
        return updated

    def record_event(
        self,
        job_id: str,
        event_type: str,
        *,
        message: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> AutomationJobEvent:
        if self.get_job(job_id) is None:
            raise KeyError(f"automation job not found: {job_id}")
        with self._connection:
            event_id = self._insert_event(job_id, event_type, message, metadata or {})
        event = self.get_event(int(event_id))
        if event is None:
            raise KeyError(f"automation job event not found after insert: {event_id}")
        return event

    def get_event(self, event_id: int) -> AutomationJobEvent | None:
        row = self._connection.execute(
            "SELECT * FROM automation_job_events WHERE id = ?",
            (event_id,),
        ).fetchone()
        if row is None:
            return None
        return self._event_from_row(row)

    def list_events(self, job_id: str, *, limit: int = 50) -> list[AutomationJobEvent]:
        rows = self._connection.execute(
            """
            SELECT *
            FROM automation_job_events
            WHERE job_id = ?
            ORDER BY id DESC
            LIMIT ?
            """,
            (job_id, max(1, int(limit))),
        ).fetchall()
        return [self._event_from_row(row) for row in rows]

    def _replace_articles(self, job_id: str, articles: list[AutomationJobArticle]) -> None:
        self._connection.execute(
            "DELETE FROM automation_job_articles WHERE job_id = ?",
            (job_id,),
        )
        now_text = _format_datetime(utc_now())
        for index, article in enumerate(articles, start=1):
            self._connection.execute(
                """
                INSERT INTO automation_job_articles (
                    job_id, article_id, title, source_type, source_url,
                    published_at, sort_order, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    job_id,
                    article.article_id,
                    article.title,
                    article.source_type,
                    article.source_url,
                    article.published_at,
                    index,
                    now_text,
                ),
            )

    def _insert_event(
        self,
        job_id: str,
        event_type: str,
        message: str,
        metadata: dict[str, Any],
    ) -> int:
        cursor = self._connection.execute(
            """
            INSERT INTO automation_job_events (
                job_id, event_type, message, metadata_json, created_at
            )
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                job_id,
                event_type,
                message,
                _json_dumps(metadata),
                _format_datetime(utc_now()),
            ),
        )
        return int(cursor.lastrowid)

    def _job_from_row(self, row: sqlite3.Row) -> AutomationJob:
        articles = self._list_articles(str(row["job_id"]))
        return AutomationJob(
            job_id=str(row["job_id"]),
            topic_id=str(row["topic_id"]),
            topic_name=str(row["topic_name"]),
            status=str(row["status"]),  # type: ignore[arg-type]
            notification_level=str(row["notification_level"]),
            virality_potential_score=float(row["virality_potential_score"] or 0.0),
            account_fit_score=float(row["account_fit_score"] or 0.0),
            recommendation_summary=str(row["recommendation_summary"] or ""),
            approval_run_dir=row["approval_run_dir"],
            editor_url=row["editor_url"],
            render_png_path=row["render_png_path"],
            social_copy_md_path=row["social_copy_md_path"],
            failure_message=row["failure_message"],
            metadata=_json_loads(row["metadata_json"]),
            articles=articles,
            created_at=_parse_datetime(str(row["created_at"])),
            updated_at=_parse_datetime(str(row["updated_at"])),
        )

    def _list_articles(self, job_id: str) -> list[AutomationJobArticle]:
        rows = self._connection.execute(
            """
            SELECT *
            FROM automation_job_articles
            WHERE job_id = ?
            ORDER BY sort_order ASC, id ASC
            """,
            (job_id,),
        ).fetchall()
        return [
            AutomationJobArticle(
                article_id=row["article_id"],
                title=str(row["title"] or ""),
                source_type=str(row["source_type"] or ""),
                source_url=str(row["source_url"] or ""),
                published_at=row["published_at"],
            )
            for row in rows
        ]

    @staticmethod
    def _event_from_row(row: sqlite3.Row) -> AutomationJobEvent:
        return AutomationJobEvent(
            id=int(row["id"]),
            job_id=str(row["job_id"]),
            event_type=str(row["event_type"]),
            message=str(row["message"] or ""),
            metadata=_json_loads(row["metadata_json"]),
            created_at=_parse_datetime(str(row["created_at"])),
        )

    @staticmethod
    def _validate_status(status: str) -> None:
        if status not in VALID_JOB_STATUSES:
            allowed = ", ".join(sorted(VALID_JOB_STATUSES))
            raise ValueError(f"invalid automation job status: {status}; allowed={allowed}")


def job_to_dict(job: AutomationJob) -> dict[str, Any]:
    return {
        "job_id": job.job_id,
        "topic_id": job.topic_id,
        "topic_name": job.topic_name,
        "status": job.status,
        "notification_level": job.notification_level,
        "virality_potential_score": job.virality_potential_score,
        "account_fit_score": job.account_fit_score,
        "recommendation_summary": job.recommendation_summary,
        "approval_run_dir": job.approval_run_dir,
        "editor_url": job.editor_url,
        "render_png_path": job.render_png_path,
        "social_copy_md_path": job.social_copy_md_path,
        "failure_message": job.failure_message,
        "metadata": job.metadata,
        "articles": [
            {
                "article_id": article.article_id,
                "title": article.title,
                "source_type": article.source_type,
                "source_url": article.source_url,
                "published_at": article.published_at,
            }
            for article in job.articles
        ],
        "created_at": _format_datetime(job.created_at),
        "updated_at": _format_datetime(job.updated_at),
    }


def event_to_dict(event: AutomationJobEvent) -> dict[str, Any]:
    return {
        "id": event.id,
        "job_id": event.job_id,
        "event_type": event.event_type,
        "message": event.message,
        "metadata": event.metadata,
        "created_at": _format_datetime(event.created_at),
    }


def _json_dumps(value: dict[str, Any]) -> str:
    return json.dumps(value or {}, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _json_loads(value: object) -> dict[str, Any]:
    if not value:
        return {}
    parsed = json.loads(str(value))
    return parsed if isinstance(parsed, dict) else {}


def _format_datetime(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat(timespec="seconds")


def _parse_datetime(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed
