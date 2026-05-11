from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from kbo_card_news.collectors.base import BaseCollector
from kbo_card_news.models.collector import CollectedItem


@dataclass(slots=True)
class CollectorRunResult:
    items: list[CollectedItem] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


class CollectorService:
    def __init__(self, collectors: list[BaseCollector]) -> None:
        self.collectors = collectors

    def collect_all(
        self,
        *,
        window_start: datetime | None = None,
        window_end: datetime | None = None,
    ) -> CollectorRunResult:
        result = CollectorRunResult()
        for collector in self.collectors:
            try:
                result.items.extend(
                    collector.collect(
                        window_start=window_start,
                        window_end=window_end,
                    )
                )
            except Exception as exc:  # noqa: BLE001
                result.errors.append(f"{collector.source_name}: {exc}")
        return result
