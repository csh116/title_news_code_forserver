from __future__ import annotations

import os
from pathlib import Path


_DEFAULT_ENV_LOADED = False


def load_env_file(env_path: str | Path) -> None:
    path = Path(env_path)
    if not path.exists():
        return

    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def load_default_env(start_dir: str | Path | None = None) -> Path | None:
    global _DEFAULT_ENV_LOADED
    if _DEFAULT_ENV_LOADED:
        return None

    base_dir = Path(start_dir) if start_dir else Path.cwd()
    for directory in [base_dir, *base_dir.parents]:
        candidate = directory / ".env"
        if candidate.exists():
            load_env_file(candidate)
            _DEFAULT_ENV_LOADED = True
            return candidate

    _DEFAULT_ENV_LOADED = True
    return None
