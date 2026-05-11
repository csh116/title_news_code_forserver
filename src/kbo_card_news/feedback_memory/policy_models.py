from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


HEADLINE_POLICY_SCOPE_PRIORITY = {
    "topic_fingerprint_exact": 400,
    "topic_feature_bucket": 300,
    "team_specific": 200,
    "global": 100,
}


@dataclass(slots=True)
class HeadlineCorrectionPolicy:
    id: str
    created_at: str
    updated_at: str
    policy_key: str
    scope_type: str
    topic_type: str | None
    entity_focus: str | None
    event_type: str | None
    angle_type: str | None
    team_name: str | None
    topic_fingerprint: str | None
    rule_type: str
    rule_payload: dict[str, Any] = field(default_factory=dict)
    evidence_count: int = 0
    success_count: int = 0
    last_applied_at: str | None = None
    priority: int = 0
    active: bool = False


@dataclass(slots=True)
class HeadlinePolicyEvidence:
    policy_id: str
    headline_memory_id: str
    evidence_role: str


@dataclass(slots=True)
class HeadlinePolicyCandidate:
    policy_key: str
    scope_type: str
    topic_type: str | None
    entity_focus: str | None
    event_type: str | None
    angle_type: str | None
    team_name: str | None
    topic_fingerprint: str | None
    rule_type: str
    rule_payload: dict[str, Any] = field(default_factory=dict)
    priority: int = 0
    headline_memory_id: str | None = None
    evidence_role: str = "support"


@dataclass(slots=True)
class HeadlinePolicyApplyResult:
    title_text: str
    subheadline: str
    policy_correction_used: bool = False
    applied_policy_ids: list[str] = field(default_factory=list)
    applied_policy_types: list[str] = field(default_factory=list)
    policy_correction_summary: str | None = None
    pre_correction_title_text: str | None = None
    pre_correction_subheadline: str | None = None


@dataclass(slots=True)
class HeadlinePolicyResolution:
    policies: list[HeadlineCorrectionPolicy] = field(default_factory=list)
    scope_debug: list[str] = field(default_factory=list)
