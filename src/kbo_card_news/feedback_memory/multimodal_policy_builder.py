from __future__ import annotations

import json
import re
from typing import Any

from kbo_card_news.feedback_memory.multimodal_policy_models import MultimodalPolicyCandidate
from kbo_card_news.feedback_memory.multimodal_policy_storage import (
    multimodal_policy_priority_for_scope,
    upsert_multimodal_policy_candidate,
)
from kbo_card_news.feedback_memory.policy_storage import build_policy_payload_signature
from kbo_card_news.feedback_memory.storage import FeedbackMemoryRepository

_GENERIC_TAG_SUMMARY_PHRASES = ("장면", "분위기", "순간", "모습")
_GENERIC_SCENE_PHRASES = ("장면이다", "해석된다", "보인다", "포착됐다")


def build_multimodal_policy_candidates(multimodal_memory_row: dict[str, Any]) -> list[MultimodalPolicyCandidate]:
    candidates: list[MultimodalPolicyCandidate] = []

    before_usage = _text(multimodal_memory_row.get("before_usage_recommendation"))
    after_usage = _text(multimodal_memory_row.get("after_usage_recommendation"))
    if before_usage and after_usage and before_usage != after_usage:
        if after_usage == "cover" and _is_action_hero_context(multimodal_memory_row):
            candidates.extend(
                _build_candidates(
                    multimodal_memory_row,
                    scope_types=("exact_asset_fingerprint", "topic_shot_bucket"),
                    rule_type="prefer_cover_for_action_hero_shot",
                    rule_payload={"preferred_usage": "cover"},
                    base_priority=40,
                    changed_fields=["usage_recommendation"],
                )
            )
        elif after_usage in {"detail_a", "reaction"} and _is_supporting_reaction_context(multimodal_memory_row):
            candidates.extend(
                _build_candidates(
                    multimodal_memory_row,
                    scope_types=("exact_asset_fingerprint", "generic_shot_role_bucket"),
                    rule_type="prefer_detail_for_supporting_reaction_shot",
                    rule_payload={"preferred_usage": after_usage},
                    base_priority=30,
                    changed_fields=["usage_recommendation"],
                )
            )

    removed_event_tags = _diff_removed(
        multimodal_memory_row.get("before_event_tags"),
        multimodal_memory_row.get("after_event_tags"),
    )
    if removed_event_tags:
        candidates.extend(
            _build_candidates(
                multimodal_memory_row,
                scope_types=("exact_asset_fingerprint", "entity_event_bucket"),
                rule_type="remove_overclaiming_event_tag",
                rule_payload={"remove_tags": removed_event_tags},
                base_priority=25,
                changed_fields=["event_tags"],
            )
        )

    added_event_tags = _diff_added(
        multimodal_memory_row.get("before_event_tags"),
        multimodal_memory_row.get("after_event_tags"),
    )
    if added_event_tags:
        candidates.extend(
            _build_candidates(
                multimodal_memory_row,
                scope_types=("exact_asset_fingerprint", "entity_event_bucket"),
                rule_type="prefer_specific_event_tag_when_supported",
                rule_payload={"preferred_tags": added_event_tags},
                base_priority=25,
                changed_fields=["event_tags"],
            )
        )

    before_tag_summary = _text(multimodal_memory_row.get("before_tag_summary"))
    after_tag_summary = _text(multimodal_memory_row.get("after_tag_summary"))
    if (
        before_tag_summary
        and after_tag_summary
        and before_tag_summary != after_tag_summary
        and _looks_generic_summary(before_tag_summary)
    ):
        candidates.extend(
            _build_candidates(
                multimodal_memory_row,
                scope_types=("exact_asset_fingerprint",),
                rule_type="trim_generic_tag_summary_phrase",
                rule_payload={
                    "preferred_summary": after_tag_summary,
                    "generic_before": before_tag_summary,
                },
                base_priority=15,
                changed_fields=["tag_summary"],
            )
        )

    before_scene = _text(multimodal_memory_row.get("before_scene_description"))
    after_scene = _text(multimodal_memory_row.get("after_scene_description"))
    if before_scene and after_scene and before_scene != after_scene and _looks_generic_scene(before_scene):
        candidates.extend(
            _build_candidates(
                multimodal_memory_row,
                scope_types=("exact_asset_fingerprint", "topic_shot_bucket"),
                rule_type="prefer_evidence_first_scene_description",
                rule_payload={"preferred_scene_description": after_scene},
                base_priority=20,
                changed_fields=["scene_description"],
            )
        )

    before_humor = _text(multimodal_memory_row.get("before_humor_point"))
    after_humor = _text(multimodal_memory_row.get("after_humor_point"))
    if before_humor and not after_humor:
        candidates.extend(
            _build_candidates(
                multimodal_memory_row,
                scope_types=("exact_asset_fingerprint", "generic_shot_role_bucket"),
                rule_type="drop_weak_humor_point_when_non_humorous",
                rule_payload={"drop_to": ""},
                base_priority=10,
                changed_fields=["humor_point"],
            )
        )

    added_risk_tags = _diff_added(
        multimodal_memory_row.get("before_risk_tags"),
        multimodal_memory_row.get("after_risk_tags"),
    )
    after_caution = _text(multimodal_memory_row.get("after_caution_note"))
    if added_risk_tags and after_caution:
        candidates.extend(
            _build_candidates(
                multimodal_memory_row,
                scope_types=("exact_asset_fingerprint", "generic_shot_role_bucket"),
                rule_type="require_caution_note_for_risk_tag",
                rule_payload={
                    "required_risk_tags": added_risk_tags,
                    "required_caution_note": after_caution,
                },
                base_priority=15,
                changed_fields=["risk_tags", "caution_note"],
            )
        )

    return _dedupe_candidates(candidates)


def refresh_policies_from_multimodal_memory(
    multimodal_memory_row: dict[str, Any],
    *,
    repository: FeedbackMemoryRepository,
) -> list[str]:
    candidates = build_multimodal_policy_candidates(multimodal_memory_row)
    activated: list[str] = []
    for candidate in candidates:
        result = upsert_multimodal_policy_candidate(candidate, repository=repository)
        if result.ok and result.active and result.policy_id:
            activated.append(result.policy_id)
    return activated


def summarize_multimodal_policy_candidates(multimodal_memory_row: dict[str, Any]) -> str:
    return "\n".join(
        json.dumps(
            {
                "policy_key": candidate.policy_key,
                "scope_type": candidate.scope_type,
                "rule_type": candidate.rule_type,
                "rule_payload": candidate.rule_payload,
            },
            ensure_ascii=False,
        )
        for candidate in build_multimodal_policy_candidates(multimodal_memory_row)
    )


def _build_candidates(
    multimodal_memory_row: dict[str, Any],
    *,
    scope_types: tuple[str, ...],
    rule_type: str,
    rule_payload: dict[str, Any],
    base_priority: int,
    changed_fields: list[str],
) -> list[MultimodalPolicyCandidate]:
    candidates: list[MultimodalPolicyCandidate] = []
    for scope_type in scope_types:
        policy_key = _build_policy_key(
            multimodal_memory_row,
            scope_type=scope_type,
            rule_type=rule_type,
            rule_payload=rule_payload,
        )
        if not policy_key:
            continue
        candidates.append(
            MultimodalPolicyCandidate(
                policy_key=policy_key,
                scope_type=scope_type,
                topic_type=_text_or_none(multimodal_memory_row.get("topic_type")),
                entity_focus=_text_or_none(multimodal_memory_row.get("entity_focus")),
                event_type=_text_or_none(multimodal_memory_row.get("event_type")),
                angle_type=_text_or_none(multimodal_memory_row.get("angle_type")),
                asset_fingerprint=_text_or_none(multimodal_memory_row.get("asset_fingerprint")),
                topic_fingerprint=_text_or_none(multimodal_memory_row.get("topic_fingerprint")),
                shot_type=_text_or_none(multimodal_memory_row.get("shot_type")),
                subject_role=_text_or_none(multimodal_memory_row.get("subject_role")),
                rule_type=rule_type,
                rule_payload=dict(rule_payload),
                priority=multimodal_policy_priority_for_scope(scope_type, base_priority=base_priority),
                multimodal_memory_id=_text_or_none(multimodal_memory_row.get("id")),
                changed_fields=list(changed_fields),
                source_run_dir=_text_or_none(multimodal_memory_row.get("source_run_dir")),
                before_snapshot=_snapshot(multimodal_memory_row, prefix="before_"),
                after_snapshot=_snapshot(multimodal_memory_row, prefix="after_"),
            )
        )
    return candidates


def _build_policy_key(
    multimodal_memory_row: dict[str, Any],
    *,
    scope_type: str,
    rule_type: str,
    rule_payload: dict[str, Any],
) -> str | None:
    payload_sig = build_policy_payload_signature(rule_payload)
    if scope_type == "exact_asset_fingerprint":
        asset_fingerprint = _text(multimodal_memory_row.get("asset_fingerprint"))
        if not asset_fingerprint:
            return None
        return f"{scope_type}|{asset_fingerprint}|{rule_type}|{payload_sig}"
    if scope_type == "topic_shot_bucket":
        topic_fingerprint = _text(multimodal_memory_row.get("topic_fingerprint"))
        shot_type = _text(multimodal_memory_row.get("shot_type"))
        if not topic_fingerprint or not shot_type:
            return None
        return f"{scope_type}|{topic_fingerprint}|{shot_type}|{rule_type}|{payload_sig}"
    if scope_type == "entity_event_bucket":
        bucket = [
            _text(multimodal_memory_row.get("entity_focus")) or "*",
            _text(multimodal_memory_row.get("event_type")) or "*",
            _text(multimodal_memory_row.get("shot_type")) or "*",
        ]
        return f"{scope_type}|{'|'.join(bucket)}|{rule_type}|{payload_sig}"
    if scope_type == "generic_shot_role_bucket":
        shot_type = _text(multimodal_memory_row.get("shot_type")) or "*"
        subject_role = _text(multimodal_memory_row.get("subject_role")) or "*"
        return f"{scope_type}|{shot_type}|{subject_role}|{rule_type}|{payload_sig}"
    return None


def _snapshot(multimodal_memory_row: dict[str, Any], *, prefix: str) -> dict[str, Any]:
    field_map = {
        "usage_recommendation": multimodal_memory_row.get(f"{prefix}usage_recommendation"),
        "scene_description": multimodal_memory_row.get(f"{prefix}scene_description"),
        "humor_point": multimodal_memory_row.get(f"{prefix}humor_point"),
        "tag_summary": multimodal_memory_row.get(f"{prefix}tag_summary"),
        "subject_tags": list(multimodal_memory_row.get(f"{prefix}subject_tags") or []),
        "event_tags": list(multimodal_memory_row.get(f"{prefix}event_tags") or []),
        "emotion_tags": list(multimodal_memory_row.get(f"{prefix}emotion_tags") or []),
        "composition_tags": list(multimodal_memory_row.get(f"{prefix}composition_tags") or []),
        "risk_tags": list(multimodal_memory_row.get(f"{prefix}risk_tags") or []),
        "caution_note": multimodal_memory_row.get(f"{prefix}caution_note"),
    }
    return field_map


def _diff_added(before: Any, after: Any) -> list[str]:
    before_set = {str(item).strip() for item in (before or []) if str(item).strip()}
    return [str(item).strip() for item in (after or []) if str(item).strip() and str(item).strip() not in before_set]


def _diff_removed(before: Any, after: Any) -> list[str]:
    after_set = {str(item).strip() for item in (after or []) if str(item).strip()}
    return [str(item).strip() for item in (before or []) if str(item).strip() and str(item).strip() not in after_set]


def _is_action_hero_context(row: dict[str, Any]) -> bool:
    shot_type = _text(row.get("shot_type"))
    entity_focus = _text(row.get("entity_focus"))
    event_type = _text(row.get("event_type"))
    angle_type = _text(row.get("angle_type"))
    subject_role = _text(row.get("subject_role"))
    return (
        shot_type in {"closeup", "medium"}
        and entity_focus in {"player", ""}
        and (
            bool(row.get("is_action_shot"))
            or event_type in {"home_run", "walk_off", "hit", "player_highlight"}
            or angle_type in {"celebration", "highlight", "impact"}
            or subject_role in {"batter", "pitcher", "player"}
        )
    )


def _is_supporting_reaction_context(row: dict[str, Any]) -> bool:
    return (not bool(row.get("is_action_shot"))) and _text(row.get("shot_type")) in {"closeup", "medium", "wide"}


def _looks_generic_summary(text: str) -> bool:
    compact = re.sub(r"\s+", " ", text).strip()
    return len(compact) <= 20 or any(phrase in compact for phrase in _GENERIC_TAG_SUMMARY_PHRASES)


def _looks_generic_scene(text: str) -> bool:
    compact = re.sub(r"\s+", " ", text).strip()
    return len(compact) <= 28 or any(phrase in compact for phrase in _GENERIC_SCENE_PHRASES)


def _dedupe_candidates(candidates: list[MultimodalPolicyCandidate]) -> list[MultimodalPolicyCandidate]:
    deduped: dict[str, MultimodalPolicyCandidate] = {}
    for candidate in candidates:
        deduped.setdefault(candidate.policy_key, candidate)
    return list(deduped.values())


def _text(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _text_or_none(value: Any) -> str | None:
    text = _text(value)
    return text or None
