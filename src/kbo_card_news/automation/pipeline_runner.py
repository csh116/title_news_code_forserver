from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import urllib.parse
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import ModuleType
from typing import Any, Callable

from kbo_card_news.config.env import load_default_env
from kbo_card_news.workflows import (
    build_approval_run_name,
    confirm_topic_selection,
    ensure_stage_dir,
)

ROOT_DIR = Path(__file__).resolve().parents[3]
SRC_DIR = ROOT_DIR / "src"
OUTPUT_ROOT = ROOT_DIR / "outputs"
SCRIPT_BATCH_TOPIC_SELECTION = ROOT_DIR / "tests" / "manual_checks" / "manual_check_batch_topic_selection.py"
SCRIPT_TITLE_HTML_EDITOR = ROOT_DIR / "tests" / "manual_checks" / "manual_check_title_html_editor_no_multimodal.py"
KST = timezone(timedelta(hours=9))


@dataclass(slots=True)
class CandidateGenerationResult:
    approval_run_dir: Path
    choice_json_path: Path
    report_path: Path
    candidate_text_path: Path


@dataclass(slots=True)
class TopicConfirmationResult:
    approval_run_dir: Path
    choice_json_path: Path
    confirmed_json_path: Path
    selected_topic_ids: list[str]


@dataclass(slots=True)
class EditorRunResult:
    approval_run_dir: Path
    manifest_path: Path
    report_path: Path
    editor_url: str
    topic_count: int


def create_approval_run_dir(
    explicit_dir: str | Path | None = None,
    *,
    timestamp: datetime | None = None,
) -> Path:
    if explicit_dir:
        run_dir = Path(explicit_dir).expanduser()
    else:
        run_dir = OUTPUT_ROOT / build_approval_run_name(timestamp=timestamp)
    run_dir.mkdir(parents=True, exist_ok=True)
    return run_dir


def generate_topic_candidates(
    *,
    approval_run_dir: str | Path | None = None,
    window_start_kst: str | None = None,
    window_end_kst: str | None = None,
    candidate_count: int | None = None,
    selection_engine: str = "heuristic",
) -> CandidateGenerationResult:
    run_dir = create_approval_run_dir(approval_run_dir)
    resolved_window_start, resolved_window_end = _resolve_candidate_window(
        window_start_kst=window_start_kst,
        window_end_kst=window_end_kst,
    )
    resolved_candidate_count = int(candidate_count or 10)
    _run_python_script(
        SCRIPT_BATCH_TOPIC_SELECTION,
        approval_run_dir=run_dir,
        args=[
            "--non-interactive",
            "--window-start-kst",
            resolved_window_start,
            "--window-end-kst",
            resolved_window_end,
            "--candidate-count",
            str(resolved_candidate_count),
            "--selection-engine",
            selection_engine,
        ],
    )
    stage_dir = run_dir / "01_topic_candidates"
    return CandidateGenerationResult(
        approval_run_dir=run_dir,
        choice_json_path=stage_dir / "topic_selection_choice.json",
        report_path=stage_dir / "topic_candidates_report.json",
        candidate_text_path=stage_dir / "topic_candidates.txt",
    )


def _resolve_candidate_window(
    *,
    window_start_kst: str | None,
    window_end_kst: str | None,
) -> tuple[str, str]:
    if window_start_kst and window_end_kst:
        return window_start_kst, window_end_kst
    window_end = datetime.now(KST).replace(second=0, microsecond=0)
    window_start = window_end - timedelta(hours=24)
    return (
        window_start_kst or window_start.strftime("%Y-%m-%d %H:%M"),
        window_end_kst or window_end.strftime("%Y-%m-%d %H:%M"),
    )


def confirm_topic_candidates(
    choice_json_path: str | Path,
    *,
    selected_topic_ids: list[str] | None = None,
    selected_indices: list[int] | None = None,
    approval_run_dir: str | Path | None = None,
) -> TopicConfirmationResult:
    choice_path = Path(choice_json_path).expanduser()
    payload = json.loads(choice_path.read_text(encoding="utf-8"))
    candidates = payload.get("candidates") or []
    if not isinstance(candidates, list) or not candidates:
        raise ValueError(f"no candidates found in {choice_path}")

    selected_ids = _resolve_selected_topic_ids(
        candidates,
        selected_topic_ids=selected_topic_ids,
        selected_indices=selected_indices,
    )
    payload["required_selection_count"] = len(selected_ids)
    payload["selected_topic_ids"] = selected_ids
    for candidate in candidates:
        if isinstance(candidate, dict):
            topic_id = str(candidate.get("topic_id") or "")
            candidate["selected"] = topic_id in selected_ids
    choice_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    confirmed = confirm_topic_selection(
        payload,
        required_count=int(payload.get("required_selection_count") or 0),
    )
    run_dir = create_approval_run_dir(approval_run_dir or _infer_approval_run_dir(choice_path))
    output_dir = ensure_stage_dir("02_topic_selection", run_dir=run_dir)
    output_path = output_dir / "topic_selection_confirmed.json"
    output_path.write_text(json.dumps(confirmed, ensure_ascii=False, indent=2), encoding="utf-8")
    return TopicConfirmationResult(
        approval_run_dir=run_dir,
        choice_json_path=choice_path,
        confirmed_json_path=output_path,
        selected_topic_ids=[str(item) for item in confirmed["selected_topic_ids"]],
    )


def build_title_editor_run(
    *,
    approval_run_dir: str | Path,
    confirmed_json_path: str | Path | None = None,
    host: str = "127.0.0.1",
    public_host: str | None = None,
    port: int = 8787,
    editor_token: str | None = None,
) -> EditorRunResult:
    load_default_env(ROOT_DIR)
    run_dir = create_approval_run_dir(approval_run_dir)
    confirmed_path = (
        Path(confirmed_json_path).expanduser()
        if confirmed_json_path
        else run_dir / "02_topic_selection" / "topic_selection_confirmed.json"
    )
    module = _load_title_editor_module()
    manifest_path = Path(module.build_no_multimodal_editor_run(run_dir, confirmed_path))
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    report_path = Path(str(manifest.get("report_path") or manifest_path.with_name("title_html_editor_report.json")))
    return EditorRunResult(
        approval_run_dir=run_dir,
        manifest_path=manifest_path,
        report_path=report_path,
        editor_url=build_editor_url(
            host=public_host or _public_host_from_bind_host(host),
            port=port,
            topic_index=1,
            editor_token=editor_token,
        ),
        topic_count=int(manifest.get("topic_count") or 0),
    )


def serve_title_editor(
    *,
    approval_run_dir: str | Path,
    manifest_path: str | Path | None = None,
    host: str = "127.0.0.1",
    port: int = 8787,
    editor_token: str | None = None,
    render_callback: Callable[[dict[str, Any]], None] | None = None,
    shutdown_after_render: bool = False,
    idle_timeout_seconds: int | None = None,
) -> None:
    load_default_env(ROOT_DIR)
    run_dir = create_approval_run_dir(approval_run_dir)
    resolved_manifest = (
        Path(manifest_path).expanduser()
        if manifest_path
        else run_dir / "03_title_html_editor_no_multimodal" / "title_html_editor_manifest.json"
    )
    module = _load_title_editor_module()
    module.EditorServer(
        run_dir=run_dir,
        manifest_path=resolved_manifest,
        host=host,
        port=port,
        token=editor_token,
        render_callback=render_callback,
        shutdown_after_render=shutdown_after_render,
        idle_timeout_seconds=idle_timeout_seconds,
    ).serve()


def build_editor_url(*, host: str, port: int, topic_index: int = 1, editor_token: str | None = None) -> str:
    url = f"http://{host}:{port}/topic/{topic_index}"
    if not editor_token:
        return url
    return f"{url}?{urllib.parse.urlencode({'token': editor_token})}"


def _public_host_from_bind_host(host: str) -> str:
    if host in {"0.0.0.0", "::"}:
        return "127.0.0.1"
    return host


def _resolve_selected_topic_ids(
    candidates: list[Any],
    *,
    selected_topic_ids: list[str] | None,
    selected_indices: list[int] | None,
) -> list[str]:
    if selected_topic_ids and selected_indices:
        raise ValueError("provide either selected_topic_ids or selected_indices, not both")
    if selected_topic_ids:
        available_ids = {
            str(candidate.get("topic_id") or "")
            for candidate in candidates
            if isinstance(candidate, dict)
        }
        missing = [topic_id for topic_id in selected_topic_ids if topic_id not in available_ids]
        if missing:
            raise ValueError(f"selected topic ids not found in candidates: {', '.join(missing)}")
        return [str(topic_id) for topic_id in selected_topic_ids]
    if selected_indices:
        selected_ids: list[str] = []
        for index in selected_indices:
            if index < 1 or index > len(candidates):
                raise ValueError(f"selected index out of range: {index}")
            candidate = candidates[index - 1]
            if not isinstance(candidate, dict):
                raise ValueError(f"candidate at index {index} is not an object")
            topic_id = str(candidate.get("topic_id") or "")
            if not topic_id:
                raise ValueError(f"candidate at index {index} has no topic_id")
            selected_ids.append(topic_id)
        if len(selected_ids) != len(set(selected_ids)):
            raise ValueError("selected indices resolve to duplicate topic ids")
        return selected_ids
    raise ValueError("selected_topic_ids or selected_indices is required")


def _infer_approval_run_dir(path: Path) -> Path:
    resolved = path.resolve()
    for parent in [resolved.parent, *resolved.parents]:
        if parent.name.startswith("approval_run_"):
            return parent
    raise ValueError(f"could not infer approval run dir from path: {path}")


def _run_python_script(
    script_path: Path,
    *,
    approval_run_dir: Path,
    args: list[str] | None = None,
) -> None:
    env = os.environ.copy()
    pythonpath_parts = [str(SRC_DIR)]
    existing_pythonpath = env.get("PYTHONPATH", "").strip()
    if existing_pythonpath:
        pythonpath_parts.append(existing_pythonpath)
    env["PYTHONPATH"] = os.pathsep.join(pythonpath_parts)
    env["APPROVAL_RUN_DIR"] = str(approval_run_dir)
    subprocess.run(
        [sys.executable, str(script_path), *(args or [])],
        cwd=str(ROOT_DIR),
        env=env,
        text=True,
        check=True,
    )


def _load_title_editor_module() -> ModuleType:
    module_name = "_kbo_card_news_title_html_editor_no_multimodal"
    spec = importlib.util.spec_from_file_location(module_name, SCRIPT_TITLE_HTML_EDITOR)
    if spec is None or spec.loader is None:
        raise ImportError(f"could not load title editor script: {SCRIPT_TITLE_HTML_EDITOR}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module
