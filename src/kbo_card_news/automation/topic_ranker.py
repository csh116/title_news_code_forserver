from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any


PRIMARY_TEAMS = ("KIA", "한화", "LG", "롯데")
SECONDARY_TEAMS = ("두산", "삼성")
OTHER_TEAMS = ("KT", "NC", "SSG", "키움")
ALL_TEAMS = PRIMARY_TEAMS + SECONDARY_TEAMS + OTHER_TEAMS

INJURY_KEYWORDS = (
    "부상",
    "인대",
    "수술",
    "시즌 아웃",
    "시즌아웃",
    "말소",
    "이탈",
    "병원",
    "MRI",
    "검진",
    "구급차",
    "앰뷸런스",
    "통증",
)
CONTROVERSY_KEYWORDS = (
    "징계",
    "논란",
    "사과",
    "욕설",
    "물의",
    "도박",
    "불법",
    "사건",
    "감독",
    "발언",
)
DRAMA_KEYWORDS = (
    "끝내기",
    "역전",
    "대승",
    "완승",
    "스윕",
    "연승",
    "연패 탈출",
    "혈투",
    "위닝",
)
RECORD_KEYWORDS = (
    "기록",
    "신기록",
    "최다",
    "통산",
    "500타점",
    "홈런 선두",
    "연속",
    "MVP",
    "호투",
    "세이브",
)
ROSTER_KEYWORDS = (
    "복귀",
    "무산",
    "영입",
    "방출",
    "교체",
    "대체",
    "외국인",
    "2군",
    "콜업",
    "엔트리",
    "거취",
)
LOW_PRIORITY_KEYWORDS = (
    "이벤트",
    "상품",
    "협업",
    "관중",
    "중계",
    "월간 MVP 후보",
    "행사",
)


@dataclass(slots=True)
class TopicRankResult:
    virality_potential_score: float
    account_fit_score: float
    notification_level: str
    recommendation_summary: str
    reasons: list[str] = field(default_factory=list)
    risk_flags: list[str] = field(default_factory=list)
    matched_teams: list[str] = field(default_factory=list)
    matched_keywords: list[str] = field(default_factory=list)


def rank_candidate(candidate: dict[str, Any]) -> TopicRankResult:
    topic_name = str(candidate.get("topic_name") or "").strip()
    reason_summary = str(candidate.get("reason_summary") or "").strip()
    article_titles = _article_titles(candidate)
    primary_text = _normalize_text(" ".join([topic_name, reason_summary]))
    article_text = _normalize_text(" ".join(article_titles))
    base_score = _coerce_score(candidate.get("topic_score"))
    article_count = len(article_titles)
    matched_teams = _matched_keywords(primary_text, ALL_TEAMS)
    if not matched_teams:
        matched_teams = _matched_keywords(article_text, ALL_TEAMS)[:2]

    virality = min(base_score * 0.35, 35.0)
    account_fit = min(base_score * 0.25, 25.0)
    reasons: list[str] = []
    risk_flags: list[str] = []
    matched_keywords: list[str] = []

    virality += _apply_keyword_group(
        primary_text,
        INJURY_KEYWORDS,
        points=35,
        reasons=reasons,
        matched_keywords=matched_keywords,
        reason="부상/말소/수술/이탈 키워드",
    )
    virality += _apply_keyword_group(
        primary_text,
        CONTROVERSY_KEYWORDS,
        points=25,
        reasons=reasons,
        matched_keywords=matched_keywords,
        reason="논란/징계/사과/발언 키워드",
    )
    virality += _apply_keyword_group(
        primary_text,
        DRAMA_KEYWORDS,
        points=20,
        reasons=reasons,
        matched_keywords=matched_keywords,
        reason="끝내기/역전/연승 등 경기 임팩트",
    )
    virality += _apply_keyword_group(
        primary_text,
        RECORD_KEYWORDS,
        points=20,
        reasons=reasons,
        matched_keywords=matched_keywords,
        reason="기록/홈런/세이브 등 기록성",
    )
    virality += _apply_keyword_group(
        primary_text,
        ROSTER_KEYWORDS,
        points=20,
        reasons=reasons,
        matched_keywords=matched_keywords,
        reason="복귀/영입/엔트리/거취 키워드",
    )

    if article_count >= 10:
        virality += 20
        account_fit += 5
        reasons.append(f"근거 기사 {article_count}개로 기사량이 많음")
    elif article_count >= 4:
        virality += 10
        reasons.append(f"근거 기사 {article_count}개")
    elif article_count <= 1:
        virality -= 10
        risk_flags.append("기사 수가 적어 후속 확산 확인 필요")

    if any(team in PRIMARY_TEAMS for team in matched_teams):
        virality += 10
        account_fit += 15
        reasons.append("계정에서 자주 다룬 인기 구단 포함")
    elif any(team in SECONDARY_TEAMS for team in matched_teams):
        virality += 7
        account_fit += 10
        reasons.append("계정에서 종종 다룬 구단 포함")
    elif matched_teams:
        virality += 5
        account_fit += 5

    if _has_any(primary_text, INJURY_KEYWORDS + ROSTER_KEYWORDS + CONTROVERSY_KEYWORDS):
        account_fit += 20
    if _has_any(primary_text, RECORD_KEYWORDS + DRAMA_KEYWORDS):
        account_fit += 15
    if _has_short_headline_shape(topic_name):
        account_fit += 10
        reasons.append("짧은 타이틀로 압축하기 쉬운 주제")

    if _has_any(primary_text, LOW_PRIORITY_KEYWORDS):
        virality -= 10
        account_fit -= 10
        risk_flags.append("행사/상품/리그 일반 이슈 성격")

    if not matched_teams:
        account_fit -= 10
        risk_flags.append("명확한 팀 주체가 약함")

    virality = _clamp(virality, 0, 100)
    account_fit = _clamp(account_fit, 0, 100)
    notification_level = _notification_level(virality, account_fit, primary_text)
    if not reasons:
        reasons.append("후보 점수와 기사 요약 기준으로 감지")

    summary = _build_summary(
        topic_name=topic_name,
        notification_level=notification_level,
        reasons=reasons,
        risk_flags=risk_flags,
    )
    return TopicRankResult(
        virality_potential_score=virality,
        account_fit_score=account_fit,
        notification_level=notification_level,
        recommendation_summary=summary,
        reasons=reasons,
        risk_flags=risk_flags,
        matched_teams=matched_teams,
        matched_keywords=sorted(set(matched_keywords)),
    )


def rank_result_to_metadata(result: TopicRankResult) -> dict[str, Any]:
    return {
        "ranker": "rule_based_selection_policy_v1",
        "ranker_reasons": result.reasons,
        "ranker_risk_flags": result.risk_flags,
        "matched_teams": result.matched_teams,
        "matched_keywords": result.matched_keywords,
    }


def _article_titles(candidate: dict[str, Any]) -> list[str]:
    metadata = candidate.get("metadata") if isinstance(candidate.get("metadata"), dict) else {}
    publication_summary = (
        metadata.get("article_publication_summary")
        if isinstance(metadata.get("article_publication_summary"), dict)
        else {}
    )
    raw_articles = publication_summary.get("articles") if isinstance(publication_summary, dict) else []
    if not isinstance(raw_articles, list):
        return []
    titles: list[str] = []
    for article in raw_articles:
        if isinstance(article, dict):
            title = str(article.get("title") or "").strip()
            if title:
                titles.append(title)
    return titles


def _apply_keyword_group(
    text: str,
    keywords: tuple[str, ...],
    *,
    points: float,
    reasons: list[str],
    matched_keywords: list[str],
    reason: str,
) -> float:
    matched = _matched_keywords(text, keywords)
    if not matched:
        return 0.0
    matched_keywords.extend(matched)
    reasons.append(reason)
    return points


def _matched_keywords(text: str, keywords: tuple[str, ...]) -> list[str]:
    return [keyword for keyword in keywords if _normalize_text(keyword) in text]


def _has_any(text: str, keywords: tuple[str, ...]) -> bool:
    return any(_normalize_text(keyword) in text for keyword in keywords)


def _has_short_headline_shape(topic_name: str) -> bool:
    compact = re.sub(r"\s+", "", topic_name)
    if len(compact) <= 14:
        return True
    strong_tokens = sum(
        1
        for keyword in INJURY_KEYWORDS + CONTROVERSY_KEYWORDS + DRAMA_KEYWORDS + RECORD_KEYWORDS + ROSTER_KEYWORDS
        if keyword in topic_name
    )
    return strong_tokens >= 1 and len(compact) <= 28


def _notification_level(virality: float, account_fit: float, text: str) -> str:
    urgent = _has_any(text, INJURY_KEYWORDS + CONTROVERSY_KEYWORDS) or _has_any(text, ("끝내기", "신기록", "시즌 아웃", "시즌아웃"))
    if virality >= 70 or urgent or (account_fit >= 70 and virality >= 45):
        return "immediate"
    if virality >= 50 or account_fit >= 60:
        return "watch"
    return "digest"


def _build_summary(
    *,
    topic_name: str,
    notification_level: str,
    reasons: list[str],
    risk_flags: list[str],
) -> str:
    level_label = {
        "immediate": "즉시 확인",
        "watch": "지켜볼 만함",
        "digest": "묶어서 확인",
    }.get(notification_level, notification_level)
    reason_text = "; ".join(reasons[:4])
    if risk_flags:
        return f"[{level_label}] {topic_name}: {reason_text} / 리스크: {'; '.join(risk_flags[:2])}"
    return f"[{level_label}] {topic_name}: {reason_text}"


def _coerce_score(value: object) -> float:
    try:
        score = float(value or 0.0)
    except (TypeError, ValueError):
        return 0.0
    if 0 <= score <= 1:
        return score * 100
    return _clamp(score, 0, 100)


def _normalize_text(value: str) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip().lower()


def _clamp(value: float, minimum: float, maximum: float) -> float:
    return max(minimum, min(maximum, value))
