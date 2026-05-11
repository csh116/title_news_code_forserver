from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from kbo_card_news.automation.job_state import (
    AutomationJob,
    AutomationJobArticle,
    AutomationJobRepository,
    build_job_id,
    utc_now,
)
from kbo_card_news.automation.pipeline_runner import CandidateGenerationResult, generate_topic_candidates
from kbo_card_news.automation.topic_ranker import rank_candidate, rank_result_to_metadata


DEFAULT_WATCH_CANDIDATE_COUNT = 10
DEFAULT_WATCH_MAX_CANDIDATES = 10
DEFAULT_WATCHER_INTERVAL_SECONDS = 1800
PEAK_WATCHER_INTERVAL_SECONDS = 900
DEFAULT_DUPLICATE_LOOKBACK_HOURS = 24


@dataclass(slots=True)
class WatchOnceResult:
    choice_json_path: Path
    created_jobs: list[AutomationJob] = field(default_factory=list)
    duplicate_jobs: list[AutomationJob] = field(default_factory=list)
    skipped_count: int = 0


@dataclass(slots=True)
class CandidateFingerprint:
    topic_fingerprint: str
    representative_article_url: str
    article_url_fingerprint: str
    normalized_topic_key: str
    article_urls: list[str] = field(default_factory=list)


def watch_once(
    *,
    repository: AutomationJobRepository,
    choice_json_path: str | Path | None = None,
    approval_run_dir: str | Path | None = None,
    window_start_kst: str | None = None,
    window_end_kst: str | None = None,
    candidate_count: int | None = None,
    max_candidates: int | None = None,
    initial_status: str = "detected",
    selection_engine: str = "heuristic",
) -> WatchOnceResult:
    candidate_result: CandidateGenerationResult | None = None
    if choice_json_path is None:
        candidate_result = generate_topic_candidates(
            approval_run_dir=approval_run_dir,
            window_start_kst=window_start_kst,
            window_end_kst=window_end_kst,
            candidate_count=candidate_count,
            selection_engine=selection_engine,
        )
        choice_path = candidate_result.choice_json_path
    else:
        choice_path = Path(choice_json_path).expanduser()

    payload = json.loads(choice_path.read_text(encoding="utf-8"))
    candidates = payload.get("candidates") or []
    if not isinstance(candidates, list):
        raise ValueError(f"candidates must be a list in {choice_path}")

    created: list[AutomationJob] = []
    duplicates: list[AutomationJob] = []
    skipped_count = 0
    limited_candidates = candidates[:max_candidates] if max_candidates is not None else candidates
    for index, candidate in enumerate(limited_candidates, start=1):
        if not isinstance(candidate, dict):
            skipped_count += 1
            continue
        topic_id = str(candidate.get("topic_id") or "").strip()
        topic_name = str(candidate.get("topic_name") or "").strip()
        if not topic_id or not topic_name:
            skipped_count += 1
            continue
        fingerprint = _candidate_fingerprint(candidate)
        existing, duplicate_reason = _find_duplicate_job(
            repository,
            topic_id=topic_id,
            fingerprint=fingerprint,
        )
        if existing is not None:
            repository.record_event(
                existing.job_id,
                "duplicate_candidate_seen",
                message=f"duplicate candidate seen by watcher: {duplicate_reason}",
                metadata={
                    "choice_json_path": str(choice_path),
                    "candidate_index": index,
                    **_fingerprint_metadata(fingerprint),
                    "duplicate_match_reason": duplicate_reason,
                },
            )
            duplicates.append(existing)
            continue
        job = _job_from_candidate(
            candidate,
            choice_json_path=choice_path,
            candidate_index=index,
            initial_status=initial_status,
            fingerprint=fingerprint,
        )
        created.append(repository.create_job(job))

    return WatchOnceResult(
        choice_json_path=choice_path,
        created_jobs=created,
        duplicate_jobs=duplicates,
        skipped_count=skipped_count,
    )


def _job_from_candidate(
    candidate: dict[str, Any],
    *,
    choice_json_path: Path,
    candidate_index: int,
    initial_status: str,
    fingerprint: CandidateFingerprint,
) -> AutomationJob:
    metadata = candidate.get("metadata") if isinstance(candidate.get("metadata"), dict) else {}
    rank_result = rank_candidate(candidate)
    topic_id = str(candidate.get("topic_id") or "").strip()
    topic_name = str(candidate.get("topic_name") or "").strip()
    articles = _articles_from_candidate(candidate)
    return AutomationJob(
        job_id=_candidate_job_id(topic_id=topic_id, candidate_index=candidate_index),
        topic_id=topic_id,
        topic_name=topic_name,
        status=initial_status,  # type: ignore[arg-type]
        notification_level=rank_result.notification_level,
        virality_potential_score=rank_result.virality_potential_score,
        account_fit_score=rank_result.account_fit_score,
        recommendation_summary=rank_result.recommendation_summary,
        metadata={
            "source": "watch_once",
            "choice_json_path": str(choice_json_path),
            "candidate_index": candidate_index,
            "importance_rank": candidate.get("importance_rank"),
            "representative_article_id": candidate.get("representative_article_id"),
            "article_ids": candidate.get("article_ids") or [],
            "original_topic_score": candidate.get("topic_score"),
            "original_reason_summary": str(candidate.get("reason_summary") or "").strip(),
            "candidate_metadata": metadata,
            **_fingerprint_metadata(fingerprint),
            **rank_result_to_metadata(rank_result),
        },
        articles=articles,
        created_at=utc_now(),
        updated_at=utc_now(),
    )


def _find_duplicate_job(
    repository: AutomationJobRepository,
    *,
    topic_id: str,
    fingerprint: CandidateFingerprint,
) -> tuple[AutomationJob | None, str]:
    existing = repository.get_job_by_topic_id(topic_id)
    if existing is not None:
        return existing, "same_topic_id"

    candidate_urls = set(fingerprint.article_urls)
    for job in repository.list_recent_jobs(hours=DEFAULT_DUPLICATE_LOOKBACK_HOURS, limit=300):
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
            _normalize_url(article.source_url)
            for article in job.articles
            if _normalize_url(article.source_url)
        }
        if candidate_urls and existing_urls and candidate_urls.intersection(existing_urls):
            return job, "article_url_overlap"
        if fingerprint.normalized_topic_key and metadata.get("normalized_topic_key") == fingerprint.normalized_topic_key:
            return job, "same_normalized_topic_key"
    return None, ""


def _candidate_fingerprint(candidate: dict[str, Any]) -> CandidateFingerprint:
    topic_name = str(candidate.get("topic_name") or "").strip()
    representative_article_id = str(candidate.get("representative_article_id") or "").strip()
    article_ids = [str(value).strip() for value in candidate.get("article_ids") or [] if str(value).strip()]
    articles = _articles_from_candidate(candidate)
    article_urls = sorted(
        {
            _normalize_url(article.source_url)
            for article in articles
            if _normalize_url(article.source_url)
        }
    )
    representative_url = _representative_article_url(
        articles,
        representative_article_id=representative_article_id,
    )
    normalized_topic_key = _normalize_topic_key(topic_name)
    article_url_fingerprint = _hash_parts(article_urls)
    stable_basis = representative_url or article_url_fingerprint or _hash_parts(article_ids) or normalized_topic_key
    topic_fingerprint = f"watch:{stable_basis}" if stable_basis else ""
    return CandidateFingerprint(
        topic_fingerprint=topic_fingerprint,
        representative_article_url=representative_url,
        article_url_fingerprint=article_url_fingerprint,
        normalized_topic_key=normalized_topic_key,
        article_urls=article_urls,
    )


def _fingerprint_metadata(fingerprint: CandidateFingerprint) -> dict[str, Any]:
    return {
        "topic_fingerprint": fingerprint.topic_fingerprint,
        "representative_article_url": fingerprint.representative_article_url,
        "article_url_fingerprint": fingerprint.article_url_fingerprint,
        "normalized_topic_key": fingerprint.normalized_topic_key,
        "article_urls": fingerprint.article_urls,
        "duplicate_lookback_hours": DEFAULT_DUPLICATE_LOOKBACK_HOURS,
        "duplicate_match_reason": "",
    }


def _articles_from_candidate(candidate: dict[str, Any]) -> list[AutomationJobArticle]:
    metadata = candidate.get("metadata") if isinstance(candidate.get("metadata"), dict) else {}
    publication_summary = (
        metadata.get("article_publication_summary")
        if isinstance(metadata.get("article_publication_summary"), dict)
        else {}
    )
    raw_articles = publication_summary.get("articles") if isinstance(publication_summary, dict) else []
    if not isinstance(raw_articles, list):
        return []
    articles: list[AutomationJobArticle] = []
    seen_urls: set[str] = set()
    for raw in raw_articles:
        if not isinstance(raw, dict):
            continue
        source_url = str(raw.get("source_url") or "").strip()
        if source_url and source_url in seen_urls:
            continue
        if source_url:
            seen_urls.add(source_url)
        articles.append(
            AutomationJobArticle(
                article_id=_optional_string(raw.get("article_id")),
                title=str(raw.get("title") or ""),
                source_type=str(raw.get("source_type") or ""),
                source_url=source_url,
                published_at=_optional_string(raw.get("published_at")),
            )
        )
    return articles


def _candidate_job_id(*, topic_id: str, candidate_index: int) -> str:
    compact = re.sub(r"[^0-9A-Za-z가-힣_-]+", "-", topic_id).strip("-")
    if compact:
        return compact[:80]
    return build_job_id(sequence=candidate_index)


def _representative_article_url(
    articles: list[AutomationJobArticle],
    *,
    representative_article_id: str,
) -> str:
    if representative_article_id:
        for article in articles:
            if article.article_id == representative_article_id:
                return _normalize_url(article.source_url)
    if articles:
        return _normalize_url(articles[0].source_url)
    return ""


def _normalize_url(value: str | None) -> str:
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


def _normalize_topic_key(value: str) -> str:
    tokens = re.findall(r"[0-9A-Za-z가-힣]+", value.lower())
    return "-".join(tokens[:10])


def _hash_parts(parts: list[str]) -> str:
    normalized = [part for part in parts if part]
    if not normalized:
        return ""
    return hashlib.sha1("\n".join(normalized).encode("utf-8")).hexdigest()[:16]


def _optional_string(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None
