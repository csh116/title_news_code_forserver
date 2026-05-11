from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from kbo_card_news.automation.issue_keywords import (
    ALL_TEAMS,
    FUTURES_KEYWORDS,
    KEYWORD_GROUPS,
    LOW_PRIORITY_KEYWORDS,
    PRIMARY_TEAMS,
    SECONDARY_TEAMS,
    TEAM_ALIASES,
)
from kbo_card_news.automation.job_deduplication import (
    JobFingerprint,
    find_duplicate_job_by_fingerprint,
    fingerprint_metadata,
    hash_parts,
    normalize_topic_key,
    normalize_url,
)
from kbo_card_news.automation.job_state import (
    AUTOMATION_OUTPUT_DIR,
    AutomationJob,
    AutomationJobArticle,
    AutomationJobRepository,
    utc_now,
)
from kbo_card_news.automation.news_collection import build_news_collectors
from kbo_card_news.collectors.service import CollectorService
from kbo_card_news.config.env import load_default_env
from kbo_card_news.pipeline.storage import (
    PersistedSourceItem,
    SQLiteSourceItemRepository,
    SourceItemIngestionService,
)

ROOT_DIR = Path(__file__).resolve().parents[3]
OUTPUT_ROOT = ROOT_DIR / "outputs"
SOURCE_DB_PATH = OUTPUT_ROOT / "source_collection.db"
KST = timezone(timedelta(hours=9))


@dataclass(slots=True)
class FreshIssueDetectorConfig:
    collection_window_minutes: int = 10
    context_window_hours: int = 24
    duplicate_lookback_hours: int = 72
    min_issue_score: float = 65.0
    max_jobs: int = 5
    gemini_review_enabled: bool = True
    context_limit: int = 500


@dataclass(slots=True)
class FreshIssueCandidate:
    issue_id: str
    topic_name: str
    representative_article_id: str
    fresh_articles: list[PersistedSourceItem]
    context_articles: list[PersistedSourceItem]
    matched_teams: list[str]
    matched_keywords: list[str]
    keyword_groups: list[str]
    issue_score: float
    gemini_decision: str | None
    gemini_confidence: float | None
    notification_level: str
    reasons: list[str]
    risk_flags: list[str]
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class FreshWatchResult:
    collection_window_start: datetime
    collection_window_end: datetime
    context_window_start: datetime
    context_window_end: datetime
    collected_count: int
    inserted_count: int
    duplicate_count: int
    fresh_article_count: int
    context_article_count: int
    candidate_count: int
    created_jobs: list[AutomationJob]
    duplicate_jobs: list[AutomationJob]
    skipped_count: int
    collector_errors: list[str]
    report_path: Path
    choice_json_path: Path
    candidates: list[FreshIssueCandidate] = field(default_factory=list)


@dataclass(slots=True)
class IssueGroup:
    key: str
    fresh_articles: list[PersistedSourceItem]
    context_articles: list[PersistedSourceItem]
    matched_teams: list[str]
    matched_keywords: list[str]
    keyword_groups: list[str]
    player_candidate: str


def watch_fresh_once(
    *,
    job_repository: AutomationJobRepository,
    source_db_path: str | Path = SOURCE_DB_PATH,
    config: FreshIssueDetectorConfig | None = None,
    now: datetime | None = None,
) -> FreshWatchResult:
    config = config or FreshIssueDetectorConfig()
    current = _normalize_now(now)
    collection_start = current - timedelta(minutes=max(1, int(config.collection_window_minutes)))
    context_start = current - timedelta(hours=max(1, int(config.context_window_hours)))
    run_dir = _fresh_run_dir(current)
    run_dir.mkdir(parents=True, exist_ok=True)
    choice_json_path = run_dir / "topic_selection_choice.json"
    report_path = run_dir / "fresh_watch_report.json"

    with SQLiteSourceItemRepository(str(Path(source_db_path).expanduser())) as source_repository:
        collector_result = CollectorService(build_news_collectors()).collect_all(
            window_start=collection_start,
            window_end=current,
        )
        ingestion_result = SourceItemIngestionService(repository=source_repository).ingest(collector_result.items)
        source_repository.save_collection_window(
            window_start=collection_start,
            window_end=current,
            status="completed" if not collector_result.errors else "partial",
            item_count=len(collector_result.items),
            inserted_count=len(ingestion_result.inserted),
            duplicate_count=len(ingestion_result.duplicates),
            errors=collector_result.errors,
        )
        context_articles = source_repository.list_items_published_between(
            window_start=context_start,
            window_end=current,
            limit=config.context_limit,
        )

    fresh_articles = ingestion_result.inserted
    candidates = _build_candidates(
        fresh_articles=fresh_articles,
        context_articles=context_articles,
        now=current,
        config=config,
    )
    if config.gemini_review_enabled and candidates:
        _apply_gemini_review(candidates)

    created_jobs: list[AutomationJob] = []
    duplicate_jobs: list[AutomationJob] = []
    skipped_count = 0
    selected_candidates: list[FreshIssueCandidate] = []
    for candidate in candidates:
        if candidate.issue_score < config.min_issue_score:
            skipped_count += 1
            continue
        if config.gemini_review_enabled and candidate.gemini_decision != "approve":
            skipped_count += 1
            continue
        fingerprint = _candidate_fingerprint(candidate)
        existing, duplicate_reason = find_duplicate_job_by_fingerprint(
            job_repository,
            topic_id=candidate.issue_id,
            fingerprint=fingerprint,
            lookback_hours=config.duplicate_lookback_hours,
        )
        if existing is not None:
            job_repository.record_event(
                existing.job_id,
                "duplicate_fresh_issue_seen",
                message=f"duplicate fresh issue seen: {duplicate_reason}",
                metadata={
                    **fingerprint_metadata(fingerprint, duplicate_lookback_hours=config.duplicate_lookback_hours),
                    "duplicate_match_reason": duplicate_reason,
                },
            )
            duplicate_jobs.append(existing)
            continue
        job = _job_from_candidate(
            candidate,
            run_dir=run_dir,
            choice_json_path=choice_json_path,
            fingerprint=fingerprint,
            config=config,
            collection_start=collection_start,
            collection_end=current,
            context_start=context_start,
            context_end=current,
        )
        created_jobs.append(job_repository.create_job(job))
        selected_candidates.append(candidate)
        if len(created_jobs) >= max(1, int(config.max_jobs)):
            break

    _write_choice_json(choice_json_path, candidates=selected_candidates or candidates)
    result = FreshWatchResult(
        collection_window_start=collection_start,
        collection_window_end=current,
        context_window_start=context_start,
        context_window_end=current,
        collected_count=len(collector_result.items),
        inserted_count=len(ingestion_result.inserted),
        duplicate_count=len(ingestion_result.duplicates),
        fresh_article_count=len(fresh_articles),
        context_article_count=len(context_articles),
        candidate_count=len(candidates),
        created_jobs=created_jobs,
        duplicate_jobs=duplicate_jobs,
        skipped_count=skipped_count,
        collector_errors=collector_result.errors,
        report_path=report_path,
        choice_json_path=choice_json_path,
        candidates=candidates,
    )
    _write_report(report_path, result)
    return result


def _build_candidates(
    *,
    fresh_articles: list[PersistedSourceItem],
    context_articles: list[PersistedSourceItem],
    now: datetime,
    config: FreshIssueDetectorConfig,
) -> list[FreshIssueCandidate]:
    if not fresh_articles:
        return []
    groups = _group_articles(fresh_articles=fresh_articles, context_articles=context_articles)
    candidates = [_score_group(group, now=now, config=config) for group in groups]
    candidates.sort(key=lambda value: value.issue_score, reverse=True)
    return candidates


def _group_articles(
    *,
    fresh_articles: list[PersistedSourceItem],
    context_articles: list[PersistedSourceItem],
) -> list[IssueGroup]:
    buckets: dict[str, list[PersistedSourceItem]] = {}
    group_meta: dict[str, tuple[list[str], list[str], list[str], str]] = {}
    for article in fresh_articles:
        text = _article_text(article)
        teams = _matched_teams(text)
        keyword_groups, keywords = _matched_keyword_groups(text)
        player = _player_candidate(text, teams=teams, keywords=keywords)
        team_key = teams[0] if teams else "KBO"
        group_key = f"{team_key}:{keyword_groups[0] if keyword_groups else 'general'}:{player or normalize_topic_key(article.item.title or '')}"
        buckets.setdefault(group_key, []).append(article)
        group_meta[group_key] = (teams, keywords, keyword_groups, player)

    groups: list[IssueGroup] = []
    for key, articles in buckets.items():
        teams, keywords, keyword_groups, player = group_meta[key]
        related_context = [
            item
            for item in context_articles
            if item.item.id not in {article.item.id for article in articles}
            and _is_related_context(item, teams=teams, keywords=keywords, player=player)
        ]
        groups.append(
            IssueGroup(
                key=key,
                fresh_articles=articles,
                context_articles=related_context,
                matched_teams=teams,
                matched_keywords=keywords,
                keyword_groups=keyword_groups,
                player_candidate=player,
            )
        )
    return groups


def _score_group(group: IssueGroup, *, now: datetime, config: FreshIssueDetectorConfig) -> FreshIssueCandidate:
    representative = sorted(
        group.fresh_articles,
        key=lambda item: _effective_time(item),
        reverse=True,
    )[0]
    fresh_count = len(group.fresh_articles)
    source_count = len({article.item.source_type for article in group.fresh_articles})
    context_count = len(group.context_articles)
    score = 0.0
    reasons: list[str] = []
    risk_flags: list[str] = []

    score += 8 if fresh_count == 1 else 15 if fresh_count == 2 else 22
    reasons.append(f"신규 기사 {fresh_count}건")
    if source_count >= 3:
        score += 18
        reasons.append("3개 이상 매체")
    elif source_count == 2:
        score += 10
        reasons.append("2개 매체")

    if context_count >= 5:
        score += 18
        reasons.append("24시간 관련 기사 5건 이상")
    elif context_count >= 3:
        score += 12
        reasons.append("24시간 관련 기사 3건 이상")
    elif context_count == 2:
        score += 6

    strong_group = group.keyword_groups[0] if group.keyword_groups else ""
    keyword_points = {"injury": 35, "controversy": 35, "roster": 25, "record": 18, "drama": 16}
    if strong_group in keyword_points:
        score += keyword_points[strong_group]
        reasons.append(f"{strong_group} 상태 변화 키워드")

    if group.matched_teams:
        if any(team in PRIMARY_TEAMS for team in group.matched_teams):
            score += 12
            reasons.append("1순위 구단 포함")
        elif any(team in SECONDARY_TEAMS for team in group.matched_teams):
            score += 8
        else:
            score += 5
    elif strong_group not in {"controversy"}:
        score -= 8
        risk_flags.append("명확한 팀 주체가 약함")

    age_minutes = max(0.0, (now - _effective_time(representative)).total_seconds() / 60)
    if age_minutes <= 10:
        score += 10
    elif age_minutes <= 30:
        score += 6
    elif age_minutes <= 60:
        score += 3

    combined_text = _article_text_many(group.fresh_articles)
    if _has_any(combined_text, LOW_PRIORITY_KEYWORDS):
        score -= 20
        risk_flags.append("종합/프리뷰/홍보성 키워드 포함")
    if _has_any(combined_text, FUTURES_KEYWORDS) and not _has_any(combined_text, ("복귀", "재활", "1군")):
        score -= 15
        risk_flags.append("2군/퓨처스 중심")
    if fresh_count == 1 and source_count == 1 and strong_group not in {"injury", "controversy", "roster"}:
        score -= 20
        risk_flags.append("단일 기사/단일 매체")

    score = max(0.0, min(100.0, score))
    notification_level = "immediate" if score >= 75 or (strong_group in {"injury", "controversy"} and score >= 65) else "watch"
    topic_name = _topic_name(group, representative)
    return FreshIssueCandidate(
        issue_id=_issue_id(group, representative),
        topic_name=topic_name,
        representative_article_id=representative.item.id,
        fresh_articles=group.fresh_articles,
        context_articles=group.context_articles,
        matched_teams=group.matched_teams,
        matched_keywords=group.matched_keywords,
        keyword_groups=group.keyword_groups,
        issue_score=score,
        gemini_decision="approve" if not config.gemini_review_enabled else None,
        gemini_confidence=None,
        notification_level=notification_level,
        reasons=reasons,
        risk_flags=risk_flags,
        metadata={
            "source_diversity": source_count,
            "fresh_article_count": fresh_count,
            "context_article_count": context_count,
            "age_minutes": age_minutes,
        },
    )


def _apply_gemini_review(candidates: list[FreshIssueCandidate]) -> None:
    load_default_env(ROOT_DIR)
    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        raise RuntimeError("GEMINI_API_KEY is required unless --no-gemini-review is set")
    model_name = os.getenv("GEMINI_FRESH_ISSUE_MODEL") or "gemini-2.5-flash-lite"
    for candidate in candidates:
        payload = _request_gemini_review(candidate, api_key=api_key, model_name=model_name)
        candidate.gemini_decision = str(payload.get("decision") or "hold")
        candidate.gemini_confidence = _coerce_float(payload.get("confidence"))
        if payload.get("notification_level") in {"immediate", "watch"}:
            candidate.notification_level = str(payload["notification_level"])
        if payload.get("suggested_topic_name"):
            candidate.topic_name = str(payload["suggested_topic_name"])[:80]
        if payload.get("main_reason"):
            candidate.reasons.insert(0, f"Gemini: {payload['main_reason']}")
        candidate.metadata["gemini_review"] = payload


def _request_gemini_review(candidate: FreshIssueCandidate, *, api_key: str, model_name: str) -> dict[str, Any]:
    prompt = {
        "instruction": "KBO 카드뉴스 Discord 알림 전 최종 게이트다. approve/reject/hold 중 하나만 결정하고 JSON만 반환한다.",
        "candidate": _candidate_report(candidate),
    }
    body = json.dumps(
        {
            "contents": [
                {
                    "role": "user",
                    "parts": [{"text": json.dumps(prompt, ensure_ascii=False)}],
                }
            ],
            "generationConfig": {"responseMimeType": "application/json", "temperature": 0.2},
        },
        ensure_ascii=False,
    ).encode("utf-8")
    request = urllib.request.Request(
        f"https://generativelanguage.googleapis.com/v1beta/models/{model_name}:generateContent?key={api_key}",
        data=body,
        headers={"Content-Type": "application/json; charset=utf-8"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            response_payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="ignore")
        raise RuntimeError(f"Gemini fresh issue review failed: HTTP {exc.code} {detail}") from exc
    text = response_payload["candidates"][0]["content"]["parts"][0]["text"]
    parsed = json.loads(text)
    if not isinstance(parsed, dict):
        raise RuntimeError(f"Gemini fresh issue review returned non-object JSON: {parsed!r}")
    return parsed


def _job_from_candidate(
    candidate: FreshIssueCandidate,
    *,
    run_dir: Path,
    choice_json_path: Path,
    fingerprint: JobFingerprint,
    config: FreshIssueDetectorConfig,
    collection_start: datetime,
    collection_end: datetime,
    context_start: datetime,
    context_end: datetime,
) -> AutomationJob:
    metadata = {
        "source": "watch_fresh_once",
        "choice_json_path": str(choice_json_path),
        "fresh_watch_run_dir": str(run_dir),
        "issue_score": candidate.issue_score,
        "gemini_decision": candidate.gemini_decision,
        "gemini_confidence": candidate.gemini_confidence,
        "score_reasons": candidate.reasons,
        "risk_flags": candidate.risk_flags,
        "matched_teams": candidate.matched_teams,
        "matched_keywords": candidate.matched_keywords,
        "keyword_groups": candidate.keyword_groups,
        "fresh_article_count": len(candidate.fresh_articles),
        "context_article_count": len(candidate.context_articles),
        "source_diversity": candidate.metadata.get("source_diversity"),
        "collection_window_start": collection_start.isoformat(),
        "collection_window_end": collection_end.isoformat(),
        "context_window_start": context_start.isoformat(),
        "context_window_end": context_end.isoformat(),
        **fingerprint_metadata(fingerprint, duplicate_lookback_hours=config.duplicate_lookback_hours),
    }
    articles = _job_articles(candidate)[:5]
    return AutomationJob(
        job_id=candidate.issue_id[:80],
        topic_id=candidate.issue_id,
        topic_name=candidate.topic_name,
        status="detected",
        notification_level=candidate.notification_level,
        virality_potential_score=candidate.issue_score,
        account_fit_score=_account_fit_score(candidate),
        recommendation_summary=" / ".join(candidate.reasons[:3]),
        metadata=metadata,
        articles=articles,
        created_at=utc_now(),
        updated_at=utc_now(),
    )


def _candidate_fingerprint(candidate: FreshIssueCandidate) -> JobFingerprint:
    article_urls = sorted(
        {
            normalize_url(article.item.source_url)
            for article in candidate.fresh_articles
            if normalize_url(article.item.source_url)
        }
    )
    representative_url = ""
    for article in candidate.fresh_articles:
        if article.item.id == candidate.representative_article_id:
            representative_url = normalize_url(article.item.source_url)
            break
    article_url_fingerprint = hash_parts(article_urls)
    normalized_topic_key = normalize_topic_key(
        " ".join([*candidate.matched_teams, *candidate.matched_keywords, candidate.topic_name])
    )
    stable_basis = representative_url or article_url_fingerprint or normalized_topic_key
    return JobFingerprint(
        topic_fingerprint=f"fresh:{stable_basis}" if stable_basis else "",
        representative_article_url=representative_url,
        article_url_fingerprint=article_url_fingerprint,
        normalized_topic_key=normalized_topic_key,
        article_urls=article_urls,
    )


def _write_choice_json(path: Path, *, candidates: list[FreshIssueCandidate]) -> None:
    payload = {
        "required_selection_count": 1 if candidates else 0,
        "selected_topic_ids": [],
        "candidates": [_choice_candidate(candidate, index=index) for index, candidate in enumerate(candidates, start=1)],
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _choice_candidate(candidate: FreshIssueCandidate, *, index: int) -> dict[str, Any]:
    return {
        "topic_id": candidate.issue_id,
        "topic_name": candidate.topic_name,
        "importance_rank": index,
        "topic_score": candidate.issue_score,
        "reason_summary": " / ".join(candidate.reasons[:4]),
        "representative_article_id": candidate.representative_article_id,
        "article_ids": [article.item.id for article in candidate.fresh_articles],
        "selected": False,
        "metadata": {
            "article_publication_summary": {
                "articles": [_article_summary(article) for article in _job_article_sources(candidate)[:5]],
            },
            "fresh_issue": _candidate_report(candidate),
        },
    }


def _write_report(path: Path, result: FreshWatchResult) -> None:
    payload = fresh_watch_result_to_dict(result)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def fresh_watch_result_to_dict(result: FreshWatchResult) -> dict[str, Any]:
    return {
        "collection_window_start": result.collection_window_start.isoformat(),
        "collection_window_end": result.collection_window_end.isoformat(),
        "context_window_start": result.context_window_start.isoformat(),
        "context_window_end": result.context_window_end.isoformat(),
        "collected_count": result.collected_count,
        "inserted_count": result.inserted_count,
        "duplicate_count": result.duplicate_count,
        "fresh_article_count": result.fresh_article_count,
        "context_article_count": result.context_article_count,
        "candidate_count": result.candidate_count,
        "created_count": len(result.created_jobs),
        "duplicate_job_count": len(result.duplicate_jobs),
        "skipped_count": result.skipped_count,
        "collector_errors": result.collector_errors,
        "report_path": str(result.report_path),
        "choice_json_path": str(result.choice_json_path),
        "created_jobs": [_job_summary(job) for job in result.created_jobs],
        "duplicate_jobs": [_job_summary(job) for job in result.duplicate_jobs],
        "candidates": [_candidate_report(candidate) for candidate in result.candidates],
    }


def _job_articles(candidate: FreshIssueCandidate) -> list[AutomationJobArticle]:
    return [
        AutomationJobArticle(
            article_id=article.item.id,
            title=article.item.title or "",
            source_type=article.item.source_type,
            source_url=article.item.source_url,
            published_at=article.item.published_at.isoformat() if article.item.published_at else None,
        )
        for article in _job_article_sources(candidate)
    ]


def _job_article_sources(candidate: FreshIssueCandidate) -> list[PersistedSourceItem]:
    rows = sorted(candidate.fresh_articles, key=lambda item: _effective_time(item), reverse=True)
    rows.extend(sorted(candidate.context_articles, key=lambda item: _effective_time(item), reverse=True))
    seen: set[str] = set()
    unique: list[PersistedSourceItem] = []
    for row in rows:
        if row.item.id in seen:
            continue
        seen.add(row.item.id)
        unique.append(row)
    return unique


def _article_summary(article: PersistedSourceItem) -> dict[str, Any]:
    return {
        "article_id": article.item.id,
        "title": article.item.title,
        "source_type": article.item.source_type,
        "published_at": article.item.published_at.isoformat() if article.item.published_at else None,
        "source_url": article.item.source_url,
    }


def _candidate_report(candidate: FreshIssueCandidate) -> dict[str, Any]:
    return {
        "issue_id": candidate.issue_id,
        "topic_name": candidate.topic_name,
        "issue_score": candidate.issue_score,
        "notification_level": candidate.notification_level,
        "gemini_decision": candidate.gemini_decision,
        "gemini_confidence": candidate.gemini_confidence,
        "matched_teams": candidate.matched_teams,
        "matched_keywords": candidate.matched_keywords,
        "keyword_groups": candidate.keyword_groups,
        "reasons": candidate.reasons,
        "risk_flags": candidate.risk_flags,
        "fresh_articles": [_article_summary(article) for article in candidate.fresh_articles],
        "context_article_count": len(candidate.context_articles),
        "metadata": candidate.metadata,
    }


def _job_summary(job: AutomationJob) -> dict[str, Any]:
    return {
        "job_id": job.job_id,
        "topic_id": job.topic_id,
        "topic_name": job.topic_name,
        "status": job.status,
        "notification_level": job.notification_level,
        "virality_potential_score": job.virality_potential_score,
    }


def _topic_name(group: IssueGroup, representative: PersistedSourceItem) -> str:
    parts = []
    if group.matched_teams:
        parts.append(group.matched_teams[0])
    if group.player_candidate:
        parts.append(group.player_candidate)
    if group.matched_keywords:
        parts.append(group.matched_keywords[0])
    if len(parts) >= 2:
        return " ".join(parts)[:80]
    return (representative.item.title or "KBO fresh issue")[:80]


def _issue_id(group: IssueGroup, representative: PersistedSourceItem) -> str:
    basis = "|".join(
        [
            group.key,
            representative.item.source_url,
            ",".join(sorted(article.item.source_url for article in group.fresh_articles)),
        ]
    )
    timestamp = _effective_time(representative).astimezone(KST).strftime("%Y%m%d%H%M")
    return f"fresh-{timestamp}-{hash_parts([basis])}"


def _account_fit_score(candidate: FreshIssueCandidate) -> float:
    if any(team in PRIMARY_TEAMS for team in candidate.matched_teams):
        return 90.0
    if any(team in SECONDARY_TEAMS for team in candidate.matched_teams):
        return 75.0
    if candidate.matched_teams:
        return 60.0
    return 40.0


def _is_related_context(item: PersistedSourceItem, *, teams: list[str], keywords: list[str], player: str) -> bool:
    text = _article_text(item)
    if teams and any(team in _matched_teams(text) for team in teams):
        return True
    if player and player in text:
        return True
    return bool(keywords and any(keyword in text for keyword in keywords))


def _matched_teams(text: str) -> list[str]:
    matched: list[str] = []
    upper_text = text.upper()
    for team in ALL_TEAMS:
        aliases = TEAM_ALIASES.get(team, (team,))
        if any(alias.upper() in upper_text for alias in aliases):
            matched.append(team)
    return matched


def _matched_keyword_groups(text: str) -> tuple[list[str], list[str]]:
    groups: list[str] = []
    keywords: list[str] = []
    for group_name, group_keywords in KEYWORD_GROUPS.items():
        matched = [keyword for keyword in group_keywords if keyword.lower() in text.lower()]
        if matched:
            groups.append(group_name)
            keywords.extend(matched)
    groups = [group for group in groups if group != "low_priority"] + [group for group in groups if group == "low_priority"]
    return groups, sorted(set(keywords))


def _player_candidate(text: str, *, teams: list[str], keywords: list[str]) -> str:
    blocked = set(ALL_TEAMS) | {"프로야구", "야구", "감독", "선수", "외국인", "구단", "기록", "오늘"}
    blocked.update(teams)
    blocked.update(keywords)
    tokens = re.findall(r"[가-힣]{2,4}", text)
    for token in tokens:
        if token not in blocked and not any(token in alias for aliases in TEAM_ALIASES.values() for alias in aliases):
            return token
    return ""


def _article_text(article: PersistedSourceItem) -> str:
    return " ".join(
        value
        for value in [article.item.title or "", article.item.excerpt_text or "", article.item.body_text or ""]
        if value
    )


def _article_text_many(articles: list[PersistedSourceItem]) -> str:
    return " ".join(_article_text(article) for article in articles)


def _has_any(text: str, keywords: tuple[str, ...]) -> bool:
    lowered = text.lower()
    return any(keyword.lower() in lowered for keyword in keywords)


def _effective_time(article: PersistedSourceItem) -> datetime:
    value = article.item.published_at or article.item.collected_at
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _normalize_now(value: datetime | None) -> datetime:
    current = value or datetime.now(KST)
    if current.tzinfo is None:
        current = current.replace(tzinfo=KST)
    return current.astimezone(KST).replace(second=0, microsecond=0)


def _fresh_run_dir(now: datetime) -> Path:
    return AUTOMATION_OUTPUT_DIR / "fresh_watch_runs" / now.astimezone(KST).strftime("%Y%m%d_%H%M%S")


def _coerce_float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
