from __future__ import annotations

import fcntl
import json
import shutil
import traceback
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path
from typing import Any, Callable, TypeVar

from kbo_card_news.automation.job_state import (
    AUTOMATION_OUTPUT_DIR,
    AutomationJob,
    AutomationJobRepository,
    utc_now,
)

LOCK_DIR = AUTOMATION_OUTPUT_DIR / "locks"
LOG_DIR = AUTOMATION_OUTPUT_DIR / "logs"
DEFAULT_WATCHER_LOCK_PATH = LOCK_DIR / "watcher.lock"

T = TypeVar("T")


class AutomationLockBusy(RuntimeError):
    pass


class AutomationFileLock:
    def __init__(self, path: str | Path = DEFAULT_WATCHER_LOCK_PATH) -> None:
        self.path = Path(path).expanduser()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._handle: Any | None = None

    def __enter__(self) -> AutomationFileLock:
        self._handle = self.path.open("a+", encoding="utf-8")
        try:
            fcntl.flock(self._handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            self._handle.close()
            self._handle = None
            raise AutomationLockBusy(f"automation lock is busy: {self.path}") from exc
        self._handle.seek(0)
        self._handle.truncate()
        self._handle.write(json.dumps({"locked_at": utc_now().isoformat()}, ensure_ascii=False))
        self._handle.flush()
        return self

    def __exit__(self, exc_type: object, exc: object, traceback_obj: object) -> None:
        if self._handle is None:
            return
        fcntl.flock(self._handle.fileno(), fcntl.LOCK_UN)
        self._handle.close()
        self._handle = None


def run_with_lock(lock_path: str | Path | None, action: Callable[[], T]) -> T:
    with AutomationFileLock(lock_path or DEFAULT_WATCHER_LOCK_PATH):
        return action()


@dataclass(slots=True)
class HealthReport:
    db_path: str
    total_jobs: int
    status_counts: dict[str, int]
    stale_pipeline_running: list[str] = field(default_factory=list)
    stale_pending_approval: list[str] = field(default_factory=list)
    disk_path: str = ""
    disk_free_gb: float = 0.0
    disk_ok: bool = True
    log_dir: str = ""
    lock_path: str = ""


@dataclass(slots=True)
class RecoveryResult:
    recovered_jobs: list[AutomationJob]
    skipped_jobs: list[AutomationJob]


@dataclass(slots=True)
class ExpireResult:
    expired_jobs: list[AutomationJob]
    skipped_jobs: list[AutomationJob]


@dataclass(slots=True)
class DigestReport:
    since_hours: int
    status_counts: dict[str, int]
    recent_jobs: list[AutomationJob]
    failed_jobs: list[AutomationJob]
    render_ready_jobs: list[AutomationJob]


def build_health_report(
    repository: AutomationJobRepository,
    *,
    disk_path: str | Path = AUTOMATION_OUTPUT_DIR,
    min_free_gb: float = 5.0,
    pending_hours: int = 12,
    pipeline_hours: int = 2,
    limit: int = 500,
) -> HealthReport:
    jobs = repository.list_jobs(limit=limit)
    counts = _status_counts(jobs)
    now = utc_now()
    pending_cutoff = now - timedelta(hours=max(1, int(pending_hours)))
    pipeline_cutoff = now - timedelta(hours=max(1, int(pipeline_hours)))
    stale_pending = [
        job.job_id
        for job in jobs
        if job.status in {"detected", "pending_approval", "notified"} and job.updated_at < pending_cutoff
    ]
    stale_pipeline = [
        job.job_id
        for job in jobs
        if job.status == "pipeline_running" and job.updated_at < pipeline_cutoff
    ]
    disk_root = Path(disk_path).expanduser()
    disk_root.mkdir(parents=True, exist_ok=True)
    usage = shutil.disk_usage(disk_root)
    free_gb = round(usage.free / (1024**3), 2)
    return HealthReport(
        db_path=str(repository.db_path),
        total_jobs=len(jobs),
        status_counts=counts,
        stale_pipeline_running=stale_pipeline,
        stale_pending_approval=stale_pending,
        disk_path=str(disk_root),
        disk_free_gb=free_gb,
        disk_ok=free_gb >= float(min_free_gb),
        log_dir=str(LOG_DIR),
        lock_path=str(DEFAULT_WATCHER_LOCK_PATH),
    )


def recover_stale_pipeline_jobs(
    repository: AutomationJobRepository,
    *,
    stale_hours: int = 2,
    target_status: str = "approved",
    limit: int = 200,
) -> RecoveryResult:
    cutoff = utc_now() - timedelta(hours=max(1, int(stale_hours)))
    recovered: list[AutomationJob] = []
    skipped: list[AutomationJob] = []
    for job in repository.list_jobs(status="pipeline_running", limit=limit):
        if job.updated_at >= cutoff:
            skipped.append(job)
            continue
        next_status = _infer_recovery_status(job, fallback=target_status)
        recovered.append(
            repository.update_status(
                job.job_id,
                next_status,  # type: ignore[arg-type]
                message="recovered stale pipeline_running job after restart",
                metadata={
                    "recovered_from": "pipeline_running",
                    "recovery_target_status": next_status,
                    "stale_hours": stale_hours,
                },
            )
        )
    return RecoveryResult(recovered_jobs=recovered, skipped_jobs=skipped)


def expire_old_pending_jobs(
    repository: AutomationJobRepository,
    *,
    stale_hours: int = 12,
    statuses: set[str] | None = None,
    limit: int = 300,
) -> ExpireResult:
    target_statuses = statuses or {"detected", "pending_approval", "notified"}
    cutoff = utc_now() - timedelta(hours=max(1, int(stale_hours)))
    expired: list[AutomationJob] = []
    skipped: list[AutomationJob] = []
    for status in sorted(target_statuses):
        for job in repository.list_jobs(status=status, limit=limit):
            if job.updated_at >= cutoff:
                skipped.append(job)
                continue
            expired.append(
                repository.update_status(
                    job.job_id,
                    "expired",
                    message="pending approval expired",
                    metadata={"expired_from": status, "stale_hours": stale_hours},
                )
            )
    return ExpireResult(expired_jobs=expired, skipped_jobs=skipped)


def build_digest_report(
    repository: AutomationJobRepository,
    *,
    since_hours: int = 24,
    limit: int = 200,
) -> DigestReport:
    cutoff = utc_now() - timedelta(hours=max(1, int(since_hours)))
    jobs = repository.list_jobs(limit=limit)
    recent_jobs = [job for job in jobs if job.updated_at >= cutoff]
    return DigestReport(
        since_hours=since_hours,
        status_counts=_status_counts(jobs),
        recent_jobs=recent_jobs,
        failed_jobs=[job for job in recent_jobs if job.status == "failed"],
        render_ready_jobs=[job for job in recent_jobs if job.status == "render_ready"],
    )


def write_failure_log(
    *,
    operation: str,
    exc: BaseException,
    metadata: dict[str, Any] | None = None,
) -> Path:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    timestamp = utc_now().strftime("%Y%m%d-%H%M%S")
    path = LOG_DIR / f"{timestamp}_{operation}_failure.log"
    payload = {
        "operation": operation,
        "created_at": utc_now().isoformat(),
        "error_type": type(exc).__name__,
        "error_message": str(exc),
        "metadata": metadata or {},
        "traceback": traceback.format_exception(type(exc), exc, exc.__traceback__),
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def health_report_to_dict(report: HealthReport) -> dict[str, Any]:
    return {
        "db_path": report.db_path,
        "total_jobs": report.total_jobs,
        "status_counts": report.status_counts,
        "stale_pipeline_running": report.stale_pipeline_running,
        "stale_pending_approval": report.stale_pending_approval,
        "disk_path": report.disk_path,
        "disk_free_gb": report.disk_free_gb,
        "disk_ok": report.disk_ok,
        "log_dir": report.log_dir,
        "lock_path": report.lock_path,
    }


def digest_report_to_dict(report: DigestReport) -> dict[str, Any]:
    return {
        "since_hours": report.since_hours,
        "status_counts": report.status_counts,
        "recent_count": len(report.recent_jobs),
        "failed_count": len(report.failed_jobs),
        "render_ready_count": len(report.render_ready_jobs),
        "recent_jobs": [_job_summary(job) for job in report.recent_jobs],
        "failed_jobs": [_job_summary(job) for job in report.failed_jobs],
        "render_ready_jobs": [_job_summary(job) for job in report.render_ready_jobs],
    }


def _infer_recovery_status(job: AutomationJob, *, fallback: str) -> str:
    manifest_path = str(job.metadata.get("title_editor_manifest_path") or "").strip()
    if manifest_path and Path(manifest_path).expanduser().exists():
        return "editor_ready"
    return fallback


def _status_counts(jobs: list[AutomationJob]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for job in jobs:
        counts[job.status] = counts.get(job.status, 0) + 1
    return dict(sorted(counts.items()))


def _job_summary(job: AutomationJob) -> dict[str, Any]:
    return {
        "job_id": job.job_id,
        "status": job.status,
        "notification_level": job.notification_level,
        "virality_potential_score": job.virality_potential_score,
        "topic_name": job.topic_name,
        "updated_at": job.updated_at.isoformat(),
    }
