from __future__ import annotations

import hashlib
import json
from datetime import datetime
from uuid import uuid4

from kbo_card_news.feedback_memory.models import FeedbackMemoryPolicyUpsertResult
from kbo_card_news.feedback_memory.policy_models import (
    HEADLINE_POLICY_SCOPE_PRIORITY,
    HeadlineCorrectionPolicy,
    HeadlinePolicyCandidate,
)
from kbo_card_news.feedback_memory.storage import FeedbackMemoryRepository

_POLICY_JSON_FIELDS = {"rule_payload"}
_POLICY_BOOL_FIELDS = {"active"}
_POLICY_TEXT_FIELDS = {
    "policy_key",
    "scope_type",
    "topic_type",
    "entity_focus",
    "event_type",
    "angle_type",
    "team_name",
    "topic_fingerprint",
    "rule_type",
    "last_applied_at",
}
_POLICY_EVIDENCE_TEXT_FIELDS = {"policy_id", "headline_memory_id", "evidence_role"}


def build_policy_payload_signature(rule_payload: dict[str, object] | None) -> str:
    encoded = json.dumps(rule_payload or {}, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha1(encoded.encode("utf-8")).hexdigest()[:12]


def policy_priority_for_scope(scope_type: str, *, base_priority: int = 0) -> int:
    return HEADLINE_POLICY_SCOPE_PRIORITY.get(scope_type, 0) + int(base_priority)


def candidate_to_policy_record(candidate: HeadlinePolicyCandidate) -> dict[str, object]:
    timestamp = datetime.now().isoformat(timespec="seconds")
    return {
        "id": str(uuid4()),
        "created_at": timestamp,
        "updated_at": timestamp,
        "policy_key": candidate.policy_key,
        "scope_type": candidate.scope_type,
        "topic_type": candidate.topic_type,
        "entity_focus": candidate.entity_focus,
        "event_type": candidate.event_type,
        "angle_type": candidate.angle_type,
        "team_name": candidate.team_name,
        "topic_fingerprint": candidate.topic_fingerprint,
        "rule_type": candidate.rule_type,
        "rule_payload": dict(candidate.rule_payload or {}),
        "evidence_count": 0,
        "success_count": 0,
        "last_applied_at": None,
        "priority": int(candidate.priority),
        "active": False,
    }


def upsert_headline_policy_candidate(
    candidate: HeadlinePolicyCandidate,
    *,
    repository: FeedbackMemoryRepository,
    activation_threshold: int = 3,
    candidate_threshold: int = 2,
) -> FeedbackMemoryPolicyUpsertResult:
    existing_rows = repository.safe_select_records(
        """
        SELECT id, created_at, updated_at, policy_key, scope_type, topic_type, entity_focus,
               event_type, angle_type, team_name, topic_fingerprint, rule_type, rule_payload,
               evidence_count, success_count, last_applied_at, priority, active
        FROM headline_correction_policies
        WHERE policy_key = ?
        LIMIT 1
        """,
        (candidate.policy_key,),
        json_fields=_POLICY_JSON_FIELDS,
        bool_fields=_POLICY_BOOL_FIELDS,
        operation_name="select_headline_policy_by_key",
    )
    if not existing_rows.ok:
        return FeedbackMemoryPolicyUpsertResult(ok=False, error_message=existing_rows.error_message)

    timestamp = datetime.now().isoformat(timespec="seconds")
    if existing_rows.rows:
        current = dict(existing_rows.rows[0])
        evidence_count = int(current.get("evidence_count") or 0) + 1
        active = bool(current.get("active"))
        update_result = repository.safe_execute(
            """
            UPDATE headline_correction_policies
            SET updated_at = ?, evidence_count = ?, priority = ?
            WHERE id = ?
            """,
            (
                timestamp,
                evidence_count,
                int(candidate.priority),
                current["id"],
            ),
            operation_name="update_headline_correction_policy",
        )
        if not update_result.ok:
            return FeedbackMemoryPolicyUpsertResult(ok=False, error_message=update_result.error_message)
        policy_id = str(current["id"])
    else:
        record = candidate_to_policy_record(candidate)
        record["evidence_count"] = 1
        insert_result = repository.safe_insert_record(
            "headline_correction_policies",
            record,
            json_fields=_POLICY_JSON_FIELDS,
            bool_fields=_POLICY_BOOL_FIELDS,
            plain_text_fields=_POLICY_TEXT_FIELDS,
            operation_name="insert_headline_correction_policy",
        )
        if not insert_result.ok:
            return FeedbackMemoryPolicyUpsertResult(ok=False, error_message=insert_result.error_message)
        policy_id = str(record["id"])
        evidence_count = 1
        active = False

    if candidate.headline_memory_id:
        repository.safe_insert_record(
            "headline_correction_policy_evidence",
            {
                "policy_id": policy_id,
                "headline_memory_id": candidate.headline_memory_id,
                "evidence_role": candidate.evidence_role,
            },
            plain_text_fields=_POLICY_EVIDENCE_TEXT_FIELDS,
            operation_name="insert_headline_correction_policy_evidence",
        )

    active = _refresh_policy_activation(
        policy_id=policy_id,
        candidate=candidate,
        repository=repository,
        activation_threshold=activation_threshold,
        candidate_threshold=candidate_threshold,
    )
    return FeedbackMemoryPolicyUpsertResult(
        ok=True,
        policy_id=policy_id,
        active=active,
        evidence_count=evidence_count,
    )


def _refresh_policy_activation(
    *,
    policy_id: str,
    candidate: HeadlinePolicyCandidate,
    repository: FeedbackMemoryRepository,
    activation_threshold: int,
    candidate_threshold: int,
) -> bool:
    sibling_rows = repository.safe_select_records(
        """
        SELECT id, evidence_count, active
        FROM headline_correction_policies
        WHERE scope_type = ?
          AND COALESCE(topic_type, '') = COALESCE(?, '')
          AND COALESCE(entity_focus, '') = COALESCE(?, '')
          AND COALESCE(event_type, '') = COALESCE(?, '')
          AND COALESCE(angle_type, '') = COALESCE(?, '')
          AND COALESCE(team_name, '') = COALESCE(?, '')
          AND COALESCE(topic_fingerprint, '') = COALESCE(?, '')
          AND rule_type = ?
        """,
        (
            candidate.scope_type,
            candidate.topic_type,
            candidate.entity_focus,
            candidate.event_type,
            candidate.angle_type,
            candidate.team_name,
            candidate.topic_fingerprint,
            candidate.rule_type,
        ),
        bool_fields=_POLICY_BOOL_FIELDS,
        operation_name="select_headline_policy_siblings",
    )
    if not sibling_rows.ok:
        return False

    target_row = next((row for row in sibling_rows.rows if str(row.get("id")) == policy_id), None)
    if target_row is None:
        return False

    target_evidence = int(target_row.get("evidence_count") or 0)
    sibling_max = max(
        [
            int(row.get("evidence_count") or 0)
            for row in sibling_rows.rows
            if str(row.get("id")) != policy_id
        ]
        or [0]
    )
    should_activate = target_evidence >= activation_threshold and target_evidence > sibling_max
    if not should_activate and target_evidence < candidate_threshold:
        should_activate = False

    timestamp = datetime.now().isoformat(timespec="seconds")
    repository.safe_execute(
        """
        UPDATE headline_correction_policies
        SET active = ?, updated_at = ?
        WHERE id = ?
        """,
        (1 if should_activate else 0, timestamp, policy_id),
        operation_name="set_headline_policy_active_flag",
    )
    if should_activate:
        repository.safe_execute(
            """
            UPDATE headline_correction_policies
            SET active = 0, updated_at = ?
            WHERE scope_type = ?
              AND COALESCE(topic_type, '') = COALESCE(?, '')
              AND COALESCE(entity_focus, '') = COALESCE(?, '')
              AND COALESCE(event_type, '') = COALESCE(?, '')
              AND COALESCE(angle_type, '') = COALESCE(?, '')
              AND COALESCE(team_name, '') = COALESCE(?, '')
              AND COALESCE(topic_fingerprint, '') = COALESCE(?, '')
              AND rule_type = ?
              AND id != ?
            """,
            (
                timestamp,
                candidate.scope_type,
                candidate.topic_type,
                candidate.entity_focus,
                candidate.event_type,
                candidate.angle_type,
                candidate.team_name,
                candidate.topic_fingerprint,
                candidate.rule_type,
                policy_id,
            ),
            operation_name="deactivate_conflicting_headline_policies",
        )
    return should_activate


def select_active_headline_policies(
    *,
    repository: FeedbackMemoryRepository,
) -> list[HeadlineCorrectionPolicy]:
    result = repository.safe_select_records(
        """
        SELECT id, created_at, updated_at, policy_key, scope_type, topic_type, entity_focus,
               event_type, angle_type, team_name, topic_fingerprint, rule_type, rule_payload,
               evidence_count, success_count, last_applied_at, priority, active
        FROM headline_correction_policies
        WHERE active = 1
        ORDER BY priority DESC, evidence_count DESC, updated_at DESC, id DESC
        """,
        json_fields=_POLICY_JSON_FIELDS,
        bool_fields=_POLICY_BOOL_FIELDS,
        operation_name="select_active_headline_policies",
    )
    if not result.ok:
        return []
    return [HeadlineCorrectionPolicy(**row) for row in result.rows]


def mark_policies_applied(
    policy_ids: list[str],
    *,
    repository: FeedbackMemoryRepository,
) -> None:
    if not policy_ids:
        return
    timestamp = datetime.now().isoformat(timespec="seconds")
    placeholders = ", ".join("?" for _ in policy_ids)
    repository.safe_execute(
        f"""
        UPDATE headline_correction_policies
        SET success_count = success_count + 1,
            last_applied_at = ?,
            updated_at = ?
        WHERE id IN ({placeholders})
        """,
        (timestamp, timestamp, *policy_ids),
        operation_name="mark_headline_policies_applied",
    )
