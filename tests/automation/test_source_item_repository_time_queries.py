from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone

from kbo_card_news.pipeline.storage import (
    PersistedSourceItem,
    SourceItemRecord,
    SQLiteSourceItemRepository,
)


class SourceItemRepositoryTimeQueryTest(unittest.TestCase):
    def test_list_items_published_between_uses_published_at_and_end_exclusive(self) -> None:
        db_path = self._tmp_db_path()
        with SQLiteSourceItemRepository(str(db_path)) as repository:
            repository.insert(_item("in", published_at=datetime(2026, 5, 11, 1, 5, tzinfo=timezone.utc)))
            repository.insert(_item("end", published_at=datetime(2026, 5, 11, 1, 10, tzinfo=timezone.utc)))
            rows = repository.list_items_published_between(
                window_start=datetime(2026, 5, 11, 1, 0, tzinfo=timezone.utc),
                window_end=datetime(2026, 5, 11, 1, 10, tzinfo=timezone.utc),
            )

        self.assertEqual([row.item.id for row in rows], ["in"])

    def test_list_items_published_between_falls_back_to_collected_at(self) -> None:
        db_path = self._tmp_db_path()
        with SQLiteSourceItemRepository(str(db_path)) as repository:
            repository.insert(
                _item(
                    "fallback",
                    published_at=None,
                    collected_at=datetime(2026, 5, 11, 1, 5, tzinfo=timezone.utc),
                )
            )
            rows = repository.list_items_published_between(
                window_start=datetime(2026, 5, 11, 1, 0, tzinfo=timezone.utc),
                window_end=datetime(2026, 5, 11, 1, 10, tzinfo=timezone.utc),
            )

        self.assertEqual([row.item.id for row in rows], ["fallback"])

    def test_list_items_published_between_handles_timezone_offsets(self) -> None:
        kst = timezone(timedelta(hours=9))
        db_path = self._tmp_db_path()
        with SQLiteSourceItemRepository(str(db_path)) as repository:
            repository.insert(_item("kst", published_at=datetime(2026, 5, 11, 10, 5, tzinfo=kst)))
            rows = repository.list_items_published_between(
                window_start=datetime(2026, 5, 11, 1, 0, tzinfo=timezone.utc),
                window_end=datetime(2026, 5, 11, 1, 10, tzinfo=timezone.utc),
            )

        self.assertEqual([row.item.id for row in rows], ["kst"])

    def _tmp_db_path(self):
        import tempfile
        from pathlib import Path

        return Path(tempfile.mkdtemp()) / "source.db"


def _item(
    item_id: str,
    *,
    published_at: datetime | None,
    collected_at: datetime | None = None,
) -> PersistedSourceItem:
    created_at = collected_at or published_at or datetime(2026, 5, 11, 1, 0, tzinfo=timezone.utc)
    return PersistedSourceItem(
        item=SourceItemRecord(
            id=item_id,
            source_type="news",
            source_item_type="article",
            source_url=f"https://example.com/{item_id}",
            source_external_id=None,
            title=f"LG 오스틴 말소 {item_id}",
            body_text="KBO 기사",
            author_name=None,
            published_at=published_at,
            collected_at=created_at,
            language_code="ko",
            engagement_view_count=0,
            engagement_like_count=0,
            engagement_comment_count=0,
            engagement_share_count=0,
            status="collected",
            raw_payload={},
            content_hash=f"hash-{item_id}",
            created_at=created_at,
            updated_at=created_at,
        ),
        assets=[],
    )


if __name__ == "__main__":
    unittest.main()
