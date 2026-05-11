from __future__ import annotations

from copy import deepcopy
from typing import Any

from kbo_card_news.feedback_memory.multimodal_policy_models import (
    AssetPolicyCorrectionDebug,
    MULTIMODAL_POLICY_SCOPE_PRIORITY,
    MultimodalCorrectionPolicy,
    MultimodalPolicyApplyResult,
    MultimodalPolicyResolution,
)
from kbo_card_news.feedback_memory.multimodal_policy_rules import apply_multimodal_policy_rule
from kbo_card_news.feedback_memory.multimodal_policy_storage import (
    mark_multimodal_policies_applied,
    select_active_multimodal_policies,
)
from kbo_card_news.feedback_memory.storage import FeedbackMemoryRepository
from kbo_card_news.models.issue import AssetMultimodalInsight, IssueAssetContext, IssueMultimodalAnalysis


def resolve_applicable_multimodal_policies(
    context: dict[str, Any],
    *,
    repository: FeedbackMemoryRepository | None,
) -> MultimodalPolicyResolution:
    if repository is None:
        return MultimodalPolicyResolution()

    active_policies = select_active_multimodal_policies(repository=repository)
    matched: list[MultimodalCorrectionPolicy] = []
    scope_debug: list[str] = []
    for policy in active_policies:
        if _policy_matches_context(policy, context):
            matched.append(policy)
            scope_debug.append(f"{policy.scope_type}:{policy.rule_type}:{policy.id}")
    matched.sort(
        key=lambda item: (
            MULTIMODAL_POLICY_SCOPE_PRIORITY.get(item.scope_type, 0),
            int(item.priority),
            int(item.evidence_count),
        ),
        reverse=True,
    )
    return MultimodalPolicyResolution(policies=matched, scope_debug=scope_debug)


def apply_multimodal_policies(
    *,
    analysis: IssueMultimodalAnalysis,
    input_assets: list[IssueAssetContext],
    topic_metadata: dict[str, Any] | None,
    repository: FeedbackMemoryRepository | None,
) -> MultimodalPolicyApplyResult:
    if repository is None:
        return MultimodalPolicyApplyResult()

    input_by_ref = {asset.asset_id or asset.origin_url: asset for asset in input_assets}
    all_applied_ids: list[str] = []
    all_applied_types: list[str] = []
    summary_lines: list[str] = []
    asset_debug: dict[str, AssetPolicyCorrectionDebug] = {}

    for insight in analysis.assets:
        asset_context = input_by_ref.get(insight.asset_reference)
        context = _build_asset_context(insight, asset_context=asset_context, topic_metadata=topic_metadata)
        resolution = resolve_applicable_multimodal_policies(context, repository=repository)
        if not resolution.policies:
            continue

        current_payload = _insight_to_payload(insight)
        original_payload = deepcopy(current_payload)
        claimed_fields: set[str] = set()
        applied_ids: list[str] = []
        applied_types: list[str] = []
        corrected_fields: list[str] = []
        asset_summaries: list[str] = []

        for policy in resolution.policies:
            next_payload, changed_fields, summary = apply_multimodal_policy_rule(
                policy,
                asset_payload=current_payload,
                context=context,
            )
            effective_fields = [field_name for field_name in changed_fields if field_name not in claimed_fields]
            if not effective_fields:
                continue
            for field_name in effective_fields:
                current_payload[field_name] = next_payload.get(field_name)
                claimed_fields.add(field_name)
                corrected_fields.append(field_name)
            applied_ids.append(policy.id)
            applied_types.append(policy.rule_type)
            if summary:
                asset_summaries.append(summary)

        if not applied_ids:
            continue

        _apply_payload_to_insight(insight, current_payload)
        debug = AssetPolicyCorrectionDebug(
            asset_reference=insight.asset_reference,
            corrected_fields=corrected_fields,
            applied_policy_ids=applied_ids,
            applied_policy_types=applied_types,
            policy_correction_summary=", ".join(asset_summaries) if asset_summaries else None,
            pre_correction_snapshot=original_payload,
        )
        asset_debug[insight.asset_reference] = debug
        all_applied_ids.extend(applied_ids)
        all_applied_types.extend(applied_types)
        summary_lines.append(f"{insight.asset_reference}: {debug.policy_correction_summary or ','.join(corrected_fields)}")

    if all_applied_ids:
        mark_multimodal_policies_applied(all_applied_ids, repository=repository)

    return MultimodalPolicyApplyResult(
        policy_correction_used=bool(all_applied_ids),
        applied_policy_ids=all_applied_ids,
        applied_policy_types=all_applied_types,
        policy_correction_summary=" | ".join(summary_lines) if summary_lines else None,
        corrected_fields_by_asset={
            asset_reference: debug.corrected_fields
            for asset_reference, debug in asset_debug.items()
        },
        asset_debug=asset_debug,
    )


def _build_asset_context(
    insight: AssetMultimodalInsight,
    *,
    asset_context: IssueAssetContext | None,
    topic_metadata: dict[str, Any] | None,
) -> dict[str, Any]:
    topic_metadata = topic_metadata or {}
    payload = dict(insight.analysis_payload or {})
    return {
        "asset_reference": insight.asset_reference,
        "asset_fingerprint": payload.get("asset_fingerprint"),
        "topic_fingerprint": topic_metadata.get("topic_fingerprint") or payload.get("topic_fingerprint"),
        "topic_type": topic_metadata.get("topic_type"),
        "entity_focus": topic_metadata.get("entity_focus"),
        "event_type": topic_metadata.get("event_type"),
        "angle_type": topic_metadata.get("angle_type"),
        "shot_type": payload.get("shot_type"),
        "subject_role": payload.get("subject_role"),
        "asset_caption": getattr(asset_context, "caption", None),
        "asset_ocr_text": getattr(asset_context, "ocr_text", None),
    }


def _policy_matches_context(policy: MultimodalCorrectionPolicy, context: dict[str, Any]) -> bool:
    if policy.scope_type == "exact_asset_fingerprint":
        return bool(policy.asset_fingerprint) and policy.asset_fingerprint == _text(context.get("asset_fingerprint"))
    if policy.scope_type == "topic_shot_bucket":
        return (
            bool(policy.topic_fingerprint)
            and policy.topic_fingerprint == _text(context.get("topic_fingerprint"))
            and _match_optional(policy.shot_type, _text(context.get("shot_type")))
        )
    if policy.scope_type == "entity_event_bucket":
        return (
            _match_optional(policy.entity_focus, _text(context.get("entity_focus")))
            and _match_optional(policy.event_type, _text(context.get("event_type")))
            and _match_optional(policy.shot_type, _text(context.get("shot_type")))
        )
    if policy.scope_type == "generic_shot_role_bucket":
        return (
            _match_optional(policy.shot_type, _text(context.get("shot_type")))
            and _match_optional(policy.subject_role, _text(context.get("subject_role")))
        )
    return False


def _insight_to_payload(insight: AssetMultimodalInsight) -> dict[str, Any]:
    return {
        "usage_recommendation": insight.usage_recommendation,
        "subject_tags": list(insight.subject_tags),
        "event_tags": list(insight.event_tags),
        "emotion_tags": list(insight.emotion_tags),
        "composition_tags": list(insight.composition_tags),
        "risk_tags": list(insight.risk_tags),
        "tag_summary": insight.tag_summary,
        "scene_description": insight.scene_description,
        "humor_point": insight.humor_point,
        "caution_note": insight.caution_note or "",
    }


def _apply_payload_to_insight(insight: AssetMultimodalInsight, payload: dict[str, Any]) -> None:
    insight.usage_recommendation = _text(payload.get("usage_recommendation"))
    insight.subject_tags = list(payload.get("subject_tags") or [])
    insight.event_tags = list(payload.get("event_tags") or [])
    insight.emotion_tags = list(payload.get("emotion_tags") or [])
    insight.composition_tags = list(payload.get("composition_tags") or [])
    insight.risk_tags = list(payload.get("risk_tags") or [])
    insight.tag_summary = _text(payload.get("tag_summary"))
    insight.scene_description = _text(payload.get("scene_description"))
    insight.humor_point = _text(payload.get("humor_point"))
    insight.caution_note = _text(payload.get("caution_note")) or None


def _match_optional(policy_value: str | None, actual_value: str) -> bool:
    if not policy_value:
        return True
    return policy_value == actual_value


def _text(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()
