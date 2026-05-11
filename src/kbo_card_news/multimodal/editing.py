from __future__ import annotations

from copy import deepcopy
import json
import mimetypes
import shutil
import urllib.parse
import urllib.request
from html import escape
from dataclasses import asdict, dataclass, is_dataclass
from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from kbo_card_news.feedback_memory import (
    FeedbackMemoryRepository,
    FeedbackMemoryWriteResult,
    build_asset_fingerprint,
    build_topic_fingerprint,
    extract_asset_features,
    extract_topic_features,
    refresh_policies_from_multimodal_memory,
)
from kbo_card_news.multimodal.prompts import MULTIMODAL_TAG_DICTIONARY

ROOT_DIR = Path(__file__).resolve().parents[3]
MULTIMODAL_EDIT_HISTORY_PATH = ROOT_DIR / "multimodal_edit_history.md"


@dataclass(slots=True)
class MultimodalMemoryRecordInput:
    records: list[dict[str, Any]]
    change_count: int


MULTIMODAL_MEMORY_TABLE_FIELDS = {
    "id",
    "created_at",
    "topic_fingerprint",
    "asset_fingerprint",
    "topic_type",
    "entity_focus",
    "event_type",
    "angle_type",
    "article_count",
    "asset_count",
    "has_notable_numbers",
    "recommended_focus",
    "shot_type",
    "subject_role",
    "person_count_bucket",
    "is_action_shot",
    "is_post_game",
    "width",
    "height",
    "aspect_ratio",
    "caption_signal",
    "asset_reference",
    "before_usage_recommendation",
    "after_usage_recommendation",
    "before_scene_description",
    "after_scene_description",
    "before_humor_point",
    "after_humor_point",
    "before_tag_summary",
    "after_tag_summary",
    "before_subject_tags",
    "after_subject_tags",
    "before_event_tags",
    "after_event_tags",
    "before_emotion_tags",
    "after_emotion_tags",
    "before_composition_tags",
    "after_composition_tags",
    "before_risk_tags",
    "after_risk_tags",
    "before_caution_note",
    "after_caution_note",
    "source_run_dir",
    "source_report_path",
    "memory_context_used",
    "referenced_memory_ids",
}


def _serialize(value: Any) -> Any:
    if is_dataclass(value):
        return _serialize(asdict(value))
    if isinstance(value, dict):
        return {key: _serialize(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_serialize(item) for item in value]
    if isinstance(value, tuple):
        return [_serialize(item) for item in value]
    if isinstance(value, datetime):
        return value.isoformat()
    return value


def _analysis_assets(analysis: Any) -> list[dict[str, Any]]:
    payload = _serialize(analysis)
    return list(payload.get("assets") or [])


def write_multimodal_assets_text(run_dir: Path, analyses: list[Any]) -> Path:
    lines: list[str] = []
    for analysis_index, analysis in enumerate(analyses, start=1):
        payload = _serialize(analysis)
        lines.append(f"[analysis {analysis_index}] issue_id={payload.get('issue_id')}")
        lines.append(f"overall_summary={payload.get('overall_summary')}")
        for asset_index, asset in enumerate(payload.get("assets") or [], start=1):
            lines.append(f"  - asset[{asset_index}] ref={asset.get('asset_reference')}")
            lines.append(f"    usage={asset.get('usage_recommendation')} confidence={asset.get('confidence')}")
            lines.append(f"    subject_tags={', '.join(asset.get('subject_tags') or []) or '-'}")
            lines.append(f"    event_tags={', '.join(asset.get('event_tags') or []) or '-'}")
            lines.append(f"    emotion_tags={', '.join(asset.get('emotion_tags') or []) or '-'}")
            lines.append(f"    composition_tags={', '.join(asset.get('composition_tags') or []) or '-'}")
            lines.append(f"    risk_tags={', '.join(asset.get('risk_tags') or []) or '-'}")
            lines.append(f"    tag_summary={asset.get('tag_summary') or '-'}")
            lines.append(f"    scene_description={asset.get('scene_description') or '-'}")
            lines.append(f"    humor_point={asset.get('humor_point') or '-'}")
            lines.append(f"    caution_note={asset.get('caution_note') or '-'}")
        lines.append("")
    output_path = run_dir / "topic_multimodal_assets.txt"
    output_path.write_text("\n".join(lines), encoding="utf-8")
    return output_path


def export_multimodal_asset_images(
    *,
    run_dir: Path,
    structuring_inputs: list[Any],
    analyses: list[Any],
) -> tuple[Path, Path]:
    image_dir = run_dir / "multimodal_asset_images"
    image_dir.mkdir(parents=True, exist_ok=True)

    input_assets_by_ref: dict[str, dict[str, Any]] = {}
    for structuring_input in structuring_inputs:
        payload = _serialize(structuring_input)
        for asset in payload.get("assets") or []:
            ref = str(asset.get("asset_id") or asset.get("origin_url") or "").strip()
            if ref and ref not in input_assets_by_ref:
                input_assets_by_ref[ref] = asset

    manifest: list[dict[str, Any]] = []
    seen_refs: set[str] = set()
    for analysis in analyses:
        for asset in _analysis_assets(analysis):
            asset_reference = str(asset.get("asset_reference") or "").strip()
            if not asset_reference or asset_reference in seen_refs:
                continue
            seen_refs.add(asset_reference)
            source_asset = input_assets_by_ref.get(asset_reference, {})
            exported_path, export_error = _export_single_asset_image(
                source_asset=source_asset,
                asset_reference=asset_reference,
                image_dir=image_dir,
            )
            manifest.append(
                {
                    "asset_reference": asset_reference,
                    "origin_url": source_asset.get("origin_url"),
                    "storage_path": source_asset.get("storage_path"),
                    "mime_type": source_asset.get("mime_type"),
                    "exported_path": str(exported_path) if exported_path else None,
                    "export_error": export_error,
                }
            )

    manifest_path = image_dir / "image_manifest.json"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return image_dir, manifest_path


def _export_single_asset_image(
    *,
    source_asset: dict[str, Any],
    asset_reference: str,
    image_dir: Path,
) -> tuple[Path | None, str | None]:
    storage_path = str(source_asset.get("storage_path") or "").strip()
    origin_url = str(source_asset.get("origin_url") or "").strip()
    mime_type = str(source_asset.get("mime_type") or "").strip() or None
    extension = _guess_extension(storage_path or origin_url, mime_type=mime_type)
    output_path = image_dir / f"{len(list(image_dir.glob('*')))+1:02d}_{asset_reference[:8]}{extension}"

    if storage_path:
        candidate = Path(storage_path).expanduser()
        if candidate.exists():
            shutil.copy2(candidate, output_path)
            return output_path, None

    if origin_url.startswith("http://") or origin_url.startswith("https://"):
        request = urllib.request.Request(
            origin_url,
            headers={"User-Agent": "Mozilla/5.0"},
            method="GET",
        )
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                output_path.write_bytes(response.read())
            return output_path, None
        except Exception as exc:
            return None, str(exc)

    return None, "no usable local or remote image source"


def _guess_extension(source: str, *, mime_type: str | None) -> str:
    if mime_type:
        guessed = mimetypes.guess_extension(mime_type)
        if guessed:
            return guessed
    suffix = Path(urllib.parse.urlparse(source).path).suffix
    return suffix if suffix else ".jpg"


def write_multimodal_simple_edit_spec(
    *,
    run_dir: Path,
    report_path: Path,
    assets_text_path: Path,
    image_manifest_path: Path,
    analyses: list[Any],
) -> tuple[Path, Path]:
    original_report_path = run_dir / "topic_multimodal_report.original.json"
    if not original_report_path.exists():
        original_report_path.write_text(report_path.read_text(encoding="utf-8"), encoding="utf-8")

    image_manifest = json.loads(image_manifest_path.read_text(encoding="utf-8"))
    image_by_ref = {str(item.get("asset_reference")): item for item in image_manifest}
    first_analysis = _serialize(analyses[0]) if analyses else {}
    history_path = MULTIMODAL_EDIT_HISTORY_PATH
    if not history_path.exists():
        history_path.write_text(
            "# Multimodal Edit History\n\n수정 전에는 비어 있습니다.\n",
            encoding="utf-8",
        )

    payload = {
        "spec_kind": "multimodal_simple_edit",
        "spec_path": str(run_dir / "topic_multimodal.simple_edit.json"),
        "report_path": str(report_path),
        "original_report_path": str(original_report_path),
        "assets_text_path": str(assets_text_path),
        "image_manifest_path": str(image_manifest_path),
        "history_path": str(history_path),
        "issue_id": first_analysis.get("issue_id"),
        "overall_summary": first_analysis.get("overall_summary") or "",
        "memory_context_used": bool(first_analysis.get("metadata", {}).get("memory_context_used")),
        "num_similar_cases": int(first_analysis.get("metadata", {}).get("num_similar_cases") or 0),
        "referenced_memory_ids": list(first_analysis.get("metadata", {}).get("referenced_memory_ids") or []),
        "memory_context_by_asset": dict(first_analysis.get("metadata", {}).get("memory_context_by_asset") or {}),
        "assets": [],
        "edit_iteration": 0,
    }
    for asset in first_analysis.get("assets") or []:
        manifest_item = image_by_ref.get(str(asset.get("asset_reference")))
        analysis_payload = dict(asset.get("analysis_payload") or {})
        payload["assets"].append(
            {
                "asset_reference": asset.get("asset_reference"),
                "asset_type": asset.get("asset_type"),
                "image_file": Path(str(manifest_item.get("exported_path"))).name if manifest_item and manifest_item.get("exported_path") else None,
                "image_path": manifest_item.get("exported_path") if manifest_item else None,
                "origin_url": manifest_item.get("origin_url") if manifest_item else None,
                "storage_path": manifest_item.get("storage_path") if manifest_item else None,
                "usage_recommendation": asset.get("usage_recommendation"),
                "subject_tags": list(asset.get("subject_tags") or []),
                "event_tags": list(asset.get("event_tags") or []),
                "emotion_tags": list(asset.get("emotion_tags") or []),
                "composition_tags": list(asset.get("composition_tags") or []),
                "risk_tags": list(asset.get("risk_tags") or []),
                "tag_summary": asset.get("tag_summary") or "",
                "scene_description": asset.get("scene_description") or "",
                "humor_point": asset.get("humor_point") or "",
                "caution_note": asset.get("caution_note") or "",
                "confidence": asset.get("confidence"),
                "memory_context_used": bool(analysis_payload.get("memory_context_used")),
                "num_similar_cases": int(analysis_payload.get("num_similar_cases") or 0),
                "referenced_memory_ids": list(analysis_payload.get("referenced_memory_ids") or []),
                "policy_correction_used": bool(analysis_payload.get("policy_correction_used")),
                "applied_policy_ids": list(analysis_payload.get("applied_policy_ids") or []),
                "applied_policy_types": list(analysis_payload.get("applied_policy_types") or []),
                "corrected_fields": list(analysis_payload.get("corrected_fields") or []),
                "pre_correction_snapshot": dict(analysis_payload.get("pre_correction_snapshot") or {}),
                "crop_focus_note": str(analysis_payload.get("crop_focus_note") or ""),
                "avoid_region_note": str(analysis_payload.get("avoid_region_note") or ""),
                "layout_focus_hint": str(analysis_payload.get("layout_focus_hint") or ""),
            }
        )
    spec_path = Path(payload["spec_path"])
    spec_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return spec_path, history_path


def write_multimodal_review_html(
    *,
    run_dir: Path,
    simple_edit_path: Path,
) -> Path:
    spec = json.loads(simple_edit_path.read_text(encoding="utf-8"))
    image_manifest_path = Path(str(spec.get("image_manifest_path") or "")).expanduser().resolve()
    manifest = json.loads(image_manifest_path.read_text(encoding="utf-8")) if image_manifest_path.exists() else []
    image_by_ref = {str(item.get("asset_reference")): item for item in manifest}

    cards: list[str] = []
    for index, asset in enumerate(spec.get("assets") or [], start=1):
        ref = str(asset.get("asset_reference") or "")
        image_item = image_by_ref.get(ref, {})
        image_path = str(image_item.get("exported_path") or asset.get("image_path") or "")
        relative_image_src = ""
        if image_path:
            try:
                relative_image_src = Path(image_path).resolve().relative_to(run_dir.resolve()).as_posix()
            except Exception:
                relative_image_src = Path(image_path).name
        tags_html = _render_tag_block(asset)
        image_html = (
            f'<img src="{escape(relative_image_src)}" alt="{escape(ref)}" loading="lazy">'
            if relative_image_src
            else '<div class="missing">이미지 없음</div>'
        )
        cards.append(
            f"""
<article class="card" data-asset-reference="{escape(ref)}" data-asset-index="{index - 1}">
  <div class="image">{image_html}</div>
  <div class="body">
    <div class="meta">asset[{index}] · {escape(ref)}</div>
    <div class="meta">image_file: {escape(str(asset.get("image_file") or "-"))}</div>
    <div class="meta">usage: {escape(str(asset.get("usage_recommendation") or "-"))} · confidence: {escape(str(asset.get("confidence") or "-"))}</div>
    <div class="summary">{escape(str(asset.get("tag_summary") or "-"))}</div>
    <div class="desc">{escape(str(asset.get("scene_description") or "-"))}</div>
    {tags_html}
    <section class="editor">
      <div class="editor-title">이 asset 수정</div>
      <div class="editor-grid">
        { _render_edit_field("usage_recommendation", asset.get("usage_recommendation")) }
        { _render_edit_field("subject_tags", ", ".join(asset.get("subject_tags") or []), multiline=True) }
        { _render_edit_field("event_tags", ", ".join(asset.get("event_tags") or []), multiline=True) }
        { _render_edit_field("emotion_tags", ", ".join(asset.get("emotion_tags") or []), multiline=True) }
        { _render_edit_field("composition_tags", ", ".join(asset.get("composition_tags") or []), multiline=True) }
        { _render_edit_field("risk_tags", ", ".join(asset.get("risk_tags") or []), multiline=True) }
        { _render_edit_field("tag_summary", asset.get("tag_summary") or "", multiline=True) }
        { _render_edit_field("scene_description", asset.get("scene_description") or "", multiline=True) }
        { _render_edit_field("humor_point", asset.get("humor_point") or "", multiline=True) }
        { _render_edit_field("caution_note", asset.get("caution_note") or "", multiline=True) }
        { _render_edit_field("crop_focus_note", asset.get("crop_focus_note") or "", multiline=True) }
        { _render_edit_field("avoid_region_note", asset.get("avoid_region_note") or "", multiline=True) }
        { _render_edit_field("layout_focus_hint", asset.get("layout_focus_hint") or "", multiline=True) }
        { _render_edit_field("confidence", asset.get("confidence")) }
      </div>
    </section>
  </div>
</article>
"""
        )

    review_payload = json.dumps(spec, ensure_ascii=False)
    tag_dictionary_payload = json.dumps(MULTIMODAL_TAG_DICTIONARY, ensure_ascii=False)
    html = f"""<!doctype html>
<html lang="ko">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Multimodal Review</title>
  <style>
    :root {{
      --bg: #f5f1e8;
      --card: #fffdf8;
      --ink: #1f1b16;
      --line: #d7ccba;
      --accent: #b44d1f;
      --soft: #f0e2cf;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      font-family: "Apple SD Gothic Neo", "Malgun Gothic", sans-serif;
      background: linear-gradient(180deg, #efe5d5 0%, var(--bg) 100%);
      color: var(--ink);
    }}
    .wrap {{
      max-width: 1400px;
      margin: 0 auto;
      padding: 24px;
    }}
    .hero {{
      background: var(--card);
      border: 1px solid var(--line);
      border-radius: 20px;
      padding: 20px 24px;
      margin-bottom: 20px;
    }}
    .hero h1 {{
      margin: 0 0 10px;
      font-size: 28px;
    }}
    .hero p {{
      margin: 6px 0;
      white-space: pre-wrap;
    }}
    .viewer {{
      display: grid;
      gap: 18px;
    }}
    .viewer-top {{
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 12px;
      background: var(--card);
      border: 1px solid var(--line);
      border-radius: 18px;
      padding: 14px 16px;
    }}
    .viewer-status {{
      font-size: 14px;
      color: #6d6052;
      font-weight: 700;
    }}
    .viewer-actions {{
      display: flex;
      gap: 10px;
      align-items: center;
    }}
    .card {{
      background: var(--card);
      border: 1px solid var(--line);
      border-radius: 20px;
      overflow: hidden;
      display: grid;
      grid-template-columns: minmax(420px, 0.95fr) minmax(420px, 1.05fr);
      min-height: 640px;
      display: none;
    }}
    .card.active {{
      display: grid;
    }}
    .image {{
      background: #efe6d7;
      display: flex;
      align-items: center;
      justify-content: center;
      min-height: 640px;
      padding: 18px;
    }}
    .image img {{
      width: 100%;
      height: 100%;
      object-fit: contain;
      display: block;
      cursor: zoom-in;
    }}
    .missing {{
      padding: 16px;
      color: #7a6d5c;
      font-size: 14px;
    }}
    .lightbox {{
      position: fixed;
      inset: 0;
      background: rgba(15, 11, 8, 0.88);
      display: none;
      align-items: center;
      justify-content: center;
      padding: 24px;
      z-index: 9999;
    }}
    .lightbox.open {{
      display: flex;
    }}
    .lightbox-inner {{
      max-width: min(92vw, 1600px);
      max-height: 92vh;
      display: grid;
      gap: 10px;
      justify-items: center;
    }}
    .lightbox img {{
      max-width: 100%;
      max-height: calc(92vh - 60px);
      object-fit: contain;
      background: #f6efe3;
      border-radius: 14px;
    }}
    .lightbox-caption {{
      color: #f6efe3;
      font-size: 13px;
      text-align: center;
      word-break: break-all;
    }}
    .body {{
      padding: 18px 20px 20px;
      overflow-y: auto;
      max-height: 640px;
    }}
    .meta {{
      font-size: 12px;
      color: #6d6052;
      margin-bottom: 4px;
      word-break: break-all;
    }}
    .summary {{
      margin-top: 10px;
      font-size: 18px;
      font-weight: 700;
    }}
    .desc {{
      margin-top: 8px;
      font-size: 14px;
      line-height: 1.45;
      color: #40372f;
    }}
    .manual-guidance {{
      margin-top: 12px;
      display: grid;
      gap: 8px;
    }}
    .guidance-row {{
      background: #f6edde;
      border: 1px dashed var(--line);
      border-radius: 12px;
      padding: 10px 12px;
      font-size: 13px;
      line-height: 1.5;
    }}
    .guidance-label {{
      display: inline-block;
      min-width: 118px;
      color: var(--accent);
      font-weight: 700;
    }}
    .tags {{
      margin-top: 14px;
      display: grid;
      gap: 8px;
    }}
    .tag-row {{
      background: var(--soft);
      border-radius: 12px;
      padding: 10px 12px;
      font-size: 13px;
      line-height: 1.5;
    }}
    .tag-label {{
      display: inline-block;
      min-width: 112px;
      color: var(--accent);
      font-weight: 700;
    }}
    .toolbar {{
      display: flex;
      gap: 10px;
      flex-wrap: wrap;
      margin-top: 14px;
    }}
    button {{
      border: 0;
      border-radius: 999px;
      padding: 10px 16px;
      background: var(--accent);
      color: white;
      font-weight: 700;
      cursor: pointer;
    }}
    button.secondary {{
      background: #6b5b4d;
    }}
    .status {{
      margin-top: 10px;
      font-size: 13px;
      color: #6d6052;
    }}
    .editor {{
      margin-top: 14px;
      border-top: 1px solid var(--line);
      padding-top: 12px;
    }}
    .editor-title {{
      font-weight: 700;
      color: var(--accent);
      margin-bottom: 10px;
    }}
    .editor-grid {{
      display: grid;
      gap: 10px;
      margin-top: 10px;
    }}
    .field {{
      display: grid;
      gap: 6px;
    }}
    .field label {{
      font-size: 12px;
      font-weight: 700;
      color: #6d6052;
    }}
    .field input,
    .field textarea {{
      width: 100%;
      border: 1px solid var(--line);
      border-radius: 10px;
      padding: 10px 12px;
      font: inherit;
      background: #fff;
      color: var(--ink);
    }}
    .field textarea {{
      min-height: 74px;
      resize: vertical;
    }}
    .tag-picker {{
      margin-top: 8px;
      display: grid;
      gap: 6px;
    }}
    .tag-picker-row {{
      display: flex;
      gap: 6px;
      flex-wrap: wrap;
    }}
    .tag-chip {{
      border: 1px solid var(--line);
      background: #fff;
      color: var(--ink);
      border-radius: 999px;
      padding: 6px 10px;
      font-size: 12px;
      font-weight: 600;
      cursor: pointer;
    }}
    .tag-chip:hover {{
      background: var(--soft);
    }}
    .field-help {{
      font-size: 11px;
      color: #7a6d5c;
    }}
    @media (max-width: 900px) {{
      .card {{
        grid-template-columns: 1fr;
      }}
      .image {{
        min-height: 360px;
      }}
      .body {{
        max-height: none;
      }}
    }}
  </style>
</head>
<body>
  <div class="wrap">
    <section class="hero">
      <h1>Multimodal Review</h1>
      <p><strong>simple_edit.json</strong>: {escape(str(simple_edit_path))}</p>
      <p><strong>overall_summary</strong>: {escape(str(spec.get("overall_summary") or "-"))}</p>
      <div class="field">
        <label for="overall_summary">overall_summary 수정</label>
        <textarea id="overall_summary">{escape(str(spec.get("overall_summary") or ""))}</textarea>
      </div>
      <div class="toolbar">
        <button id="connect-file" class="secondary">simple_edit.json 연결</button>
        <button id="save-file">JSON 직접 저장</button>
        <button id="download-file" class="secondary">JSON 다운로드</button>
      </div>
      <div class="status" id="status">수정 후 저장하면 `topic_multimodal.simple_edit.json` 에 반영됩니다. 브라우저가 직접 저장을 지원하지 않으면 다운로드를 사용하세요.</div>
    </section>
    <section class="viewer">
      <div class="viewer-top">
        <div class="viewer-status" id="viewer-status">asset 1 / {max(len(spec.get("assets") or []), 1)}</div>
        <div class="viewer-actions">
          <button id="prev-asset" class="secondary">이전 사진</button>
          <button id="next-asset">다음 사진</button>
        </div>
      </div>
      <section class="viewer-stage">
      {''.join(cards)}
      </section>
    </section>
  </div>
  <div class="lightbox" id="lightbox" aria-hidden="true">
    <div class="lightbox-inner">
      <img id="lightbox-image" src="" alt="">
      <div class="lightbox-caption" id="lightbox-caption"></div>
    </div>
  </div>
  <script>
    const reviewData = {review_payload};
    const tagDictionary = {tag_dictionary_payload};
    let fileHandle = null;
    let currentIndex = 0;

    function setStatus(text) {{
      document.getElementById('status').textContent = text;
    }}

    function parseTagList(text) {{
      return String(text || '')
        .split(',')
        .map(item => item.trim())
        .filter(Boolean);
    }}

    function collectEdits() {{
      const next = structuredClone(reviewData);
      next.overall_summary = document.getElementById('overall_summary').value.trim();
      for (const card of document.querySelectorAll('.card[data-asset-reference]')) {{
        const ref = card.dataset.assetReference;
        const asset = (next.assets || []).find(item => item.asset_reference === ref);
        if (!asset) continue;
        for (const field of card.querySelectorAll('[data-field]')) {{
          const name = field.dataset.field;
          const raw = field.value;
          if (['subject_tags','event_tags','emotion_tags','composition_tags','risk_tags'].includes(name)) {{
            asset[name] = parseTagList(raw);
          }} else if (name === 'confidence') {{
            asset[name] = raw.trim() === '' ? null : Number(raw);
          }} else {{
            asset[name] = raw;
          }}
        }}
      }}
      return next;
    }}

    async function connectFile() {{
      if (!window.showOpenFilePicker) {{
        setStatus('이 브라우저는 직접 파일 연결을 지원하지 않습니다. 다운로드 방식으로 저장하세요.');
        return;
      }}
      const [handle] = await window.showOpenFilePicker({{
        multiple: false,
        types: [{{ description: 'JSON', accept: {{ 'application/json': ['.json'] }} }}],
      }});
      fileHandle = handle;
      setStatus(`연결됨: ${{handle.name}}`);
    }}

    async function saveFile() {{
      const payload = JSON.stringify(collectEdits(), null, 2);
      try {{
        if (!fileHandle) {{
          if (!window.showSaveFilePicker) {{
            throw new Error('direct save unsupported');
          }}
          fileHandle = await window.showSaveFilePicker({{
            suggestedName: '{escape(simple_edit_path.name)}',
            types: [{{ description: 'JSON', accept: {{ 'application/json': ['.json'] }} }}],
          }});
        }}
        const writable = await fileHandle.createWritable();
        await writable.write(payload);
        await writable.close();
        setStatus(`저장 완료: ${{fileHandle.name}}`);
      }} catch (error) {{
        setStatus(`직접 저장 실패: ${{error.message || error}}. 다운로드를 사용하세요.`);
      }}
    }}

    function downloadFile() {{
      const payload = JSON.stringify(collectEdits(), null, 2);
      const blob = new Blob([payload], {{ type: 'application/json' }});
      const url = URL.createObjectURL(blob);
      const anchor = document.createElement('a');
      anchor.href = url;
      anchor.download = '{escape(simple_edit_path.name)}';
      anchor.click();
      URL.revokeObjectURL(url);
      setStatus('다운로드 파일을 저장한 뒤 apply 스크립트를 실행하세요.');
    }}

    document.getElementById('connect-file').addEventListener('click', () => connectFile().catch(error => setStatus(`파일 연결 실패: ${{error.message || error}}`)));
    document.getElementById('save-file').addEventListener('click', () => saveFile());
    document.getElementById('download-file').addEventListener('click', () => downloadFile());

    const cards = Array.from(document.querySelectorAll('.card[data-asset-reference]'));
    const viewerStatus = document.getElementById('viewer-status');

    function updateViewer() {{
      if (!cards.length) {{
        viewerStatus.textContent = 'asset 0 / 0';
        return;
      }}
      currentIndex = Math.max(0, Math.min(currentIndex, cards.length - 1));
      cards.forEach((card, index) => {{
        card.classList.toggle('active', index === currentIndex);
      }});
      const activeCard = cards[currentIndex];
      const ref = activeCard?.dataset.assetReference || '-';
      viewerStatus.textContent = `asset ${{currentIndex + 1}} / ${{cards.length}} · ${{ref}}`;
      window.scrollTo({{ top: 0, behavior: 'smooth' }});
    }}

    function moveViewer(delta) {{
      if (!cards.length) return;
      currentIndex = (currentIndex + delta + cards.length) % cards.length;
      updateViewer();
    }}

    document.getElementById('prev-asset').addEventListener('click', () => moveViewer(-1));
    document.getElementById('next-asset').addEventListener('click', () => moveViewer(1));
    updateViewer();

    function appendTag(field, tag) {{
      const current = parseTagList(field.value);
      if (!current.includes(tag)) {{
        current.push(tag);
        field.value = current.join(', ');
      }}
      field.dispatchEvent(new Event('input', {{ bubbles: true }}));
    }}

    for (const field of document.querySelectorAll('textarea[data-tag-group]')) {{
      const group = field.dataset.tagGroup;
      const tags = tagDictionary[group] || [];
      const picker = document.createElement('div');
      picker.className = 'tag-picker';
      const help = document.createElement('div');
      help.className = 'field-help';
      help.textContent = '후보 클릭 시 아래 입력칸에 추가됩니다. 직접 수정도 가능합니다.';
      picker.appendChild(help);
      const rows = [];
      for (let i = 0; i < tags.length; i += 8) {{
        rows.push(tags.slice(i, i + 8));
      }}
      for (const rowTags of rows) {{
        const row = document.createElement('div');
        row.className = 'tag-picker-row';
        for (const tag of rowTags) {{
          const button = document.createElement('button');
          button.type = 'button';
          button.className = 'tag-chip';
          button.textContent = tag;
          button.addEventListener('click', () => appendTag(field, tag));
          row.appendChild(button);
        }}
        picker.appendChild(row);
      }}
      field.insertAdjacentElement('afterend', picker);
    }}

    const lightbox = document.getElementById('lightbox');
    const lightboxImage = document.getElementById('lightbox-image');
    const lightboxCaption = document.getElementById('lightbox-caption');

    function closeLightbox() {{
      lightbox.classList.remove('open');
      lightbox.setAttribute('aria-hidden', 'true');
      lightboxImage.src = '';
      lightboxImage.alt = '';
      lightboxCaption.textContent = '';
    }}

    for (const image of document.querySelectorAll('.image img')) {{
      image.addEventListener('click', () => {{
        lightboxImage.src = image.getAttribute('src') || '';
        lightboxImage.alt = image.getAttribute('alt') || '';
        const card = image.closest('.card');
        const meta = card?.querySelector('.meta');
        lightboxCaption.textContent = meta ? meta.textContent || '' : '';
        lightbox.classList.add('open');
        lightbox.setAttribute('aria-hidden', 'false');
      }});
    }}

    lightbox.addEventListener('click', (event) => {{
      if (event.target === lightbox) {{
        closeLightbox();
      }}
    }});

    window.addEventListener('keydown', (event) => {{
      if (event.key === 'Escape') {{
        closeLightbox();
        return;
      }}
      if (event.key === 'ArrowLeft') {{
        moveViewer(-1);
      }}
      if (event.key === 'ArrowRight') {{
        moveViewer(1);
      }}
    }});
  </script>
</body>
</html>
"""
    output_path = run_dir / "multimodal_review.html"
    output_path.write_text(html, encoding="utf-8")
    return output_path


def _render_tag_block(asset: dict[str, Any]) -> str:
    rows = [
        ("subject_tags", asset.get("subject_tags") or []),
        ("event_tags", asset.get("event_tags") or []),
        ("emotion_tags", asset.get("emotion_tags") or []),
        ("composition_tags", asset.get("composition_tags") or []),
        ("risk_tags", asset.get("risk_tags") or []),
        ("humor_point", asset.get("humor_point") or "-"),
        ("caution_note", asset.get("caution_note") or "-"),
    ]
    rendered = []
    for label, value in rows:
        if isinstance(value, list):
            text = ", ".join(str(item) for item in value) or "-"
        else:
            text = str(value)
        rendered.append(
            f'<div class="tag-row"><span class="tag-label">{escape(label)}</span>{escape(text)}</div>'
        )
    guidance_rows = [
        ("crop_focus_note", asset.get("crop_focus_note") or "-"),
        ("avoid_region_note", asset.get("avoid_region_note") or "-"),
        ("layout_focus_hint", asset.get("layout_focus_hint") or "-"),
    ]
    guidance_html = "".join(
        f'<div class="guidance-row"><span class="guidance-label">{escape(label)}</span>{escape(str(value))}</div>'
        for label, value in guidance_rows
    )
    return f'<div class="tags">{"".join(rendered)}</div><div class="manual-guidance">{guidance_html}</div>'


def _render_edit_field(field_name: str, value: object, *, multiline: bool = False) -> str:
    safe_value = escape("" if value is None else str(value))
    if multiline:
        tag_group_attr = ""
        if field_name in {
            "subject_tags",
            "event_tags",
            "emotion_tags",
            "composition_tags",
            "risk_tags",
        }:
            tag_group_attr = f' data-tag-group="{escape(field_name)}"'
        control = f'<textarea data-field="{escape(field_name)}"{tag_group_attr}>{safe_value}</textarea>'
    else:
        control = f'<input data-field="{escape(field_name)}" value="{safe_value}">'
    return f'<div class="field"><label>{escape(field_name)}</label>{control}</div>'


def _build_multimodal_memory_record_input(
    *,
    target_analysis: dict[str, Any],
    spec: dict[str, Any],
) -> MultimodalMemoryRecordInput:
    current_by_ref = {
        str(asset.get("asset_reference") or ""): asset
        for asset in target_analysis.get("assets") or []
    }
    spec_assets_by_ref = {
        str(asset.get("asset_reference") or ""): asset
        for asset in spec.get("assets") or []
    }
    source_run_dir = _infer_multimodal_source_run_dir(spec)
    issue_id = str(spec.get("issue_id") or target_analysis.get("issue_id") or "").strip() or None
    topic_features = extract_topic_features(
        spec,
        overrides={key: spec.get(key, target_analysis.get(key)) for key in (
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

    records: list[dict[str, Any]] = []
    change_count = 0
    for ref, edited_asset in spec_assets_by_ref.items():
        current_asset = current_by_ref.get(ref)
        if current_asset is None:
            continue
        field_changes = _build_multimodal_field_changes(current_asset, edited_asset)
        if not field_changes:
            continue
        change_count += 1
        fingerprint_source_payload = {
            **edited_asset,
            **current_asset,
            "asset_reference": ref,
        }
        asset_features = extract_asset_features(fingerprint_source_payload)
        stable_asset_payload = {
            **fingerprint_source_payload,
            **asset_features,
        }
        merged_asset_payload = {
            **current_asset,
            **edited_asset,
            "asset_reference": ref,
            **asset_features,
        }
        records.append(
            {
                "id": None,
                "created_at": None,
                "topic_fingerprint": build_topic_fingerprint(
                    spec,
                    overrides={key: topic_features.get(key) for key in (
                        "topic_type",
                        "entity_focus",
                        "event_type",
                        "angle_type",
                    )},
                ),
                "asset_fingerprint": build_asset_fingerprint(stable_asset_payload),
                **topic_features,
                **asset_features,
                "asset_reference": ref,
                "before_usage_recommendation": current_asset.get("usage_recommendation"),
                "after_usage_recommendation": edited_asset.get("usage_recommendation"),
                "before_scene_description": current_asset.get("scene_description"),
                "after_scene_description": edited_asset.get("scene_description"),
                "before_humor_point": current_asset.get("humor_point"),
                "after_humor_point": edited_asset.get("humor_point"),
                "before_tag_summary": current_asset.get("tag_summary"),
                "after_tag_summary": edited_asset.get("tag_summary"),
                "before_subject_tags": list(current_asset.get("subject_tags") or []),
                "after_subject_tags": list(edited_asset.get("subject_tags") or []),
                "before_event_tags": list(current_asset.get("event_tags") or []),
                "after_event_tags": list(edited_asset.get("event_tags") or []),
                "before_emotion_tags": list(current_asset.get("emotion_tags") or []),
                "after_emotion_tags": list(edited_asset.get("emotion_tags") or []),
                "before_composition_tags": list(current_asset.get("composition_tags") or []),
                "after_composition_tags": list(edited_asset.get("composition_tags") or []),
                "before_risk_tags": list(current_asset.get("risk_tags") or []),
                "after_risk_tags": list(edited_asset.get("risk_tags") or []),
                "before_caution_note": current_asset.get("caution_note"),
                "after_caution_note": edited_asset.get("caution_note"),
                "source_run_dir": str(source_run_dir) if source_run_dir else None,
                "source_report_path": str(spec.get("report_path") or ""),
                "memory_context_used": edited_asset.get("memory_context_used", spec.get("memory_context_used")),
                "referenced_memory_ids": list(
                    edited_asset.get("referenced_memory_ids")
                    or spec.get("referenced_memory_ids")
                    or []
                ),
                "issue_id": issue_id,
                "image_file": edited_asset.get("image_file"),
                "field_change_count": len(field_changes),
            }
        )
    return MultimodalMemoryRecordInput(records=records, change_count=change_count)


def _infer_multimodal_source_run_dir(spec: dict[str, Any]) -> Path | None:
    for key in ("report_path", "assets_text_path", "spec_path"):
        raw = str(spec.get(key) or "").strip()
        if not raw:
            continue
        try:
            return Path(raw).expanduser().resolve().parent
        except Exception:
            continue
    return None


def _build_multimodal_field_changes(
    current_asset: dict[str, Any],
    edited_asset: dict[str, Any],
) -> list[dict[str, Any]]:
    editable_fields = [
        "usage_recommendation",
        "subject_tags",
        "event_tags",
        "emotion_tags",
        "composition_tags",
        "risk_tags",
        "tag_summary",
        "scene_description",
        "humor_point",
        "caution_note",
    ]
    field_changes: list[dict[str, Any]] = []
    for field_name in editable_fields:
        before = current_asset.get(field_name)
        after = edited_asset.get(field_name)
        if before != after:
            field_changes.append({"field": field_name, "before": before, "after": after})
    return field_changes


def _prepare_multimodal_memory_records(memory_input: MultimodalMemoryRecordInput) -> list[dict[str, Any]]:
    prepared_records: list[dict[str, Any]] = []
    for raw_record in memory_input.records:
        record = {key: value for key, value in raw_record.items() if key in MULTIMODAL_MEMORY_TABLE_FIELDS}
        record.update(
            {
                "id": str(uuid4()),
                "created_at": datetime.now().isoformat(timespec="seconds"),
                "memory_context_used": (
                    False if record.get("memory_context_used") is None else record.get("memory_context_used")
                ),
                "referenced_memory_ids": record.get("referenced_memory_ids") or [],
            }
        )
        prepared_records.append(record)
    return prepared_records


def _insert_prepared_multimodal_memory_records(
    *,
    prepared_records: list[dict[str, Any]],
    repository: FeedbackMemoryRepository,
) -> FeedbackMemoryWriteResult:
    total_rowcount = 0
    for record in prepared_records:
        result = repository.safe_insert_record(
            "multimodal_edit_memory",
            record,
            json_fields={
                "before_subject_tags",
                "after_subject_tags",
                "before_event_tags",
                "after_event_tags",
                "before_emotion_tags",
                "after_emotion_tags",
                "before_composition_tags",
                "after_composition_tags",
                "before_risk_tags",
                "after_risk_tags",
                "referenced_memory_ids",
            },
            bool_fields={
                "memory_context_used",
                "has_notable_numbers",
                "is_action_shot",
                "is_post_game",
            },
            plain_text_fields={
                "topic_fingerprint",
                "asset_fingerprint",
                "topic_type",
                "entity_focus",
                "event_type",
                "angle_type",
                "recommended_focus",
                "shot_type",
                "subject_role",
                "person_count_bucket",
                "caption_signal",
                "asset_reference",
                "source_run_dir",
                "source_report_path",
            },
            operation_name="insert_multimodal_edit_memory",
        )
        if not result.ok:
            return result
        total_rowcount += result.rowcount
    return FeedbackMemoryWriteResult(ok=True, rowcount=total_rowcount, error_message=None)


def insert_multimodal_edit_memory(
    *,
    target_analysis: dict[str, Any],
    spec: dict[str, Any],
    repository: FeedbackMemoryRepository | None = None,
) -> FeedbackMemoryWriteResult:
    memory_input = _build_multimodal_memory_record_input(target_analysis=target_analysis, spec=spec)
    if not memory_input.records:
        return FeedbackMemoryWriteResult(ok=True, rowcount=0, error_message=None)

    owns_repository = repository is None
    active_repository = repository or FeedbackMemoryRepository()
    try:
        prepared_records = _prepare_multimodal_memory_records(memory_input)
        return _insert_prepared_multimodal_memory_records(
            prepared_records=prepared_records,
            repository=active_repository,
        )
    finally:
        if owns_repository:
            active_repository.close()


def apply_multimodal_simple_edits(
    simple_edit_path: Path | str,
    *,
    feedback_repository: FeedbackMemoryRepository | None = None,
) -> dict[str, Any]:
    spec_path = Path(simple_edit_path).expanduser().resolve()
    spec = json.loads(spec_path.read_text(encoding="utf-8"))
    report_path = Path(str(spec.get("report_path") or "")).expanduser().resolve()
    history_path = Path(str(spec.get("history_path") or "")).expanduser().resolve()
    assets_text_path = Path(str(spec.get("assets_text_path") or "")).expanduser().resolve()

    report = json.loads(report_path.read_text(encoding="utf-8"))
    analyses = report.get("multimodal_analyses") or []
    issue_id = str(spec.get("issue_id") or "")
    target_analysis = next((item for item in analyses if str(item.get("issue_id") or "") == issue_id), analyses[0] if analyses else None)
    if target_analysis is None:
        raise ValueError("multimodal analysis not found in report")

    memory_before_analysis = deepcopy(target_analysis)
    memory_input = _build_multimodal_memory_record_input(
        target_analysis=memory_before_analysis,
        spec=spec,
    )
    changes = _apply_asset_edits(target_analysis, spec)
    target_analysis["overall_summary"] = str(spec.get("overall_summary") or target_analysis.get("overall_summary") or "")
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    write_multimodal_assets_text(assets_text_path.parent, analyses)

    edit_iteration = int(spec.get("edit_iteration") or 0) + 1
    spec["edit_iteration"] = edit_iteration
    spec["last_edited_at"] = datetime.now().isoformat(timespec="seconds")
    spec_path.write_text(json.dumps(spec, ensure_ascii=False, indent=2), encoding="utf-8")
    _append_multimodal_edit_history(history_path, issue_id=issue_id, changes=changes, overall_summary=spec.get("overall_summary"))
    memory_result = FeedbackMemoryWriteResult(ok=True, rowcount=0, error_message=None)
    if memory_input.change_count > 0:
        owns_repository = feedback_repository is None
        active_repository = feedback_repository or FeedbackMemoryRepository()
        if owns_repository:
            active_repository.safe_initialize()
        prepared_records = _prepare_multimodal_memory_records(memory_input)
        try:
            memory_result = _insert_prepared_multimodal_memory_records(
                prepared_records=prepared_records,
                repository=active_repository,
            )
            if memory_result.ok:
                for record in prepared_records:
                    try:
                        refresh_policies_from_multimodal_memory(record, repository=active_repository)
                    except Exception:
                        pass
        finally:
            if owns_repository:
                active_repository.close()
    return {
        "spec_path": spec_path,
        "report_path": report_path,
        "assets_text_path": assets_text_path,
        "history_path": history_path,
        "edit_iteration": edit_iteration,
        "change_count": len(changes),
        "memory_rowcount": memory_result.rowcount,
        "memory_insert_ok": memory_result.ok,
        "memory_insert_error": memory_result.error_message,
    }


def _apply_asset_edits(target_analysis: dict[str, Any], spec: dict[str, Any]) -> list[dict[str, Any]]:
    editable_fields = [
        "usage_recommendation",
        "subject_tags",
        "event_tags",
        "emotion_tags",
        "composition_tags",
        "risk_tags",
        "tag_summary",
        "scene_description",
        "humor_point",
        "caution_note",
        "confidence",
    ]
    guidance_fields = [
        "crop_focus_note",
        "avoid_region_note",
        "layout_focus_hint",
    ]
    current_by_ref = {
        str(asset.get("asset_reference") or ""): asset
        for asset in target_analysis.get("assets") or []
    }
    changes: list[dict[str, Any]] = []
    for edited_asset in spec.get("assets") or []:
        ref = str(edited_asset.get("asset_reference") or "")
        current_asset = current_by_ref.get(ref)
        if current_asset is None:
            continue
        field_changes: list[dict[str, Any]] = []
        for field_name in editable_fields:
            before = current_asset.get(field_name)
            after = edited_asset.get(field_name)
            if before != after:
                current_asset[field_name] = after
                field_changes.append({"field": field_name, "before": before, "after": after})
        analysis_payload = current_asset.setdefault("analysis_payload", {})
        for field_name in guidance_fields:
            before = analysis_payload.get(field_name) or ""
            after = str(edited_asset.get(field_name) or "")
            if before != after:
                analysis_payload[field_name] = after
                field_changes.append({"field": field_name, "before": before, "after": after})
        if field_changes:
            changes.append(
                {
                    "asset_reference": ref,
                    "image_file": edited_asset.get("image_file"),
                    "field_changes": field_changes,
                }
            )
    return changes


def _append_multimodal_edit_history(
    history_path: Path,
    *,
    issue_id: str,
    changes: list[dict[str, Any]],
    overall_summary: object,
) -> None:
    timestamp = datetime.now().isoformat(timespec="seconds")
    lines = [
        f"\n## {timestamp}",
        f"- issue_id: {issue_id}",
        f"- overall_summary: {overall_summary}",
    ]
    if not changes:
        lines.append("- changes: none")
    for change in changes:
        lines.append(
            f"- asset {change['asset_reference']} ({change.get('image_file') or 'no_image_file'})"
        )
        for field_change in change["field_changes"]:
            lines.append(
                f"  - {field_change['field']}: {field_change['before']} -> {field_change['after']}"
            )
    with history_path.open("a", encoding="utf-8") as handle:
        handle.write("\n".join(lines) + "\n")
