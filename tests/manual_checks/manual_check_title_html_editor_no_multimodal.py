from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request
from dataclasses import asdict, is_dataclass
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable

ROOT_DIR = Path(__file__).resolve().parents[2]
SRC_DIR = ROOT_DIR / "src"
OUTPUT_ROOT = ROOT_DIR / "outputs"
KST = timezone(timedelta(hours=9))

if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from kbo_card_news.config.env import load_default_env  # noqa: E402
from kbo_card_news.collectors.news_sites import NEWS_SITE_DEFINITIONS  # noqa: E402
from kbo_card_news.feedback_memory import (  # noqa: E402
    FeedbackMemoryRepository,
    apply_headline_policies,
    build_topic_fingerprint,
    extract_topic_features,
    format_headline_retrieval_summary,
    retrieve_similar_headline_edits,
)
from kbo_card_news.imaging.browser_export import export_editable_html_to_png  # noqa: E402
from kbo_card_news.imaging.title_editable_rerender import insert_headline_edit_memory  # noqa: E402
from kbo_card_news.imaging.topic_title_page import (  # noqa: E402
    TitleCopyInput,
    _build_subheadline_source,
    _build_title_output_basename,
    _detect_team_name,
    _filter_text_for_team,
    _format_date_text,
    _resolve_title_team_color,
    _sanitize_subheadline,
    _sanitize_title_text,
    _is_valid_subheadline_output,
    build_default_title_copy_engine,
)
from kbo_card_news.pipeline import (  # noqa: E402
    SQLiteSourceItemRepository,
    StoredArticleBatchBuilder,
    StoredTopicDeepResearchBuilder,
    TopicStructuringInputBuilder,
)
from kbo_card_news.research import GeminiTopicDeepResearchEngine, HeuristicTopicDeepResearchEngine, TopicDeepResearchService  # noqa: E402
from kbo_card_news.runtime.model_fallback import call_openai  # noqa: E402
from kbo_card_news.scoring.engine import UrllibHttpTransport  # noqa: E402
from kbo_card_news.structuring import GeminiCardNewsStructuringEngine, HeuristicCardNewsStructuringEngine, IssueStructuringService  # noqa: E402
from kbo_card_news.workflows import (  # noqa: E402
    append_completed_topic_entries,
    build_completed_topic_entry,
    ensure_stage_dir,
    selection_result_from_dict,
    serialize_for_json,
)

from design import TEAM_COLORS, blend_color, hex_to_rgb, resolve_font_path  # noqa: E402


SCRIPT_BATCH_TOPIC_SELECTION = ROOT_DIR / "tests" / "manual_checks" / "manual_check_batch_topic_selection.py"
SCRIPT_APPLY_TOPIC_SELECTION = ROOT_DIR / "tests" / "manual_checks" / "manual_check_apply_topic_selection.py"
RENDER_PNG_DIR_NAME = "title_render_pngs"
SOCIAL_COPY_MD_NAME = "title_render_social_copy.md"
SOCIAL_COPY_MODEL = "gpt-5.4"
SOCIAL_COPY_SCHEMA = {
    "type": "object",
    "properties": {
        "summary_paragraphs": {
            "type": "array",
            "items": {"type": "string"},
            "minItems": 4,
            "maxItems": 6,
        },
        "hashtags": {
            "type": "array",
            "items": {"type": "string"},
            "minItems": 10,
            "maxItems": 18,
        },
    },
    "required": ["summary_paragraphs", "hashtags"],
    "additionalProperties": False,
}


def _run(script_path: Path, *args: str, approval_run_dir: Path) -> None:
    env = os.environ.copy()
    pythonpath_parts = [str(SRC_DIR)]
    existing_pythonpath = env.get("PYTHONPATH", "").strip()
    if existing_pythonpath:
        pythonpath_parts.append(existing_pythonpath)
    env["PYTHONPATH"] = os.pathsep.join(pythonpath_parts)
    env["APPROVAL_RUN_DIR"] = str(approval_run_dir)
    subprocess.run([sys.executable, str(script_path), *args], cwd=str(ROOT_DIR), env=env, check=True)


def choose_research_engine():
    load_default_env(ROOT_DIR)
    if os.getenv("GEMINI_API_KEY"):
        return GeminiTopicDeepResearchEngine()
    return HeuristicTopicDeepResearchEngine()


def choose_structuring_engine():
    load_default_env(ROOT_DIR)
    if os.getenv("GEMINI_API_KEY"):
        return GeminiCardNewsStructuringEngine()
    return HeuristicCardNewsStructuringEngine()


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
    if isinstance(value, Path):
        return str(value)
    return value


def _slugify(value: str) -> str:
    compact = "".join(ch if ch.isalnum() or ch in {"_", "-"} or ("\uAC00" <= ch <= "\uD7A3") else "_" for ch in value.strip())
    return "_".join(part for part in compact.split("_") if part)[:48] or "topic"


def _collect_topic_assets(db_path: Path, article_ids: list[str]) -> list[dict[str, Any]]:
    if not article_ids:
        return []
    placeholders = ",".join("?" for _ in article_ids)
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            f"""
            SELECT source_assets.id, source_assets.source_item_id, source_assets.asset_type,
                   source_assets.origin_url, source_assets.storage_path, source_assets.mime_type,
                   source_assets.width, source_assets.height, source_assets.sort_order,
                   source_assets.vision_caption, source_assets.ocr_text,
                   source_items.source_type AS source_type,
                   source_items.title AS source_article_title,
                   source_items.source_url AS source_article_url
            FROM source_assets
            JOIN source_items ON source_items.id = source_assets.source_item_id
            WHERE source_assets.source_item_id IN ({placeholders})
            ORDER BY source_assets.source_item_id ASC, source_assets.sort_order ASC, source_assets.id ASC
            """,
            article_ids,
        ).fetchall()
    assets = []
    for row in rows:
        if not row["origin_url"] and not row["storage_path"]:
            continue
        asset = dict(row)
        asset["source_site_name"] = _source_site_name(str(row["source_type"] or ""))
        assets.append(asset)
    return assets


def _source_site_name(source_type: str) -> str:
    definition = NEWS_SITE_DEFINITIONS.get(str(source_type or "").strip())
    if definition:
        return definition.site_name
    return str(source_type or "").strip()


def _collect_social_copy_context(db_path: Path, topic_id: str, *, title_copy: dict[str, Any] | None = None) -> dict[str, Any]:
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        topic_row = conn.execute(
            """
            SELECT topic_id, topic_name, reason_summary, representative_article_id,
                   article_ids_json, metadata_json
            FROM selected_topics
            WHERE topic_id = ?
            """,
            (topic_id,),
        ).fetchone()
        if topic_row is None:
            raise RuntimeError(f"selected topic not found in db: {topic_id}")
        research_row = conn.execute(
            """
            SELECT angle_summary, key_points_json, timeline_json, notable_numbers_json,
                   risk_flags_json, recommended_focus
            FROM topic_deep_research_results
            WHERE topic_id = ?
            """,
            (topic_id,),
        ).fetchone()
        article_ids = [str(item) for item in json.loads(topic_row["article_ids_json"] or "[]")]
    research = dict(research_row) if research_row else {}
    topic = dict(topic_row)
    topic["article_count"] = len(article_ids)
    topic["article_ids"] = article_ids
    topic.pop("article_ids_json", None)
    return {
        "title_copy": title_copy or {},
        "topic": topic,
        "research": {
            "angle_summary": research.get("angle_summary", ""),
            "key_points": json.loads(str(research.get("key_points_json") or "[]")),
            "timeline": json.loads(str(research.get("timeline_json") or "[]")),
            "notable_numbers": json.loads(str(research.get("notable_numbers_json") or "[]")),
            "risk_flags": json.loads(str(research.get("risk_flags_json") or "[]")),
            "recommended_focus": research.get("recommended_focus", ""),
        },
    }


def _build_social_copy_prompt(context: dict[str, Any]) -> str:
    compact_context = json.dumps(context, ensure_ascii=False, separators=(",", ":"))
    return f"""KBO 인스타그램 카드뉴스 타이틀 이미지에 함께 올릴 게시글 요약과 해시태그를 작성하라.

규칙:
- 반드시 입력 JSON의 deep research 정리 내용만 근거로 사용한다.
- title_copy.headline과 title_copy.subheadline의 앵글, 톤, 핵심 단어를 게시글 첫 흐름에 반영한다.
- summary_paragraphs는 한국어 4~6개 문단으로 작성한다.
- 각 문단은 1~2문장, 총 흐름은 사건 요약 -> 핵심 장면 -> 의미/순위 영향 순서로 작성한다.
- 과장, 확정되지 않은 전망, 입력 JSON에 없는 선수/스코어/기록은 쓰지 않는다.
- articles 원문은 제공되지 않는다. research 필드에 없는 세부 사실을 추측해 보강하지 않는다.
- hashtags는 10~18개, 각 항목은 #으로 시작하고 공백 없이 작성한다.
- 팀명, 연승/순위/승리/핵심 선수/장면/프로야구 관련 태그를 우선한다.
- 출력은 지정된 JSON schema만 따른다.

DB_CONTEXT:
{compact_context}
"""


def _normalize_hashtag(value: str) -> str:
    compact = "".join(str(value or "").split())
    if not compact:
        return ""
    return compact if compact.startswith("#") else f"#{compact}"


def _generate_social_copy(context: dict[str, Any]) -> dict[str, Any]:
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        return _fallback_social_copy(context)
    try:
        response_text = call_openai(
            model_name=SOCIAL_COPY_MODEL,
            prompt=_build_social_copy_prompt(context),
            schema_name="title_render_social_copy",
            json_schema=SOCIAL_COPY_SCHEMA,
            transport=UrllibHttpTransport(timeout_seconds=120),
            api_key=api_key,
            endpoint="https://api.openai.com/v1/responses",
        )
        parsed = json.loads(response_text)
    except Exception as exc:
        print(f"[SOCIAL COPY FALLBACK] {exc}")
        return _fallback_social_copy(context)
    summary_paragraphs = [
        " ".join(str(item or "").split())
        for item in parsed.get("summary_paragraphs", [])
        if str(item or "").strip()
    ]
    hashtags = [_normalize_hashtag(str(item)) for item in parsed.get("hashtags", [])]
    hashtags = [item for item in hashtags if item]
    if not summary_paragraphs or not hashtags:
        return _fallback_social_copy(context)
    return {"summary_paragraphs": summary_paragraphs, "hashtags": hashtags}


def _fallback_social_copy(context: dict[str, Any]) -> dict[str, Any]:
    title_copy = dict(context.get("title_copy") or {})
    research = dict(context.get("research") or {})
    topic = dict(context.get("topic") or {})
    headline = str(title_copy.get("headline") or topic.get("topic_name") or "").strip()
    subheadline = str(title_copy.get("subheadline") or "").strip()
    summary = str(research.get("overall_summary") or research.get("summary") or "").strip()
    recommended_focus = str(research.get("recommended_focus") or "").strip()
    topic_name = str(topic.get("topic_name") or headline or "KBO 이슈").strip()
    paragraphs = []
    if headline:
        paragraphs.append(f"{headline} 이슈를 카드뉴스로 정리했습니다.")
    if subheadline:
        paragraphs.append(subheadline.replace("\n", " "))
    if summary:
        paragraphs.append(summary)
    if recommended_focus:
        paragraphs.append(recommended_focus)
    if not paragraphs:
        paragraphs.append(f"{topic_name} 관련 소식을 정리했습니다.")
    team_name = str(title_copy.get("team_name") or topic.get("team_name") or "").strip()
    hashtags = [
        "#KBO",
        "#프로야구",
        "#야구뉴스",
        "#카드뉴스",
    ]
    for value in [team_name, topic_name, headline]:
        for token in str(value or "").replace("-", " ").replace(",", " ").split():
            normalized = _normalize_hashtag(token)
            if normalized and normalized not in hashtags and len(hashtags) < 12:
                hashtags.append(normalized)
    return {
        "summary_paragraphs": paragraphs[:6],
        "hashtags": hashtags,
    }


def _social_copy_title_payload_from_editor_payload(editor_payload: dict[str, Any]) -> dict[str, Any]:
    original = dict(editor_payload.get("original") or {})
    return {
        "headline": str(original.get("title_text") or ""),
        "subheadline": str(original.get("subheadline") or ""),
        "team_name": str(original.get("team_name") or ""),
        "date_text": str(original.get("date_text") or ""),
    }


def _social_copy_section_marker(topic_id: str, *, end: bool = False) -> str:
    suffix = "end" if end else "start"
    return f"<!-- topic:{topic_id}:{suffix} -->"


def _build_social_copy_md_section(
    *,
    db_path: Path,
    topic_id: str,
    topic_index: int,
    topic_name: str,
    title_copy: dict[str, Any],
) -> str:
    context = _collect_social_copy_context(db_path, topic_id, title_copy=title_copy)
    social_copy = _generate_social_copy(context)
    photo_credit = str(title_copy.get("photo_credit") or "").strip()
    lines = [
        _social_copy_section_marker(topic_id),
        f"## {topic_index:02d}. {topic_name}",
        "",
    ]
    lines.extend(f"{paragraph}\n" for paragraph in social_copy["summary_paragraphs"])
    lines.append(" ".join(social_copy["hashtags"]))
    if photo_credit:
        lines.append(f"사진출처:{photo_credit}")
    lines.append(_social_copy_section_marker(topic_id, end=True))
    return "\n".join(lines).rstrip()


def _write_title_render_social_copy_md(*, run_dir: Path, db_path: Path, topic_entries: list[dict[str, Any]]) -> Path:
    output_dir = run_dir / RENDER_PNG_DIR_NAME
    output_dir.mkdir(parents=True, exist_ok=True)
    md_path = output_dir / SOCIAL_COPY_MD_NAME
    lines = [
        "# 타이틀 렌더 요약/태그",
        "",
        f"- source_db: `{db_path}`",
        f"- model: `{SOCIAL_COPY_MODEL}`",
        "",
    ]
    for topic in topic_entries:
        topic_index = int(topic.get("topic_index") or 0)
        topic_name = str(topic.get("topic_name") or "").strip()
        topic_id = str(topic.get("topic_id") or "")
        editor_payload = json.loads(Path(str(topic["editor_payload_path"])).read_text(encoding="utf-8"))
        lines.append(
            _build_social_copy_md_section(
                db_path=db_path,
                topic_id=topic_id,
                topic_index=topic_index,
                topic_name=topic_name,
                title_copy=_social_copy_title_payload_from_editor_payload(editor_payload),
            )
        )
        lines.append("")
    md_path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
    return md_path


def _record_rendered_topic(
    *,
    topic: dict[str, Any],
    approval_run_dir: Path,
    manifest_path: Path,
    state_path: Path,
    output_png_path: Path,
) -> Path:
    topic_name = str(topic.get("topic_name") or "").strip()
    topic_id = str(topic.get("topic_id") or "").strip()
    if not topic_name or not topic_id:
        return append_completed_topic_entries([])

    entry = build_completed_topic_entry(
        topic_name=topic_name,
        issue_id=topic_id,
        representative_article_id=str(topic.get("representative_article_id") or "").strip(),
        article_ids=[str(article_id) for article_id in topic.get("article_ids", [])],
        approval_run_dir=str(approval_run_dir),
        approval_manifest_path=str(manifest_path),
        final_manifest_path=str(state_path),
    )
    entry["status"] = "rendered"
    entry["rendered_state_path"] = str(state_path)
    entry["rendered_png_path"] = str(output_png_path)
    return append_completed_topic_entries([entry])


def _replace_social_copy_md_section(md_path: Path, *, topic_id: str, section: str) -> None:
    start_marker = _social_copy_section_marker(topic_id)
    end_marker = _social_copy_section_marker(topic_id, end=True)
    if not md_path.exists():
        md_path.write_text(section.rstrip() + "\n", encoding="utf-8")
        return
    text = md_path.read_text(encoding="utf-8")
    start = text.find(start_marker)
    end = text.find(end_marker)
    if start == -1 or end == -1 or end < start:
        md_path.write_text(text.rstrip() + "\n\n" + section.rstrip() + "\n", encoding="utf-8")
        return
    end += len(end_marker)
    md_path.write_text(text[:start] + section.rstrip() + text[end:], encoding="utf-8")


def _build_title_copy_payload(
    *,
    topic: Any,
    research_result: Any,
    structuring_input: Any,
    draft: Any,
) -> dict[str, Any]:
    topic_name = str(topic.topic_name or "").strip()
    draft_title = str(draft.title or topic_name).strip()
    cover_page = next((page for page in draft.pages if page.page_role == "cover"), draft.pages[0] if draft.pages else None)
    cover_headline = str(cover_page.headline if cover_page else "").strip()
    cover_body = str(cover_page.body if cover_page else "").strip()
    summary = str(research_result.angle_summary or structuring_input.candidate.summary or topic.reason_summary or "").strip()
    draft_subtitle = str(draft.subtitle or cover_body or summary).strip()
    team_name = _detect_team_name(topic_name) or _detect_team_name(draft_title, draft_subtitle, summary)

    draft_title = _filter_text_for_team(draft_title, team_name) or topic_name
    draft_subtitle = _filter_text_for_team(draft_subtitle, team_name) or draft_subtitle
    summary = _filter_text_for_team(summary, team_name) or summary
    cover_headline = _filter_text_for_team(cover_headline, team_name)
    cover_body = _filter_text_for_team(cover_body, team_name) or cover_body

    topic_features = extract_topic_features(
        {
            "topic_id": topic.topic_id,
            "topic_name": topic_name,
            "team_name": team_name,
            "draft_title": draft_title,
            "draft_subtitle": draft_subtitle,
            "cover_headline": cover_headline,
            "cover_body": cover_body,
            "overall_summary": summary,
            "asset_count": len(structuring_input.assets),
        }
    )
    topic_fingerprint = build_topic_fingerprint(
        {
            "topic_id": topic.topic_id,
            "topic_name": topic_name,
            "team_name": team_name,
            "draft_title": draft_title,
            "draft_subtitle": draft_subtitle,
            "overall_summary": summary,
            **topic_features,
        },
        overrides={key: topic_features.get(key) for key in ("topic_type", "entity_focus", "event_type", "angle_type")},
    )

    feedback_repository = FeedbackMemoryRepository()
    feedback_repository.safe_initialize()
    try:
        memory_context_summary, referenced_memory_ids = _build_title_memory_context(
            topic_id=topic.topic_id,
            topic_name=topic_name,
            team_name=team_name,
            draft_title=draft_title,
            draft_subtitle=draft_subtitle,
            cover_headline=cover_headline,
            cover_body=cover_body,
            overall_summary=summary,
            topic_features=topic_features,
            repository=feedback_repository,
        )
        copy_input = TitleCopyInput(
            topic_id=topic.topic_id,
            topic_name=topic_name,
            draft_title=draft_title,
            draft_subtitle=draft_subtitle,
            cover_headline=cover_headline,
            cover_body=cover_body,
            overall_summary=summary,
            team_name=team_name,
            memory_context_summary=memory_context_summary,
            referenced_memory_ids=referenced_memory_ids,
        )
        copy_engine = build_default_title_copy_engine()
        print(
            "[TITLE HTML EDITOR NO MM] title copy engine "
            f"{type(copy_engine).__name__} "
            f"headline_model={getattr(copy_engine, 'headline_model_name', '-')} "
            f"subheadline_model={getattr(copy_engine, 'subheadline_model_name', '-')} "
            f"fallback={type(getattr(copy_engine, 'fallback_engine', None)).__name__ if hasattr(copy_engine, 'fallback_engine') else '-'}"
        )
        if referenced_memory_ids:
            print(
                "[TITLE HTML EDITOR NO MM] headline memory context "
                f"cases={len(referenced_memory_ids)} ids={','.join(referenced_memory_ids)}"
            )
        copy_output = copy_engine.rewrite(copy_input)
        print(
            "[TITLE HTML EDITOR NO MM] title copy output "
            f"source={copy_output.copy_source} model={copy_output.model_name}"
        )
        copy_output = type(copy_output)(
            title_text=_sanitize_title_text(copy_output.title_text, copy_input=copy_input),
            subheadline=_sanitize_subheadline(
                copy_output.subheadline,
                team_name=team_name,
                fallback_text=_build_subheadline_source(copy_input, team_name=team_name),
            ),
            copy_source=copy_output.copy_source,
            model_name=copy_output.model_name,
        )
        policy_result = apply_headline_policies(
            title_text=copy_output.title_text,
            subheadline=copy_output.subheadline,
            context={
                "topic_id": topic.topic_id,
                "topic_name": topic_name,
                "team_name": team_name,
                "topic_fingerprint": topic_fingerprint,
                **topic_features,
            },
            repository=feedback_repository,
        )
    finally:
        feedback_repository.close()
    title_text = _sanitize_title_text(policy_result.title_text, copy_input=copy_input)
    subheadline = _sanitize_subheadline(
        policy_result.subheadline,
        team_name=team_name,
        fallback_text=_build_subheadline_source(copy_input, team_name=team_name),
    )
    published_at = structuring_input.candidate.published_at.isoformat() if structuring_input.candidate.published_at else None
    date_text = _format_date_text(published_at)
    return {
        "topic_id": topic.topic_id,
        "topic_name": topic_name,
        "draft_title": draft_title,
        "title_text": title_text,
        "subheadline": subheadline,
        "team_name": team_name,
        "team_color": _resolve_title_team_color(team_name),
        "date_text": date_text,
        "copy_source": copy_output.copy_source,
        "copy_model_name": copy_output.model_name,
        "memory_context_used": bool(referenced_memory_ids),
        "num_similar_cases": len(referenced_memory_ids),
        "referenced_memory_ids": list(referenced_memory_ids),
        "memory_context_summary": memory_context_summary,
        "policy_correction_used": policy_result.policy_correction_used,
        "applied_policy_ids": list(policy_result.applied_policy_ids),
        "applied_policy_types": list(policy_result.applied_policy_types),
        "policy_correction_summary": policy_result.policy_correction_summary,
        "pre_correction_title_text": policy_result.pre_correction_title_text,
        "pre_correction_subheadline": policy_result.pre_correction_subheadline,
        "topic_features": topic_features,
    }


def _build_title_memory_context(
    *,
    topic_id: str,
    topic_name: str,
    team_name: str | None,
    draft_title: str,
    draft_subtitle: str,
    cover_headline: str,
    cover_body: str,
    overall_summary: str,
    topic_features: dict[str, Any],
    repository: FeedbackMemoryRepository,
) -> tuple[str | None, list[str]]:
    retrieval_source = {
        "topic_id": topic_id,
        "topic_name": topic_name,
        "team_name": team_name,
        "draft_title": draft_title,
        "draft_subtitle": draft_subtitle,
        "cover_headline": cover_headline,
        "cover_body": cover_body,
        "overall_summary": overall_summary,
        "summary": overall_summary,
        "topic_type": topic_features.get("topic_type"),
        "entity_focus": topic_features.get("entity_focus"),
        "event_type": topic_features.get("event_type"),
        "angle_type": topic_features.get("angle_type"),
        "recommended_focus": topic_features.get("recommended_focus"),
        "has_notable_numbers": topic_features.get("has_notable_numbers"),
    }
    rows = retrieve_similar_headline_edits(
        retrieval_source,
        repository=repository,
        top_k=3,
    )
    if not rows:
        return None, []
    summary = format_headline_retrieval_summary(rows)
    referenced_memory_ids = [
        str(row.get("id") or "").strip()
        for row in rows
        if str(row.get("id") or "").strip()
    ]
    return summary or None, referenced_memory_ids


def _write_editor_html(topic_dir: Path, editor_payload_path: Path) -> Path:
    html_path = topic_dir / "title_image_editor.html"
    html_path.write_text(
        _EDITOR_HTML.replace("__EDITOR_PAYLOAD__", editor_payload_path.name).replace(
            "__FONT_FACE_CSS__",
            _editor_font_face_css(),
        ),
        encoding="utf-8",
    )
    return html_path


def _editor_font_face_css() -> str:
    return """
    @font-face { font-family: TitleFont; src: url("/font/bold"); font-weight: 400; font-style: normal; font-display: block; }
    @font-face { font-family: BodyFont; src: url("/font/medium"); font-weight: 400; font-style: normal; font-display: block; }
    @font-face { font-family: LightFont; src: url("/font/light"); font-weight: 400; font-style: normal; font-display: block; }
    """.strip()


def _font_face_css() -> str:
    bold_path = resolve_font_path("bold")
    medium_path = resolve_font_path("medium")
    light_path = resolve_font_path("light")

    def source(path: str | None, fallback_names: list[str]) -> str:
        parts = []
        if path:
            parts.append(f'url("{Path(path).resolve().as_uri()}")')
        parts.extend(f'local("{name}")' for name in fallback_names)
        return ", ".join(parts)

    return f"""
    @font-face {{ font-family: TitleFont; src: {source(bold_path, ["esamanru OTF Bold", "이사만루OTF Bold", "Pretendard Bold"])}; font-weight: 400; font-style: normal; font-display: block; }}
    @font-face {{ font-family: BodyFont; src: {source(medium_path, ["esamanru OTF Medium", "이사만루OTF Medium", "Pretendard Medium"])}; font-weight: 400; font-style: normal; font-display: block; }}
    @font-face {{ font-family: LightFont; src: {source(light_path, ["esamanru OTF Light", "이사만루OTF Light", "Pretendard Light"])}; font-weight: 400; font-style: normal; font-display: block; }}
    """.strip()


def _write_render_html(path: Path, state: dict[str, Any], *, asset_url: str) -> None:
    title = _html_escape(str(state.get("title_text") or ""))
    subheadline = _html_escape(str(state.get("subheadline") or "")).replace("\n", "<br>")
    raw_team_color = str(state.get("team_color") or "#111111")
    team_color = _html_escape(raw_team_color)
    gradient_top = _html_escape(_blend_hex(raw_team_color, "#ffffff", 0.18))
    gradient_bottom = _html_escape(_blend_hex(raw_team_color, "#000000", 0.15))
    fog_color = _html_escape(_blend_hex(raw_team_color, "#ffffff", 0.45))
    panel_light = _html_escape(_blend_hex(raw_team_color, "#ffffff", 0.22))
    date_text = _html_escape(str(state.get("date_text") or ""))
    handle = _html_escape(str(state.get("instagram_handle") or "@news_kbo"))
    scale = float(state.get("image_scale") or 1.0)
    offset_x = float(state.get("image_offset_x") or 0)
    offset_y = float(state.get("image_offset_y") or 0)
    compact_title_len = len("".join(str(state.get("title_text") or "").split()))
    title_font_size = max(84, 120 - (max(0, compact_title_len - 7) * 10))
    path.write_text(
        f"""<!doctype html>
<html lang="ko">
<head>
  <meta charset="utf-8">
  <style>
    {_font_face_css()}
    html, body {{ margin: 0; width: 1080px; height: 1350px; overflow: hidden; background: {team_color}; }}
    .card {{ position: relative; width: 1080px; height: 1350px; background: {team_color}; color: white; font-family: BodyFont, sans-serif; }}
    .photo {{ position: absolute; left: 20px; top: 26px; width: 1040px; height: 1298px; overflow: hidden; border-radius: 138px; background: #111; }}
    .photo img {{ position: absolute; left: 50%; bottom: 0; width: 100%; height: 100%; object-fit: contain; object-position: center bottom; transform: translate(calc(-50% + {offset_x}px), {offset_y}px) scale({scale}); transform-origin: center bottom; filter: blur(.3px); }}
    .topDim {{ position:absolute; inset:0; background: linear-gradient(to bottom, rgba(0,0,0,.07), rgba(0,0,0,0) 28%); }}
    .fog {{ position:absolute; inset:0; background: linear-gradient(to bottom, rgba(255,255,255,0) 60%, {fog_color} 100%); opacity:.45; filter: blur(16px); }}
    .teamPanel {{ position:absolute; inset:0; background: linear-gradient(to bottom, rgba(0,0,0,0) 68%, {panel_light} 78%, {team_color} 86%, {team_color} 100%); opacity:.96; }}
    .vertical {{ position:absolute; inset:0; background: linear-gradient(to bottom, rgba(0,0,0,0) 66%, {gradient_top} 76%, {gradient_bottom} 100%); opacity:.70; }}
    .date {{ position: absolute; top: 4px; left: 0; width: 100%; text-align: center; font-size: 20px; font-family: LightFont, BodyFont, sans-serif; font-weight: 400; }}
    .handle {{ position: absolute; top: 30px; left: 0; width: 100%; text-align: center; font-size: 30px; font-family: LightFont, BodyFont, sans-serif; font-weight: 400; }}
    .title {{ position: absolute; left: 44px; right: 44px; top: 1019px; height: auto; max-height: none; overflow: visible; font-family: TitleFont, sans-serif; font-weight: 400; font-size: {title_font_size}px; line-height: .90; letter-spacing: -7.5px; text-shadow: 0 2px 3px rgba(0,0,0,.12); white-space: nowrap; }}
    .sub {{ position: absolute; left: 44px; right: 44px; top: 1185px; height: auto; max-height: none; overflow: visible; font-family: BodyFont, sans-serif; font-weight: 400; font-size: 29px; line-height: 1.32; letter-spacing: 0; text-shadow: 0 .5px .8px rgba(0,0,0,.06); -webkit-font-smoothing: antialiased; text-rendering: geometricPrecision; }}
  </style>
</head>
<body>
  <main class="card">
    <section class="photo"><img src="{_html_escape(asset_url)}"><div class="topDim"></div><div class="fog"></div><div class="teamPanel"></div><div class="vertical"></div></section>
    <div class="date">{date_text}</div>
    <div class="handle">{handle}</div>
    <div class="title">{title}</div>
    <div class="sub">{subheadline}</div>
  </main>
</body>
</html>
""",
        encoding="utf-8",
    )


def _html_escape(value: str) -> str:
    return (
        value.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def _blend_hex(hex_color: str, target_hex: str, amount: float) -> str:
    blended = blend_color(_hex_to_rgb(hex_color), _hex_to_rgb(target_hex), amount)
    return f"#{blended[0]:02x}{blended[1]:02x}{blended[2]:02x}"


def _hex_to_rgb(hex_color: str) -> tuple[int, int, int]:
    value = str(hex_color or "#111111").strip().lstrip("#")
    if len(value) != 6:
        return (17, 17, 17)
    try:
        return tuple(int(value[index : index + 2], 16) for index in (0, 2, 4))
    except ValueError:
        return (17, 17, 17)


class EditorServer:
    def __init__(
        self,
        *,
        run_dir: Path,
        manifest_path: Path,
        host: str = "127.0.0.1",
        port: int = 8787,
        token: str | None = None,
        render_callback: Callable[[dict[str, Any]], None] | None = None,
        shutdown_after_render: bool = False,
        idle_timeout_seconds: int | None = None,
    ) -> None:
        self.run_dir = run_dir
        self.manifest_path = manifest_path
        self.host = host
        self.port = port
        self.token = str(token or "").strip()
        self.render_callback = render_callback
        self.shutdown_after_render = shutdown_after_render
        self.idle_timeout_seconds = idle_timeout_seconds
        self.last_activity_monotonic = time.monotonic()
        self.server: ThreadingHTTPServer | None = None
        self.manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

    def serve(self) -> None:
        parent = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802
                parent.handle_get(self)

            def do_POST(self) -> None:  # noqa: N802
                parent.handle_post(self)

            def log_message(self, format: str, *args: Any) -> None:
                print("[EDITOR SERVER] " + format % args)

        server = ThreadingHTTPServer((self.host, self.port), Handler)
        self.server = server
        first_url = self._with_token(f"http://{self.host}:{self.port}/topic/1")
        print(f"editor_url               : {first_url}")
        if self.idle_timeout_seconds:
            print(f"idle_timeout_seconds     : {self.idle_timeout_seconds}")
            threading.Thread(target=self._idle_shutdown_loop, daemon=True).start()
        if self.shutdown_after_render:
            print("shutdown_after_render    : enabled")
        print("종료하려면 Ctrl+C 를 누르세요.")
        server.serve_forever()

    def handle_get(self, handler: BaseHTTPRequestHandler) -> None:
        self._touch_activity()
        parsed = urllib.parse.urlparse(handler.path)
        if parsed.path in {"", "/"}:
            self._redirect(handler, self._with_token("/topic/1"))
            return
        if parsed.path.startswith("/topic/"):
            if not self._validate_token(handler, parsed):
                return
            index = int(parsed.path.rsplit("/", 1)[-1])
            topic = self._topic(index)
            self._send_file(handler, Path(topic["editor_html_path"]), "text/html; charset=utf-8")
            return
        if parsed.path.startswith("/payload/"):
            if not self._validate_token(handler, parsed):
                return
            index = int(parsed.path.rsplit("/", 1)[-1])
            topic = self._topic(index)
            self._send_file(handler, Path(topic["editor_payload_path"]), "application/json; charset=utf-8")
            return
        if parsed.path.startswith("/asset/"):
            if not self._validate_token(handler, parsed):
                return
            _, _, topic_index_text, asset_index_text = parsed.path.split("/", 3)
            self._send_asset(handler, int(topic_index_text), int(asset_index_text))
            return
        if parsed.path.startswith("/font/"):
            weight = parsed.path.rsplit("/", 1)[-1]
            self._send_font(handler, weight)
            return
        handler.send_error(404)

    def handle_post(self, handler: BaseHTTPRequestHandler) -> None:
        self._touch_activity()
        parsed = urllib.parse.urlparse(handler.path)
        if parsed.path != "/render":
            handler.send_error(404)
            return
        if not self._validate_token(handler, parsed):
            return
        length = int(handler.headers.get("Content-Length") or 0)
        state = json.loads(handler.rfile.read(length).decode("utf-8"))
        try:
            result = self._render_state(state)
        except Exception as exc:
            self._send_json(
                handler,
                {
                    "ok": False,
                    "error": str(exc),
                },
                status_code=500,
            )
            return
        if self.render_callback is not None:
            try:
                self.render_callback(result)
            except Exception as exc:
                result["automation_notification_ok"] = False
                result["automation_notification_error"] = str(exc)
        self._send_json(handler, result)
        if self.shutdown_after_render and result.get("ok"):
            threading.Timer(0.5, self._shutdown_server).start()

    def _touch_activity(self) -> None:
        self.last_activity_monotonic = time.monotonic()

    def _idle_shutdown_loop(self) -> None:
        timeout = max(1, int(self.idle_timeout_seconds or 0))
        while self.server is not None:
            time.sleep(min(30, timeout))
            if time.monotonic() - self.last_activity_monotonic >= timeout:
                print(f"[EDITOR SERVER] idle timeout reached; shutting down after {timeout}s")
                self._shutdown_server()
                return

    def _shutdown_server(self) -> None:
        if self.server is None:
            return
        self.server.shutdown()

    def _validate_token(self, handler: BaseHTTPRequestHandler, parsed: urllib.parse.ParseResult) -> bool:
        if not self.token:
            return True
        query = urllib.parse.parse_qs(parsed.query)
        request_token = str((query.get("token") or [""])[0])
        if request_token == self.token:
            return True
        handler.send_error(403)
        return False

    def _with_token(self, url: str) -> str:
        if not self.token:
            return url
        separator = "&" if "?" in url else "?"
        return f"{url}{separator}{urllib.parse.urlencode({'token': self.token})}"

    def _topic(self, index: int) -> dict[str, Any]:
        topics = self.manifest.get("topics") or []
        if index < 1 or index > len(topics):
            raise ValueError(f"topic index out of range: {index}")
        return topics[index - 1]

    def _send_asset(self, handler: BaseHTTPRequestHandler, topic_index: int, asset_index: int) -> None:
        topic = self._topic(topic_index)
        payload = json.loads(Path(topic["editor_payload_path"]).read_text(encoding="utf-8"))
        assets = payload.get("assets") or []
        if asset_index < 1 or asset_index > len(assets):
            handler.send_error(404)
            return
        asset = assets[asset_index - 1]
        storage_path = str(asset.get("storage_path") or "").strip()
        if storage_path and Path(storage_path).expanduser().exists():
            self._send_file(handler, Path(storage_path).expanduser(), str(asset.get("mime_type") or "image/jpeg"))
            return
        origin_url = str(asset.get("origin_url") or "").strip()
        if not origin_url:
            handler.send_error(404)
            return
        request = urllib.request.Request(origin_url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(request, timeout=20) as response:
            body = response.read()
            content_type = response.headers.get("Content-Type") or asset.get("mime_type") or "image/jpeg"
        handler.send_response(200)
        handler.send_header("Content-Type", str(content_type))
        handler.send_header("Content-Length", str(len(body)))
        handler.end_headers()
        handler.wfile.write(body)

    def _send_font(self, handler: BaseHTTPRequestHandler, weight: str) -> None:
        if weight not in {"bold", "medium", "light"}:
            handler.send_error(404)
            return
        font_path_text = resolve_font_path(weight)
        if not font_path_text:
            handler.send_error(404)
            return
        font_path = Path(font_path_text)
        content_type = {
            ".otf": "font/otf",
            ".ttf": "font/ttf",
            ".ttc": "font/collection",
            ".woff": "font/woff",
            ".woff2": "font/woff2",
        }.get(font_path.suffix.lower(), "application/octet-stream")
        self._send_file(handler, font_path, content_type)

    def _render_state(self, state: dict[str, Any]) -> dict[str, Any]:
        topic_index = int(state.get("topic_index") or 1)
        topic = self._topic(topic_index)
        topic_dir = Path(topic["topic_dir"])
        payload_path = Path(topic["editor_payload_path"])
        payload = json.loads(payload_path.read_text(encoding="utf-8"))
        selected_asset_index = int(state.get("selected_asset_index") or 1)
        asset_url = self._with_token(f"http://{self.host}:{self.port}/asset/{topic_index}/{selected_asset_index}")
        output_dir = topic_dir / "renders"
        output_dir.mkdir(parents=True, exist_ok=True)
        png_output_dir = self.run_dir / RENDER_PNG_DIR_NAME
        png_output_dir.mkdir(parents=True, exist_ok=True)
        title_text = str(state.get("title_text") or payload["original"]["title_text"])
        base_name = _build_title_output_basename(
            date_text=str(state.get("date_text") or payload["original"]["date_text"]),
            title_text=title_text,
            fallback_text=str(payload["original"]["topic_name"]),
        )
        topic_base_name = f"{topic_index:02d}_{base_name}"
        iteration = len(list(png_output_dir.glob(f"{topic_base_name}_*.png"))) + 1
        render_html_path = output_dir / f"{base_name}_{iteration:02d}.html"
        output_png_path = png_output_dir / f"{topic_base_name}_{iteration:02d}.png"
        _write_render_html(render_html_path, state, asset_url=asset_url)
        export_editable_html_to_png(render_html_path, output_png_path)

        original = dict(payload["original"])
        selected_asset = {}
        assets = payload.get("assets") or []
        if 1 <= selected_asset_index <= len(assets):
            selected_asset = dict(assets[selected_asset_index - 1])
        photo_credit = str(
            state.get("selected_image_source_name")
            or selected_asset.get("source_site_name")
            or _source_site_name(str(selected_asset.get("source_type") or ""))
        ).strip()
        edited = {
            **original,
            "title_text": title_text,
            "subheadline": str(state.get("subheadline") or original.get("subheadline") or ""),
            "team_name": state.get("team_name"),
            "team_color": state.get("team_color"),
            "selected_asset_reference": state.get("selected_asset_reference") or selected_asset.get("id"),
            "selected_image_candidate_index": selected_asset_index,
            "selected_image_source_type": selected_asset.get("source_type"),
            "selected_image_source_name": photo_credit,
            "selected_image_origin_url": selected_asset.get("origin_url"),
            "selected_image_storage_path": selected_asset.get("storage_path"),
            "selected_image_offset_x": state.get("image_offset_x"),
            "selected_image_offset_y": state.get("image_offset_y"),
            "selected_image_scale": state.get("image_scale"),
            "output_path": str(output_png_path),
        }
        state_path = output_png_path.with_suffix(".state.json")
        state_path.write_text(json.dumps(edited, ensure_ascii=False, indent=2), encoding="utf-8")
        completed_registry_path = _record_rendered_topic(
            topic=topic,
            approval_run_dir=self.run_dir,
            manifest_path=self.manifest_path,
            state_path=state_path,
            output_png_path=output_png_path,
        )
        if (
            str(original.get("title_text") or "") != str(edited.get("title_text") or "")
            or str(original.get("subheadline") or "") != str(edited.get("subheadline") or "")
        ):
            insert_headline_edit_memory(
                original_payload=original,
                edited_payload=edited,
                spec_path=state_path,
            )
        social_copy_md_path = Path(str(self.manifest.get("social_copy_md_path") or self.run_dir / RENDER_PNG_DIR_NAME / SOCIAL_COPY_MD_NAME))
        run_db_path = Path(str(self.manifest.get("run_db_path") or ""))
        if run_db_path.exists():
            section = _build_social_copy_md_section(
                db_path=run_db_path,
                topic_id=str(topic.get("topic_id") or ""),
                topic_index=topic_index,
                topic_name=str(topic.get("topic_name") or ""),
                title_copy={
                    "headline": title_text,
                    "subheadline": str(edited.get("subheadline") or ""),
                    "team_name": str(edited.get("team_name") or ""),
                    "date_text": str(edited.get("date_text") or ""),
                    "output_path": str(output_png_path),
                    "photo_credit": photo_credit,
                },
            )
            _replace_social_copy_md_section(social_copy_md_path, topic_id=str(topic.get("topic_id") or ""), section=section)
        return {
            "ok": True,
            "output_png_path": str(output_png_path),
            "state_path": str(state_path),
            "social_copy_md_path": str(social_copy_md_path),
            "completed_registry_path": str(completed_registry_path),
        }

    @staticmethod
    def _send_file(handler: BaseHTTPRequestHandler, path: Path, content_type: str) -> None:
        body = path.read_bytes()
        handler.send_response(200)
        handler.send_header("Content-Type", content_type)
        handler.send_header("Content-Length", str(len(body)))
        handler.end_headers()
        handler.wfile.write(body)

    @staticmethod
    def _send_json(handler: BaseHTTPRequestHandler, payload: dict[str, Any], *, status_code: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
        handler.send_response(status_code)
        handler.send_header("Content-Type", "application/json; charset=utf-8")
        handler.send_header("Content-Length", str(len(body)))
        handler.end_headers()
        handler.wfile.write(body)

    @staticmethod
    def _redirect(handler: BaseHTTPRequestHandler, location: str) -> None:
        handler.send_response(302)
        handler.send_header("Location", location)
        handler.end_headers()


def build_no_multimodal_editor_run(approval_run_dir: Path, confirmed_json_path: Path) -> Path:
    confirmed_payload = json.loads(confirmed_json_path.read_text(encoding="utf-8"))
    report_path = Path(str(confirmed_payload["candidate_report_path"])).expanduser()
    candidate_report = json.loads(report_path.read_text(encoding="utf-8"))
    source_db_path = Path(str(candidate_report["collection_db_path"])).expanduser()

    selection_result = selection_result_from_dict(candidate_report["selection_result"])
    selected_ids = set(str(item) for item in confirmed_payload["selected_topic_ids"])
    selection_result.topics = [topic for topic in selection_result.topics if topic.topic_id in selected_ids]
    if len(selection_result.topics) != len(selected_ids):
        raise RuntimeError("confirmed topic ids do not match candidate report topics")

    run_dir = ensure_stage_dir("03_title_html_editor_no_multimodal", run_dir=approval_run_dir)
    db_path = run_dir / "title_editor_collection.db"
    shutil.copy2(source_db_path, db_path)

    research_engine = choose_research_engine()
    structuring_engine = choose_structuring_engine()
    research_service = TopicDeepResearchService(engine=research_engine)
    structuring_service = IssueStructuringService(engine=structuring_engine)
    structuring_builder = TopicStructuringInputBuilder()

    with SQLiteSourceItemRepository(str(db_path)) as repository:
        persisted_items = repository.list_items()
        batch_input = StoredArticleBatchBuilder().build(
            persisted_items,
            batch_id=selection_result.batch_id,
            window_start=datetime.fromisoformat(str(candidate_report["window_start_kst"])).astimezone(timezone.utc),
            window_end=datetime.fromisoformat(str(candidate_report["window_end_kst"])).astimezone(timezone.utc),
        )
        repository.save_topic_selection_result(batch_input, selection_result)

        topic_entries: list[dict[str, Any]] = []
        report_topics: list[dict[str, Any]] = []
        for index, bundle in enumerate(
            StoredTopicDeepResearchBuilder().build(
                persisted_items,
                batch_input=batch_input,
                selection_result=selection_result,
            ),
            start=1,
        ):
            research_result = research_service.research_topic(bundle.topic_input)
            repository.save_topic_deep_research_result(research_result)
            structuring_input = structuring_builder.build(bundle.topic_input, research_result)
            draft = structuring_service.build_draft(structuring_input)
            topic = bundle.topic_input.topic
            topic_dir = run_dir / f"{index:02d}_{_slugify(topic.topic_name)}"
            topic_dir.mkdir(parents=True, exist_ok=True)
            assets = _collect_topic_assets(db_path, list(topic.article_ids))
            copy_payload = _build_title_copy_payload(
                topic=topic,
                research_result=research_result,
                structuring_input=structuring_input,
                draft=draft,
            )
            editor_payload = {
                "topic_index": index,
                "topic_count": len(selection_result.topics),
                "original": copy_payload,
                "team_colors": TEAM_COLORS,
                "assets": assets,
                "draft": serialize_for_json(draft),
                "structuring_input": serialize_for_json(structuring_input),
                "research_result": serialize_for_json(research_result),
            }
            editor_payload_path = topic_dir / "editor_payload.json"
            editor_payload_path.write_text(json.dumps(editor_payload, ensure_ascii=False, indent=2), encoding="utf-8")
            editor_html_path = _write_editor_html(topic_dir, editor_payload_path)
            topic_entry = {
                "topic_index": index,
                "topic_name": topic.topic_name,
                "topic_id": topic.topic_id,
                "representative_article_id": topic.representative_article_id,
                "article_ids": list(topic.article_ids),
                "topic_dir": str(topic_dir),
                "editor_payload_path": str(editor_payload_path),
                "editor_html_path": str(editor_html_path),
                "asset_count": len(assets),
            }
            topic_entries.append(topic_entry)
            report_topics.append(
                {
                    **topic_entry,
                    "topic": serialize_for_json(topic),
                    "research_result": serialize_for_json(research_result),
                    "structuring_input": serialize_for_json(structuring_input),
                    "draft": serialize_for_json(draft),
                    "title_copy": copy_payload,
                }
            )

    social_copy_md_path = _write_title_render_social_copy_md(
        run_dir=approval_run_dir,
        db_path=db_path,
        topic_entries=topic_entries,
    )
    print(f"title_render_social_copy_md: {social_copy_md_path}")

    report = {
        "confirmed_selection_path": str(confirmed_json_path),
        "candidate_report_path": str(report_path),
        "source_collection_db_path": str(source_db_path),
        "run_db_path": str(db_path),
        "render_png_dir": str(approval_run_dir / RENDER_PNG_DIR_NAME),
        "social_copy_md_path": str(social_copy_md_path),
        "research_model_name": getattr(research_engine, "model_name", "heuristic-topic-deep-research-v1"),
        "structuring_model_name": getattr(structuring_engine, "model_name", "heuristic-card-structuring-v1"),
        "topic_count": len(topic_entries),
        "topics": report_topics,
    }
    report_path = run_dir / "title_html_editor_report.json"
    report_path.write_text(json.dumps(_serialize(report), ensure_ascii=False, indent=2), encoding="utf-8")
    manifest_path = run_dir / "title_html_editor_manifest.json"
    manifest_path.write_text(
        json.dumps(
            {
                "run_dir": str(approval_run_dir),
                "stage_dir": str(run_dir),
                "run_db_path": str(db_path),
                "render_png_dir": str(approval_run_dir / RENDER_PNG_DIR_NAME),
                "social_copy_md_path": str(social_copy_md_path),
                "report_path": str(report_path),
                "topic_count": len(topic_entries),
                "topics": topic_entries,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    return manifest_path


def main() -> None:
    load_default_env(ROOT_DIR)
    approval_run_dir = OUTPUT_ROOT / f"approval_run_{datetime.now(KST).strftime('%Y%m%d_%H%M%S')}"
    approval_run_dir.mkdir(parents=True, exist_ok=True)
    print(f"approval_run_dir         : {approval_run_dir}")

    print("[TITLE HTML EDITOR NO MM] 1/4 후보군 생성")
    _run(SCRIPT_BATCH_TOPIC_SELECTION, approval_run_dir=approval_run_dir)
    choice_json_path = approval_run_dir / "01_topic_candidates" / "topic_selection_choice.json"
    print(f"topic_selection_choice_json: {choice_json_path}")

    print("[TITLE HTML EDITOR NO MM] 2/4 선택 주제 확정")
    _run(SCRIPT_APPLY_TOPIC_SELECTION, str(choice_json_path), approval_run_dir=approval_run_dir)
    confirmed_json_path = approval_run_dir / "02_topic_selection" / "topic_selection_confirmed.json"
    print(f"topic_selection_confirmed_json: {confirmed_json_path}")

    print("[TITLE HTML EDITOR NO MM] 3/4 리서치 + 카드뉴스 구조 + title copy 생성")
    manifest_path = build_no_multimodal_editor_run(approval_run_dir, confirmed_json_path)
    print(f"title_html_editor_manifest: {manifest_path}")

    print("[TITLE HTML EDITOR NO MM] 4/4 HTML 편집 서버 시작")
    EditorServer(run_dir=approval_run_dir, manifest_path=manifest_path).serve()


_EDITOR_HTML = r"""<!doctype html>
<html lang="ko">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>KBO title image editor</title>
  <style>
    :root { --panel: #151515; --line: #303030; --text: #f5f0e8; --muted: #a8a095; --accent: #e85d2a; }
    * { box-sizing: border-box; }
    body { margin: 0; background: #0b0b0b; color: var(--text); font-family: ui-sans-serif, -apple-system, BlinkMacSystemFont, "Apple SD Gothic Neo", sans-serif; }
    .app { display: grid; grid-template-columns: minmax(560px, 1fr) 420px; gap: 18px; height: 100vh; padding: 18px; }
    .stageWrap { min-height: 0; display: grid; place-items: center; background: radial-gradient(circle at 50% 20%, #2d2b28, #090909 68%); border: 1px solid var(--line); border-radius: 24px; overflow: auto; }
    __FONT_FACE_CSS__
    .stageViewport { width: 562px; height: 702px; flex: 0 0 auto; }
    .stage { width: 1080px; height: 1350px; position: relative; background: var(--team, #111); overflow: hidden; transform: scale(var(--preview-scale, .52)); transform-origin: top left; color: white; font-family: BodyFont, sans-serif; }
    .photo { position: absolute; left: 20px; top: 26px; width: 1040px; height: 1298px; overflow: hidden; border-radius: 138px; background: #111; cursor: grab; }
    .photo:active { cursor: grabbing; }
    .photo img { position: absolute; left: 50%; bottom: 0; width: 100%; height: 100%; object-fit: contain; object-position: center bottom; transform-origin: center bottom; filter: blur(.3px); user-select: none; pointer-events: none; }
    .topDim { position:absolute; inset:0; background: linear-gradient(to bottom, rgba(0,0,0,.07), rgba(0,0,0,0) 28%); pointer-events: none; }
    .fog { position:absolute; inset:0; background: linear-gradient(to bottom, rgba(255,255,255,0) 60%, var(--fog) 100%); opacity:.45; filter: blur(16px); pointer-events: none; }
    .teamPanel { position:absolute; inset:0; background: linear-gradient(to bottom, rgba(0,0,0,0) 68%, var(--panel-light) 78%, var(--team) 86%, var(--team) 100%); opacity:.96; pointer-events: none; }
    .vertical { position:absolute; inset:0; background: linear-gradient(to bottom, rgba(0,0,0,0) 66%, var(--gradient-top) 76%, var(--gradient-bottom) 100%); opacity:.70; pointer-events: none; }
    .date { position: absolute; top: 4px; left: 0; width: 100%; text-align: center; font-size: 20px; font-family: LightFont, BodyFont, sans-serif; font-weight: 400; }
    .handle { position: absolute; top: 30px; left: 0; width: 100%; text-align: center; font-size: 30px; font-family: LightFont, BodyFont, sans-serif; font-weight: 400; }
    .title { position: absolute; left: 44px; right: 44px; top: 1019px; height: auto; max-height: none; overflow: visible; font-family: TitleFont, sans-serif; font-weight: 400; font-size: 120px; line-height: .90; letter-spacing: -7.5px; white-space: nowrap; text-shadow: 0 2px 3px rgba(0,0,0,.12); }
    .sub { position: absolute; left: 44px; right: 44px; top: 1185px; height: auto; max-height: none; overflow: visible; font-family: BodyFont, sans-serif; font-weight: 400; font-size: 29px; line-height: 1.32; letter-spacing: 0; white-space: pre-line; text-shadow: 0 .5px .8px rgba(0,0,0,.06); -webkit-font-smoothing: antialiased; text-rendering: geometricPrecision; }
    .side { min-height: 0; display: flex; flex-direction: column; gap: 12px; }
    .panel { background: var(--panel); border: 1px solid var(--line); border-radius: 18px; padding: 14px; }
    .panel h2 { font-size: 14px; margin: 0 0 10px; color: #fff; }
    label { display: block; font-size: 12px; color: var(--muted); margin: 10px 0 5px; }
    input, textarea, select, button { width: 100%; border: 1px solid #3b3b3b; border-radius: 10px; background: #0d0d0d; color: var(--text); padding: 10px; font: inherit; }
    textarea { min-height: 84px; resize: vertical; }
    button { background: var(--accent); border: 0; color: #120700; font-weight: 800; cursor: pointer; }
    button.secondary { background: #242424; color: var(--text); border: 1px solid #3b3b3b; }
    .assets { overflow: auto; min-height: 0; flex: 1; padding-right: 4px; }
    .asset { display: grid; grid-template-columns: 92px 1fr; gap: 10px; padding: 9px; border: 1px solid var(--line); border-radius: 14px; margin-bottom: 10px; cursor: pointer; background: #101010; }
    .asset.selected { border-color: var(--accent); box-shadow: 0 0 0 1px var(--accent) inset; }
    .asset img { width: 92px; height: 92px; object-fit: contain; border-radius: 10px; background: #222; }
    .asset strong { display: block; font-size: 13px; margin-bottom: 4px; }
    .asset span { display: block; color: var(--muted); font-size: 12px; line-height: 1.35; word-break: break-all; }
    .row { display: grid; grid-template-columns: 1fr 1fr; gap: 8px; }
    .navRow { display: grid; grid-template-columns: 1fr 1fr; gap: 8px; }
    .navRow button:disabled { opacity: .35; cursor: not-allowed; }
    .status { font-size: 12px; color: var(--muted); min-height: 18px; }
  </style>
</head>
<body>
  <div class="app">
    <section class="stageWrap">
      <div class="stageViewport" id="stageViewport">
        <div class="stage" id="stage">
          <div class="photo" id="photo"><img id="hero" alt=""><div class="topDim"></div><div class="fog"></div><div class="teamPanel"></div><div class="vertical"></div></div>
          <div class="date" id="dateText"></div>
          <div class="handle" id="handle">@news_kbo</div>
          <div class="title" id="titlePreview"></div>
          <div class="sub" id="subPreview"></div>
        </div>
      </div>
    </section>
    <aside class="side">
      <section class="panel">
        <h2 id="topicName">Title editor</h2>
        <label>Headline</label>
        <input id="titleInput">
        <label>Subheadline</label>
        <textarea id="subInput"></textarea>
        <div class="row">
          <div><label>Team</label><input id="teamInput"></div>
          <div><label>Color</label><input id="colorInput" type="color"></div>
        </div>
      </section>
      <section class="panel">
        <h2>Image transform</h2>
        <label>Scale <span id="scaleValue"></span></label>
        <input id="scaleInput" type="range" min="0.7" max="2.2" step="0.01">
        <div class="row">
          <div><label>X</label><input id="xInput" type="range" min="-400" max="400" step="1"></div>
          <div><label>Y</label><input id="yInput" type="range" min="-400" max="400" step="1"></div>
        </div>
        <button class="secondary" id="resetBtn" type="button">이미지 위치 초기화</button>
      </section>
      <section class="panel assets" id="assetList"></section>
      <section class="panel">
        <div class="navRow">
          <button class="secondary" id="prevTopicBtn" type="button">이전 토픽</button>
          <button class="secondary" id="nextTopicBtn" type="button">다음 토픽</button>
        </div>
        <button id="saveBtn" type="button">PNG 저장</button>
        <p class="status" id="status"></p>
      </section>
    </aside>
  </div>
  <script>
    const editorToken = new URLSearchParams(location.search).get("token") || "";
    const tokenQuery = editorToken ? `?token=${encodeURIComponent(editorToken)}` : "";
    const payloadUrl = "/payload/" + location.pathname.split("/").filter(Boolean).pop() + tokenQuery;
    const state = { selectedAssetIndex: 1, scale: 1, x: 0, y: 0 };
    let payload = null;
    const $ = (id) => document.getElementById(id);

    function assetUrl(index) {
      return `/asset/${payload.topic_index}/${index}${tokenQuery}`;
    }

    function hexToRgb(hexColor) {
      const value = String(hexColor || "#111111").trim().replace(/^#/, "");
      if (!/^[0-9a-fA-F]{6}$/.test(value)) return [17, 17, 17];
      return [0, 2, 4].map((start) => parseInt(value.slice(start, start + 2), 16));
    }

    function blendHex(hexColor, targetHex, amount) {
      const source = hexToRgb(hexColor);
      const target = hexToRgb(targetHex);
      const blended = source.map((channel, index) => Math.trunc(channel + ((target[index] - channel) * amount)));
      return "#" + blended.map((channel) => channel.toString(16).padStart(2, "0")).join("");
    }

    function apply() {
      const teamColor = $("colorInput").value || "#111111";
      $("stage").style.setProperty("--team", teamColor);
      $("stage").style.setProperty("--gradient-top", blendHex(teamColor, "#ffffff", 0.18));
      $("stage").style.setProperty("--gradient-bottom", blendHex(teamColor, "#000000", 0.15));
      $("stage").style.setProperty("--fog", blendHex(teamColor, "#ffffff", 0.45));
      $("stage").style.setProperty("--panel-light", blendHex(teamColor, "#ffffff", 0.22));
      $("titlePreview").textContent = $("titleInput").value;
      $("subPreview").textContent = $("subInput").value;
      const compactTitleLength = $("titleInput").value.replace(/\s+/g, "").length;
      const titleSize = compactTitleLength > 7 ? Math.max(84, 120 - ((compactTitleLength - 7) * 10)) : 120;
      $("titlePreview").style.fontSize = `${titleSize}px`;
      $("dateText").textContent = payload.original.date_text || "";
      $("hero").src = assetUrl(state.selectedAssetIndex);
      $("hero").style.transform = `translate(calc(-50% + ${state.x}px), ${state.y}px) scale(${state.scale})`;
      $("scaleValue").textContent = state.scale.toFixed(2);
      document.querySelectorAll(".asset").forEach((node) => {
        node.classList.toggle("selected", Number(node.dataset.index) === state.selectedAssetIndex);
      });
    }

    function resizePreview() {
      const wrap = document.querySelector(".stageWrap");
      if (!wrap) return;
      const scale = Math.max(0.25, Math.min((wrap.clientWidth - 36) / 1080, (wrap.clientHeight - 36) / 1350));
      $("stageViewport").style.setProperty("--preview-scale", String(scale));
      $("stageViewport").style.width = `${1080 * scale}px`;
      $("stageViewport").style.height = `${1350 * scale}px`;
      $("stageViewport").dataset.scale = String(scale);
      $("stage").style.setProperty("--preview-scale", String(scale));
    }

    function renderAssets() {
      const list = $("assetList");
      list.innerHTML = "<h2>Image candidates</h2>";
      payload.assets.forEach((asset, idx) => {
        const index = idx + 1;
        const item = document.createElement("div");
        item.className = "asset";
        item.dataset.index = String(index);
        const sourceName = asset.source_site_name || asset.source_type || "";
        const sourceLabel = sourceName ? ` · ${sourceName}` : "";
        item.innerHTML = `<img src="${assetUrl(index)}"><div><strong>#${index} ${asset.asset_type || "image"}${sourceLabel}</strong><span>${asset.origin_url || asset.storage_path || ""}</span></div>`;
        item.addEventListener("click", () => {
          state.selectedAssetIndex = index;
          state.scale = 1;
          state.x = 0;
          state.y = 0;
          syncInputs();
          apply();
        });
        list.appendChild(item);
      });
    }

    function syncInputs() {
      $("scaleInput").value = state.scale;
      $("xInput").value = state.x;
      $("yInput").value = state.y;
    }

    async function savePng() {
      $("status").textContent = "렌더링 중...";
      const asset = payload.assets[state.selectedAssetIndex - 1] || {};
      const response = await fetch(`/render${tokenQuery}`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          topic_index: payload.topic_index,
          topic_id: payload.original.topic_id,
          topic_name: payload.original.topic_name,
          title_text: $("titleInput").value,
          subheadline: $("subInput").value,
          team_name: $("teamInput").value,
          team_color: $("colorInput").value,
          date_text: payload.original.date_text,
          instagram_handle: "@news_kbo",
          selected_asset_index: state.selectedAssetIndex,
          selected_asset_reference: asset.id,
          selected_image_source_name: asset.source_site_name || asset.source_type || "",
          image_scale: state.scale,
          image_offset_x: state.x,
          image_offset_y: state.y
        })
      });
      const result = await response.json();
      $("status").textContent = result.ok ? `저장됨: ${result.output_png_path}` : "저장 실패";
    }

    function wire() {
      ["titleInput", "subInput", "teamInput", "colorInput"].forEach((id) => $(id).addEventListener("input", apply));
      $("scaleInput").addEventListener("input", (event) => { state.scale = Number(event.target.value); apply(); });
      $("xInput").addEventListener("input", (event) => { state.x = Number(event.target.value); apply(); });
      $("yInput").addEventListener("input", (event) => { state.y = Number(event.target.value); apply(); });
      $("resetBtn").addEventListener("click", () => { state.scale = 1; state.x = 0; state.y = 0; syncInputs(); apply(); });
      $("saveBtn").addEventListener("click", savePng);
      $("prevTopicBtn").addEventListener("click", () => moveTopic(-1));
      $("nextTopicBtn").addEventListener("click", () => moveTopic(1));
      let dragging = false, lastX = 0, lastY = 0;
      $("photo").addEventListener("pointerdown", (event) => { dragging = true; lastX = event.clientX; lastY = event.clientY; $("photo").setPointerCapture(event.pointerId); });
      $("photo").addEventListener("pointermove", (event) => {
        if (!dragging) return;
        const previewScale = Number($("stageViewport").dataset.scale || 1);
        state.x += (event.clientX - lastX) / previewScale;
        state.y += (event.clientY - lastY) / previewScale;
        lastX = event.clientX;
        lastY = event.clientY;
        syncInputs();
        apply();
      });
      $("photo").addEventListener("pointerup", () => { dragging = false; });
      $("photo").addEventListener("wheel", (event) => {
        event.preventDefault();
        state.scale = Math.max(0.7, Math.min(2.2, state.scale + (event.deltaY > 0 ? -0.04 : 0.04)));
        syncInputs();
        apply();
      }, { passive: false });
    }

    function moveTopic(delta) {
      const current = Number(location.pathname.split("/").filter(Boolean).pop() || payload.topic_index || 1);
      const next = current + delta;
      if (next < 1 || next > payload.topic_count) return;
      location.href = `/topic/${next}${tokenQuery}`;
    }

    function syncTopicNav() {
      const current = Number(location.pathname.split("/").filter(Boolean).pop() || payload.topic_index || 1);
      const total = Number(payload.topic_count || 1);
      $("prevTopicBtn").disabled = current <= 1;
      $("nextTopicBtn").disabled = current >= total;
      $("prevTopicBtn").textContent = `이전 토픽 (${current}/${total})`;
      $("nextTopicBtn").textContent = `다음 토픽 (${current}/${total})`;
    }

    fetch(payloadUrl).then((response) => response.json()).then((data) => {
      payload = data;
      $("topicName").textContent = payload.original.topic_name;
      $("titleInput").value = payload.original.title_text;
      $("subInput").value = payload.original.subheadline;
      $("teamInput").value = payload.original.team_name || "";
      $("colorInput").value = payload.original.team_color || "#111111";
      renderAssets();
      syncInputs();
      wire();
      syncTopicNav();
      resizePreview();
      window.addEventListener("resize", resizePreview);
      apply();
    });
  </script>
</body>
</html>
"""


if __name__ == "__main__":
    main()
