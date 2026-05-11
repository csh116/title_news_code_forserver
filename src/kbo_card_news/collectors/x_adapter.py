from __future__ import annotations

from dataclasses import dataclass

from kbo_card_news.collectors.base import BaseCollector
from kbo_card_news.models.collector import CollectedItem


@dataclass(slots=True)
class XCollectorConfig:
    query: str


class XCollectorAdapter(BaseCollector):
    source_name = "x"

    def __init__(self, config: XCollectorConfig) -> None:
        self.config = config

    def collect(self) -> list[CollectedItem]:
        raise NotImplementedError(
            "X collection is intentionally deferred until authentication and "
            "operational policy are confirmed."
        )

