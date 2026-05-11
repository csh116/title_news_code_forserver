from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime

from kbo_card_news.models.collector import CollectedItem


class BaseCollector(ABC):
    source_name: str

    @abstractmethod
    def collect(
        self,
        *,
        window_start: datetime | None = None,
        window_end: datetime | None = None,
    ) -> list[CollectedItem]:
        """Collect items from the source and return normalized results."""
