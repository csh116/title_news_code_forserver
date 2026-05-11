from __future__ import annotations

import json

from kbo_card_news.automation.job_state import AutomationJobRepository
from kbo_card_news.automation.news_watcher import watch_once


def test_watch_once_deduplicates_stable_article_url(tmp_path):
    db_path = tmp_path / "automation_state.db"
    first_choice = tmp_path / "first_choice.json"
    second_choice = tmp_path / "second_choice.json"
    article_url = "https://example.com/news/123?utm_source=test"

    first_choice.write_text(
        json.dumps({"candidates": [_candidate("kbo-news-20260507_170738:2", article_url)]}, ensure_ascii=False),
        encoding="utf-8",
    )
    second_choice.write_text(
        json.dumps({"candidates": [_candidate("kbo-news-20260507_172400:2", article_url)]}, ensure_ascii=False),
        encoding="utf-8",
    )

    with AutomationJobRepository(db_path) as repository:
        first = watch_once(repository=repository, choice_json_path=first_choice)
        second = watch_once(repository=repository, choice_json_path=second_choice)

    assert len(first.created_jobs) == 1
    assert len(second.created_jobs) == 0
    assert len(second.duplicate_jobs) == 1
    assert second.duplicate_jobs[0].job_id == first.created_jobs[0].job_id
    assert first.created_jobs[0].metadata["representative_article_url"] == "https://example.com/news/123"
    assert first.created_jobs[0].metadata["topic_fingerprint"]


def _candidate(topic_id: str, article_url: str) -> dict:
    return {
        "topic_id": topic_id,
        "topic_name": "LG 트윈스 주축 선수 부상 이탈",
        "importance_rank": 1,
        "topic_score": 80,
        "reason_summary": "부상 이슈",
        "representative_article_id": "article-1",
        "article_ids": ["article-1"],
        "metadata": {
            "article_publication_summary": {
                "articles": [
                    {
                        "article_id": "article-1",
                        "title": "LG 주축 선수 부상",
                        "source_type": "news",
                        "source_url": article_url,
                        "published_at": "2026-05-07T17:00:00+09:00",
                    }
                ]
            }
        },
    }
