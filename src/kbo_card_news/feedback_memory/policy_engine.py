from __future__ import annotations

from typing import Any

from kbo_card_news.feedback_memory.policy_models import (
    HEADLINE_POLICY_SCOPE_PRIORITY,
    HeadlineCorrectionPolicy,
    HeadlinePolicyApplyResult,
    HeadlinePolicyResolution,
)
from kbo_card_news.feedback_memory.policy_rules import apply_policy_rule
from kbo_card_news.feedback_memory.policy_storage import (
    mark_policies_applied,
    select_active_headline_policies,
)
from kbo_card_news.feedback_memory.storage import FeedbackMemoryRepository


def resolve_applicable_headline_policies(
    context: dict[str, Any],
    *,
    repository: FeedbackMemoryRepository | None,
) -> HeadlinePolicyResolution:
    if repository is None:
        return HeadlinePolicyResolution()

    active_policies = select_active_headline_policies(repository=repository)
    if not active_policies:
        return HeadlinePolicyResolution()

    matched: list[HeadlineCorrectionPolicy] = []
    scope_debug: list[str] = []
    for policy in active_policies:
        if _policy_matches_context(policy, context):
            matched.append(policy)
            scope_debug.append(f"{policy.scope_type}:{policy.rule_type}:{policy.id}")
    matched.sort(
        key=lambda item: (
            HEADLINE_POLICY_SCOPE_PRIORITY.get(item.scope_type, 0),
            int(item.priority),
            int(item.evidence_count),
        ),
        reverse=True,
    )
    return HeadlinePolicyResolution(policies=matched, scope_debug=scope_debug)


def apply_headline_policies(
    *,
    title_text: str,
    subheadline: str,
    context: dict[str, Any],
    repository: FeedbackMemoryRepository | None,
) -> HeadlinePolicyApplyResult:
    resolution = resolve_applicable_headline_policies(context, repository=repository)
    if not resolution.policies:
        return HeadlinePolicyApplyResult(
            title_text=title_text,
            subheadline=subheadline,
            pre_correction_title_text=title_text,
            pre_correction_subheadline=subheadline,
        )

    current_title = title_text
    current_subheadline = subheadline
    applied_ids: list[str] = []
    applied_types: list[str] = []
    summaries: list[str] = []

    for policy in resolution.policies:
        next_title, next_subheadline, summary = apply_policy_rule(
            policy,
            title_text=current_title,
            subheadline=current_subheadline,
            context=context,
        )
        if next_title == current_title and next_subheadline == current_subheadline:
            continue
        current_title = next_title
        current_subheadline = next_subheadline
        applied_ids.append(policy.id)
        applied_types.append(policy.rule_type)
        if summary:
            summaries.append(summary)

    if repository is not None and applied_ids:
        mark_policies_applied(applied_ids, repository=repository)

    return HeadlinePolicyApplyResult(
        title_text=current_title,
        subheadline=current_subheadline,
        policy_correction_used=bool(applied_ids),
        applied_policy_ids=applied_ids,
        applied_policy_types=applied_types,
        policy_correction_summary=", ".join(summaries) if summaries else None,
        pre_correction_title_text=title_text,
        pre_correction_subheadline=subheadline,
    )


def _policy_matches_context(policy: HeadlineCorrectionPolicy, context: dict[str, Any]) -> bool:
    topic_fingerprint = _text(context.get("topic_fingerprint"))
    team_name = _text(context.get("team_name"))
    topic_type = _text(context.get("topic_type"))
    entity_focus = _text(context.get("entity_focus"))
    event_type = _text(context.get("event_type"))
    angle_type = _text(context.get("angle_type"))

    if policy.scope_type == "topic_fingerprint_exact":
        return bool(policy.topic_fingerprint) and policy.topic_fingerprint == topic_fingerprint
    if policy.scope_type == "team_specific":
        return bool(policy.team_name) and policy.team_name == team_name
    if policy.scope_type == "topic_feature_bucket":
        return (
            _match_optional(policy.topic_type, topic_type)
            and _match_optional(policy.entity_focus, entity_focus)
            and _match_optional(policy.event_type, event_type)
            and _match_optional(policy.angle_type, angle_type)
        )
    return True


def _match_optional(policy_value: str | None, actual_value: str) -> bool:
    if not policy_value:
        return True
    return policy_value == actual_value


def _text(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()
