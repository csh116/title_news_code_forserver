from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from kbo_card_news.models.issue import TopicCandidate
from kbo_card_news.workflows.approval_paths import OUTPUT_ROOT


def completed_topic_registry_path(*, registry_path: str | Path | None = None) -> Path:
    if registry_path:
        return Path(registry_path).expanduser()
    return OUTPUT_ROOT / "completed_topic_registry.json"


def load_completed_topic_registry(*, registry_path: str | Path | None = None) -> dict[str, Any]:
    path = completed_topic_registry_path(registry_path=registry_path)
    if not path.exists():
        return {"topics": []}
    text = path.read_text(encoding="utf-8").strip()
    if not text:
        return {"topics": []}
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return {"topics": []}
    if not isinstance(payload, dict):
        return {"topics": []}
    topics = payload.get("topics")
    if not isinstance(topics, list):
        payload["topics"] = []
    return payload


def append_completed_topic_entries(
    entries: list[dict[str, Any]],
    *,
    registry_path: str | Path | None = None,
) -> Path:
    path = completed_topic_registry_path(registry_path=registry_path)
    payload = load_completed_topic_registry(registry_path=path)
    existing_topics = payload.get("topics")
    if not isinstance(existing_topics, list):
        existing_topics = []

    existing_fingerprint_indexes = {
        str(item.get("topic_fingerprint") or "").strip(): index
        for index, item in enumerate(existing_topics)
        if isinstance(item, dict) and str(item.get("topic_fingerprint") or "").strip()
    }
    for entry in entries:
        fingerprint = str(entry.get("topic_fingerprint") or "").strip()
        if not fingerprint:
            continue
        existing_index = existing_fingerprint_indexes.get(fingerprint)
        if existing_index is not None:
            existing_entry = existing_topics[existing_index]
            if (
                isinstance(existing_entry, dict)
                and not _has_existing_final_manifest(existing_entry)
                and _has_existing_final_manifest(entry)
            ):
                existing_topics[existing_index] = entry
            continue
        existing_topics.append(entry)
        existing_fingerprint_indexes[fingerprint] = len(existing_topics) - 1

    payload["topics"] = existing_topics
    payload["updated_at"] = datetime.now(timezone.utc).isoformat()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def build_completed_topic_entry(
    *,
    topic_name: str,
    issue_id: str,
    representative_article_id: str | None,
    article_ids: list[str],
    approval_run_dir: str,
    approval_manifest_path: str,
    final_manifest_path: str,
) -> dict[str, Any]:
    normalized_name = normalize_topic_name(topic_name)
    fingerprint = topic_fingerprint(
        representative_article_id=representative_article_id,
        article_ids=article_ids,
        normalized_topic_name=normalized_name,
    )
    return {
        "topic_name": topic_name,
        "normalized_topic_name": normalized_name,
        "topic_fingerprint": fingerprint,
        "issue_id": issue_id,
        "representative_article_id": representative_article_id or "",
        "article_ids": list(article_ids),
        "approval_run_dir": approval_run_dir,
        "approval_manifest_path": approval_manifest_path,
        "final_manifest_path": final_manifest_path,
        "completed_at": datetime.now(timezone.utc).isoformat(),
    }


def topic_fingerprint(
    *,
    representative_article_id: str | None,
    article_ids: list[str],
    normalized_topic_name: str,
) -> str:
    representative = str(representative_article_id or "").strip()
    if representative:
        return f"rep:{representative}"
    normalized_article_ids = sorted({str(article_id).strip() for article_id in article_ids if str(article_id).strip()})
    if normalized_article_ids:
        return "articles:" + "|".join(normalized_article_ids)
    return f"name:{normalized_topic_name}"


def normalize_topic_name(value: str) -> str:
    text = re.sub(r"\s+", " ", str(value or "").strip().lower())
    text = re.sub(r"[^0-9a-z가-힣 ]+", "", text)
    return text.strip()


def is_completed_topic(topic: TopicCandidate, completed_topics: list[dict[str, Any]]) -> bool:
    candidate_rep = str(topic.representative_article_id or "").strip()
    candidate_article_ids = {str(article_id).strip() for article_id in topic.article_ids if str(article_id).strip()}
    candidate_name = normalize_topic_name(topic.topic_name)

    for item in completed_topics:
        if not isinstance(item, dict):
            continue
        if not _has_existing_final_manifest(item):
            continue
        completed_rep = str(item.get("representative_article_id") or "").strip()
        if candidate_rep and completed_rep and candidate_rep == completed_rep:
            return True

        completed_article_ids = {
            str(article_id).strip()
            for article_id in item.get("article_ids", [])
            if str(article_id).strip()
        }
        if candidate_article_ids and completed_article_ids and candidate_article_ids & completed_article_ids:
            return True

        completed_name = str(item.get("normalized_topic_name") or "").strip()
        if candidate_name and completed_name and candidate_name == completed_name:
            return True

    return False


def _has_existing_final_manifest(item: dict[str, Any]) -> bool:
    final_manifest_path = str(item.get("final_manifest_path") or "").strip()
    if not final_manifest_path:
        return True
    return Path(final_manifest_path).expanduser().exists()
