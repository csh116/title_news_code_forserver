from __future__ import annotations

import json
import re
from typing import Any

from kbo_card_news.feedback_memory.policy_models import HeadlinePolicyCandidate
from kbo_card_news.feedback_memory.policy_storage import (
    build_policy_payload_signature,
    policy_priority_for_scope,
    upsert_headline_policy_candidate,
)
from kbo_card_news.feedback_memory.storage import FeedbackMemoryRepository

_GENERIC_SUBHEADLINE_PHRASES = (
    "분위기를 끌어올렸다",
    "상승세를 이어갔다",
    "관심이 쏠린다",
    "기대가 모인다",
    "시선이 쏠린다",
    "주목된다",
)
_INJURY_KEYWORDS = (
    "햄스트링",
    "어깨",
    "팔꿈치",
    "허벅지",
    "손목",
    "발목",
    "무릎",
    "옆구리",
    "척추",
    "쇄골",
    "전완근",
)
_TEAM_NAMES = {"LG", "KIA", "두산", "삼성", "롯데", "SSG", "한화", "KT", "NC", "키움"}
_STOPWORDS = _TEAM_NAMES | {
    "연승",
    "연패",
    "선두",
    "홈런",
    "결승타",
    "호투",
    "부상",
    "복귀",
    "이탈",
    "트레이드",
    "라인업",
}


def build_headline_policy_candidates(headline_memory_row: dict[str, Any]) -> list[HeadlinePolicyCandidate]:
    before_title = _normalize_text(headline_memory_row.get("before_title_text"))
    after_title = _normalize_text(headline_memory_row.get("after_title_text"))
    before_subheadline = _normalize_text(headline_memory_row.get("before_subheadline"))
    after_subheadline = _normalize_text(headline_memory_row.get("after_subheadline"))
    topic_fingerprint = _normalize_text(headline_memory_row.get("topic_fingerprint"))
    team_name = _normalize_text(headline_memory_row.get("team_name"))

    if not after_title and not after_subheadline:
        return []

    candidates: list[HeadlinePolicyCandidate] = []
    player_name = _extract_player_name(after_title)
    event_label = _extract_event_label(after_title, player_name=player_name, team_name=team_name)

    if (
        _normalize_text(headline_memory_row.get("entity_focus")) == "player"
        and player_name
        and player_name not in before_title
    ):
        candidates.extend(
            _build_candidates_for_rule(
                headline_memory_row,
                scope_types=("topic_fingerprint_exact", "topic_feature_bucket"),
                rule_type="require_player_name_in_title",
                rule_payload={
                    "player_name": player_name,
                    "event_label": event_label,
                },
                base_priority=40,
            )
        )

    specific_injury_keyword = _extract_specific_injury_keyword(after_title, after_subheadline)
    if (
        _normalize_text(headline_memory_row.get("topic_type")) == "injury"
        and specific_injury_keyword
        and specific_injury_keyword not in f"{before_title} {before_subheadline}"
    ):
        candidates.extend(
            _build_candidates_for_rule(
                headline_memory_row,
                scope_types=("topic_feature_bucket", "team_specific"),
                rule_type="prefer_specific_injury_keyword",
                rule_payload={"specific_keyword": specific_injury_keyword},
                base_priority=30,
            )
        )

    if _is_team_only_short_title(before_title, team_name=team_name) and after_title and after_title != before_title:
        candidates.extend(
            _build_candidates_for_rule(
                headline_memory_row,
                scope_types=("topic_fingerprint_exact", "team_specific"),
                rule_type="disallow_team_only_short_title",
                rule_payload={
                    "team_name": team_name,
                    "preferred_title": after_title,
                    "event_label": event_label,
                },
                base_priority=35,
            )
        )

    event_first_phrase = _extract_event_first_phrase(after_subheadline)
    if event_first_phrase and _is_generic_subheadline(before_subheadline):
        candidates.extend(
            _build_candidates_for_rule(
                headline_memory_row,
                scope_types=("topic_feature_bucket",),
                rule_type="prefer_event_first_subheadline",
                rule_payload={
                    "preferred_lead": event_first_phrase,
                    "preferred_subheadline": after_subheadline,
                },
                base_priority=20,
            )
        )

    generic_phrase = _find_generic_subheadline_phrase(before_subheadline)
    if generic_phrase and after_subheadline and generic_phrase not in after_subheadline:
        candidates.extend(
            _build_candidates_for_rule(
                headline_memory_row,
                scope_types=("topic_feature_bucket", "global"),
                rule_type="disallow_generic_subheadline_phrase",
                rule_payload={
                    "generic_phrase": generic_phrase,
                    "preferred_subheadline": after_subheadline,
                },
                base_priority=10,
            )
        )
    return _dedupe_candidates(candidates)


def refresh_policies_from_headline_memory(
    headline_memory_row: dict[str, Any],
    *,
    repository: FeedbackMemoryRepository,
) -> list[str]:
    candidates = build_headline_policy_candidates(headline_memory_row)
    activated: list[str] = []
    for candidate in candidates:
        result = upsert_headline_policy_candidate(candidate, repository=repository)
        if result.ok and result.active and result.policy_id:
            activated.append(result.policy_id)
    return activated


def _build_candidates_for_rule(
    headline_memory_row: dict[str, Any],
    *,
    scope_types: tuple[str, ...],
    rule_type: str,
    rule_payload: dict[str, Any],
    base_priority: int,
) -> list[HeadlinePolicyCandidate]:
    if not rule_payload:
        return []
    candidates: list[HeadlinePolicyCandidate] = []
    for scope_type in scope_types:
        key = _build_policy_key(
            headline_memory_row,
            scope_type=scope_type,
            rule_type=rule_type,
            rule_payload=rule_payload,
        )
        if not key:
            continue
        candidates.append(
            HeadlinePolicyCandidate(
                policy_key=key,
                scope_type=scope_type,
                topic_type=_normalize_text(headline_memory_row.get("topic_type")),
                entity_focus=_normalize_text(headline_memory_row.get("entity_focus")),
                event_type=_normalize_text(headline_memory_row.get("event_type")),
                angle_type=_normalize_text(headline_memory_row.get("angle_type")),
                team_name=_normalize_text(headline_memory_row.get("team_name")),
                topic_fingerprint=_normalize_text(headline_memory_row.get("topic_fingerprint")),
                rule_type=rule_type,
                rule_payload=dict(rule_payload),
                priority=policy_priority_for_scope(scope_type, base_priority=base_priority),
                headline_memory_id=_normalize_text(headline_memory_row.get("id")),
            )
        )
    return candidates


def _build_policy_key(
    headline_memory_row: dict[str, Any],
    *,
    scope_type: str,
    rule_type: str,
    rule_payload: dict[str, Any],
) -> str | None:
    payload_sig = build_policy_payload_signature(rule_payload)
    if scope_type == "topic_fingerprint_exact":
        fingerprint = _normalize_text(headline_memory_row.get("topic_fingerprint"))
        if not fingerprint:
            return None
        return f"{scope_type}|{fingerprint}|{rule_type}|{payload_sig}"
    if scope_type == "topic_feature_bucket":
        bucket_parts = [
            _normalize_text(headline_memory_row.get("topic_type")) or "*",
            _normalize_text(headline_memory_row.get("entity_focus")) or "*",
            _normalize_text(headline_memory_row.get("event_type")) or "*",
            _normalize_text(headline_memory_row.get("angle_type")) or "*",
        ]
        return f"{scope_type}|{'|'.join(bucket_parts)}|{rule_type}|{payload_sig}"
    if scope_type == "team_specific":
        team_name = _normalize_text(headline_memory_row.get("team_name"))
        if not team_name:
            return None
        return f"{scope_type}|{team_name}|{rule_type}|{payload_sig}"
    return f"global|{rule_type}|{payload_sig}"


def _dedupe_candidates(candidates: list[HeadlinePolicyCandidate]) -> list[HeadlinePolicyCandidate]:
    deduped: dict[str, HeadlinePolicyCandidate] = {}
    for candidate in candidates:
        deduped.setdefault(candidate.policy_key, candidate)
    return list(deduped.values())


def _normalize_text(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _extract_player_name(text: str) -> str | None:
    for token in re.findall(r"[가-힣]{2,4}", text):
        if token in _STOPWORDS:
            continue
        return token
    return None


def _extract_event_label(text: str, *, player_name: str | None, team_name: str | None) -> str | None:
    cleaned = text
    for token in (player_name or "", team_name or ""):
        if token:
            cleaned = cleaned.replace(token, " ")
    compact = re.sub(r"\s+", " ", cleaned).strip()
    return compact or None


def _extract_specific_injury_keyword(*texts: str) -> str | None:
    merged = " ".join(texts)
    for keyword in _INJURY_KEYWORDS:
        if keyword in merged:
            return keyword
    return None


def _is_team_only_short_title(text: str, *, team_name: str | None) -> bool:
    if not text or not team_name:
        return False
    compact = re.sub(r"\s+", "", text)
    return compact == team_name or (team_name in compact and len(compact) <= len(team_name) + 2)


def _extract_event_first_phrase(text: str) -> str | None:
    if not text:
        return None
    first_line = text.splitlines()[0].strip()
    if not first_line:
        return None
    return first_line[:40]


def _is_generic_subheadline(text: str) -> bool:
    if not text:
        return False
    if len(text.replace("\n", "")) < 30:
        return True
    return _find_generic_subheadline_phrase(text) is not None


def _find_generic_subheadline_phrase(text: str) -> str | None:
    if not text:
        return None
    for phrase in _GENERIC_SUBHEADLINE_PHRASES:
        if phrase in text:
            return phrase
    return None


def summarize_policy_candidates(headline_memory_row: dict[str, Any]) -> str:
    candidates = build_headline_policy_candidates(headline_memory_row)
    lines = []
    for candidate in candidates:
        lines.append(
            json.dumps(
                {
                    "policy_key": candidate.policy_key,
                    "scope_type": candidate.scope_type,
                    "rule_type": candidate.rule_type,
                    "rule_payload": candidate.rule_payload,
                },
                ensure_ascii=False,
            )
        )
    return "\n".join(lines)
