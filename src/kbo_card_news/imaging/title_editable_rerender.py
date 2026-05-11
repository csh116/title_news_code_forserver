from __future__ import annotations

import argparse
import json
import logging
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from kbo_card_news.feedback_memory import (
    FeedbackMemoryRepository,
    FeedbackMemoryWriteResult,
    build_topic_fingerprint,
    extract_topic_features,
    refresh_policies_from_headline_memory,
)

from kbo_card_news.imaging.topic_title_page import (
    TopicTitlePageRenderer,
    TopicTitlePageSpec,
    _build_title_output_basename,
    _resolve_title_team_color,
)

ROOT_DIR = Path(__file__).resolve().parents[3]
EDIT_HISTORY_PATH = ROOT_DIR / "edit_history.md"
USER_PROVIDED_IMAGE_DESCRIPTION = "사용자 지정 이미지(기존 멀티모달 분석 없음)"
LOGGER = logging.getLogger(__name__)


@dataclass(slots=True)
class EditableTitlePageRerenderResult:
    output_path: Path
    spec_path: Path
    render_iteration: int


@dataclass(slots=True)
class HeadlineMemoryRecordInput:
    record: dict[str, Any]
    change_count: int


class EditableTitlePageRerenderer:
    def __init__(
        self,
        *,
        renderer: TopicTitlePageRenderer | None = None,
        feedback_repository: FeedbackMemoryRepository | None = None,
    ) -> None:
        self.renderer = renderer or TopicTitlePageRenderer()
        self.feedback_repository = feedback_repository

    def rerender(
        self,
        editable_spec_path: Path | str,
        *,
        output_path: Path | str | None = None,
    ) -> EditableTitlePageRerenderResult:
        spec_path = Path(editable_spec_path).expanduser().resolve()
        input_payload = json.loads(spec_path.read_text(encoding="utf-8"))
        full_spec_path = _resolve_full_spec_path(spec_path, input_payload)
        editable_payload = json.loads(full_spec_path.read_text(encoding="utf-8"))
        original_payload = _load_original_payload(editable_payload)

        merged_payload = _merge_payloads(
            editable_payload=editable_payload,
            input_payload=input_payload,
            input_spec_path=spec_path,
        )
        merged_payload = _synchronize_image_selection(merged_payload, spec_path=spec_path)

        resolved_output_path = (
            Path(output_path).expanduser().resolve()
            if output_path
            else _default_rerender_output_path(merged_payload)
        )
        spec = _build_spec_from_payload(merged_payload, output_path=resolved_output_path)
        image_path = self._resolve_image_path(merged_payload)
        team_color = str(
            merged_payload.get("team_color_override")
            or merged_payload.get("team_color")
            or _resolve_title_team_color(merged_payload.get("team_name"))
        ).strip() or None
        self.renderer._render_spec_to_output(
            spec=spec,
            image_path=image_path,
            output_path=resolved_output_path,
            instagram_handle=str(merged_payload.get("instagram_handle") or self.renderer.instagram_handle),
            team_color_override=team_color,
        )

        render_iteration = int(merged_payload.get("render_iteration", 0)) + 1
        updated_payload = dict(merged_payload)
        updated_payload.update(
            {
                "output_path": str(resolved_output_path),
                "edited_output_path": str(resolved_output_path),
                "selected_image_resolved_path": str(image_path),
                "last_rendered_at": datetime.now().isoformat(timespec="seconds"),
                "render_iteration": render_iteration,
            }
        )
        full_spec_path.write_text(
            json.dumps(updated_payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        memory_before_payload = _resolve_headline_memory_before_payload(
            input_payload=input_payload,
            editable_payload=editable_payload,
            original_payload=original_payload,
        )
        _write_simple_edit_payload(updated_payload)
        _append_edit_history(
            original_payload=original_payload,
            edited_payload=updated_payload,
            output_path=resolved_output_path,
            spec_path=spec_path,
        )
        memory_input = _build_headline_memory_record_input(
            original_payload=memory_before_payload,
            edited_payload=updated_payload,
            spec_path=spec_path,
        )
        if memory_input.change_count > 0:
            memory_record = _finalize_headline_memory_record(memory_input.record)
            insert_result = insert_headline_edit_memory_record(
                memory_record,
                repository=self.feedback_repository,
            )
            if insert_result.ok:
                _refresh_headline_policies_after_insert(
                    memory_record,
                    repository=self.feedback_repository,
                )
        return EditableTitlePageRerenderResult(
            output_path=resolved_output_path,
            spec_path=spec_path,
            render_iteration=render_iteration,
        )

    def _resolve_image_path(self, payload: dict[str, Any]) -> Path:
        for key in (
            "selected_image_resolved_path",
            "selected_image_storage_path",
            "selected_image_source",
            "image_source",
            "selected_image_origin_url",
        ):
            raw = str(payload.get(key) or "").strip()
            if not raw:
                continue
            try:
                return self.renderer._resolve_image_path(raw)
            except Exception:
                continue
        raise FileNotFoundError("No usable image path found in editable spec JSON")


def _resolve_full_spec_path(spec_path: Path, input_payload: dict[str, Any]) -> Path:
    full_spec_value = str(input_payload.get("full_editable_spec_path") or "").strip()
    if full_spec_value:
        return Path(full_spec_value).expanduser().resolve()
    return spec_path


def _resolve_headline_memory_before_payload(
    *,
    input_payload: dict[str, Any],
    editable_payload: dict[str, Any],
    original_payload: dict[str, Any],
) -> dict[str, Any]:
    if str(input_payload.get("spec_kind") or "").strip() == "simple_editable":
        return editable_payload

    simple_edit_path = str(editable_payload.get("simple_edit_spec_path") or "").strip()
    if simple_edit_path:
        candidate = Path(simple_edit_path).expanduser()
        if candidate.exists():
            try:
                simple_payload = json.loads(candidate.read_text(encoding="utf-8"))
                if any(key in simple_payload for key in ("title_text", "subheadline")):
                    return simple_payload
            except Exception:
                pass
    return original_payload


def _merge_payloads(
    *,
    editable_payload: dict[str, Any],
    input_payload: dict[str, Any],
    input_spec_path: Path,
) -> dict[str, Any]:
    original_team_name = editable_payload.get("team_name")
    original_team_color = editable_payload.get("team_color")
    input_team_name = input_payload.get("team_name", editable_payload.get("team_name"))
    input_team_color = input_payload.get("team_color", editable_payload.get("team_color"))
    merged = dict(editable_payload)
    merged.update(
        {
            "title_text": input_payload.get("title_text", editable_payload.get("title_text")),
            "subheadline": input_payload.get("subheadline", editable_payload.get("subheadline")),
            "team_name": input_team_name,
            "team_color": input_team_color,
            "team_color_override": input_payload.get(
                "team_color_override",
                editable_payload.get("team_color_override"),
            ),
            "selected_image_candidate_file": input_payload.get(
                "selected_image_candidate_file",
                editable_payload.get("selected_image_candidate_file"),
            ),
            "selected_image_path": input_payload.get(
                "selected_image_path",
                editable_payload.get("selected_image_path"),
            ),
            "candidate_manifest_path": input_payload.get(
                "candidate_manifest_path",
                editable_payload.get("candidate_manifest_path"),
            ),
            "simple_edit_spec_path": editable_payload.get("simple_edit_spec_path")
            or (
                str(input_spec_path)
                if input_payload.get("spec_kind") == "simple_editable"
                else editable_payload.get("simple_edit_spec_path")
            ),
        }
    )
    if (
        input_team_name != original_team_name
        and input_team_color == original_team_color
        and not str(merged.get("team_color_override") or "").strip()
    ):
        merged["team_color"] = _resolve_title_team_color(input_team_name)
    if not str(merged.get("team_color") or "").strip():
        merged["team_color"] = _resolve_title_team_color(merged.get("team_name"))
    return merged


def _synchronize_image_selection(payload: dict[str, Any], *, spec_path: Path) -> dict[str, Any]:
    updated = dict(payload)
    custom_image_path = str(updated.get("selected_image_path") or "").strip()
    if custom_image_path:
        resolved_custom_path = Path(custom_image_path).expanduser().resolve()
        updated.update(
            {
                "selected_asset_reference": None,
                "selected_image_source": str(resolved_custom_path),
                "image_source": str(resolved_custom_path),
                "selected_image_origin_url": "",
                "selected_image_storage_path": str(resolved_custom_path),
                "selected_image_resolved_path": str(resolved_custom_path),
                "selected_image_usage_recommendation": "user_provided",
                "selected_image_confidence": None,
                "selected_image_scene_description": USER_PROVIDED_IMAGE_DESCRIPTION,
                "selected_image_humor_point": None,
                "selected_image_caution_note": None,
                "selected_image_candidate_file": None,
            }
        )
        return updated

    candidate_file = str(updated.get("selected_image_candidate_file") or "").strip()
    if not candidate_file:
        return updated
    candidate_file = Path(candidate_file).name

    manifest_path = Path(str(updated.get("candidate_manifest_path") or "")).expanduser()
    if not manifest_path.is_absolute():
        manifest_path = (spec_path.parent / manifest_path).resolve()
    if not manifest_path.exists():
        raise FileNotFoundError(f"candidate manifest not found: {manifest_path}")

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    selected_candidate = None
    for item in manifest:
        exported_path = str(item.get("exported_path") or "").strip()
        if exported_path and Path(exported_path).name == candidate_file:
            selected_candidate = item
            break
    if selected_candidate is None:
        raise ValueError(f"selected_image_candidate_file not found in manifest: {candidate_file}")

    exported_path = str(selected_candidate.get("exported_path") or "").strip()
    local_image_path = str(selected_candidate.get("local_image_path") or "").strip()
    selected_path = exported_path or local_image_path or str(selected_candidate.get("origin_url") or "").strip()
    updated.update(
        {
            "selected_asset_reference": selected_candidate.get("asset_reference"),
            "selected_image_source": selected_path,
            "image_source": selected_path,
            "selected_image_origin_url": str(selected_candidate.get("origin_url") or ""),
            "selected_image_storage_path": str(selected_candidate.get("storage_path") or local_image_path or ""),
            "selected_image_resolved_path": selected_path,
            "selected_image_usage_recommendation": selected_candidate.get("usage_recommendation"),
            "selected_image_confidence": selected_candidate.get("confidence"),
            "selected_image_scene_description": selected_candidate.get("scene_description"),
            "selected_image_humor_point": selected_candidate.get("humor_point"),
            "selected_image_caution_note": selected_candidate.get("caution_note"),
            "selected_image_path": None,
            "selected_image_candidate_file": candidate_file,
        }
    )
    return updated


def _write_simple_edit_payload(full_payload: dict[str, Any]) -> None:
    path_value = str(full_payload.get("simple_edit_spec_path") or "").strip()
    if not path_value:
        return
    simple_spec_path = Path(path_value).expanduser().resolve()
    existing = {}
    if simple_spec_path.exists():
        existing = json.loads(simple_spec_path.read_text(encoding="utf-8"))
    updated = dict(existing)
    updated.update(
        {
            "spec_kind": "simple_editable",
            "spec_path": str(simple_spec_path),
            "full_editable_spec_path": str(full_payload.get("spec_path") or ""),
            "candidate_manifest_path": str(full_payload.get("candidate_manifest_path") or ""),
            "original_output_path": str(full_payload.get("original_output_path") or full_payload.get("output_path") or ""),
            "output_path": str(full_payload.get("output_path") or ""),
            "topic_id": full_payload.get("topic_id"),
            "topic_name": full_payload.get("topic_name"),
            "date_text": full_payload.get("date_text"),
            "title_text": full_payload.get("title_text"),
            "subheadline": full_payload.get("subheadline"),
            "team_name": full_payload.get("team_name"),
            "team_color": full_payload.get("team_color"),
            "team_color_override": full_payload.get("team_color_override"),
            "memory_context_used": full_payload.get("memory_context_used"),
            "num_similar_cases": full_payload.get("num_similar_cases", 0),
            "referenced_memory_ids": full_payload.get("referenced_memory_ids") or [],
            "memory_context_summary": full_payload.get("memory_context_summary"),
            "policy_correction_used": full_payload.get("policy_correction_used"),
            "applied_policy_ids": full_payload.get("applied_policy_ids") or [],
            "applied_policy_types": full_payload.get("applied_policy_types") or [],
            "policy_correction_summary": full_payload.get("policy_correction_summary"),
            "pre_correction_title_text": full_payload.get("pre_correction_title_text"),
            "pre_correction_subheadline": full_payload.get("pre_correction_subheadline"),
            "selected_image_candidate_file": full_payload.get("selected_image_candidate_file"),
            "selected_image_path": full_payload.get("selected_image_path"),
            "render_iteration": full_payload.get("render_iteration", 0),
        }
    )
    simple_spec_path.write_text(
        json.dumps(updated, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _build_spec_from_payload(payload: dict[str, Any], *, output_path: Path) -> TopicTitlePageSpec:
    return TopicTitlePageSpec(
        topic_id=str(payload.get("topic_id") or ""),
        topic_name=str(payload.get("topic_name") or ""),
        draft_title=str(payload.get("draft_title") or ""),
        title_text=str(payload.get("title_text") or ""),
        subheadline=str(payload.get("subheadline") or ""),
        team_name=str(payload["team_name"]) if payload.get("team_name") is not None else None,
        date_text=str(payload.get("date_text") or ""),
        image_source=str(
            payload.get("selected_image_resolved_path")
            or payload.get("selected_image_storage_path")
            or payload.get("selected_image_source")
            or payload.get("image_source")
            or payload.get("selected_image_origin_url")
            or ""
        ),
        output_path=output_path,
        copy_source=str(payload.get("copy_source") or "rule_based"),
        copy_model_name=str(payload.get("copy_model_name") or "rule-based-title-copy-v1"),
        memory_context_used=bool(payload.get("memory_context_used", False)),
        num_similar_cases=int(payload.get("num_similar_cases", 0) or 0),
        referenced_memory_ids=list(payload.get("referenced_memory_ids") or []),
        memory_context_summary=str(payload.get("memory_context_summary") or "").strip() or None,
        policy_correction_used=bool(payload.get("policy_correction_used", False)),
        applied_policy_ids=list(payload.get("applied_policy_ids") or []),
        applied_policy_types=list(payload.get("applied_policy_types") or []),
        policy_correction_summary=str(payload.get("policy_correction_summary") or "").strip() or None,
        pre_correction_title_text=str(payload.get("pre_correction_title_text") or "").strip() or None,
        pre_correction_subheadline=str(payload.get("pre_correction_subheadline") or "").strip() or None,
        selected_asset_reference=payload.get("selected_asset_reference"),
        image_selection_source=str(payload.get("image_selection_source") or "rule_based"),
        image_selection_model_name=str(payload.get("image_selection_model_name") or "rule-based-title-image-v1"),
    )


def _default_rerender_output_path(spec_payload: dict[str, Any]) -> Path:
    original = Path(str(spec_payload.get("original_output_path") or spec_payload.get("output_path") or "")).expanduser()
    render_iteration = int(spec_payload.get("render_iteration", 0)) + 1
    base_name = _build_title_output_basename(
        date_text=str(spec_payload.get("date_text") or ""),
        title_text=str(spec_payload.get("title_text") or ""),
        fallback_text=str(spec_payload.get("topic_name") or "title"),
    )
    return original.with_name(f"{base_name}_edit{render_iteration:02d}{original.suffix}")


def _load_original_payload(spec_payload: dict[str, Any]) -> dict[str, Any]:
    original_spec_path = str(spec_payload.get("original_spec_path") or "").strip()
    if not original_spec_path:
        return {}
    path = Path(original_spec_path).expanduser()
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def _append_edit_history(
    *,
    original_payload: dict[str, Any],
    edited_payload: dict[str, Any],
    output_path: Path,
    spec_path: Path,
) -> None:
    changes = _collect_changes(original_payload, edited_payload)
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    lines = [
        f"## {timestamp}",
        f"- topic: `{edited_payload.get('topic_name') or edited_payload.get('topic_id') or '-'}`",
        f"- editable_json: `{spec_path}`",
        f"- output_png: `{output_path}`",
        f"- render_iteration: `{edited_payload.get('render_iteration', 0)}`",
    ]
    if not changes:
        lines.append("- 변경 사항: 없음")
    else:
        for label, before, after in changes:
            lines.append(f"- {label}: `{before or '-'} -> {after or '-'}`")
    lines.append("")
    with EDIT_HISTORY_PATH.open("a", encoding="utf-8") as handle:
        handle.write("\n".join(lines))
        handle.write("\n")


def _build_headline_memory_record_input(
    *,
    original_payload: dict[str, Any],
    edited_payload: dict[str, Any],
    spec_path: Path,
) -> HeadlineMemoryRecordInput:
    changes = _collect_headline_memory_changes(original_payload, edited_payload)
    source_run_dir = _infer_title_edit_source_run_dir(
        edited_payload=edited_payload,
        original_payload=original_payload,
        spec_path=spec_path,
    )
    topic_features = extract_topic_features(
        edited_payload,
        overrides={key: edited_payload.get(key, original_payload.get(key)) for key in (
            "topic_type",
            "entity_focus",
            "event_type",
            "angle_type",
            "article_count",
            "asset_count",
            "has_notable_numbers",
            "recommended_focus",
        )},
    )
    record = {
        "id": None,
        "created_at": None,
        "topic_fingerprint": build_topic_fingerprint(
            edited_payload,
            overrides={key: topic_features.get(key) for key in (
                "topic_type",
                "entity_focus",
                "event_type",
                "angle_type",
            )},
        ),
        **topic_features,
        "before_title_text": original_payload.get("title_text"),
        "after_title_text": edited_payload.get("title_text"),
        "before_subheadline": original_payload.get("subheadline"),
        "after_subheadline": edited_payload.get("subheadline"),
        "topic_id": edited_payload.get("topic_id") or original_payload.get("topic_id"),
        "topic_name": edited_payload.get("topic_name") or original_payload.get("topic_name"),
        "team_name": edited_payload.get("team_name") or original_payload.get("team_name"),
        "source_run_dir": str(source_run_dir) if source_run_dir else None,
        "source_spec_path": str(spec_path),
        "memory_context_used": _coerce_memory_context_used(edited_payload, original_payload),
        "referenced_memory_ids": _coerce_referenced_memory_ids(edited_payload, original_payload),
    }
    return HeadlineMemoryRecordInput(record=record, change_count=len(changes))


def _collect_headline_memory_changes(
    original_payload: dict[str, Any],
    edited_payload: dict[str, Any],
) -> list[tuple[str, str, str]]:
    tracked_keys = ("title_text", "subheadline")
    changes: list[tuple[str, str, str]] = []
    for key in tracked_keys:
        before = _normalize_history_value(original_payload.get(key))
        after = _normalize_history_value(edited_payload.get(key))
        if before == after:
            continue
        changes.append((key, before, after))
    return changes


def _coerce_memory_context_used(*payloads: dict[str, Any]) -> bool | None:
    for payload in payloads:
        value = payload.get("memory_context_used")
        if isinstance(value, bool):
            return value
        if isinstance(value, (int, float)):
            return bool(int(value))
    return None


def _coerce_referenced_memory_ids(*payloads: dict[str, Any]) -> list[str]:
    for payload in payloads:
        values = payload.get("referenced_memory_ids")
        if not isinstance(values, list):
            continue
        normalized = [str(value).strip() for value in values if str(value).strip()]
        if normalized:
            return normalized
    return []


def _finalize_headline_memory_record(record: dict[str, Any]) -> dict[str, Any]:
    finalized = dict(record)
    finalized.update(
        {
            "id": str(uuid4()),
            "created_at": datetime.now().isoformat(timespec="seconds"),
            "memory_context_used": (
                False if finalized.get("memory_context_used") is None else finalized.get("memory_context_used")
            ),
            "referenced_memory_ids": finalized.get("referenced_memory_ids") or [],
        }
    )
    return finalized


def insert_headline_edit_memory_record(
    record: dict[str, Any],
    *,
    repository: FeedbackMemoryRepository | None = None,
) -> FeedbackMemoryWriteResult:
    owns_repository = repository is None
    active_repository = repository or FeedbackMemoryRepository()
    try:
        return active_repository.safe_insert_record(
            "headline_edit_memory",
            record,
            json_fields={"referenced_memory_ids"},
            bool_fields={"memory_context_used", "has_notable_numbers"},
            plain_text_fields={
                "topic_fingerprint",
                "topic_type",
                "entity_focus",
                "event_type",
                "angle_type",
                "recommended_focus",
                "topic_id",
                "topic_name",
                "team_name",
                "source_run_dir",
                "source_spec_path",
            },
            operation_name="insert_headline_edit_memory",
        )
    finally:
        if owns_repository:
            active_repository.close()


def _refresh_headline_policies_after_insert(
    record: dict[str, Any],
    *,
    repository: FeedbackMemoryRepository | None = None,
) -> None:
    owns_repository = repository is None
    active_repository = repository or FeedbackMemoryRepository()
    try:
        refresh_policies_from_headline_memory(record, repository=active_repository)
    except Exception as exc:
        LOGGER.warning("headline policy refresh failed: %s", exc)
    finally:
        if owns_repository:
            active_repository.close()


def insert_headline_edit_memory(
    *,
    original_payload: dict[str, Any],
    edited_payload: dict[str, Any],
    spec_path: Path,
    repository: FeedbackMemoryRepository | None = None,
) -> FeedbackMemoryWriteResult:
    memory_input = _build_headline_memory_record_input(
        original_payload=original_payload,
        edited_payload=edited_payload,
        spec_path=spec_path,
    )
    memory_record = _finalize_headline_memory_record(memory_input.record)
    insert_result = insert_headline_edit_memory_record(
        memory_record,
        repository=repository,
    )
    if insert_result.ok:
        _refresh_headline_policies_after_insert(
            memory_record,
            repository=repository,
        )
    return insert_result


def _collect_changes(original_payload: dict[str, Any], edited_payload: dict[str, Any]) -> list[tuple[str, str, str]]:
    labels = (
        ("date_text", "날짜"),
        ("title_text", "타이틀"),
        ("subheadline", "부제"),
        ("team_name", "팀명"),
        ("team_color", "팀 컬러"),
        ("team_color_override", "팀 컬러 오버라이드"),
        ("selected_asset_reference", "선택 이미지 asset"),
        ("selected_image_candidate_file", "선택 이미지 후보 파일"),
        ("selected_image_path", "사용자 지정 이미지 경로"),
        ("selected_image_origin_url", "이미지 출처 URL"),
        ("selected_image_storage_path", "이미지 저장 경로"),
        ("selected_image_resolved_path", "이미지 실사용 경로"),
        ("selected_image_usage_recommendation", "이미지 추천 용도"),
        ("selected_image_confidence", "이미지 적합도"),
        ("selected_image_scene_description", "이미지 설명"),
        ("selected_image_humor_point", "이미지 포인트"),
        ("selected_image_caution_note", "이미지 주의사항"),
    )
    changes: list[tuple[str, str, str]] = []
    for key, label in labels:
        before = _normalize_history_value(original_payload.get(key))
        after = _normalize_history_value(edited_payload.get(key))
        if before == after:
            continue
        changes.append((label, before, after))
    return changes


def _normalize_history_value(value: Any) -> str:
    if value is None:
        return ""
    return str(value).replace("\n", " / ").strip()


def _infer_title_edit_source_run_dir(
    *,
    edited_payload: dict[str, Any],
    original_payload: dict[str, Any],
    spec_path: Path,
) -> Path | None:
    for key in ("original_output_path", "output_path", "spec_path", "original_spec_path"):
        raw = str(edited_payload.get(key) or original_payload.get(key) or "").strip()
        if not raw:
            continue
        try:
            return Path(raw).expanduser().resolve().parent
        except Exception:
            continue
    return spec_path.parent


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Rerender a title PNG from an editable JSON file.",
    )
    parser.add_argument("--json", required=True, help="Path to *.editable.json or *.simple_edit.json")
    parser.add_argument("--output", help="Optional output PNG path")
    return parser


def main() -> None:
    args = build_parser().parse_args()
    rerenderer = EditableTitlePageRerenderer()
    result = rerenderer.rerender(
        args.json,
        output_path=args.output,
    )
    print(f"output_path={result.output_path}")
    print(f"spec_path={result.spec_path}")
    print(f"render_iteration={result.render_iteration}")


if __name__ == "__main__":
    main()
