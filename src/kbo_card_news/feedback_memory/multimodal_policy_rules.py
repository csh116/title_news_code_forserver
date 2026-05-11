from __future__ import annotations

from copy import deepcopy
from typing import Any

from kbo_card_news.feedback_memory.multimodal_policy_models import MultimodalCorrectionPolicy

_ALLOWED_USAGE_VALUES = {"cover", "detail_a", "detail_b", "reaction", "quick_info", "data_context"}
_TAG_FIELDS = (
    "subject_tags",
    "event_tags",
    "emotion_tags",
    "composition_tags",
    "risk_tags",
)
_TEXT_FIELDS = (
    "usage_recommendation",
    "tag_summary",
    "scene_description",
    "humor_point",
    "caution_note",
)


def apply_multimodal_policy_rule(
    policy: MultimodalCorrectionPolicy,
    *,
    asset_payload: dict[str, Any],
    context: dict[str, Any],
) -> tuple[dict[str, Any], list[str], str | None]:
    updated = deepcopy(asset_payload)
    if policy.rule_type == "prefer_cover_for_action_hero_shot":
        updated["usage_recommendation"] = policy.rule_payload.get("preferred_usage") or "cover"
    elif policy.rule_type == "prefer_detail_for_supporting_reaction_shot":
        updated["usage_recommendation"] = policy.rule_payload.get("preferred_usage") or "detail_a"
    elif policy.rule_type == "remove_overclaiming_event_tag":
        removed = set(_string_list(policy.rule_payload.get("remove_tags")))
        updated["event_tags"] = [tag for tag in _string_list(updated.get("event_tags")) if tag not in removed]
    elif policy.rule_type == "prefer_specific_event_tag_when_supported":
        current = _string_list(updated.get("event_tags"))
        for tag in _string_list(policy.rule_payload.get("preferred_tags")):
            if tag not in current:
                current.append(tag)
        updated["event_tags"] = current
    elif policy.rule_type == "trim_generic_tag_summary_phrase":
        updated["tag_summary"] = _text(policy.rule_payload.get("preferred_summary"))
    elif policy.rule_type == "prefer_evidence_first_scene_description":
        updated["scene_description"] = _text(policy.rule_payload.get("preferred_scene_description"))
    elif policy.rule_type == "drop_weak_humor_point_when_non_humorous":
        updated["humor_point"] = _text(policy.rule_payload.get("drop_to"))
    elif policy.rule_type == "require_caution_note_for_risk_tag":
        current_risks = _string_list(updated.get("risk_tags"))
        for tag in _string_list(policy.rule_payload.get("required_risk_tags")):
            if tag not in current_risks:
                current_risks.append(tag)
        updated["risk_tags"] = current_risks
        if current_risks:
            updated["caution_note"] = _text(policy.rule_payload.get("required_caution_note"))
    else:
        return asset_payload, [], None

    corrected_fields = [
        field_name
        for field_name in (*_TEXT_FIELDS, *_TAG_FIELDS)
        if updated.get(field_name) != asset_payload.get(field_name)
    ]
    validated = validate_corrected_asset(
        original_asset=asset_payload,
        corrected_asset=updated,
        corrected_fields=corrected_fields,
        context=context,
    )
    effective_fields = [
        field_name
        for field_name in corrected_fields
        if validated.get(field_name) != asset_payload.get(field_name)
    ]
    summary = f"{policy.rule_type}:{','.join(effective_fields)}" if effective_fields else None
    return validated, effective_fields, summary


def validate_corrected_asset(
    *,
    original_asset: dict[str, Any],
    corrected_asset: dict[str, Any],
    corrected_fields: list[str],
    context: dict[str, Any],
) -> dict[str, Any]:
    validated = deepcopy(corrected_asset)

    if "usage_recommendation" in corrected_fields:
        usage = _text(validated.get("usage_recommendation"))
        if usage not in _ALLOWED_USAGE_VALUES:
            validated["usage_recommendation"] = original_asset.get("usage_recommendation")

    for field_name in _TAG_FIELDS:
        if field_name not in corrected_fields:
            continue
        validated[field_name] = _limit_tags(_string_list(validated.get(field_name)), group_name=field_name)
        if field_name != "risk_tags" and not validated[field_name] and field_name != "event_tags":
            validated[field_name] = original_asset.get(field_name)

    for field_name in ("tag_summary", "scene_description"):
        if field_name not in corrected_fields:
            continue
        text = _normalize_text(validated.get(field_name))
        if not text or len(text) > 180:
            validated[field_name] = original_asset.get(field_name)
        else:
            validated[field_name] = text

    if "humor_point" in corrected_fields:
        text = _normalize_text(validated.get("humor_point"))
        validated["humor_point"] = text
        if len(text) > 160:
            validated["humor_point"] = original_asset.get("humor_point")

    if "caution_note" in corrected_fields:
        text = _normalize_text(validated.get("caution_note"))
        validated["caution_note"] = text
        if len(text) > 160:
            validated["caution_note"] = original_asset.get("caution_note")

    risk_tags = _string_list(validated.get("risk_tags"))
    if risk_tags and not _normalize_text(validated.get("caution_note")):
        if "caution_note" in corrected_fields:
            validated["caution_note"] = original_asset.get("caution_note")
        if "risk_tags" in corrected_fields and not _normalize_text(validated.get("caution_note")):
            validated["risk_tags"] = original_asset.get("risk_tags")

    return validated


def _limit_tags(tags: list[str], *, group_name: str) -> list[str]:
    from kbo_card_news.multimodal.prompts import MULTIMODAL_TAG_DICTIONARY

    allowed = set(MULTIMODAL_TAG_DICTIONARY[group_name])
    unique: list[str] = []
    seen: set[str] = set()
    for tag in tags:
        if tag not in allowed or tag in seen:
            continue
        seen.add(tag)
        unique.append(tag)
    return unique


def _string_list(value: Any) -> list[str]:
    if isinstance(value, list):
        items = value
    else:
        items = list(value or [])
    return [str(item).strip() for item in items if str(item).strip()]


def _text(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _normalize_text(value: Any) -> str:
    return " ".join(_text(value).split()).strip()
