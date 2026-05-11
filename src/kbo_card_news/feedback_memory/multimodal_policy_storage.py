from __future__ import annotations

from datetime import datetime

from kbo_card_news.feedback_memory.models import FeedbackMemoryPolicyUpsertResult
from kbo_card_news.feedback_memory.multimodal_policy_models import (
    MULTIMODAL_POLICY_SCOPE_PRIORITY,
    MultimodalCorrectionPolicy,
    MultimodalPolicyCandidate,
)
from kbo_card_news.feedback_memory.policy_storage import build_policy_payload_signature
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
    "asset_fingerprint",
    "topic_fingerprint",
    "shot_type",
    "subject_role",
    "rule_type",
    "last_applied_at",
}
_EVIDENCE_JSON_FIELDS = {"changed_fields", "before_snapshot", "after_snapshot"}
_EVIDENCE_TEXT_FIELDS = {"policy_id", "multimodal_memory_id", "evidence_role", "source_run_dir"}


def multimodal_policy_priority_for_scope(scope_type: str, *, base_priority: int = 0) -> int:
    return MULTIMODAL_POLICY_SCOPE_PRIORITY.get(scope_type, 0) + int(base_priority)


def candidate_to_multimodal_policy_record(candidate: MultimodalPolicyCandidate) -> dict[str, object]:
    timestamp = datetime.now().isoformat(timespec="seconds")
    return {
        "id": candidate.policy_key,
        "created_at": timestamp,
        "updated_at": timestamp,
        "policy_key": candidate.policy_key,
        "scope_type": candidate.scope_type,
        "topic_type": candidate.topic_type,
        "entity_focus": candidate.entity_focus,
        "event_type": candidate.event_type,
        "angle_type": candidate.angle_type,
        "asset_fingerprint": candidate.asset_fingerprint,
        "topic_fingerprint": candidate.topic_fingerprint,
        "shot_type": candidate.shot_type,
        "subject_role": candidate.subject_role,
        "rule_type": candidate.rule_type,
        "rule_payload": dict(candidate.rule_payload or {}),
        "evidence_count": 0,
        "success_count": 0,
        "last_applied_at": None,
        "priority": int(candidate.priority),
        "active": False,
    }


def upsert_multimodal_policy_candidate(
    candidate: MultimodalPolicyCandidate,
    *,
    repository: FeedbackMemoryRepository,
    activation_threshold: int = 3,
    candidate_threshold: int = 2,
) -> FeedbackMemoryPolicyUpsertResult:
    existing_rows = repository.safe_select_records(
        """
        SELECT id, evidence_count, active
        FROM multimodal_correction_policies
        WHERE policy_key = ?
        LIMIT 1
        """,
        (candidate.policy_key,),
        bool_fields=_POLICY_BOOL_FIELDS,
        operation_name="select_multimodal_policy_by_key",
    )
    if not existing_rows.ok:
        return FeedbackMemoryPolicyUpsertResult(ok=False, error_message=existing_rows.error_message)

    timestamp = datetime.now().isoformat(timespec="seconds")
    if existing_rows.rows:
        current = dict(existing_rows.rows[0])
        policy_id = str(current["id"])
        evidence_count = int(current.get("evidence_count") or 0) + 1
        update_result = repository.safe_execute(
            """
            UPDATE multimodal_correction_policies
            SET updated_at = ?, evidence_count = ?, priority = ?
            WHERE id = ?
            """,
            (timestamp, evidence_count, int(candidate.priority), policy_id),
            operation_name="update_multimodal_correction_policy",
        )
        if not update_result.ok:
            return FeedbackMemoryPolicyUpsertResult(ok=False, error_message=update_result.error_message)
    else:
        record = candidate_to_multimodal_policy_record(candidate)
        record["evidence_count"] = 1
        policy_id = str(record["id"])
        evidence_count = 1
        insert_result = repository.safe_insert_record(
            "multimodal_correction_policies",
            record,
            json_fields=_POLICY_JSON_FIELDS,
            bool_fields=_POLICY_BOOL_FIELDS,
            plain_text_fields=_POLICY_TEXT_FIELDS,
            operation_name="insert_multimodal_correction_policy",
        )
        if not insert_result.ok:
            return FeedbackMemoryPolicyUpsertResult(ok=False, error_message=insert_result.error_message)

    if candidate.multimodal_memory_id:
        repository.safe_insert_record(
            "multimodal_correction_policy_evidence",
            {
                "policy_id": policy_id,
                "multimodal_memory_id": candidate.multimodal_memory_id,
                "evidence_role": candidate.evidence_role,
                "changed_fields": list(candidate.changed_fields or []),
                "source_run_dir": candidate.source_run_dir,
                "before_snapshot": dict(candidate.before_snapshot or {}),
                "after_snapshot": dict(candidate.after_snapshot or {}),
            },
            json_fields=_EVIDENCE_JSON_FIELDS,
            plain_text_fields=_EVIDENCE_TEXT_FIELDS,
            operation_name="insert_multimodal_correction_policy_evidence",
        )

    active = _refresh_multimodal_policy_activation(
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


def _refresh_multimodal_policy_activation(
    *,
    policy_id: str,
    candidate: MultimodalPolicyCandidate,
    repository: FeedbackMemoryRepository,
    activation_threshold: int,
    candidate_threshold: int,
) -> bool:
    sibling_rows = repository.safe_select_records(
        """
        SELECT id, evidence_count, active
        FROM multimodal_correction_policies
        WHERE scope_type = ?
          AND COALESCE(topic_type, '') = COALESCE(?, '')
          AND COALESCE(entity_focus, '') = COALESCE(?, '')
          AND COALESCE(event_type, '') = COALESCE(?, '')
          AND COALESCE(angle_type, '') = COALESCE(?, '')
          AND COALESCE(asset_fingerprint, '') = COALESCE(?, '')
          AND COALESCE(topic_fingerprint, '') = COALESCE(?, '')
          AND COALESCE(shot_type, '') = COALESCE(?, '')
          AND COALESCE(subject_role, '') = COALESCE(?, '')
          AND rule_type = ?
        """,
        (
            candidate.scope_type,
            candidate.topic_type,
            candidate.entity_focus,
            candidate.event_type,
            candidate.angle_type,
            candidate.asset_fingerprint,
            candidate.topic_fingerprint,
            candidate.shot_type,
            candidate.subject_role,
            candidate.rule_type,
        ),
        bool_fields=_POLICY_BOOL_FIELDS,
        operation_name="select_multimodal_policy_siblings",
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
        UPDATE multimodal_correction_policies
        SET active = ?, updated_at = ?
        WHERE id = ?
        """,
        (1 if should_activate else 0, timestamp, policy_id),
        operation_name="set_multimodal_policy_active_flag",
    )
    if should_activate:
        repository.safe_execute(
            """
            UPDATE multimodal_correction_policies
            SET active = 0, updated_at = ?
            WHERE scope_type = ?
              AND COALESCE(topic_type, '') = COALESCE(?, '')
              AND COALESCE(entity_focus, '') = COALESCE(?, '')
              AND COALESCE(event_type, '') = COALESCE(?, '')
              AND COALESCE(angle_type, '') = COALESCE(?, '')
              AND COALESCE(asset_fingerprint, '') = COALESCE(?, '')
              AND COALESCE(topic_fingerprint, '') = COALESCE(?, '')
              AND COALESCE(shot_type, '') = COALESCE(?, '')
              AND COALESCE(subject_role, '') = COALESCE(?, '')
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
                candidate.asset_fingerprint,
                candidate.topic_fingerprint,
                candidate.shot_type,
                candidate.subject_role,
                candidate.rule_type,
                policy_id,
            ),
            operation_name="deactivate_conflicting_multimodal_policies",
        )
    return should_activate


def select_active_multimodal_policies(
    *,
    repository: FeedbackMemoryRepository,
) -> list[MultimodalCorrectionPolicy]:
    result = repository.safe_select_records(
        """
        SELECT id, created_at, updated_at, policy_key, scope_type, topic_type, entity_focus,
               event_type, angle_type, asset_fingerprint, topic_fingerprint, shot_type,
               subject_role, rule_type, rule_payload, evidence_count, success_count,
               last_applied_at, priority, active
        FROM multimodal_correction_policies
        WHERE active = 1
        ORDER BY priority DESC, evidence_count DESC, updated_at DESC, id DESC
        """,
        json_fields=_POLICY_JSON_FIELDS,
        bool_fields=_POLICY_BOOL_FIELDS,
        operation_name="select_active_multimodal_policies",
    )
    if not result.ok:
        return []
    return [MultimodalCorrectionPolicy(**row) for row in result.rows]


def mark_multimodal_policies_applied(
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
        UPDATE multimodal_correction_policies
        SET success_count = success_count + 1,
            last_applied_at = ?,
            updated_at = ?
        WHERE id IN ({placeholders})
        """,
        (timestamp, timestamp, *policy_ids),
        operation_name="mark_multimodal_policies_applied",
    )
