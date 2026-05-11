from __future__ import annotations

import json
import os
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from kbo_card_news.collectors.base import BaseCollector
from kbo_card_news.collectors.service import CollectorService
from kbo_card_news.pipeline.storage import SQLiteSourceItemRepository, SourceItemIngestionService


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(slots=True)
class RuntimePaths:
    db_path: str
    log_dir: str


@dataclass(slots=True)
class PipelineRunSummary:
    run_id: str
    started_at: str
    finished_at: str
    duration_seconds: float
    collected_count: int
    inserted_count: int
    duplicate_count: int
    collector_error_count: int
    collector_errors: list[str]
    db_path: str
    log_path: str


class JsonlRunLogger:
    def __init__(self, log_dir: str) -> None:
        self.log_dir = log_dir
        os.makedirs(log_dir, exist_ok=True)

    def create_log_path(self, started_at: datetime) -> str:
        timestamp = started_at.strftime("%Y%m%dT%H%M%SZ")
        return str(Path(self.log_dir) / f"pipeline_run_{timestamp}.jsonl")

    def write_event(self, log_path: str, event: dict) -> None:
        with open(log_path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(event, ensure_ascii=False, sort_keys=True))
            handle.write("\n")

    def write_summary(self, summary: PipelineRunSummary) -> None:
        latest_path = Path(self.log_dir) / "latest_run_summary.json"
        with open(latest_path, "w", encoding="utf-8") as handle:
            json.dump(asdict(summary), handle, ensure_ascii=False, indent=2, sort_keys=True)


class ScheduledPipelineRunner:
    def __init__(
        self,
        collectors: list[BaseCollector],
        runtime_paths: RuntimePaths,
        interval_seconds: int = 600,
        sleep_fn: Callable[[int], None] | None = None,
    ) -> None:
        self.collectors = collectors
        self.runtime_paths = runtime_paths
        self.interval_seconds = interval_seconds
        self.sleep_fn = sleep_fn or time.sleep
        self.logger = JsonlRunLogger(runtime_paths.log_dir)

    def run_once(self) -> PipelineRunSummary:
        started_at = _utc_now()
        run_id = started_at.strftime("%Y%m%dT%H%M%SZ")
        log_path = self.logger.create_log_path(started_at)
        self.logger.write_event(
            log_path,
            {
                "event": "run_started",
                "run_id": run_id,
                "started_at": started_at.isoformat(),
                "db_path": self.runtime_paths.db_path,
            },
        )

        collector_service = CollectorService(self.collectors)
        collector_result = collector_service.collect_all()

        with SQLiteSourceItemRepository(self.runtime_paths.db_path) as repository:
            ingestion_service = SourceItemIngestionService(repository=repository)
            ingestion_result = ingestion_service.ingest(collector_result.items)

        finished_at = _utc_now()
        summary = PipelineRunSummary(
            run_id=run_id,
            started_at=started_at.isoformat(),
            finished_at=finished_at.isoformat(),
            duration_seconds=round((finished_at - started_at).total_seconds(), 3),
            collected_count=len(collector_result.items),
            inserted_count=len(ingestion_result.inserted),
            duplicate_count=len(ingestion_result.duplicates),
            collector_error_count=len(collector_result.errors),
            collector_errors=collector_result.errors,
            db_path=self.runtime_paths.db_path,
            log_path=log_path,
        )
        self.logger.write_event(
            log_path,
            {
                "event": "run_finished",
                **asdict(summary),
            },
        )
        self.logger.write_summary(summary)
        return summary

    def run_forever(self, max_cycles: int | None = None) -> list[PipelineRunSummary]:
        summaries: list[PipelineRunSummary] = []
        cycle = 0
        while max_cycles is None or cycle < max_cycles:
            summaries.append(self.run_once())
            cycle += 1
            if max_cycles is not None and cycle >= max_cycles:
                break
            self.sleep_fn(self.interval_seconds)
        return summaries
