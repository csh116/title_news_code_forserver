from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[3]
OUTPUT_ROOT = ROOT_DIR / "outputs"
KST = timezone(timedelta(hours=9))


def build_approval_run_name(*, timestamp: datetime | None = None) -> str:
    value = timestamp or datetime.now(KST)
    return f"approval_run_{value.strftime('%Y%m%d_%H%M%S')}"


def resolve_approval_run_dir(explicit_dir: str | Path | None = None) -> Path:
    if explicit_dir:
        return Path(explicit_dir).expanduser()
    env_value = str(os.getenv("APPROVAL_RUN_DIR") or "").strip()
    if env_value:
        return Path(env_value).expanduser()
    return OUTPUT_ROOT / build_approval_run_name()


def ensure_stage_dir(stage_name: str, *, run_dir: str | Path | None = None) -> Path:
    base_dir = resolve_approval_run_dir(run_dir)
    stage_dir = base_dir / stage_name
    stage_dir.mkdir(parents=True, exist_ok=True)
    return stage_dir
