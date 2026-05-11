from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


@dataclass(slots=True)
class MediaAsset:
    asset_type: str
    origin_url: str
    mime_type: str | None = None
    width: int | None = None
    height: int | None = None
    sort_order: int = 0
    caption: str | None = None


@dataclass(slots=True)
class CollectedItem:
    source_type: str
    source_item_type: str
    source_url: str
    source_external_id: str | None
    title: str | None
    body_text: str | None
    author_name: str | None
    published_at: datetime | None
    collected_at: datetime
    assets: list[MediaAsset] = field(default_factory=list)
    raw_payload: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)
    excerpt_text: str | None = None
