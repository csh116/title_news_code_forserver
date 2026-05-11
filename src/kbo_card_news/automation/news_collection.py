from __future__ import annotations

from kbo_card_news.collectors.news_sites import NEWS_SITE_DEFINITIONS, NewsSiteCollector, NewsSiteCollectorConfig


def build_news_collectors() -> list[NewsSiteCollector]:
    return [
        NewsSiteCollector(
            NewsSiteCollectorConfig(
                definition=definition,
                default_page_limit=1,
                window_page_limit_min=8,
                window_page_limit_per_day=8,
                window_page_limit_max=40,
            )
        )
        for definition in NEWS_SITE_DEFINITIONS.values()
    ]
