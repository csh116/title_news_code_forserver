from __future__ import annotations

import unittest
from datetime import datetime, timezone

from kbo_card_news.automation.fresh_issue_detector import (
    FreshIssueDetectorConfig,
    _build_candidates,
)
from kbo_card_news.pipeline.storage import PersistedSourceItem, SourceItemRecord


class FreshIssueDetectorTest(unittest.TestCase):
    def test_strong_team_injury_issue_passes_threshold(self) -> None:
        now = datetime(2026, 5, 11, 1, 10, tzinfo=timezone.utc)
        fresh = [_item("a1", "LG 오스틴 1군 말소, 검진 예정", now)]
        candidates = _build_candidates(
            fresh_articles=fresh,
            context_articles=[],
            now=now,
            config=FreshIssueDetectorConfig(gemini_review_enabled=False),
        )

        self.assertEqual(len(candidates), 1)
        self.assertGreaterEqual(candidates[0].issue_score, 65)
        self.assertEqual(candidates[0].notification_level, "immediate")
        self.assertIn("LG", candidates[0].matched_teams)

    def test_low_priority_single_article_stays_below_threshold(self) -> None:
        now = datetime(2026, 5, 11, 1, 10, tzinfo=timezone.utc)
        fresh = [_item("a1", "프로야구 주말 3연전 관전 포인트와 선발 예고", now)]
        candidates = _build_candidates(
            fresh_articles=fresh,
            context_articles=[],
            now=now,
            config=FreshIssueDetectorConfig(gemini_review_enabled=False),
        )

        self.assertEqual(len(candidates), 1)
        self.assertLess(candidates[0].issue_score, 65)
        self.assertTrue(candidates[0].risk_flags)


def _item(item_id: str, title: str, published_at: datetime) -> PersistedSourceItem:
    return PersistedSourceItem(
        item=SourceItemRecord(
            id=item_id,
            source_type="news",
            source_item_type="article",
            source_url=f"https://example.com/{item_id}",
            source_external_id=None,
            title=title,
            body_text=title,
            author_name=None,
            published_at=published_at,
            collected_at=published_at,
            language_code="ko",
            engagement_view_count=0,
            engagement_like_count=0,
            engagement_comment_count=0,
            engagement_share_count=0,
            status="collected",
            raw_payload={},
            content_hash=f"hash-{item_id}",
            created_at=published_at,
            updated_at=published_at,
        ),
        assets=[],
    )


if __name__ == "__main__":
    unittest.main()
