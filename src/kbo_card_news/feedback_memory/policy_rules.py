from __future__ import annotations

import re
from typing import Any

from kbo_card_news.feedback_memory.policy_models import HeadlineCorrectionPolicy


def apply_policy_rule(
    policy: HeadlineCorrectionPolicy,
    *,
    title_text: str,
    subheadline: str,
    context: dict[str, Any],
) -> tuple[str, str, str | None]:
    if policy.rule_type == "require_player_name_in_title":
        return _apply_require_player_name_in_title(policy, title_text=title_text, subheadline=subheadline)
    if policy.rule_type == "prefer_specific_injury_keyword":
        return _apply_prefer_specific_injury_keyword(policy, title_text=title_text, subheadline=subheadline)
    if policy.rule_type == "disallow_team_only_short_title":
        return _apply_disallow_team_only_short_title(policy, title_text=title_text, subheadline=subheadline)
    if policy.rule_type == "prefer_event_first_subheadline":
        return _apply_prefer_event_first_subheadline(policy, title_text=title_text, subheadline=subheadline)
    if policy.rule_type == "disallow_generic_subheadline_phrase":
        return _apply_disallow_generic_subheadline_phrase(policy, title_text=title_text, subheadline=subheadline)
    return title_text, subheadline, None


def _apply_require_player_name_in_title(
    policy: HeadlineCorrectionPolicy,
    *,
    title_text: str,
    subheadline: str,
) -> tuple[str, str, str | None]:
    player_name = _text(policy.rule_payload.get("player_name"))
    event_label = _text(policy.rule_payload.get("event_label"))
    if not player_name or player_name in title_text:
        return title_text, subheadline, None
    next_title = f"{player_name} {event_label}".strip() if event_label else f"{player_name} 활약"
    return next_title, subheadline, f"player_name:{player_name}"


def _apply_prefer_specific_injury_keyword(
    policy: HeadlineCorrectionPolicy,
    *,
    title_text: str,
    subheadline: str,
) -> tuple[str, str, str | None]:
    keyword = _text(policy.rule_payload.get("specific_keyword"))
    if not keyword:
        return title_text, subheadline, None
    next_title = title_text
    next_subheadline = subheadline
    changed = False
    generic_terms = ("부상", "이탈", "악재", "통증")
    if keyword not in next_title and any(term in next_title for term in generic_terms):
        next_title = _replace_first_generic_term(next_title, keyword, generic_terms)
        changed = True
    if keyword not in next_subheadline and any(term in next_subheadline for term in generic_terms):
        next_subheadline = _replace_first_generic_term(next_subheadline, keyword, generic_terms)
        changed = True
    return next_title, next_subheadline, f"injury_keyword:{keyword}" if changed else None


def _apply_disallow_team_only_short_title(
    policy: HeadlineCorrectionPolicy,
    *,
    title_text: str,
    subheadline: str,
) -> tuple[str, str, str | None]:
    team_name = _text(policy.rule_payload.get("team_name")) or policy.team_name or ""
    preferred_title = _text(policy.rule_payload.get("preferred_title"))
    event_label = _text(policy.rule_payload.get("event_label"))
    compact = re.sub(r"\s+", "", title_text)
    if not team_name:
        return title_text, subheadline, None
    if compact != team_name and not (team_name in compact and len(compact) <= len(team_name) + 2):
        return title_text, subheadline, None
    next_title = preferred_title or f"{team_name} {event_label}".strip()
    return next_title, subheadline, f"expand_team_only:{team_name}"


def _apply_prefer_event_first_subheadline(
    policy: HeadlineCorrectionPolicy,
    *,
    title_text: str,
    subheadline: str,
) -> tuple[str, str, str | None]:
    preferred_lead = _text(policy.rule_payload.get("preferred_lead"))
    preferred_subheadline = _text(policy.rule_payload.get("preferred_subheadline"))
    current_first_line = subheadline.splitlines()[0].strip() if subheadline else ""
    if preferred_lead and current_first_line and preferred_lead in current_first_line:
        return title_text, subheadline, None
    if preferred_subheadline:
        return title_text, preferred_subheadline, "event_first_subheadline"
    return title_text, subheadline, None


def _apply_disallow_generic_subheadline_phrase(
    policy: HeadlineCorrectionPolicy,
    *,
    title_text: str,
    subheadline: str,
) -> tuple[str, str, str | None]:
    generic_phrase = _text(policy.rule_payload.get("generic_phrase"))
    preferred_subheadline = _text(policy.rule_payload.get("preferred_subheadline"))
    if not generic_phrase or generic_phrase not in subheadline:
        return title_text, subheadline, None
    if preferred_subheadline:
        return title_text, preferred_subheadline, f"generic_subheadline:{generic_phrase}"
    return title_text, subheadline.replace(generic_phrase, "").strip(), f"generic_subheadline:{generic_phrase}"


def _replace_first_generic_term(text: str, replacement: str, generic_terms: tuple[str, ...]) -> str:
    for term in generic_terms:
        if term in text:
            return text.replace(term, replacement, 1)
    return text


def _text(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()
