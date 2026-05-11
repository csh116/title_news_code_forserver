from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import uuid
from dataclasses import asdict, dataclass, field, is_dataclass
from datetime import datetime

from kbo_card_news.models.collector import CollectedItem, MediaAsset
from kbo_card_news.models.issue import (
    BatchIssueSelectionInput,
    BatchIssueSelectionResult,
    TopicDeepResearchResult,
)


def _utc_now() -> datetime:
    return datetime.utcnow()


@dataclass(slots=True)
class SourceAssetRecord:
    id: str
    source_item_id: str
    asset_type: str
    origin_url: str
    storage_path: str | None
    mime_type: str | None
    width: int | None
    height: int | None
    file_size_bytes: int | None
    sort_order: int
    vision_caption: str | None
    ocr_text: str | None
    created_at: datetime


@dataclass(slots=True)
class SourceItemRecord:
    id: str
    source_type: str
    source_item_type: str
    source_url: str
    source_external_id: str | None
    title: str | None
    body_text: str | None
    author_name: str | None
    published_at: datetime | None
    collected_at: datetime
    language_code: str
    engagement_view_count: int
    engagement_like_count: int
    engagement_comment_count: int
    engagement_share_count: int
    status: str
    raw_payload: dict
    content_hash: str
    created_at: datetime
    updated_at: datetime
    excerpt_text: str | None = None


@dataclass(slots=True)
class PersistedSourceItem:
    item: SourceItemRecord
    assets: list[SourceAssetRecord] = field(default_factory=list)


@dataclass(slots=True)
class DuplicateCandidate:
    existing_item_id: str
    reason: str


@dataclass(slots=True)
class IngestionResult:
    inserted: list[PersistedSourceItem] = field(default_factory=list)
    duplicates: list[PersistedSourceItem] = field(default_factory=list)
    duplicate_reasons: list[str] = field(default_factory=list)


@dataclass(slots=True)
class CollectionWindowRecord:
    window_start: datetime
    window_end: datetime
    status: str
    item_count: int
    inserted_count: int
    duplicate_count: int
    error_count: int
    created_at: datetime


class SourceItemRepository:
    def find_duplicate(self, candidate: PersistedSourceItem) -> DuplicateCandidate | None:
        raise NotImplementedError

    def insert(self, candidate: PersistedSourceItem) -> None:
        raise NotImplementedError

    def list_items(self) -> list[PersistedSourceItem]:
        raise NotImplementedError


class SourceItemTransformer:
    @staticmethod
    def transform(item: CollectedItem) -> PersistedSourceItem:
        record_id = str(uuid.uuid4())
        timestamp = _utc_now()
        content_hash = SourceItemTransformer.build_content_hash(item)
        source_item = SourceItemRecord(
            id=record_id,
            source_type=item.source_type,
            source_item_type=item.source_item_type,
            source_url=item.source_url,
            source_external_id=item.source_external_id,
            title=item.title,
            body_text=item.body_text,
            author_name=item.author_name,
            published_at=item.published_at,
            collected_at=item.collected_at,
            language_code=str(item.metadata.get("language_code", "ko")),
            engagement_view_count=int(item.metadata.get("engagement_view_count", 0)),
            engagement_like_count=int(item.metadata.get("engagement_like_count", 0)),
            engagement_comment_count=int(item.metadata.get("engagement_comment_count", 0)),
            engagement_share_count=int(item.metadata.get("engagement_share_count", 0)),
            status="collected",
            raw_payload=item.raw_payload,
            content_hash=content_hash,
            created_at=timestamp,
            updated_at=timestamp,
            excerpt_text=SourceItemTransformer.build_excerpt_text(item),
        )
        assets = [
            SourceItemTransformer._build_asset_record(record_id, asset)
            for asset in item.assets
        ]
        return PersistedSourceItem(item=source_item, assets=assets)

    @staticmethod
    def build_content_hash(item: CollectedItem) -> str:
        normalized_title = SourceItemTransformer._normalize_text(item.title)
        normalized_body = SourceItemTransformer._normalize_text(item.body_text)
        payload = {
            "title": normalized_title,
            "body_text": normalized_body,
        }
        return hashlib.sha256(
            json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
        ).hexdigest()

    @staticmethod
    def build_identity_key(item: SourceItemRecord) -> str:
        external_id = item.source_external_id or ""
        return f"{item.source_type}:{item.source_item_type}:{item.source_url}:{external_id}"

    @staticmethod
    def build_excerpt_text(item: CollectedItem, max_chars: int = 900) -> str | None:
        if item.excerpt_text:
            compact = SourceItemTransformer._compact_whitespace(item.excerpt_text)
            return compact[:max_chars] if compact else None

        body_text = SourceItemTransformer._compact_whitespace(item.body_text)
        if not body_text:
            return None
        return body_text[:max_chars]

    @staticmethod
    def _build_asset_record(source_item_id: str, asset: MediaAsset) -> SourceAssetRecord:
        vision_caption = asset.caption if isinstance(asset.caption, str) and asset.caption else None
        return SourceAssetRecord(
            id=str(uuid.uuid4()),
            source_item_id=source_item_id,
            asset_type=asset.asset_type,
            origin_url=asset.origin_url,
            storage_path=None,
            mime_type=asset.mime_type,
            width=asset.width,
            height=asset.height,
            file_size_bytes=None,
            sort_order=asset.sort_order,
            vision_caption=vision_caption,
            ocr_text=None,
            created_at=_utc_now(),
        )

    @staticmethod
    def _normalize_text(value: str | None) -> str:
        if not value:
            return ""
        compact = re.sub(r"[\W_]+", " ", value.lower(), flags=re.UNICODE)
        return " ".join(compact.split())

    @staticmethod
    def _compact_whitespace(value: str | None) -> str:
        if not value:
            return ""
        return " ".join(str(value).split()).strip()


class InMemorySourceItemRepository(SourceItemRepository):
    def __init__(self) -> None:
        self._items_by_id: dict[str, PersistedSourceItem] = {}
        self._ids_by_source_url: dict[str, str] = {}
        self._ids_by_external_identity: dict[str, str] = {}
        self._ids_by_content_hash: dict[str, str] = {}

    def find_duplicate(self, candidate: PersistedSourceItem) -> DuplicateCandidate | None:
        source_url_key = self._build_source_url_key(candidate.item)
        existing_id = self._ids_by_source_url.get(source_url_key)
        if existing_id:
            return DuplicateCandidate(existing_item_id=existing_id, reason="same_source_identity")

        external_identity_key = self._build_external_identity_key(candidate.item)
        if external_identity_key:
            existing_id = self._ids_by_external_identity.get(external_identity_key)
            if existing_id:
                return DuplicateCandidate(existing_item_id=existing_id, reason="same_source_identity")

        existing_id = self._ids_by_content_hash.get(candidate.item.content_hash)
        if existing_id:
            return DuplicateCandidate(existing_item_id=existing_id, reason="same_content_hash")
        return None

    def insert(self, candidate: PersistedSourceItem) -> None:
        self._items_by_id[candidate.item.id] = candidate
        self._ids_by_source_url[self._build_source_url_key(candidate.item)] = candidate.item.id
        external_identity_key = self._build_external_identity_key(candidate.item)
        if external_identity_key:
            self._ids_by_external_identity[external_identity_key] = candidate.item.id
        self._ids_by_content_hash[candidate.item.content_hash] = candidate.item.id

    def list_items(self) -> list[PersistedSourceItem]:
        return list(self._items_by_id.values())

    @staticmethod
    def _build_source_url_key(item: SourceItemRecord) -> str:
        return f"{item.source_type}:{item.source_item_type}:{item.source_url}"

    @staticmethod
    def _build_external_identity_key(item: SourceItemRecord) -> str | None:
        if not item.source_external_id:
            return None
        return f"{item.source_type}:{item.source_item_type}:{item.source_external_id}"


class SQLiteSourceItemRepository(SourceItemRepository):
    def __init__(self, db_path: str) -> None:
        self.db_path = db_path
        parent_dir = os.path.dirname(db_path)
        if parent_dir:
            os.makedirs(parent_dir, exist_ok=True)
        self._connection = sqlite3.connect(db_path)
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA foreign_keys = ON")
        self.ensure_schema()

    def ensure_schema(self) -> None:
        self._connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS source_items (
                id TEXT PRIMARY KEY,
                source_type TEXT NOT NULL,
                source_item_type TEXT NOT NULL,
                source_url TEXT NOT NULL UNIQUE,
                source_external_id TEXT,
                title TEXT,
                body_text TEXT,
                author_name TEXT,
                published_at TEXT,
                collected_at TEXT NOT NULL,
                language_code TEXT NOT NULL DEFAULT 'ko',
                engagement_view_count INTEGER NOT NULL DEFAULT 0,
                engagement_like_count INTEGER NOT NULL DEFAULT 0,
                engagement_comment_count INTEGER NOT NULL DEFAULT 0,
                engagement_share_count INTEGER NOT NULL DEFAULT 0,
                status TEXT NOT NULL,
                raw_payload TEXT NOT NULL,
                content_hash TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                excerpt_text TEXT
            );

            CREATE TABLE IF NOT EXISTS source_assets (
                id TEXT PRIMARY KEY,
                source_item_id TEXT NOT NULL,
                asset_type TEXT NOT NULL,
                origin_url TEXT NOT NULL,
                storage_path TEXT,
                mime_type TEXT,
                width INTEGER,
                height INTEGER,
                file_size_bytes INTEGER,
                sort_order INTEGER NOT NULL DEFAULT 0,
                vision_caption TEXT,
                ocr_text TEXT,
                created_at TEXT NOT NULL,
                FOREIGN KEY (source_item_id) REFERENCES source_items(id) ON DELETE CASCADE
            );

            CREATE UNIQUE INDEX IF NOT EXISTS idx_source_items_identity
            ON source_items(source_type, source_item_type, COALESCE(source_external_id, ''), source_url);

            CREATE UNIQUE INDEX IF NOT EXISTS idx_source_items_external_identity
            ON source_items(source_type, source_item_type, source_external_id)
            WHERE source_external_id IS NOT NULL;

            CREATE INDEX IF NOT EXISTS idx_source_items_content_hash
            ON source_items(content_hash);

            CREATE INDEX IF NOT EXISTS idx_source_assets_source_item_id
            ON source_assets(source_item_id);

            CREATE TABLE IF NOT EXISTS topic_selection_runs (
                batch_id TEXT PRIMARY KEY,
                window_start TEXT NOT NULL,
                window_end TEXT NOT NULL,
                model_name TEXT NOT NULL,
                prompt_version TEXT NOT NULL,
                topic_count INTEGER NOT NULL DEFAULT 0,
                metadata_json TEXT NOT NULL,
                raw_payload_json TEXT NOT NULL,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS selected_topics (
                topic_id TEXT PRIMARY KEY,
                batch_id TEXT NOT NULL,
                importance_rank INTEGER NOT NULL,
                topic_name TEXT NOT NULL,
                topic_score REAL NOT NULL,
                reason_summary TEXT NOT NULL,
                representative_article_id TEXT,
                article_ids_json TEXT NOT NULL,
                metadata_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                FOREIGN KEY (batch_id) REFERENCES topic_selection_runs(batch_id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS topic_deep_research_results (
                topic_id TEXT PRIMARY KEY,
                batch_id TEXT NOT NULL,
                topic_name TEXT NOT NULL,
                model_name TEXT NOT NULL,
                prompt_version TEXT NOT NULL,
                representative_article_id TEXT,
                angle_summary TEXT NOT NULL,
                key_points_json TEXT NOT NULL,
                timeline_json TEXT NOT NULL,
                notable_numbers_json TEXT NOT NULL,
                source_article_ids_json TEXT NOT NULL,
                source_asset_ids_json TEXT NOT NULL,
                risk_flags_json TEXT NOT NULL,
                recommended_focus TEXT NOT NULL,
                raw_payload_json TEXT NOT NULL,
                metadata_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                FOREIGN KEY (batch_id) REFERENCES topic_selection_runs(batch_id) ON DELETE CASCADE
            );

            CREATE INDEX IF NOT EXISTS idx_selected_topics_batch_id
            ON selected_topics(batch_id);

            CREATE INDEX IF NOT EXISTS idx_topic_deep_research_batch_id
            ON topic_deep_research_results(batch_id);

            CREATE TABLE IF NOT EXISTS source_collection_windows (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                window_start TEXT NOT NULL,
                window_end TEXT NOT NULL,
                status TEXT NOT NULL,
                item_count INTEGER NOT NULL DEFAULT 0,
                inserted_count INTEGER NOT NULL DEFAULT 0,
                duplicate_count INTEGER NOT NULL DEFAULT 0,
                error_count INTEGER NOT NULL DEFAULT 0,
                errors_json TEXT NOT NULL,
                created_at TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_source_collection_windows_range
            ON source_collection_windows(window_start, window_end, status);
            """
        )
        self._ensure_source_items_column("excerpt_text", "TEXT")
        self._connection.commit()

    def _ensure_source_items_column(self, column_name: str, column_sql: str) -> None:
        rows = self._connection.execute("PRAGMA table_info(source_items)").fetchall()
        existing_columns = {str(row["name"]) for row in rows}
        if column_name in existing_columns:
            return
        self._connection.execute(f"ALTER TABLE source_items ADD COLUMN {column_name} {column_sql}")

    def find_duplicate(self, candidate: PersistedSourceItem) -> DuplicateCandidate | None:
        row = self._connection.execute(
            """
            SELECT id
            FROM source_items
            WHERE source_type = ?
              AND source_item_type = ?
              AND source_url = ?
            LIMIT 1
            """,
            (
                candidate.item.source_type,
                candidate.item.source_item_type,
                candidate.item.source_url,
            ),
        ).fetchone()
        if row:
            return DuplicateCandidate(existing_item_id=str(row["id"]), reason="same_source_identity")

        if candidate.item.source_external_id:
            row = self._connection.execute(
                """
                SELECT id
                FROM source_items
                WHERE source_type = ?
                  AND source_item_type = ?
                  AND source_external_id = ?
                LIMIT 1
                """,
                (
                    candidate.item.source_type,
                    candidate.item.source_item_type,
                    candidate.item.source_external_id,
                ),
            ).fetchone()
            if row:
                return DuplicateCandidate(existing_item_id=str(row["id"]), reason="same_source_identity")

        row = self._connection.execute(
            """
            SELECT id
            FROM source_items
            WHERE content_hash = ?
            LIMIT 1
            """,
            (candidate.item.content_hash,),
        ).fetchone()
        if row:
            return DuplicateCandidate(existing_item_id=str(row["id"]), reason="same_content_hash")
        return None

    def insert(self, candidate: PersistedSourceItem) -> None:
        self._connection.execute(
            """
            INSERT INTO source_items (
                id, source_type, source_item_type, source_url, source_external_id,
                title, body_text, author_name, published_at, collected_at,
                language_code, engagement_view_count, engagement_like_count,
                engagement_comment_count, engagement_share_count, status,
                raw_payload, content_hash, created_at, updated_at, excerpt_text
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                candidate.item.id,
                candidate.item.source_type,
                candidate.item.source_item_type,
                candidate.item.source_url,
                candidate.item.source_external_id,
                candidate.item.title,
                candidate.item.body_text,
                candidate.item.author_name,
                _serialize_datetime(candidate.item.published_at),
                _serialize_datetime(candidate.item.collected_at),
                candidate.item.language_code,
                candidate.item.engagement_view_count,
                candidate.item.engagement_like_count,
                candidate.item.engagement_comment_count,
                candidate.item.engagement_share_count,
                candidate.item.status,
                json.dumps(candidate.item.raw_payload, ensure_ascii=False, sort_keys=True),
                candidate.item.content_hash,
                _serialize_datetime(candidate.item.created_at),
                _serialize_datetime(candidate.item.updated_at),
                candidate.item.excerpt_text,
            ),
        )
        self._connection.executemany(
            """
            INSERT INTO source_assets (
                id, source_item_id, asset_type, origin_url, storage_path,
                mime_type, width, height, file_size_bytes, sort_order,
                vision_caption, ocr_text, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    asset.id,
                    asset.source_item_id,
                    asset.asset_type,
                    asset.origin_url,
                    asset.storage_path,
                    asset.mime_type,
                    asset.width,
                    asset.height,
                    asset.file_size_bytes,
                    asset.sort_order,
                    asset.vision_caption,
                    asset.ocr_text,
                    _serialize_datetime(asset.created_at),
                )
                for asset in candidate.assets
            ],
        )
        self._connection.commit()

    def list_items(self) -> list[PersistedSourceItem]:
        item_rows = self._connection.execute(
            """
            SELECT *
            FROM source_items
            ORDER BY collected_at ASC, id ASC
            """
        ).fetchall()
        asset_rows = self._connection.execute(
            """
            SELECT *
            FROM source_assets
            ORDER BY source_item_id ASC, sort_order ASC, id ASC
            """
        ).fetchall()

        assets_by_item_id: dict[str, list[SourceAssetRecord]] = {}
        for row in asset_rows:
            asset = SourceAssetRecord(
                id=str(row["id"]),
                source_item_id=str(row["source_item_id"]),
                asset_type=str(row["asset_type"]),
                origin_url=str(row["origin_url"]),
                storage_path=row["storage_path"],
                mime_type=row["mime_type"],
                width=row["width"],
                height=row["height"],
                file_size_bytes=row["file_size_bytes"],
                sort_order=int(row["sort_order"]),
                vision_caption=row["vision_caption"],
                ocr_text=row["ocr_text"],
                created_at=_deserialize_datetime(row["created_at"]) or _utc_now(),
            )
            assets_by_item_id.setdefault(asset.source_item_id, []).append(asset)

        persisted_items: list[PersistedSourceItem] = []
        for row in item_rows:
            item_id = str(row["id"])
            item = SourceItemRecord(
                id=item_id,
                source_type=str(row["source_type"]),
                source_item_type=str(row["source_item_type"]),
                source_url=str(row["source_url"]),
                source_external_id=row["source_external_id"],
                title=row["title"],
                body_text=row["body_text"],
                author_name=row["author_name"],
                published_at=_deserialize_datetime(row["published_at"]),
                collected_at=_deserialize_datetime(row["collected_at"]) or _utc_now(),
                language_code=str(row["language_code"]),
                engagement_view_count=int(row["engagement_view_count"]),
                engagement_like_count=int(row["engagement_like_count"]),
                engagement_comment_count=int(row["engagement_comment_count"]),
                engagement_share_count=int(row["engagement_share_count"]),
                status=str(row["status"]),
                raw_payload=json.loads(str(row["raw_payload"])),
                content_hash=str(row["content_hash"]),
                created_at=_deserialize_datetime(row["created_at"]) or _utc_now(),
                updated_at=_deserialize_datetime(row["updated_at"]) or _utc_now(),
                excerpt_text=row["excerpt_text"],
            )
            persisted_items.append(
                PersistedSourceItem(
                    item=item,
                    assets=assets_by_item_id.get(item_id, []),
                )
            )
        return persisted_items

    def count_rows(self, table_name: str) -> int:
        row = self._connection.execute(f"SELECT COUNT(*) AS count FROM {table_name}").fetchone()
        return int(row["count"])

    def list_completed_collection_windows(self) -> list[CollectionWindowRecord]:
        rows = self._connection.execute(
            """
            SELECT window_start, window_end, status, item_count, inserted_count,
                   duplicate_count, error_count, created_at
            FROM source_collection_windows
            WHERE status = 'completed'
            ORDER BY window_start ASC, window_end ASC
            """
        ).fetchall()
        records: list[CollectionWindowRecord] = []
        for row in rows:
            window_start = _deserialize_datetime(row["window_start"])
            window_end = _deserialize_datetime(row["window_end"])
            created_at = _deserialize_datetime(row["created_at"])
            if window_start is None or window_end is None:
                continue
            records.append(
                CollectionWindowRecord(
                    window_start=window_start,
                    window_end=window_end,
                    status=str(row["status"]),
                    item_count=int(row["item_count"]),
                    inserted_count=int(row["inserted_count"]),
                    duplicate_count=int(row["duplicate_count"]),
                    error_count=int(row["error_count"]),
                    created_at=created_at or _utc_now(),
                )
            )
        return records

    def save_collection_window(
        self,
        *,
        window_start: datetime,
        window_end: datetime,
        status: str,
        item_count: int,
        inserted_count: int,
        duplicate_count: int,
        errors: list[str],
    ) -> None:
        self._connection.execute(
            """
            INSERT INTO source_collection_windows (
                window_start, window_end, status, item_count, inserted_count,
                duplicate_count, error_count, errors_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                _serialize_datetime(window_start),
                _serialize_datetime(window_end),
                status,
                item_count,
                inserted_count,
                duplicate_count,
                len(errors),
                _json_dumps(errors),
                _serialize_datetime(_utc_now()),
            ),
        )
        self._connection.commit()

    def save_topic_selection_result(
        self,
        batch_input: BatchIssueSelectionInput,
        selection_result: BatchIssueSelectionResult,
    ) -> None:
        created_at = selection_result.created_at or _utc_now()
        self._connection.execute(
            """
            INSERT OR REPLACE INTO topic_selection_runs (
                batch_id, window_start, window_end, model_name, prompt_version,
                topic_count, metadata_json, raw_payload_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                batch_input.batch_id,
                _serialize_datetime(batch_input.window_start),
                _serialize_datetime(batch_input.window_end),
                selection_result.model_name,
                selection_result.prompt_version,
                len(selection_result.topics),
                _json_dumps(batch_input.metadata),
                _json_dumps(selection_result.raw_payload),
                _serialize_datetime(created_at),
            ),
        )
        self._connection.execute(
            """
            DELETE FROM selected_topics
            WHERE batch_id = ?
            """,
            (batch_input.batch_id,),
        )
        self._connection.executemany(
            """
            INSERT INTO selected_topics (
                topic_id, batch_id, importance_rank, topic_name, topic_score,
                reason_summary, representative_article_id, article_ids_json,
                metadata_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    topic.topic_id,
                    batch_input.batch_id,
                    topic.importance_rank,
                    topic.topic_name,
                    topic.topic_score,
                    topic.reason_summary,
                    topic.representative_article_id,
                    _json_dumps(topic.article_ids),
                    _json_dumps(topic.metadata),
                    _serialize_datetime(created_at),
                )
                for topic in selection_result.topics
            ],
        )
        self._connection.commit()

    def save_topic_deep_research_result(self, result: TopicDeepResearchResult) -> None:
        created_at = result.created_at or _utc_now()
        self._connection.execute(
            """
            INSERT OR REPLACE INTO topic_deep_research_results (
                topic_id, batch_id, topic_name, model_name, prompt_version,
                representative_article_id, angle_summary, key_points_json,
                timeline_json, notable_numbers_json, source_article_ids_json,
                source_asset_ids_json, risk_flags_json, recommended_focus,
                raw_payload_json, metadata_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                result.topic_id,
                result.batch_id,
                result.topic_name,
                result.model_name,
                result.prompt_version,
                result.representative_article_id,
                result.angle_summary,
                _json_dumps(result.key_points),
                _json_dumps(result.timeline),
                _json_dumps(result.notable_numbers),
                _json_dumps(result.source_article_ids),
                _json_dumps(result.source_asset_ids),
                _json_dumps(result.risk_flags),
                result.recommended_focus,
                _json_dumps(result.raw_payload),
                _json_dumps(result.metadata),
                _serialize_datetime(created_at),
            ),
        )
        self._connection.commit()

    def close(self) -> None:
        self._connection.close()

    def __enter__(self) -> SQLiteSourceItemRepository:
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()


class SourceItemIngestionService:
    def __init__(
        self,
        repository: SourceItemRepository | None = None,
        transformer: SourceItemTransformer | None = None,
    ) -> None:
        self.repository = repository or InMemorySourceItemRepository()
        self.transformer = transformer or SourceItemTransformer()

    def ingest(self, items: list[CollectedItem]) -> IngestionResult:
        result = IngestionResult()
        for item in items:
            candidate = self.transformer.transform(item)
            duplicate = self.repository.find_duplicate(candidate)
            if duplicate:
                candidate.item.status = "filtered"
                result.duplicates.append(candidate)
                result.duplicate_reasons.append(
                    f"{candidate.item.source_url} -> {duplicate.reason} ({duplicate.existing_item_id})"
                )
                continue

            self.repository.insert(candidate)
            result.inserted.append(candidate)
        return result

    def export_rows(self) -> dict[str, list[dict]]:
        source_items: list[dict] = []
        source_assets: list[dict] = []
        for persisted in self.repository.list_items():
            source_items.append(asdict(persisted.item))
            source_assets.extend(asdict(asset) for asset in persisted.assets)
        return {
            "source_items": source_items,
            "source_assets": source_assets,
        }


def _serialize_datetime(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.isoformat()


def _deserialize_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _json_dumps(value: object) -> str:
    return json.dumps(_json_ready(value), ensure_ascii=False, sort_keys=True)


def _json_ready(value: object) -> object:
    if is_dataclass(value):
        return _json_ready(asdict(value))
    if isinstance(value, dict):
        return {str(key): _json_ready(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_json_ready(item) for item in value]
    if isinstance(value, tuple):
        return [_json_ready(item) for item in value]
    if isinstance(value, datetime):
        return _serialize_datetime(value)
    return value
