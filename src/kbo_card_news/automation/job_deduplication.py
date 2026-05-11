from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from kbo_card_news.automation.job_state import AutomationJob, AutomationJobRepository


@dataclass(slots=True)
class JobFingerprint:
    topic_fingerprint: str
    representative_article_url: str
    article_url_fingerprint: str
    normalized_topic_key: str
    article_urls: list[str] = field(default_factory=list)


def normalize_url(value: str | None) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    parts = urlsplit(text)
    query = [
        (key, value)
        for key, value in parse_qsl(parts.query, keep_blank_values=True)
        if not key.lower().startswith("utm_")
    ]
    path = re.sub(r"/+$", "", parts.path)
    return urlunsplit(
        (
            parts.scheme.lower(),
            parts.netloc.lower(),
            path,
            urlencode(query, doseq=True),
            "",
        )
    )


def normalize_topic_key(value: str) -> str:
    tokens = re.findall(r"[0-9A-Za-z가-힣]+", value.lower())
    return "-".join(tokens[:10])


def hash_parts(parts: list[str]) -> str:
    normalized = [part for part in parts if part]
    if not normalized:
        return ""
    return hashlib.sha1("\n".join(normalized).encode("utf-8")).hexdigest()[:16]


def fingerprint_metadata(fingerprint: JobFingerprint, *, duplicate_lookback_hours: int) -> dict[str, object]:
    return {
        "topic_fingerprint": fingerprint.topic_fingerprint,
        "representative_article_url": fingerprint.representative_article_url,
        "article_url_fingerprint": fingerprint.article_url_fingerprint,
        "normalized_topic_key": fingerprint.normalized_topic_key,
        "article_urls": fingerprint.article_urls,
        "duplicate_lookback_hours": duplicate_lookback_hours,
        "duplicate_match_reason": "",
    }


def find_duplicate_job_by_fingerprint(
    repository: AutomationJobRepository,
    *,
    topic_id: str,
    fingerprint: JobFingerprint,
    lookback_hours: int,
    limit: int = 300,
) -> tuple[AutomationJob | None, str]:
    existing = repository.get_job_by_topic_id(topic_id)
    if existing is not None:
        return existing, "same_topic_id"

    candidate_urls = set(fingerprint.article_urls)
    for job in repository.list_recent_jobs(hours=lookback_hours, limit=limit):
        metadata = job.metadata or {}
        if fingerprint.topic_fingerprint and metadata.get("topic_fingerprint") == fingerprint.topic_fingerprint:
            return job, "same_topic_fingerprint"
        if (
            fingerprint.article_url_fingerprint
            and metadata.get("article_url_fingerprint") == fingerprint.article_url_fingerprint
        ):
            return job, "same_article_url_fingerprint"
        if (
            fingerprint.representative_article_url
            and metadata.get("representative_article_url") == fingerprint.representative_article_url
        ):
            return job, "same_representative_article_url"

        existing_urls = {
            normalize_url(article.source_url)
            for article in job.articles
            if normalize_url(article.source_url)
        }
        if candidate_urls and existing_urls and candidate_urls.intersection(existing_urls):
            return job, "article_url_overlap"
        if fingerprint.normalized_topic_key and metadata.get("normalized_topic_key") == fingerprint.normalized_topic_key:
            return job, "same_normalized_topic_key"
    return None, ""
