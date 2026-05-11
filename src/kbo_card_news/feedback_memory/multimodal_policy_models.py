from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


MULTIMODAL_POLICY_SCOPE_PRIORITY = {
    "exact_asset_fingerprint": 400,
    "topic_shot_bucket": 300,
    "entity_event_bucket": 200,
    "generic_shot_role_bucket": 100,
}


@dataclass(slots=True)
class MultimodalCorrectionPolicy:
    id: str
    created_at: str
    updated_at: str
    policy_key: str
    scope_type: str
    topic_type: str | None
    entity_focus: str | None
    event_type: str | None
    angle_type: str | None
    asset_fingerprint: str | None
    topic_fingerprint: str | None
    shot_type: str | None
    subject_role: str | None
    rule_type: str
    rule_payload: dict[str, Any] = field(default_factory=dict)
    evidence_count: int = 0
    success_count: int = 0
    last_applied_at: str | None = None
    priority: int = 0
    active: bool = False


@dataclass(slots=True)
class MultimodalPolicyEvidence:
    policy_id: str
    multimodal_memory_id: str
    evidence_role: str
    changed_fields: list[str] = field(default_factory=list)
    source_run_dir: str | None = None
    before_snapshot: dict[str, Any] = field(default_factory=dict)
    after_snapshot: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class MultimodalPolicyCandidate:
    policy_key: str
    scope_type: str
    topic_type: str | None
    entity_focus: str | None
    event_type: str | None
    angle_type: str | None
    asset_fingerprint: str | None
    topic_fingerprint: str | None
    shot_type: str | None
    subject_role: str | None
    rule_type: str
    rule_payload: dict[str, Any] = field(default_factory=dict)
    priority: int = 0
    multimodal_memory_id: str | None = None
    evidence_role: str = "support"
    changed_fields: list[str] = field(default_factory=list)
    source_run_dir: str | None = None
    before_snapshot: dict[str, Any] = field(default_factory=dict)
    after_snapshot: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class AssetPolicyCorrectionDebug:
    asset_reference: str
    corrected_fields: list[str] = field(default_factory=list)
    applied_policy_ids: list[str] = field(default_factory=list)
    applied_policy_types: list[str] = field(default_factory=list)
    policy_correction_summary: str | None = None
    pre_correction_snapshot: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class MultimodalPolicyApplyResult:
    policy_correction_used: bool = False
    applied_policy_ids: list[str] = field(default_factory=list)
    applied_policy_types: list[str] = field(default_factory=list)
    policy_correction_summary: str | None = None
    corrected_fields_by_asset: dict[str, list[str]] = field(default_factory=dict)
    asset_debug: dict[str, AssetPolicyCorrectionDebug] = field(default_factory=dict)


@dataclass(slots=True)
class MultimodalPolicyResolution:
    policies: list[MultimodalCorrectionPolicy] = field(default_factory=list)
    scope_debug: list[str] = field(default_factory=list)
