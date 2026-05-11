from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(slots=True)
class FeedbackMemoryConfig:
    db_path: Path


@dataclass(slots=True)
class FeedbackMemoryInitializationResult:
    db_path: Path
    initialized: bool
    schema_version: int
    error_message: str | None = None


@dataclass(slots=True)
class FeedbackMemoryWriteResult:
    ok: bool
    rowcount: int = 0
    error_message: str | None = None


@dataclass(slots=True)
class FeedbackMemorySelectResult:
    ok: bool
    rows: list[dict[str, Any]]
    error_message: str | None = None


@dataclass(slots=True)
class FeedbackMemoryPolicyUpsertResult:
    ok: bool
    policy_id: str | None = None
    active: bool = False
    evidence_count: int = 0
    error_message: str | None = None
