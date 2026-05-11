from __future__ import annotations

import base64
import hashlib
import importlib.util
import json
import os
import re
import sqlite3
import sys
import socket
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from types import ModuleType
from typing import Any, Protocol

from PIL import Image, ImageDraw

from kbo_card_news.config.env import load_default_env
from kbo_card_news.feedback_memory import (
    FeedbackMemoryRepository,
    apply_headline_policies,
    build_topic_fingerprint,
    extract_topic_features,
    format_headline_retrieval_summary,
    retrieve_similar_headline_edits,
)
from kbo_card_news.runtime.model_fallback import call_openai
from kbo_card_news.scoring.engine import HttpTransport, UrllibHttpTransport


ROOT_DIR = Path(__file__).resolve().parents[3]
MM_PROMPT_PATH = ROOT_DIR / "mm_prompt.md"
TITLE_MIN_NON_SPACE_CHARS = 4
TITLE_MAX_NON_SPACE_CHARS = 7
SUBHEADLINE_MAX_CHARS = 70
SUBHEADLINE_TWO_LINE_MIN_CHARS = 18
SUBHEADLINE_TARGET_MIN_CHARS = 70
SUBHEADLINE_TARGET_MAX_CHARS = 85
SUBHEADLINE_FIRST_LINE_MIN_CHARS = 40
SUBHEADLINE_FIRST_LINE_MAX_CHARS = 48

TEAM_KEYWORDS = [
    "LG",
    "KIA",
    "두산",
    "삼성",
    "롯데",
    "SSG",
    "한화",
    "KT",
    "NC",
    "키움",
]


@dataclass
class TopicTitlePageSpec:
    topic_id: str
    topic_name: str
    draft_title: str
    title_text: str
    subheadline: str
    team_name: str | None
    date_text: str
    image_source: str
    output_path: Path
    copy_source: str = "rule_based"
    copy_model_name: str = "rule-based-title-copy-v1"
    memory_context_used: bool = False
    num_similar_cases: int = 0
    referenced_memory_ids: list[str] = field(default_factory=list)
    memory_context_summary: str | None = None
    policy_correction_used: bool = False
    applied_policy_ids: list[str] = field(default_factory=list)
    applied_policy_types: list[str] = field(default_factory=list)
    policy_correction_summary: str | None = None
    pre_correction_title_text: str | None = None
    pre_correction_subheadline: str | None = None
    selected_asset_reference: str | None = None
    image_selection_source: str = "rule_based"
    image_selection_model_name: str = "rule-based-title-image-v1"


@dataclass
class TopicTitlePageResult:
    spec: TopicTitlePageSpec
    cached_image_path: Path
    output_path: Path
    original_spec_path: Path
    editable_spec_path: Path
    simple_edit_spec_path: Path
    candidate_image_dir: Path
    candidate_manifest_path: Path


@dataclass
class TitleImageCandidate:
    asset_reference: str
    image_source: str
    origin_url: str
    storage_path: str | None
    sort_order: int
    usage_recommendation: str | None = None
    confidence: float | None = None
    scene_description: str | None = None
    humor_point: str | None = None
    caution_note: str | None = None
    local_image_path: Path | None = None


@dataclass
class TitleImageSelectionInput:
    topic_id: str
    topic_name: str
    article_summary: str
    title_text: str
    subheadline: str
    team_candidates: list[str]
    player_candidates: list[str]
    candidates: list[TitleImageCandidate]


@dataclass
class TitleImageSelectionOutput:
    asset_reference: str
    image_source: str
    selection_source: str
    model_name: str
    fit_reason: str = ""


class TopicTitlePageRenderer:
    def __init__(
        self,
        *,
        cache_dir: Path | None = None,
        instagram_handle: str = "@news_kbo",
        copy_engine: "TitleCopyEngine | None" = None,
        image_selection_engine: "TitleImageSelectionEngine | None" = None,
        feedback_repository: FeedbackMemoryRepository | None = None,
    ) -> None:
        self.cache_dir = cache_dir
        self.instagram_handle = instagram_handle
        self._design_module: ModuleType | None = None
        self.copy_engine = copy_engine or build_default_title_copy_engine()
        self.image_selection_engine = image_selection_engine or build_default_title_image_selection_engine()
        self.feedback_repository = feedback_repository

    def render_from_run_dir(
        self,
        run_dir: Path | str,
        *,
        output_path: Path | str | None = None,
        topic_index: int = 1,
    ) -> TopicTitlePageResult:
        run_dir = Path(run_dir).expanduser()
        spec, candidate_assets = self._build_spec_context(
            run_dir,
            output_path=output_path,
            topic_index=topic_index,
        )
        image_path = self._resolve_image_path(spec.image_source)

        try:
            self._render_spec_to_output(spec=spec, image_path=image_path)
        except (FileNotFoundError, ImportError, AttributeError):
            self._render_basic_card(spec=spec, image_path=image_path)
        original_spec_path, editable_spec_path = self._write_spec_outputs(
            spec=spec,
            image_path=image_path,
            candidates=candidate_assets,
        )
        candidate_image_dir, candidate_manifest_path = self._export_candidate_images(
            spec=spec,
            candidates=candidate_assets,
        )
        simple_edit_spec_path = self._write_simple_edit_spec(
            spec=spec,
            editable_spec_path=editable_spec_path,
            candidate_manifest_path=candidate_manifest_path,
        )
        self._augment_editable_specs(
            original_spec_path=original_spec_path,
            editable_spec_path=editable_spec_path,
            simple_edit_spec_path=simple_edit_spec_path,
            candidate_manifest_path=candidate_manifest_path,
        )
        return TopicTitlePageResult(
            spec=spec,
            cached_image_path=image_path,
            output_path=spec.output_path,
            original_spec_path=original_spec_path,
            editable_spec_path=editable_spec_path,
            simple_edit_spec_path=simple_edit_spec_path,
            candidate_image_dir=candidate_image_dir,
            candidate_manifest_path=candidate_manifest_path,
        )

    def build_spec(
        self,
        run_dir: Path | str,
        *,
        output_path: Path | str | None = None,
        topic_index: int = 1,
    ) -> TopicTitlePageSpec:
        spec, _ = self._build_spec_context(
            run_dir,
            output_path=output_path,
            topic_index=topic_index,
        )
        return spec

    def _build_spec_context(
        self,
        run_dir: Path | str,
        *,
        output_path: Path | str | None = None,
        topic_index: int = 1,
    ) -> tuple[TopicTitlePageSpec, list[TitleImageCandidate]]:
        run_dir = Path(run_dir).expanduser()
        report_path = run_dir / "topic_multimodal_report.json"
        db_path = run_dir / "topic_multimodal_collection.db"
        payload = json.loads(report_path.read_text(encoding="utf-8"))

        analyses = payload.get("multimodal_analyses") or []
        drafts = payload.get("drafts") or []
        if topic_index < 1 or topic_index > len(analyses):
            raise ValueError(f"topic_index must be between 1 and {len(analyses)}")

        analysis = analyses[topic_index - 1]
        issue_id = str(analysis.get("issue_id") or "").strip()
        if not issue_id:
            raise ValueError("multimodal_analyses entry is missing issue_id")

        draft = next((item for item in drafts if item.get("issue_id") == issue_id), None)
        if draft is None:
            raise ValueError(f"No draft found for issue_id={issue_id}")

        with sqlite3.connect(db_path) as conn:
            conn.row_factory = sqlite3.Row
            topic_row = conn.execute(
                """
                SELECT topic_id, topic_name, reason_summary, representative_article_id
                FROM selected_topics
                WHERE topic_id = ?
                """,
                (issue_id,),
            ).fetchone()
            if topic_row is None:
                raise ValueError(f"No selected_topics row found for issue_id={issue_id}")

            article_row = conn.execute(
                """
                SELECT id, title, published_at
                FROM source_items
                WHERE id = ?
                """,
                (topic_row["representative_article_id"],),
            ).fetchone()
            asset_candidates = self._collect_asset_candidates(
                conn,
                analysis=analysis,
                draft=draft,
                representative_article_id=topic_row["representative_article_id"],
            )
        if not asset_candidates:
            raise ValueError(f"No usable image asset found for issue_id={issue_id}")

        topic_name = str(topic_row["topic_name"] or "").strip()
        draft_title = str(draft.get("title") or topic_name).strip()
        cover_page = self._select_cover_page(draft)
        summary = str(analysis.get("overall_summary") or topic_row["reason_summary"] or "").strip()
        cover_body = str((cover_page or {}).get("body") or "").strip()
        draft_subtitle = str(draft.get("subtitle") or cover_body or summary).strip()
        team_name = _detect_team_name(topic_name) or _detect_team_name(draft_title, draft_subtitle, summary)
        draft_title = _filter_text_for_team(draft_title, team_name) or topic_name
        draft_subtitle = _filter_text_for_team(draft_subtitle, team_name) or draft_subtitle
        summary = _filter_text_for_team(summary, team_name) or summary
        cover_headline = _filter_text_for_team(str((cover_page or {}).get("headline") or "").strip(), team_name)
        cover_body = _filter_text_for_team(cover_body, team_name) or cover_body
        topic_features = extract_topic_features(
            {
                "topic_id": issue_id,
                "topic_name": topic_name,
                "team_name": team_name,
                "draft_title": draft_title,
                "draft_subtitle": draft_subtitle,
                "cover_headline": cover_headline,
                "cover_body": cover_body,
                "overall_summary": summary,
                "topic_type": draft.get("topic_type") or analysis.get("topic_type"),
                "entity_focus": draft.get("entity_focus") or analysis.get("entity_focus"),
                "event_type": draft.get("event_type") or analysis.get("event_type"),
                "angle_type": draft.get("angle_type") or analysis.get("angle_type"),
                "recommended_focus": draft.get("recommended_focus") or analysis.get("recommended_focus"),
                "has_notable_numbers": draft.get("has_notable_numbers", analysis.get("has_notable_numbers")),
                "asset_count": len(asset_candidates),
            }
        )
        topic_fingerprint = build_topic_fingerprint(
            {
                "topic_id": issue_id,
                "topic_name": topic_name,
                "team_name": team_name,
                "draft_title": draft_title,
                "draft_subtitle": draft_subtitle,
                "overall_summary": summary,
                **topic_features,
            },
            overrides={key: topic_features.get(key) for key in ("topic_type", "entity_focus", "event_type", "angle_type")},
        )
        memory_context_summary, referenced_memory_ids = self._build_title_memory_context(
            topic_id=issue_id,
            topic_name=topic_name,
            team_name=team_name,
            draft_title=draft_title,
            draft_subtitle=draft_subtitle,
            cover_headline=cover_headline,
            cover_body=cover_body,
            overall_summary=summary,
            topic_type=topic_features.get("topic_type"),
            entity_focus=topic_features.get("entity_focus"),
            event_type=topic_features.get("event_type"),
            angle_type=topic_features.get("angle_type"),
            recommended_focus=topic_features.get("recommended_focus"),
            has_notable_numbers=topic_features.get("has_notable_numbers"),
        )
        copy_input = TitleCopyInput(
            topic_id=issue_id,
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
        copy_output = self.copy_engine.rewrite(copy_input)
        copy_output = TitleCopyOutput(
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
                "topic_id": issue_id,
                "topic_name": topic_name,
                "team_name": team_name,
                "topic_fingerprint": topic_fingerprint,
                **topic_features,
            },
            repository=self.feedback_repository,
        )
        copy_output = TitleCopyOutput(
            title_text=_sanitize_title_text(policy_result.title_text, copy_input=copy_input),
            subheadline=_sanitize_subheadline(
                policy_result.subheadline,
                team_name=team_name,
                fallback_text=_build_subheadline_source(copy_input, team_name=team_name),
            ),
            copy_source=copy_output.copy_source,
            model_name=copy_output.model_name,
        )
        enriched_candidates = self._enrich_asset_candidates(asset_candidates)
        image_selection = self.image_selection_engine.select_image(
            TitleImageSelectionInput(
                topic_id=issue_id,
                topic_name=topic_name,
                article_summary=summary,
                title_text=copy_output.title_text,
                subheadline=copy_output.subheadline,
                team_candidates=[team_name] if team_name else _build_team_candidates(topic_name, draft_title, draft_subtitle, summary),
                player_candidates=_extract_player_candidates(topic_name, draft_title, draft_subtitle, summary, cover_body),
                candidates=enriched_candidates,
            )
        )
        date_text = _format_date_text(article_row["published_at"] if article_row else None)
        resolved_output = (
            Path(output_path)
            if output_path
            else self._default_output_path(
                run_dir,
                date_text=date_text,
                title_text=copy_output.title_text,
                fallback_text=topic_name or issue_id,
            )
        )

        spec = TopicTitlePageSpec(
            topic_id=issue_id,
            topic_name=topic_name,
            draft_title=draft_title,
            title_text=copy_output.title_text,
            subheadline=copy_output.subheadline,
            team_name=team_name,
            date_text=date_text,
            image_source=image_selection.image_source,
            output_path=resolved_output,
            copy_source=copy_output.copy_source,
            copy_model_name=copy_output.model_name,
            memory_context_used=bool(referenced_memory_ids),
            num_similar_cases=len(referenced_memory_ids),
            referenced_memory_ids=list(referenced_memory_ids),
            memory_context_summary=memory_context_summary,
            policy_correction_used=policy_result.policy_correction_used,
            applied_policy_ids=list(policy_result.applied_policy_ids),
            applied_policy_types=list(policy_result.applied_policy_types),
            policy_correction_summary=policy_result.policy_correction_summary,
            pre_correction_title_text=policy_result.pre_correction_title_text,
            pre_correction_subheadline=policy_result.pre_correction_subheadline,
            selected_asset_reference=image_selection.asset_reference,
            image_selection_source=image_selection.selection_source,
            image_selection_model_name=image_selection.model_name,
        )
        return spec, enriched_candidates

    def _collect_asset_candidates(
        self,
        conn: sqlite3.Connection,
        *,
        analysis: dict[str, Any],
        draft: dict[str, Any],
        representative_article_id: str | None,
    ) -> list[TitleImageCandidate]:
        insight_by_ref: dict[str, dict[str, Any]] = {}
        candidate_refs: list[str] = []

        for asset in sorted(
            analysis.get("assets") or [],
            key=lambda item: (
                0 if item.get("usage_recommendation") == "cover" else 1,
                -(float(item.get("confidence") or 0.0)),
            ),
        ):
            ref = str(asset.get("asset_reference") or "").strip()
            if ref:
                insight_by_ref[ref] = asset
                candidate_refs.append(ref)

        cover_page = self._select_cover_page(draft)
        page_ref = str((cover_page or {}).get("asset_reference") or "").strip()
        if page_ref:
            candidate_refs.append(page_ref)

        seen: set[str] = set()
        candidates: list[TitleImageCandidate] = []
        for ref in candidate_refs:
            if ref in seen:
                continue
            seen.add(ref)
            row = conn.execute(
                """
                SELECT id, source_item_id, origin_url, storage_path, sort_order
                FROM source_assets
                WHERE id = ?
                """,
                (ref,),
            ).fetchone()
            if row is not None and (row["storage_path"] or row["origin_url"]):
                candidates.append(self._build_asset_candidate(row, insight_by_ref.get(ref)))

        if representative_article_id:
            rows = conn.execute(
                """
                SELECT id, source_item_id, origin_url, storage_path, sort_order
                FROM source_assets
                WHERE source_item_id = ?
                ORDER BY sort_order ASC, id ASC
                """,
                (representative_article_id,),
            ).fetchall()
            for row in rows:
                ref = str(row["id"])
                if ref in seen:
                    continue
                seen.add(ref)
                candidates.append(self._build_asset_candidate(row, insight_by_ref.get(ref)))
        return candidates

    def _build_title_memory_context(
        self,
        *,
        topic_id: str,
        topic_name: str,
        team_name: str | None,
        draft_title: str,
        draft_subtitle: str,
        cover_headline: str,
        cover_body: str,
        overall_summary: str,
        topic_type: str | None = None,
        entity_focus: str | None = None,
        event_type: str | None = None,
        angle_type: str | None = None,
        recommended_focus: str | None = None,
        has_notable_numbers: bool | None = None,
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
            "topic_type": topic_type,
            "entity_focus": entity_focus,
            "event_type": event_type,
            "angle_type": angle_type,
            "recommended_focus": recommended_focus,
            "has_notable_numbers": has_notable_numbers,
        }
        try:
            rows = retrieve_similar_headline_edits(
                retrieval_source,
                repository=self.feedback_repository,
                top_k=3,
            )
        except Exception:
            return None, []
        if not rows:
            return None, []
        summary = format_headline_retrieval_summary(rows)
        referenced_memory_ids = [
            str(row.get("id") or "").strip()
            for row in rows
            if str(row.get("id") or "").strip()
        ]
        return summary or None, referenced_memory_ids

    @staticmethod
    def _build_asset_candidate(row: sqlite3.Row, insight: dict[str, Any] | None) -> TitleImageCandidate:
        image_source = str(row["storage_path"] or row["origin_url"])
        return TitleImageCandidate(
            asset_reference=str(row["id"]),
            image_source=image_source,
            origin_url=str(row["origin_url"]),
            storage_path=row["storage_path"],
            sort_order=int(row["sort_order"] or 0),
            usage_recommendation=str(insight.get("usage_recommendation")) if insight and insight.get("usage_recommendation") else None,
            confidence=float(insight.get("confidence")) if insight and insight.get("confidence") is not None else None,
            scene_description=str(insight.get("scene_description")) if insight and insight.get("scene_description") else None,
            humor_point=str(insight.get("humor_point")) if insight and insight.get("humor_point") else None,
            caution_note=str(insight.get("caution_note")) if insight and insight.get("caution_note") else None,
        )

    def _enrich_asset_candidates(self, candidates: list[TitleImageCandidate]) -> list[TitleImageCandidate]:
        enriched: list[TitleImageCandidate] = []
        for candidate in candidates:
            local_image_path = None
            try:
                local_image_path = self._resolve_image_path(candidate.image_source)
            except Exception:
                local_image_path = None
            enriched.append(
                TitleImageCandidate(
                    asset_reference=candidate.asset_reference,
                    image_source=candidate.image_source,
                    origin_url=candidate.origin_url,
                    storage_path=candidate.storage_path,
                    sort_order=candidate.sort_order,
                    usage_recommendation=candidate.usage_recommendation,
                    confidence=candidate.confidence,
                    scene_description=candidate.scene_description,
                    humor_point=candidate.humor_point,
                    caution_note=candidate.caution_note,
                    local_image_path=local_image_path,
                )
            )
        return enriched

    def _select_cover_page(self, draft: dict[str, Any]) -> dict[str, Any] | None:
        pages = draft.get("pages") or []
        return next((page for page in pages if page.get("page_role") == "cover"), pages[0] if pages else None)

    def _resolve_image_path(self, image_source: str) -> Path:
        candidate = Path(image_source).expanduser()
        if candidate.exists():
            return candidate
        if image_source.startswith(("http://", "https://")):
            return self._download_image(image_source)
        raise FileNotFoundError(f"Image source could not be resolved: {image_source}")

    def _download_image(self, url: str) -> Path:
        cache_dir = self.cache_dir or (ROOT_DIR / "outputs" / "phase4" / "topic_title" / "asset_cache")
        cache_dir.mkdir(parents=True, exist_ok=True)
        digest = hashlib.sha256(url.encode("utf-8")).hexdigest()[:24]
        output_path = cache_dir / f"{digest}.img"
        if output_path.exists():
            return output_path

        request = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(request, timeout=20) as response:
            output_path.write_bytes(response.read())
        return output_path

    def _default_output_path(
        self,
        run_dir: Path,
        *,
        date_text: str | None,
        title_text: str | None,
        fallback_text: str,
    ) -> Path:
        output_dir = ROOT_DIR / "outputs" / "phase4" / "topic_title" / run_dir.name
        output_dir.mkdir(parents=True, exist_ok=True)
        base_name = _build_title_output_basename(
            date_text=date_text,
            title_text=title_text,
            fallback_text=fallback_text,
        )
        return output_dir / f"{base_name}.png"

    def _render_spec_to_output(
        self,
        *,
        spec: TopicTitlePageSpec,
        image_path: Path,
        output_path: Path | None = None,
        instagram_handle: str | None = None,
        team_color_override: str | None = None,
    ) -> None:
        design = self._load_design_module()
        card_input = design.CardNewsInput(
            image_path=str(image_path),
            output_path=str(output_path or spec.output_path),
            headline_label=spec.title_text,
            subheadline=spec.subheadline,
            title=spec.title_text,
            team_name=spec.team_name,
            date_text=spec.date_text,
            instagram_handle=instagram_handle or self.instagram_handle,
            is_team_news=spec.team_name is not None,
            team_color_override=team_color_override,
        )
        design.render_card_news(card_input)

    def _write_spec_outputs(
        self,
        *,
        spec: TopicTitlePageSpec,
        image_path: Path,
        candidates: list[TitleImageCandidate],
    ) -> tuple[Path, Path]:
        original_spec_path, editable_spec_path = _build_spec_output_paths(spec.output_path)
        original_spec_path.parent.mkdir(parents=True, exist_ok=True)
        selected_candidate = next(
            (candidate for candidate in candidates if candidate.asset_reference == spec.selected_asset_reference),
            None,
        )
        base_payload = self._build_spec_payload(
            spec=spec,
            image_path=image_path,
            selected_candidate=selected_candidate,
        )
        original_payload = {
            **base_payload,
            "spec_kind": "original",
            "spec_path": str(original_spec_path),
            "editable_spec_path": str(editable_spec_path),
        }
        editable_payload = {
            **base_payload,
            "spec_kind": "editable",
            "original_output_path": str(spec.output_path),
            "original_spec_path": str(original_spec_path),
            "spec_path": str(editable_spec_path),
            "editable_spec_path": str(editable_spec_path),
            "render_iteration": 0,
            "edited_output_path": None,
        }
        original_spec_path.write_text(
            json.dumps(original_payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        editable_spec_path.write_text(
            json.dumps(editable_payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return original_spec_path, editable_spec_path

    def _build_spec_payload(
        self,
        *,
        spec: TopicTitlePageSpec,
        image_path: Path,
        selected_candidate: TitleImageCandidate | None,
    ) -> dict[str, Any]:
        return {
            **asdict(spec),
            "output_path": str(spec.output_path),
            "instagram_handle": self.instagram_handle,
            "team_color": _resolve_title_team_color(spec.team_name),
            "selected_image_source": spec.image_source,
            "selected_image_origin_url": selected_candidate.origin_url if selected_candidate else None,
            "selected_image_storage_path": selected_candidate.storage_path if selected_candidate else None,
            "selected_image_resolved_path": str(image_path),
            "selected_image_local_path": str(image_path),
            "selected_image_sort_order": selected_candidate.sort_order if selected_candidate else None,
            "selected_image_usage_recommendation": selected_candidate.usage_recommendation if selected_candidate else None,
            "selected_image_confidence": selected_candidate.confidence if selected_candidate else None,
            "selected_image_scene_description": selected_candidate.scene_description if selected_candidate else None,
            "selected_image_humor_point": selected_candidate.humor_point if selected_candidate else None,
            "selected_image_caution_note": selected_candidate.caution_note if selected_candidate else None,
        }

    def _export_candidate_images(
        self,
        *,
        spec: TopicTitlePageSpec,
        candidates: list[TitleImageCandidate],
    ) -> tuple[Path, Path]:
        export_dir = spec.output_path.parent / f"{spec.output_path.stem}_image_candidates"
        export_dir.mkdir(parents=True, exist_ok=True)
        manifest_path = export_dir / "candidate_manifest.json"
        manifest: list[dict[str, Any]] = []
        for index, candidate in enumerate(candidates, start=1):
            local_image_path = candidate.local_image_path
            if local_image_path is None:
                try:
                    local_image_path = self._resolve_image_path(candidate.image_source)
                except Exception:
                    local_image_path = None
            exported_path = None
            export_error = None
            if local_image_path is not None:
                exported_path = export_dir / _build_candidate_export_filename(
                    index=index,
                    asset_reference=candidate.asset_reference,
                    selected=(candidate.asset_reference == spec.selected_asset_reference),
                )
                try:
                    Image.open(local_image_path).convert("RGB").save(exported_path, format="PNG")
                except Exception as exc:
                    export_error = str(exc)
                    exported_path = None
            manifest.append(
                {
                    "index": index,
                    "asset_reference": candidate.asset_reference,
                    "selected": candidate.asset_reference == spec.selected_asset_reference,
                    "image_source": candidate.image_source,
                    "origin_url": candidate.origin_url,
                    "storage_path": candidate.storage_path,
                    "usage_recommendation": candidate.usage_recommendation,
                    "confidence": candidate.confidence,
                    "scene_description": candidate.scene_description,
                    "humor_point": candidate.humor_point,
                    "caution_note": candidate.caution_note,
                    "local_image_path": str(local_image_path) if local_image_path else None,
                    "exported_path": str(exported_path) if exported_path else None,
                    "export_error": export_error,
                }
            )
        manifest_path.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return export_dir, manifest_path

    def _write_simple_edit_spec(
        self,
        *,
        spec: TopicTitlePageSpec,
        editable_spec_path: Path,
        candidate_manifest_path: Path,
    ) -> Path:
        simple_edit_path = spec.output_path.with_suffix(".simple_edit.json")
        manifest = json.loads(candidate_manifest_path.read_text(encoding="utf-8"))
        selected_candidate = next((item for item in manifest if item.get("selected")), None)
        candidate_options = [
            {
                "index": item.get("index"),
                "candidate_file": Path(str(item.get("exported_path") or "")).name if item.get("exported_path") else None,
                "asset_reference": item.get("asset_reference"),
                "selected": bool(item.get("selected")),
                "scene_description": item.get("scene_description"),
                "usage_recommendation": item.get("usage_recommendation"),
                "origin_url": item.get("origin_url"),
            }
            for item in manifest
        ]
        payload = {
            "spec_kind": "simple_editable",
            "spec_path": str(simple_edit_path),
            "full_editable_spec_path": str(editable_spec_path),
            "candidate_manifest_path": str(candidate_manifest_path),
            "original_output_path": str(spec.output_path),
            "output_path": str(spec.output_path),
            "topic_id": spec.topic_id,
            "topic_name": spec.topic_name,
            "date_text": spec.date_text,
            "title_text": spec.title_text,
            "subheadline": spec.subheadline,
            "team_name": spec.team_name,
            "team_color": _resolve_title_team_color(spec.team_name),
            "memory_context_used": spec.memory_context_used,
            "num_similar_cases": spec.num_similar_cases,
            "referenced_memory_ids": list(spec.referenced_memory_ids),
            "memory_context_summary": spec.memory_context_summary,
            "policy_correction_used": spec.policy_correction_used,
            "applied_policy_ids": list(spec.applied_policy_ids),
            "applied_policy_types": list(spec.applied_policy_types),
            "policy_correction_summary": spec.policy_correction_summary,
            "pre_correction_title_text": spec.pre_correction_title_text,
            "pre_correction_subheadline": spec.pre_correction_subheadline,
            "selected_image_candidate_file": (
                Path(str(selected_candidate.get("exported_path"))).name
                if selected_candidate and selected_candidate.get("exported_path")
                else None
            ),
            "selected_image_path": None,
            "candidate_options": candidate_options,
            "render_iteration": 0,
        }
        simple_edit_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return simple_edit_path

    def _augment_editable_specs(
        self,
        *,
        original_spec_path: Path,
        editable_spec_path: Path,
        simple_edit_spec_path: Path,
        candidate_manifest_path: Path,
    ) -> None:
        for path in (original_spec_path, editable_spec_path):
            payload = json.loads(path.read_text(encoding="utf-8"))
            payload["candidate_manifest_path"] = str(candidate_manifest_path)
            payload["simple_edit_spec_path"] = str(simple_edit_spec_path)
            path.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )

    def _load_design_module(self) -> ModuleType:
        if self._design_module is not None:
            return self._design_module

        design_path = ROOT_DIR / "design.py"
        spec = importlib.util.spec_from_file_location("kbo_card_news_root_design", design_path)
        if spec is None or spec.loader is None:
            raise ImportError(f"Unable to load design module from {design_path}")
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        self._design_module = module
        return module

    def _render_basic_card(self, *, spec: TopicTitlePageSpec, image_path: Path) -> None:
        canvas = Image.new("RGB", (1080, 1080), "#101820")
        draw = ImageDraw.Draw(canvas)

        try:
            source = Image.open(image_path).convert("RGB")
            fitted = _cover_resize(source, (1080, 1080))
            canvas.paste(fitted, (0, 0))
        except Exception:
            pass

        draw.rectangle((0, 0, 1080, 1080), fill=(0, 0, 0, 110))
        draw.rectangle((48, 48, 1032, 1032), outline="white", width=3)
        draw.rectangle((72, 72, 1008, 240), fill=(0, 0, 0, 170))
        draw.rectangle((72, 760, 1008, 980), fill=(0, 0, 0, 180))

        team_label = spec.team_name or "KBO"
        draw.text((92, 96), team_label, fill="white")
        draw.text((92, 148), spec.date_text, fill="#D9D9D9")
        draw.text((92, 800), spec.title_text, fill="white")
        draw.multiline_text((92, 860), spec.subheadline, fill="#F5F5F5", spacing=10)
        draw.text((92, 1010), self.instagram_handle, fill="#D9D9D9")

        spec.output_path.parent.mkdir(parents=True, exist_ok=True)
        canvas.save(spec.output_path, format="PNG")


def _cover_resize(image: Image.Image, size: tuple[int, int]) -> Image.Image:
    target_width, target_height = size
    source_width, source_height = image.size
    scale = max(target_width / max(source_width, 1), target_height / max(source_height, 1))
    resized = image.resize(
        (max(1, int(source_width * scale)), max(1, int(source_height * scale)))
    )
    left = max(0, (resized.width - target_width) // 2)
    top = max(0, (resized.height - target_height) // 2)
    return resized.crop((left, top, left + target_width, top + target_height))


def _detect_team_name(*texts: str) -> str | None:
    for text in texts:
        if not text:
            continue
        positions = [
            (text.find(keyword), keyword)
            for keyword in TEAM_KEYWORDS
            if keyword in text
        ]
        if positions:
            positions.sort(key=lambda item: item[0])
            return positions[0][1]
    return None


@dataclass
class TitleCopyInput:
    topic_id: str
    topic_name: str
    draft_title: str
    draft_subtitle: str
    cover_headline: str
    cover_body: str
    overall_summary: str
    team_name: str | None
    memory_context_summary: str | None = None
    referenced_memory_ids: list[str] = field(default_factory=list)


@dataclass
class TitleCopyOutput:
    title_text: str
    subheadline: str
    copy_source: str
    model_name: str


class TitleCopyEngine(Protocol):
    def rewrite(self, copy_input: TitleCopyInput) -> TitleCopyOutput:
        ...


class TitleImageSelectionEngine(Protocol):
    def select_image(self, selection_input: TitleImageSelectionInput) -> TitleImageSelectionOutput:
        ...


class RuleBasedTitleCopyEngine:
    def __init__(self, *, model_name: str = "rule-based-title-copy-v1") -> None:
        self.model_name = model_name

    def rewrite(self, copy_input: TitleCopyInput) -> TitleCopyOutput:
        title_text = _build_rule_based_title_text(
            topic_name=copy_input.topic_name,
            draft_title=copy_input.draft_title,
            summary=copy_input.overall_summary,
        )
        subheadline = _sanitize_subheadline(
            _build_subheadline_source(copy_input, team_name=copy_input.team_name),
            team_name=copy_input.team_name,
            fallback_text=_build_subheadline_source(copy_input, team_name=copy_input.team_name),
        )
        return TitleCopyOutput(
            title_text=title_text,
            subheadline=subheadline,
            copy_source="rule_based",
            model_name=self.model_name,
        )


class GeminiTitleCopyEngine:
    def __init__(
        self,
        *,
        api_key: str | None = None,
        model_name: str | None = None,
        transport: HttpTransport | None = None,
        endpoint_base: str = "https://generativelanguage.googleapis.com/v1beta/models",
    ) -> None:
        load_default_env(ROOT_DIR)
        self.api_key = api_key or os.getenv("GEMINI_API_KEY")
        self.model_name = model_name or os.getenv("GEMINI_TITLE_COPY_MODEL") or "gemini-2.5-flash"
        self.transport = transport or UrllibHttpTransport(timeout_seconds=45)
        self.endpoint_base = endpoint_base.rstrip("/")
        self.fallback_engine = RuleBasedTitleCopyEngine()

    def rewrite(self, copy_input: TitleCopyInput) -> TitleCopyOutput:
        if not self.api_key:
            return self.fallback_engine.rewrite(copy_input)

        fallback_text = _build_subheadline_source(copy_input, team_name=copy_input.team_name)
        last_error: Exception | None = None

        for attempt in range(3):
            request_payload = self._build_request_payload(copy_input, revision_attempt=attempt)
            url = f"{self.endpoint_base}/{self.model_name}:generateContent"
            headers = {
                "Content-Type": "application/json",
                "x-goog-api-key": self.api_key,
            }

            try:
                response_payload = self.transport.post_json(url, request_payload, headers)
                parsed = self._parse_response_payload(response_payload)
                title_text = _sanitize_title_text(parsed.get("headline") or "", copy_input=copy_input)
                subheadline = _sanitize_subheadline(
                    parsed.get("subheadline") or fallback_text,
                    team_name=copy_input.team_name,
                    fallback_text=fallback_text,
                )
                if _is_valid_subheadline_output(subheadline):
                    return TitleCopyOutput(
                        title_text=title_text,
                        subheadline=subheadline,
                        copy_source="gemini_rewrite",
                        model_name=self.model_name,
                    )
                last_error = RuntimeError(
                    f"Gemini title copy did not satisfy subheadline rules: len={len(subheadline.replace(chr(10), ''))}, value={subheadline!r}"
                )
            except (RuntimeError, urllib.error.HTTPError, urllib.error.URLError, TimeoutError, socket.timeout) as exc:
                last_error = exc

        return self.fallback_engine.rewrite(copy_input)

    def _build_request_payload(self, copy_input: TitleCopyInput, *, revision_attempt: int = 0) -> dict[str, Any]:
        prompt_payload = {
            "topic_id": copy_input.topic_id,
            "topic_name": copy_input.topic_name,
            "team_name": copy_input.team_name,
            "draft_title": copy_input.draft_title,
            "draft_subtitle": copy_input.draft_subtitle,
            "cover_headline": copy_input.cover_headline,
            "cover_body": copy_input.cover_body,
            "overall_summary": copy_input.overall_summary,
            "memory_context_summary": copy_input.memory_context_summary,
            "referenced_memory_ids": list(copy_input.referenced_memory_ids or []),
        }
        correction_block = ""
        if revision_attempt > 0:
            correction_block = (
                "\n[재시도 추가 규칙]\n"
                "- 이전 응답이 headline 또는 subheadline 규칙을 지키지 못했다.\n"
                "- headline은 공백 제외 4~7자이며, '팀명 + 사건', '선수명 + 사건', 또는 사건만 강력하게 쓰는 구조로 다시 작성하라.\n"
                "- subheadline은 먼저 줄바꿈 없는 70~85자 완성 문장으로 다시 쓴 뒤, 첫 줄 40~48자 지점에서 자연스럽게 2줄로 나눠라.\n"
                "- 첫 줄은 반드시 공백 포함 40~48자로 작성하고, 둘째 줄은 나머지 문장으로 자연스럽게 조절하라.\n"
                "- 글자 수를 맞추기 위해 이미 쓴 문장을 기계적으로 자르지 말고, 처음부터 70~85자 안에 들어오는 완성 문장으로 다시 써라.\n"
                "- 하나라도 어기면 다시 생성될 것이므로 이번 응답은 조건을 정확히 만족해야 한다.\n"
                "[서브헤드라인 예시]"
                "- LG 트윈스가 8연승으로 시즌 초반 흐름을 확실히 잡아내며 선두권 경쟁에 불을 붙였고\n치열한 순위 싸움 속에서도 상승세를 계속 이어갔다\n"
                "- 두산 베어스가 투타 균형을 앞세워 경기 주도권을 끝까지 지켜내며 승부처를 넘겼고\n상위권 경쟁에서 필요한 승리를 추가하며 흐름을 이어갔다\n"
                "- KIA 타이거즈가 타선 집중력으로 초반부터 흐름을 빠르게 가져오며 리드를 만들었고\n후반 위기까지 넘기고 값진 승리를 지켜내며 반등했다\n"
                "- 김도영이 중요한 순간마다 장타로 공격 흐름을 확실히 살려내며 해결사 역할을 해냈고\n팀 승리에 직접 연결되는 존재감을 다시 보여줬다\n"
                
            )
        memory_instruction = ""
        if copy_input.memory_context_summary:
            memory_instruction = (
                "\n[유사 과거 수정 사례 참고]\n"
                "- 아래 memory_context_summary 는 비슷한 토픽에서 사람이 실제로 고친 headline/subheadline 사례 요약이다.\n"
                "- 하지만 현재 입력 JSON 의 사실이 항상 최우선이다.\n"
                "- 과거 표현을 그대로 복사하지 말고, 현재 토픽에 맞는 편집 방향만 참고하라.\n"
                "- memory_context_summary 와 현재 입력이 충돌하면 반드시 현재 입력을 따른다.\n\n"
            )
        return {
            "system_instruction": {
                "parts": [
                    {
                        "text": (
                            "너는 KBO 카드뉴스 표지 문구 편집기다.\n"
                            "반드시 한국어로만 작성하고, JSON만 반환한다.\n"
                            "새 사실을 추가하면 안 된다.\n"
                            "입력 JSON에 있는 사실만 재배열해서 headline, subheadline만 만든다."
                        )
                    }
                ]
            },
            "contents": [
                {
                    "role": "user",
                    "parts": [
                        {
                            "text": (
                                "[headline 규칙]\n"
                                "- 공백 제외 4~7자\n"
                                "- 반드시 아래 구조 중 하나로 작성:\n"
                                "  a. 팀명 + 사건\n"
                                "  b. 선수명 + 사건\n"
                                "  c. 사건만 강력하게\n"
                                "- headline은 문장이 아니라 '라벨'처럼 작성하라\n"
                                "- 사건은 결과/성과 중심 단어만 사용 (연승, 선두, 결승타, 호투 등)\n"
                                "- 불필요한 조사/수식어 금지\n"
                                "- 과장 금지, 입력 JSON 사실만 사용\n"
                                "- 특정 선수 활약이 명확하면 '선수명 + 사건' 우선\n"
                                "- 팀/선수보다 사건 자체가 강하면 사건만 사용\n\n"
                                f"{memory_instruction}"
                                "좋은 예시:\n"
                                "- LG 8연승\n"
                                "- 두산 선두탈환\n"
                                "- 오스틴 결승타\n"
                                "- 김도영 맹활약\n"
                                "- 계약 연장 가능\n"
                                "- 돌아온 문보경\n"
                                "- 뒷문 비상\n"
                                "- 볼넷 잔치\n\n"
                                "[subheadline 규칙]\n"
                                "- 작성 순서: 먼저 줄바꿈 없는 70~85자 완전한 문장을 만든 뒤, 그 문장을 첫 줄 40~48자 + 나머지로 나눈다\n"
                                "- 반드시 정확히 2줄\n"
                                "- 줄바꿈은 정확히 1개만 포함\n"
                                "- 줄바꿈 제외 전체 글자 수는 반드시 70~85자\n"
                                "- 첫 줄은 반드시 공백 포함 40~48자로 작성\n"
                                "- 둘째 줄 길이는 자율이지만 자연스럽고 짧게 정리\n"
                                "- 글자 수를 맞추려고 문장을 중간에서 끊거나 잘라내지 말 것\n"
                                "- 처음부터 70~85자 안에 들어오는 완성 문장으로 작성\n"
                                "- 전체 subheadline은 언제나 완전한 문장일 것\n"
                                "- 마지막 어절은 반드시 '했다', '이다', '이어갔다', '보여줬다'처럼 문장이 닫히는 형태로 끝낼 것\n"
                                "- 마지막이 '고', '며', '면서', '지만', '하고', '통해', '위해'처럼 다음 내용이 필요한 연결 표현이면 실패\n"
                                "- 줄바꿈은 단어와 의미 단위가 자연스러운 지점에만 넣을 것\n"
                                "- 한 줄만 길거나 짧으면 안 됨\n"
                                "- 입력 JSON의 사실만 사용\n"
                                "- 팀명, 연승, 순위 등 핵심 정보 왜곡 금지\n"
                                "- 선정적 표현, 추정, 비난 금지\n\n"
                                "작성 절차:\n"
                                "1. 입력 JSON에서 핵심 사실만 추린다\n"
                                "2. headline을 먼저 만든다\n"
                                "3. subheadline용 줄바꿈 없는 70~85자 완전한 문장을 먼저 작성한다\n"
                                "4. 그 문장을 첫 줄 40~48자 + 나머지로 자연스럽게 나눈다\n"
                                "5. 반드시 아래를 스스로 검사한다:\n"
                                "   - 줄 수가 정확히 2줄인지\n"
                                "   - 줄바꿈이 1개인지\n"
                                "   - 줄바꿈 제외 전체 글자 수가 70~85자인지\n"
                                "   - 첫 줄 길이가 공백 포함 40~48자인지\n"
                                "   - 문장이 중간에서 끊기지 않았는지\n"
                                "   - 글자 수를 맞추기 위해 억지로 잘라낸 표현이 없는지\n"
                                "6. 하나라도 어기면 조건을 만족할 때까지 수정한다\n\n"
                                "좋은 예시:\n"
                                "- LG 트윈스가 8연승으로 시즌 초반 흐름을 확실히 잡아내며 선두권 경쟁에 불을 붙였고\n치열한 순위 싸움 속에서도 상승세를 계속 이어갔다\n"
                                "- 두산 베어스가 투타 균형을 앞세워 경기 주도권을 끝까지 지켜내며 승부처를 넘겼고\n상위권 경쟁에서 필요한 승리를 추가하며 흐름을 이어갔다\n"
                                "- KIA 타이거즈가 타선 집중력으로 초반부터 흐름을 빠르게 가져오며 리드를 만들었고\n후반 위기까지 넘기고 값진 승리를 지켜내며 반등했다\n"
                                "- 김도영이 중요한 순간마다 장타로 공격 흐름을 확실히 살려내며 해결사 역할을 해냈고\n팀 승리에 직접 연결되는 존재감을 다시 보여줬다\n"
                                f"{correction_block}\n"
                                "출력 형식:\n"
                                "{\n"
                                '  "headline": "...",\n'
                                '  "subheadline": "...\\n..."\n'
                                "}\n\n"
                                "입력 JSON:\n"
                                f"{json.dumps(prompt_payload, ensure_ascii=False, indent=2)}"
                            )
                        }
                    ],
                }
            ],
            "generationConfig": {
                "temperature": 0.4,
                "responseMimeType": "application/json",
                "responseSchema": {
                    "type": "OBJECT",
                    "properties": {
                        "headline": {"type": "STRING"},
                        "subheadline": {"type": "STRING"},
                    },
                    "required": ["headline", "subheadline"],
                },
            },
        }

    def _parse_response_payload(self, response_payload: dict[str, Any]) -> dict[str, str]:
        candidates = response_payload.get("candidates") or []
        if not candidates:
            raise RuntimeError("Gemini title copy response missing candidates")
        content = candidates[0].get("content") or {}
        parts = content.get("parts") or []
        text_parts = [str(part.get("text") or "").strip() for part in parts if part.get("text")]
        if not text_parts:
            raise RuntimeError("Gemini title copy response missing text")
        return json.loads(text_parts[0])


OPENAI_HEADLINE_JSON_SCHEMA = {
    "type": "object",
    "properties": {
        "headline": {"type": "string"},
    },
    "required": ["headline"],
    "additionalProperties": False,
}


OPENAI_SUBHEADLINE_JSON_SCHEMA = {
    "type": "object",
    "properties": {
        "line1": {"type": "string"},
        "line2": {"type": "string"},
        "line1_char_count": {"type": "integer"},
        "total_char_count": {"type": "integer"},
    },
    "required": ["line1", "line2", "line1_char_count", "total_char_count"],
    "additionalProperties": False,
}


class OpenAISplitTitleCopyEngine:
    def __init__(
        self,
        *,
        api_key: str | None = None,
        headline_model_name: str | None = None,
        subheadline_model_name: str | None = None,
        transport: HttpTransport | None = None,
        endpoint: str = "https://api.openai.com/v1/responses",
        fallback_engine: TitleCopyEngine | None = None,
        subheadline_max_attempts: int | None = None,
    ) -> None:
        load_default_env(ROOT_DIR)
        self.api_key = api_key or os.getenv("OPENAI_API_KEY")
        self.headline_model_name = headline_model_name or os.getenv("OPENAI_HEADLINE_MODEL") or "gpt-5.4"
        self.subheadline_model_name = subheadline_model_name or os.getenv("OPENAI_SUBHEADLINE_MODEL") or "gpt-5.4"
        self.transport = transport or UrllibHttpTransport(timeout_seconds=90)
        self.endpoint = endpoint
        self.fallback_engine = fallback_engine or RuleBasedTitleCopyEngine()
        self.subheadline_max_attempts = (
            subheadline_max_attempts
            if subheadline_max_attempts is not None
            else int(os.getenv("OPENAI_SUBHEADLINE_MAX_ATTEMPTS") or "5")
        )

    def rewrite(self, copy_input: TitleCopyInput) -> TitleCopyOutput:
        fallback_output: TitleCopyOutput | None = None
        headline = ""
        headline_model_name = ""
        if not self.api_key:
            print("[TITLE COPY] OPENAI_API_KEY missing; title copy will use rule-based fallback")
        else:
            headline = self._rewrite_headline_with_fallback(copy_input)
            headline_model_name = self.headline_model_name if headline else ""

        fallback_text = _build_subheadline_source(copy_input, team_name=copy_input.team_name)
        subheadline = ""
        subheadline_source = ""
        subheadline_model_name = ""
        if self.api_key:
            try:
                subheadline = self._rewrite_subheadline_until_valid(
                    copy_input,
                    headline=headline,
                    fallback_text=fallback_text,
                )
            except RuntimeError as exc:
                print(f"[TITLE COPY] OpenAI subheadline exhausted; subheadline will use rule-based fallback: {exc}")
                subheadline = ""
            if subheadline:
                subheadline_source = "openai_subheadline_rewrite"
                subheadline_model_name = self.subheadline_model_name

        fallback_output: TitleCopyOutput | None = None
        if not headline:
            try:
                print("[TITLE COPY] rule-based title copy fallback for missing headline")
                fallback_output = self.fallback_engine.rewrite(copy_input)
            except (RuntimeError, urllib.error.HTTPError, urllib.error.URLError, TimeoutError, socket.timeout) as exc:
                print(f"[TITLE COPY] rule-based title copy fallback failed unexpectedly: {exc}")
                fallback_output = RuleBasedTitleCopyEngine().rewrite(copy_input)

        if not headline:
            if fallback_output is None:
                fallback_output = RuleBasedTitleCopyEngine().rewrite(copy_input)
            headline = fallback_output.title_text
            headline_model_name = fallback_output.model_name

        if not subheadline:
            if fallback_output is None:
                try:
                    print("[TITLE COPY] rule-based title copy fallback for missing subheadline")
                    fallback_output = self.fallback_engine.rewrite(copy_input)
                except (RuntimeError, urllib.error.HTTPError, urllib.error.URLError, TimeoutError, socket.timeout) as exc:
                    print(f"[TITLE COPY] rule-based title copy fallback failed unexpectedly: {exc}")
                    fallback_output = RuleBasedTitleCopyEngine().rewrite(copy_input)
            subheadline = fallback_output.subheadline
            subheadline_source = fallback_output.copy_source
            subheadline_model_name = fallback_output.model_name

        return TitleCopyOutput(
            title_text=headline,
            subheadline=subheadline,
            copy_source=_combine_copy_sources(
                headline_openai=headline_model_name == self.headline_model_name,
                subheadline_source=subheadline_source,
            ),
            model_name=(
                f"{headline_model_name}+{subheadline_model_name}"
                if headline_model_name and headline_model_name != subheadline_model_name
                else subheadline_model_name
            ),
        )

    def _rewrite_headline_with_fallback(self, copy_input: TitleCopyInput) -> str:
        last_error: Exception | None = None
        for attempt in range(3):
            try:
                print(f"[TITLE COPY] OpenAI headline try attempt={attempt + 1} model={self.headline_model_name}")
                headline = self._rewrite_headline(copy_input, revision_attempt=attempt)
                compact = headline.replace(" ", "")
                if TITLE_MIN_NON_SPACE_CHARS <= len(compact) <= TITLE_MAX_NON_SPACE_CHARS:
                    return headline
                last_error = RuntimeError(f"OpenAI headline did not satisfy length rules: value={headline!r}")
                print(f"[TITLE COPY] OpenAI headline invalid; retrying: {last_error}")
            except (RuntimeError, urllib.error.HTTPError, urllib.error.URLError, TimeoutError, socket.timeout) as exc:
                last_error = exc
                print(f"[TITLE COPY] OpenAI headline failed; retrying or falling back: {exc}")
        print(f"[TITLE COPY] OpenAI headline exhausted; headline will use rule-based fallback: {last_error}")
        return ""

    def _rewrite_headline(self, copy_input: TitleCopyInput, *, revision_attempt: int = 0) -> str:
        response_text = call_openai(
            model_name=self.headline_model_name,
            prompt=self._build_headline_prompt(copy_input, revision_attempt=revision_attempt),
            schema_name="title_headline",
            json_schema=OPENAI_HEADLINE_JSON_SCHEMA,
            transport=self.transport,
            api_key=self.api_key,
            endpoint=self.endpoint,
        )
        parsed = json.loads(response_text)
        return _sanitize_title_text(str(parsed.get("headline") or ""), copy_input=copy_input)

    def _rewrite_subheadline_until_valid(
        self,
        copy_input: TitleCopyInput,
        *,
        headline: str,
        fallback_text: str,
    ) -> str:
        previous_failure = ""
        attempt = 0
        while True:
            try:
                print(f"[TITLE COPY] OpenAI subheadline try attempt={attempt + 1} model={self.subheadline_model_name}")
                subheadline = self._rewrite_subheadline(
                    copy_input,
                    headline=headline,
                    fallback_text=fallback_text,
                    revision_attempt=attempt,
                    previous_failure=previous_failure,
                )
                validation_error = _validate_subheadline_output(
                    subheadline,
                    headline=headline,
                    copy_input=copy_input,
                )
                if validation_error is None:
                    return subheadline
                normalized = _normalize_subheadline_whitespace(subheadline)
                line_lengths = [len(line) for line in normalized.split("\n")]
                total_without_newline = len(normalized.replace("\n", ""))
                previous_failure = _build_subheadline_retry_feedback(
                    normalized=normalized,
                    validation_error=validation_error,
                    line_lengths=line_lengths,
                    total_without_newline=total_without_newline,
                )
                retry_error = RuntimeError(
                    f"OpenAI subheadline did not satisfy rules: len_without_newline={total_without_newline}, lines={line_lengths}, value={subheadline!r}"
                )
                print(f"[TITLE COPY] OpenAI subheadline invalid; retrying with stricter prompt: {retry_error}")
            except Exception as exc:
                print(f"[TITLE COPY] OpenAI subheadline failed; retrying with stricter prompt: {exc}")
                previous_failure = (
                    f"이전 호출이 예외로 실패했다. 실패 사유: {exc}. "
                    "다음 응답은 JSON schema를 지키고, line1과 line2를 반드시 채워라. "
                    "반드시 줄바꿈 제외 전체 70~85자, 첫 줄 40~48자로 작성하라."
                )
            attempt += 1
            if self.subheadline_max_attempts > 0 and attempt >= self.subheadline_max_attempts:
                raise RuntimeError("OpenAI subheadline exhausted without valid output")

    def _rewrite_subheadline(
        self,
        copy_input: TitleCopyInput,
        *,
        headline: str,
        fallback_text: str,
        revision_attempt: int = 0,
        previous_failure: str = "",
    ) -> str:
        response_text = call_openai(
            model_name=self.subheadline_model_name,
            prompt=self._build_subheadline_prompt(
                copy_input,
                headline=headline,
                revision_attempt=revision_attempt,
                previous_failure=previous_failure,
            ),
            schema_name="title_subheadline",
            json_schema=OPENAI_SUBHEADLINE_JSON_SCHEMA,
            transport=self.transport,
            api_key=self.api_key,
            endpoint=self.endpoint,
        )
        parsed = json.loads(response_text)
        raw_line1 = str(parsed.get("line1") or "").strip()
        raw_line2 = str(parsed.get("line2") or "").strip()
        if raw_line1 or raw_line2:
            raw_subheadline = f"{raw_line1}\n{raw_line2}".strip()
        else:
            raw_subheadline = str(parsed.get("subheadline") or "")
        return _sanitize_subheadline(
            raw_subheadline,
            team_name=copy_input.team_name,
            fallback_text="",
        )

    def _build_headline_prompt(self, copy_input: TitleCopyInput, *, revision_attempt: int = 0) -> str:
        correction = ""
        if revision_attempt > 0:
            correction = (
                "\n[재시도]\n"
                "- 이전 headline이 약하거나 규칙을 못 지켰다.\n"
                "- 공백 제외 4~7자, 팀명/선수명 + 사건 구조 또는 사건만 강력하게 쓰는 구조를 반드시 지켜라.\n"
                "- 더 강한 스포츠 표지 라벨처럼 다시 써라.\n"
            )
        return (
            "너는 KBO 카드뉴스 표지 headline 편집기다. JSON만 반환한다.\n"
            "입력에 없는 사실을 추가하지 않는다.\n\n"
            "[headline 목표]\n"
            "- 짧고 강한 표지 라벨\n"
            "- 공백 제외 4~7자\n"
            "- 팀명 + 사건 또는 선수명 + 사건 구조 또는 사건만 강력하게\n"
            "- 사건은 결과/성과 중심 단어 사용: 연승, 결승포, 역전승, 호투, 선두 등\n"
            "- 밋밋한 요약보다 클릭하고 싶은 스포츠 표지 문구 우선\n"
            "- 과장, 추정, 조롱, 비난 금지\n\n"
            "좋은 예시: LG 8연승, 두산 선두탈환, 오스틴 결승포, 김도영 맹활약, 계약 연장 가능, 돌아온 문보경, 뒷문 비상, 볼넷 잔치\n"
            f"{correction}\n"
            "출력 형식: {\"headline\":\"...\"}\n\n"
            "입력 JSON:\n"
            f"{json.dumps(_title_copy_prompt_payload(copy_input), ensure_ascii=False, indent=2)}"
        )

    def _build_subheadline_prompt(
        self,
        copy_input: TitleCopyInput,
        *,
        headline: str,
        revision_attempt: int = 0,
        previous_failure: str = "",
    ) -> str:
        prompt_payload = {
            **_title_copy_prompt_payload(copy_input),
            "selected_headline": headline,
            "headline_anchor_terms": _extract_headline_anchor_terms(headline, copy_input=copy_input),
        }
        correction = ""
        if revision_attempt > 0:
            correction = (
                "\n[재시도]\n"
                "- 이전 subheadline이 길이/줄 수/자연스러움 조건을 못 지켰다.\n"
                "- 먼저 줄바꿈 없는 70~85자 완성 문장을 다시 작성한 뒤, 첫 줄 40~48자 지점에서 자연스럽게 2줄로 나누라.\n"
                f"- {previous_failure}\n"
            )
        if revision_attempt > 1:
            correction += (
                "\n[최종 재작성]\n"
                "- 이전 출력과 draft_subtitle은 버린다.\n"
                "- topic_name, cover_body, overall_summary 안에서 현재 주제의 핵심 사실을 직접 찾아라.\n"
                "- 찾은 사실을 바탕으로 새 subheadline을 처음부터 다시 쓴다.\n"
                "- 짧은 제목형 문구를 늘이지 말고, 경기 결과/결정 장면/후속 의미 중 두 가지 이상을 엮어 완성 문장으로 쓴다.\n"
                "- 첫 줄 끝이 팀명, 선수명, 숫자, 조사, 쉼표로 끝나 다음 줄과 붙어야 하는 구조면 실패다.\n"
                "- 문체는 반드시 이다체/했다체로 쓴다. 입니다체, 했습니다체, 합니다체는 금지한다.\n"
                "- selected_headline의 핵심 단어(headline_anchor_terms) 중 최소 1개 이상을 자연스럽게 반영한다.\n"
                "- 반드시 line1은 40~48자, line1+line2는 줄바꿈 제외 70~85자다.\n"
            )
        memory_instruction = ""
        if copy_input.memory_context_summary:
            memory_instruction = (
                "\n[유사 과거 수정 사례 참고]\n"
                "- memory_context_summary는 사람이 실제로 고친 사례의 방향성이다.\n"
                "- 현재 입력 사실을 최우선으로 하고, 과거 문장을 그대로 복사하지 않는다.\n"
            )
        return (
            "너는 KBO 카드뉴스 표지 subheadline 편집기다. JSON만 반환한다.\n"
            "선택된 headline을 받쳐주는 두 줄 문장을 만든다. 입력에 없는 사실을 추가하지 않는다.\n\n"
            f"{memory_instruction}"
            "[subheadline 규칙]\n"
            "- 작성 순서: 먼저 줄바꿈 없는 70~85자 완전한 문장을 만든 뒤, 그 문장을 첫 줄 40~48자 + 나머지로 나눈다\n"
            "- 반드시 정확히 2줄\n"
            "- 줄바꿈은 정확히 1개\n"
            "- 줄바꿈 제외 전체 글자 수 70~85자\n"
            "- 70자 미만 출력은 실패다. 짧게 요약하지 말고 두 문장 분량으로 충분히 작성하라\n"
            "- 85자를 넘기면 실패다. 문장을 늘어뜨리지 말고 핵심 사실만 남겨 압축하라\n"
            "- draft_subtitle이 70자 미만이면 그대로 복사하지 말고, overall_summary와 cover_body의 사실을 더해 70~85자로 확장하라\n"
            "- 짧은 라벨, 구호, 문장 조각, 제목형 요약은 subheadline으로 금지한다\n"
            "- 첫 줄은 공백 포함 40~48자\n"
            "- 글자 수는 Python len 기준으로 센다. 공백은 1자로 세고 줄바꿈은 total_char_count에서 제외한다\n"
            "- 출력 전 line1_char_count와 total_char_count를 직접 세고, 조건 불만족 시 다시 써라\n"
            "- 전체 subheadline은 언제나 완전한 문장이어야 한다\n"
            "- line1과 line2는 합쳐 읽었을 때 한 문장 또는 자연스러운 두 문장으로 완결되어야 한다\n"
            "- line1은 '고', '며', '면서', '지만', '했다'처럼 자연스럽게 닫히는 지점에서 끝내라\n"
            "- line2와 전체 subheadline의 마지막 어절은 반드시 완결 어미로 끝내라\n"
            "- 마지막이 '고', '며', '면서', '지만', '하고', '통해', '위해'처럼 다음 내용이 필요한 연결 표현이면 실패다\n"
            "- line1 끝에 팀명/선수명/숫자/조사/쉼표만 남겨 line2 첫 단어와 이어 읽히게 만들지 말 것\n"
            "- 나쁜 예: 경기 초반 0-3으로 끌려가던 롯데는 SSG\\n선발 투수의 교체 이후 흐름을 바꿨다\n"
            "- 부상이나 교체 상황에 '틈타'를 쓰지 말고 '이후', '상황에서', '흐름 속에서'처럼 중립적으로 표현\n"
            "- '최하위 탈출의 시동' 같은 순위 변화 표현은 입력 JSON에 직접 근거가 있을 때만 사용\n"
            "- 문체는 반드시 이다체/했다체로 쓴다\n"
            "- 입니다, 했습니다, 합니다, 습니다, 예정입니다, 아닙니다, 됩니다 같은 입니다체 표현은 절대 금지한다\n"
            "- selected_headline과 같은 각도를 유지하되 같은 말을 반복하지 말 것\n"
            "- headline_anchor_terms 중 최소 1개 이상을 subheadline에 직접 넣거나 명확한 동의어로 연결하라\n"
            "- selected_headline이 선수명+사건이면 선수명 또는 사건을 반드시 반영하라\n"
            "- selected_headline이 팀명+사건이면 팀명 또는 사건을 반드시 반영하라\n"
            "- 팀명, 선수명, 경기 결과, 기록을 왜곡하지 말 것\n"
            "- 선정적 표현, 추정, 비난 금지\n\n"
            "좋은 예시:\n"
            "- LG 트윈스가 8연승으로 시즌 초반 흐름을 확실히 잡아내며 선두권 경쟁에 불을 붙였고\n치열한 순위 싸움 속에서도 상승세를 계속 이어갔다\n"
            "- 오스틴이 결정적인 순간 장타로 경기 흐름을 단번에 바꾸며 LG 공격 흐름을 완전히 살렸고\n마운드의 안정감까지 더해 승리를 지켜내며 LG의 2연승 분위기를 이어갔다\n"
            f"{correction}\n"
            "출력 형식:\n"
            "{\n"
            '  "line1": "...",\n'
            '  "line2": "...",\n'
            '  "line1_char_count": 0,\n'
            '  "total_char_count": 0\n'
            "}\n\n"
            "입력 JSON:\n"
            f"{json.dumps(prompt_payload, ensure_ascii=False, indent=2)}"
        )


def _title_copy_prompt_payload(copy_input: TitleCopyInput) -> dict[str, Any]:
    return {
        "topic_id": copy_input.topic_id,
        "topic_name": copy_input.topic_name,
        "team_name": copy_input.team_name,
        "draft_title": copy_input.draft_title,
        "draft_subtitle": copy_input.draft_subtitle,
        "cover_headline": copy_input.cover_headline,
        "cover_body": copy_input.cover_body,
        "overall_summary": copy_input.overall_summary,
        "memory_context_summary": copy_input.memory_context_summary,
        "referenced_memory_ids": list(copy_input.referenced_memory_ids or []),
    }


def _combine_copy_sources(*, headline_openai: bool, subheadline_source: str) -> str:
    if headline_openai and subheadline_source == "openai_subheadline_rewrite":
        return "openai_split_rewrite"
    if headline_openai:
        return f"openai_headline_{subheadline_source}"
    return subheadline_source


def build_default_title_copy_engine() -> TitleCopyEngine:
    load_default_env(ROOT_DIR)
    if os.getenv("OPENAI_API_KEY"):
        return OpenAISplitTitleCopyEngine()
    if os.getenv("GEMINI_API_KEY"):
        return GeminiTitleCopyEngine()
    return RuleBasedTitleCopyEngine()


class RuleBasedTitleImageSelectionEngine:
    def __init__(self, *, model_name: str = "rule-based-title-image-v1") -> None:
        self.model_name = model_name

    def select_image(self, selection_input: TitleImageSelectionInput) -> TitleImageSelectionOutput:
        ranked = sorted(
            selection_input.candidates,
            key=lambda item: (
                -_score_title_image_candidate(item, selection_input),
                item.sort_order,
            ),
        )
        selected = ranked[0]
        return TitleImageSelectionOutput(
            asset_reference=selected.asset_reference,
            image_source=selected.image_source,
            selection_source="rule_based",
            model_name=self.model_name,
            fit_reason="usage_recommendation, confidence, 카피 키워드 단순 매칭 기준",
        )


class OpenAITitleImageSelectionEngine:
    def __init__(
        self,
        *,
        api_key: str | None = None,
        model_name: str | None = None,
        transport: HttpTransport | None = None,
        endpoint: str = "https://api.openai.com/v1/responses",
    ) -> None:
        load_default_env(ROOT_DIR)
        self.api_key = api_key or os.getenv("OPENAI_API_KEY")
        self.model_name = model_name or os.getenv("OPENAI_TITLE_IMAGE_MODEL") or "gpt-4o"
        self.transport = transport or UrllibHttpTransport(timeout_seconds=60)
        self.endpoint = endpoint
        self.fallback_engine = RuleBasedTitleImageSelectionEngine()

    def select_image(self, selection_input: TitleImageSelectionInput) -> TitleImageSelectionOutput:
        if not self.api_key:
            return self.fallback_engine.select_image(selection_input)

        best_output: TitleImageSelectionOutput | None = None
        best_score = float("-inf")
        for candidate in selection_input.candidates:
            if candidate.local_image_path is None:
                continue
            try:
                request_payload = self._build_request_payload(selection_input, candidate)
                response_payload = self.transport.post_json(
                    self.endpoint,
                    request_payload,
                    {
                        "Content-Type": "application/json",
                        "Authorization": f"Bearer {self.api_key}",
                    },
                )
                parsed = self._parse_response(response_payload)
            except (RuntimeError, urllib.error.HTTPError, urllib.error.URLError, TimeoutError, socket.timeout, OSError):
                continue

            score = float(parsed.get("cover_fit_score", 0))
            if score > best_score:
                best_score = score
                best_output = TitleImageSelectionOutput(
                    asset_reference=candidate.asset_reference,
                    image_source=candidate.image_source,
                    selection_source="openai_mm_prompt",
                    model_name=self.model_name,
                    fit_reason=str(parsed.get("fit_reason") or parsed.get("why_funny") or ""),
                )

        if best_output is not None:
            return best_output
        return self.fallback_engine.select_image(selection_input)

    def _build_request_payload(self, selection_input: TitleImageSelectionInput, candidate: TitleImageCandidate) -> dict[str, Any]:
        prompt_text = _load_mm_prompt_text()
        prompt_text += (
            "\n\n[ADDITIONAL TASK]\n"
            "이미 타이틀 페이지 카피가 확정되어 있다. "
            "아래 headline과 subheadline에 이 이미지가 얼마나 잘 맞는지 평가하라.\n"
            "- headline과 subheadline은 새로 쓰지 말고, 반드시 그대로 유지한 상태에서 적합도만 평가하라.\n"
            "- 반드시 cover_fit_score(0~100 정수)와 fit_reason을 추가하라.\n"
            "- headline/subheadline을 뒷받침하는 감정선, 상황 일치도, 시선 집중도, 타이틀컷 적합성을 평가하라.\n"
            "- 출력은 반드시 JSON만 반환하라.\n"
        )
        input_payload = {
            "article_summary": selection_input.article_summary,
            "team_candidates": selection_input.team_candidates or ["unknown"],
            "player_candidates": selection_input.player_candidates or ["unknown"],
            "headline": selection_input.title_text,
            "subheadline": selection_input.subheadline,
            "asset_reference": candidate.asset_reference,
            "existing_scene_description": candidate.scene_description,
            "existing_usage_recommendation": candidate.usage_recommendation,
            "existing_confidence": candidate.confidence,
            "existing_humor_point": candidate.humor_point,
            "existing_caution_note": candidate.caution_note,
        }
        image_url = _local_image_to_data_url(candidate.local_image_path)
        return {
            "model": self.model_name,
            "input": [
                {
                    "role": "system",
                    "content": [
                        {"type": "input_text", "text": "You are a KBO card-news image selector. Return JSON only."},
                    ],
                },
                {
                    "role": "user",
                    "content": [
                        {"type": "input_text", "text": prompt_text},
                        {"type": "input_text", "text": json.dumps(input_payload, ensure_ascii=False, indent=2)},
                        {"type": "input_image", "image_url": image_url, "detail": "low"},
                    ],
                },
            ],
            "text": {
                "format": {
                    "type": "json_schema",
                    "name": "kbo_title_image_selection",
                    "strict": True,
                    "schema": {
                        "type": "object",
                        "properties": {
                            "scene_summary": {"type": "string"},
                            "team": {"type": "string"},
                            "player": {"type": "string"},
                            "situation": {"type": "string"},
                            "confidence": {"type": "string"},
                            "core_event": {"type": "string"},
                            "emotion": {"type": "string"},
                            "humor_point": {"type": "string"},
                            "why_funny": {"type": "string"},
                            "kbo_context": {"type": "string"},
                            "headline": {"type": "string"},
                            "subheadline": {"type": "string"},
                            "cover_fit_score": {"type": "number"},
                            "fit_reason": {"type": "string"},
                        },
                        "required": [
                            "scene_summary",
                            "team",
                            "player",
                            "situation",
                            "confidence",
                            "core_event",
                            "emotion",
                            "humor_point",
                            "why_funny",
                            "kbo_context",
                            "headline",
                            "subheadline",
                            "cover_fit_score",
                            "fit_reason",
                        ],
                        "additionalProperties": False,
                    },
                }
            },
        }

    @staticmethod
    def _parse_response(response_payload: dict[str, Any]) -> dict[str, Any]:
        output_text = response_payload.get("output_text")
        if isinstance(output_text, str) and output_text.strip():
            return json.loads(output_text)
        outputs = response_payload.get("output") or []
        fragments: list[str] = []
        for output_item in outputs:
            if not isinstance(output_item, dict):
                continue
            for content_item in output_item.get("content") or []:
                if isinstance(content_item, dict) and isinstance(content_item.get("text"), str):
                    fragments.append(content_item["text"])
        if not fragments:
            raise RuntimeError(f"Unexpected OpenAI response shape: {response_payload}")
        return json.loads("\n".join(fragments))


def build_default_title_image_selection_engine() -> TitleImageSelectionEngine:
    return RuleBasedTitleImageSelectionEngine()


def _build_rule_based_title_text(*, topic_name: str, draft_title: str, summary: str) -> str:
    team_name = _detect_team_name(topic_name, draft_title, summary)
    joined = " ".join(part for part in [topic_name, draft_title, summary] if part)

    for streak_keyword in ("연패", "연승"):
        if streak_keyword in joined:
            number = _extract_prefix_number(joined, streak_keyword)
            if team_name and number:
                return f"{team_name} {number}{streak_keyword}"

    for keyword in ("역전패", "역전승", "충격패", "대역전", "부상", "트레이드"):
        if keyword in joined and team_name:
            return f"{team_name} {keyword}"

    if team_name:
        return team_name

    compact = draft_title or topic_name or "KBO 이슈"
    compact = compact.replace(",", " ").replace("…", " ").strip()
    return compact.split()[0] if compact.split() else "KBO 이슈"


def _sanitize_title_text(text: str, *, copy_input: TitleCopyInput) -> str:
    cleaned = _collapse_spaces(text)
    if not cleaned:
        cleaned = _build_rule_based_title_text(
            topic_name=copy_input.topic_name,
            draft_title=copy_input.draft_title,
            summary=copy_input.overall_summary,
        )

    compact = cleaned.replace(" ", "")
    if len(compact) < TITLE_MIN_NON_SPACE_CHARS or len(compact) > TITLE_MAX_NON_SPACE_CHARS:
        cleaned = _build_rule_based_title_text(
            topic_name=copy_input.topic_name,
            draft_title=copy_input.draft_title,
            summary=copy_input.overall_summary,
        )
        compact = cleaned.replace(" ", "")
        if len(compact) > TITLE_MAX_NON_SPACE_CHARS:
            compact = compact[:TITLE_MAX_NON_SPACE_CHARS]
            cleaned = compact

    team_name = copy_input.team_name
    if team_name and _contains_other_team(cleaned, team_name):
        cleaned = _build_rule_based_title_text(
            topic_name=copy_input.topic_name,
            draft_title=copy_input.draft_title,
            summary=copy_input.overall_summary,
        )
    return cleaned or "KBO 이슈"


def _looks_like_player_event_headline(text: str) -> bool:
    compact = text.replace(" ", "")
    if not compact or len(compact) > TITLE_MAX_NON_SPACE_CHARS:
        return False
    if any(team in compact for team in TEAM_KEYWORDS):
        return False
    return any(
        keyword in compact
        for keyword in (
            "결승포",
            "결승타",
            "홈런",
            "만루포",
            "맹타",
            "호투",
            "역투",
            "복귀",
            "부상",
            "쐐기포",
        )
    )


def _sanitize_subheadline(text: str, *, team_name: str | None = None, fallback_text: str = "") -> str:
    cleaned = _normalize_subheadline_whitespace(text)
    if team_name:
        if team_name in cleaned and not _contains_other_team(cleaned, team_name):
            filtered = cleaned
        else:
            filtered = _filter_text_for_team(cleaned, team_name)
        if filtered:
            cleaned = filtered
        elif fallback_text:
            cleaned = _filter_text_for_team(_normalize_subheadline_whitespace(fallback_text), team_name) or _normalize_subheadline_whitespace(fallback_text)
    cleaned = cleaned.replace("..", ".").strip(" .")
    cleaned = _force_plain_subheadline_style(cleaned)
    if team_name and _contains_other_team(cleaned, team_name):
        fallback = _filter_text_for_team(fallback_text, team_name) or ""
        if fallback:
            cleaned = _normalize_subheadline_whitespace(fallback)
            cleaned = _force_plain_subheadline_style(cleaned)
    subheadline = _force_two_line_subheadline(cleaned)
    subheadline = _force_complete_subheadline_sentence(subheadline)
    return _force_two_line_subheadline(subheadline)


def _force_plain_subheadline_style(text: str) -> str:
    lines = []
    for line in (text or "").split("\n"):
        converted = _collapse_spaces(line)
        converted = converted.replace("예정입니다", "예정이다")
        converted = converted.replace("아닙니다", "아니다")
        converted = converted.replace("됩니다", "된다")
        converted = converted.replace("했습니다", "했다")
        converted = converted.replace("합니다", "한다")
        converted = converted.replace("입니다", "이다")
        converted = converted.replace("였습니다", "였다")
        converted = converted.replace("었습니다", "었다")
        converted = converted.replace("았습니다", "았다")
        converted = converted.replace("됐습니다", "됐다")
        converted = converted.replace("되었습니다", "됐다")
        converted = converted.replace("습니다", "다")
        converted = re.sub(r"([가-힣])갑니다\b", r"\1간다", converted)
        converted = re.sub(r"([가-힣])옵니다\b", r"\1온다", converted)
        lines.append(converted)
    return "\n".join(line for line in lines if line).strip()


def _score_title_image_candidate(candidate: TitleImageCandidate, selection_input: TitleImageSelectionInput) -> float:
    score = float(candidate.confidence or 0.0) * 20.0
    if candidate.usage_recommendation == "cover":
        score += 45.0
    elif candidate.usage_recommendation == "reaction":
        score += 20.0
    elif candidate.usage_recommendation == "detail_b":
        score += 15.0

    combined = " ".join(
        text for text in [
            candidate.scene_description or "",
            candidate.humor_point or "",
            selection_input.title_text,
            selection_input.subheadline,
            selection_input.article_summary,
        ]
        if text
    )
    if any(token in combined for token in ["연패", "침울", "고개", "멘붕", "역전패"]) and "연패" in selection_input.title_text:
        score += 12.0
    if any(token in combined for token in ["연승", "환호", "세리머니", "홈런", "기쁨"]) and "연승" in selection_input.title_text:
        score += 12.0
    primary_team = selection_input.team_candidates[0] if selection_input.team_candidates else None
    scene_text = " ".join(text for text in [candidate.scene_description or "", candidate.humor_point or ""] if text)
    if primary_team:
        if primary_team in scene_text:
            score += 18.0
        elif _contains_other_team(scene_text, primary_team):
            score -= 30.0
    if candidate.caution_note:
        score -= 5.0
    return score


def _collapse_spaces(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def _build_team_candidates(*texts: str) -> list[str]:
    matches: list[str] = []
    for keyword in TEAM_KEYWORDS:
        if any(keyword in (text or "") for text in texts):
            matches.append(keyword)
    return matches or ["unknown"]


def _contains_other_team(text: str, primary_team: str | None) -> bool:
    if not text:
        return False
    for keyword in TEAM_KEYWORDS:
        if keyword == primary_team:
            continue
        if keyword in text:
            return True
    return False


def _filter_text_for_team(text: str, team_name: str | None) -> str:
    cleaned = _collapse_spaces(text)
    if not cleaned or not team_name:
        return cleaned
    if team_name in cleaned and not _contains_other_team(cleaned, team_name):
        return cleaned
    if team_name in cleaned:
        kept_fragments = [
            fragment for fragment in _split_copy_fragments(cleaned)
            if team_name in fragment or not _contains_other_team(fragment, team_name)
        ]
        merged = ", ".join(fragment for fragment in kept_fragments if fragment)
        return _collapse_spaces(merged) or cleaned
    if _contains_other_team(cleaned, team_name):
        return ""
    return cleaned


def _extract_player_candidates(*texts: str) -> list[str]:
    blocked = set(TEAM_KEYWORDS) | {
        "트윈스", "타이거즈", "베어스", "라이온즈", "자이언츠", "랜더스", "이글스", "위즈", "다이노스", "히어로즈",
        "연승", "연패", "역전패", "역전승", "단독", "선두", "리그", "경기", "홈런", "세리머니", "투수", "타자",
    }
    merged = " ".join(text for text in texts if text)
    candidates: list[str] = []
    for token in re.findall(r"[가-힣]{2,4}", merged):
        if token in blocked:
            continue
        if token not in candidates:
            candidates.append(token)
    return candidates[:8] or ["unknown"]


def _load_mm_prompt_text() -> str:
    return MM_PROMPT_PATH.read_text(encoding="utf-8")


def _local_image_to_data_url(path: Path) -> str:
    suffix = path.suffix.lower()
    mime = {
        ".png": "image/png",
        ".gif": "image/gif",
        ".webp": "image/webp",
    }.get(suffix, "image/jpeg")
    encoded = base64.b64encode(path.read_bytes()).decode("ascii")
    return f"data:{mime};base64,{encoded}"


def _build_subheadline_source(copy_input: TitleCopyInput, *, team_name: str | None = None) -> str:
    raw_candidates = [
        _collapse_spaces(copy_input.draft_subtitle),
        _collapse_spaces(copy_input.cover_body),
        _collapse_spaces(copy_input.overall_summary),
        _collapse_spaces(copy_input.cover_headline),
    ]

    short_candidates: list[str] = []
    best_under_target = ""
    best_complete_sentence = ""

    for candidate in raw_candidates:
        if not candidate:
            continue
        if team_name:
            candidate = _filter_text_for_team(candidate, team_name)
            if not candidate:
                continue
        if SUBHEADLINE_TARGET_MIN_CHARS <= len(candidate) <= SUBHEADLINE_TARGET_MAX_CHARS:
            return candidate
        if len(candidate) < SUBHEADLINE_TARGET_MIN_CHARS:
            short_candidates.append(candidate)

        for fragment_candidate in _combine_copy_fragments_for_subheadline(candidate):
            if SUBHEADLINE_TARGET_MIN_CHARS <= len(fragment_candidate) <= SUBHEADLINE_TARGET_MAX_CHARS:
                return fragment_candidate
            if len(fragment_candidate) <= SUBHEADLINE_TARGET_MAX_CHARS and len(fragment_candidate) > len(best_under_target):
                best_under_target = fragment_candidate

        sentences = _split_complete_sentences(candidate)
        if not sentences:
            continue

        if SUBHEADLINE_TARGET_MIN_CHARS <= len(candidate) <= SUBHEADLINE_TARGET_MAX_CHARS and candidate.endswith((".", "!", "?", "。")):
            return candidate

        for start in range(len(sentences)):
            combined = sentences[start]
            if len(combined) <= SUBHEADLINE_TARGET_MAX_CHARS and len(combined) > len(best_under_target):
                best_under_target = combined
            if _is_sentence_complete(combined) and len(combined) > len(best_complete_sentence):
                best_complete_sentence = combined
            if SUBHEADLINE_TARGET_MIN_CHARS <= len(combined) <= SUBHEADLINE_TARGET_MAX_CHARS and _is_sentence_complete(combined):
                return combined

            for end in range(start + 1, len(sentences)):
                trial = f"{combined} {sentences[end]}"
                if len(trial) > SUBHEADLINE_TARGET_MAX_CHARS:
                    break
                if len(trial) > len(best_under_target):
                    best_under_target = trial
                if _is_sentence_complete(trial) and len(trial) > len(best_complete_sentence):
                    best_complete_sentence = trial
                if SUBHEADLINE_TARGET_MIN_CHARS <= len(trial) <= SUBHEADLINE_TARGET_MAX_CHARS and _is_sentence_complete(trial):
                    return trial
                combined = trial

    if best_under_target:
        for short_candidate in sorted(short_candidates, key=len, reverse=True):
            combined_fallback = _join_subheadline_fragments(best_under_target, short_candidate)
            if SUBHEADLINE_TARGET_MIN_CHARS <= len(combined_fallback) <= SUBHEADLINE_TARGET_MAX_CHARS:
                return combined_fallback
    best_short_candidate = max(short_candidates, key=len, default="")
    return best_under_target or best_complete_sentence or best_short_candidate or raw_candidates[0]


def _combine_copy_fragments_for_subheadline(text: str) -> list[str]:
    fragments = _split_copy_fragments(text)
    if not fragments:
        return []
    candidates: list[str] = []
    for start in range(len(fragments)):
        combined = fragments[start]
        if len(combined) <= SUBHEADLINE_TARGET_MAX_CHARS:
            candidates.append(combined)
        for end in range(start + 1, len(fragments)):
            trial = _join_subheadline_fragments(combined, fragments[end])
            if len(trial) > SUBHEADLINE_TARGET_MAX_CHARS:
                break
            candidates.append(trial)
            combined = trial
    return sorted(candidates, key=len, reverse=True)


def _join_subheadline_fragments(left: str, right: str) -> str:
    left = _collapse_spaces(left).rstrip(" ,.")
    right = _collapse_spaces(right).lstrip(" ,.")
    if not left:
        return right
    if not right:
        return left
    if left.endswith("다"):
        return f"{left[:-1]}고 {right}"
    return f"{left}, {right}"


def _force_two_line_subheadline(text: str) -> str:
    cleaned = _normalize_subheadline_whitespace(text).strip()
    if not cleaned:
        return ""

    if cleaned.count("\n") == 1:
        left, right = [part.strip() for part in cleaned.split("\n", 1)]
        if (
            left
            and right
            and len(f"{left}{right}") <= SUBHEADLINE_TARGET_MAX_CHARS
            and SUBHEADLINE_FIRST_LINE_MIN_CHARS <= len(left) <= SUBHEADLINE_FIRST_LINE_MAX_CHARS
            and _has_natural_subheadline_line_break(left, right)
        ):
            return f"{left}\n{right}"

    cleaned = cleaned.replace("\n", " ")
    if len(cleaned) < SUBHEADLINE_TWO_LINE_MIN_CHARS:
        return cleaned

    words = cleaned.split()
    if len(words) < 2:
        return cleaned

    best_text = cleaned
    best_score: float | None = None
    best_valid_text = ""
    best_valid_score: float | None = None
    for index in range(1, len(words)):
        left = " ".join(words[:index]).strip()
        right = " ".join(words[index:]).strip()
        if not left or not right:
            continue
        total = f"{left}\n{right}"
        if len(left + right) > SUBHEADLINE_TARGET_MAX_CHARS:
            continue
        if (
            SUBHEADLINE_FIRST_LINE_MIN_CHARS <= len(left) <= SUBHEADLINE_FIRST_LINE_MAX_CHARS
            and _has_natural_subheadline_line_break(left, right)
        ):
            score = abs(SUBHEADLINE_FIRST_LINE_MAX_CHARS - len(left))
            if best_valid_score is None or score < best_valid_score:
                best_valid_score = score
                best_valid_text = total
            continue
        if len(left) < SUBHEADLINE_FIRST_LINE_MIN_CHARS:
            score = SUBHEADLINE_FIRST_LINE_MIN_CHARS - len(left)
        else:
            score = len(left) - SUBHEADLINE_FIRST_LINE_MAX_CHARS
        if best_score is None or score < best_score:
            best_score = score
            best_text = total
    if best_valid_text:
        return best_valid_text
    return best_text


def _normalize_subheadline_whitespace(text: str) -> str:
    raw = (text or "").replace("\r\n", "\n").replace("\r", "\n")
    lines = [" ".join(part.split()) for part in raw.split("\n")]
    kept = [line.strip() for line in lines if line.strip()]
    if not kept:
        return ""
    if len(kept) == 1:
        return kept[0]
    return f"{kept[0]}\n{' '.join(kept[1:]).strip()}"


def _is_valid_subheadline_output(text: str) -> bool:
    return _validate_subheadline_output(text) is None


def _validate_subheadline_output(
    text: str,
    *,
    headline: str | None = None,
    copy_input: TitleCopyInput | None = None,
) -> str | None:
    issues = _collect_subheadline_validation_issues(text, headline=headline, copy_input=copy_input)
    if issues:
        return " / ".join(issues)
    return None


def _collect_subheadline_validation_issues(
    text: str,
    *,
    headline: str | None = None,
    copy_input: TitleCopyInput | None = None,
) -> list[str]:
    issues: list[str] = []
    normalized = _normalize_subheadline_whitespace(text)
    if normalized.count("\n") != 1:
        issues.append(f"줄바꿈 조건: 줄바꿈은 정확히 1개여야 하지만 현재 {normalized.count(chr(10))}개다")
    lines = normalized.split("\n")
    line1 = lines[0].strip() if lines else ""
    line2 = lines[1].strip() if len(lines) > 1 else ""
    if not line1 or not line2:
        issues.append("줄 구성 조건: 두 줄 모두 비어 있으면 안 된다")
    if line1:
        line1_length = len(line1)
        if line1_length < SUBHEADLINE_FIRST_LINE_MIN_CHARS:
            issues.append(
                f"첫 줄 조건: 첫 줄이 {line1_length}자로 {SUBHEADLINE_FIRST_LINE_MIN_CHARS - line1_length}자 짧다"
            )
        elif line1_length > SUBHEADLINE_FIRST_LINE_MAX_CHARS:
            issues.append(
                f"첫 줄 조건: 첫 줄이 {line1_length}자로 {line1_length - SUBHEADLINE_FIRST_LINE_MAX_CHARS}자 길다"
            )
    if line1 and line2 and not _has_natural_subheadline_line_break(line1, line2):
        issues.append("줄바꿈 조건: 첫 줄 끝에서 명사구나 조사가 끊기지 않아야 한다")
    if _has_disallowed_subheadline_phrase(normalized):
        issues.append("문체 조건: 금지 표현 또는 입니다체가 포함됐다")
    total_without_newline = len(normalized.replace("\n", ""))
    if total_without_newline < SUBHEADLINE_TARGET_MIN_CHARS:
        issues.append(
            f"전체 길이 조건: 줄바꿈 제외 전체가 {total_without_newline}자로 {SUBHEADLINE_TARGET_MIN_CHARS - total_without_newline}자 짧다"
        )
    elif total_without_newline > SUBHEADLINE_TARGET_MAX_CHARS:
        issues.append(
            f"전체 길이 조건: 줄바꿈 제외 전체가 {total_without_newline}자로 {total_without_newline - SUBHEADLINE_TARGET_MAX_CHARS}자 길다"
        )
    if not _is_complete_subheadline_sentence(normalized):
        issues.append("문장 완결 조건: 전체 subheadline은 연결 어미로 끝나지 않는 완전한 문장이어야 한다")
    if headline and copy_input and not _subheadline_matches_headline(normalized, headline=headline, copy_input=copy_input):
        anchors = _extract_headline_anchor_terms(headline, copy_input=copy_input)
        issues.append(f"헤드라인 연결 조건: selected_headline과 연결되지 않았다. headline_anchor_terms={anchors}")
    return issues


def _build_subheadline_retry_feedback(
    *,
    normalized: str,
    validation_error: str,
    line_lengths: list[int],
    total_without_newline: int,
) -> str:
    issue_lines = [
        f"- {issue.strip()}"
        for issue in str(validation_error or "").split("/")
        if issue.strip()
    ]
    issue_feedback = "세부 실패 항목:\n" + "\n".join(issue_lines) + "\n" if issue_lines else ""

    total_length_feedback = ""
    if total_without_newline < SUBHEADLINE_TARGET_MIN_CHARS:
        gap = SUBHEADLINE_TARGET_MIN_CHARS - total_without_newline
        total_length_feedback = (
            f"전체 길이 피드백: 줄바꿈 제외 전체가 {total_without_newline}자로 {gap}자 짧다. "
            "문장 끝에 단어나 수식어를 덧붙이지 말고, 같은 핵심 사실을 유지한 채 전체 문장을 처음부터 조금 더 길게 다시 써라. "
            "짧은 문장을 늘이는 방식이 아니라 70~85자 완성 문장으로 새로 구성하라. "
        )
    elif total_without_newline > SUBHEADLINE_TARGET_MAX_CHARS:
        gap = total_without_newline - SUBHEADLINE_TARGET_MAX_CHARS
        total_length_feedback = (
            f"전체 길이 피드백: 줄바꿈 제외 전체가 {total_without_newline}자로 {gap}자 길다. "
            "단어를 기계적으로 자르지 말고, 핵심 사실은 유지한 채 전체 문장을 처음부터 조금 더 압축해서 다시 써라. "
            "긴 문장을 잘라내는 방식이 아니라 70~85자 완성 문장으로 새로 구성하라. "
        )
    first_line_feedback = ""
    if line_lengths:
        first_line_length = line_lengths[0]
        if first_line_length < SUBHEADLINE_FIRST_LINE_MIN_CHARS:
            gap = SUBHEADLINE_FIRST_LINE_MIN_CHARS - first_line_length
            first_line_feedback = (
                f"첫 줄 피드백: 첫 줄이 {first_line_length}자로 {gap}자 짧다. "
                "첫 줄만 늘이지 말고 전체 문장을 다시 만든 뒤 40~48자 지점에서 자연스럽게 나눠라. "
            )
        elif first_line_length > SUBHEADLINE_FIRST_LINE_MAX_CHARS:
            gap = first_line_length - SUBHEADLINE_FIRST_LINE_MAX_CHARS
            first_line_feedback = (
                f"첫 줄 피드백: 첫 줄이 {first_line_length}자로 {gap}자 길다. "
                "첫 줄만 자르지 말고 전체 문장을 다시 만든 뒤 40~48자 지점에서 자연스럽게 나눠라. "
            )

    break_feedback = ""
    if "줄바꿈 조건" in validation_error:
        break_feedback = (
            "줄바꿈 피드백: 팀명, 선수명, 숫자, 조사, 소유격 '의' 바로 뒤에서 끊지 말고 "
            "의미 단위가 닫히는 지점에서 줄을 나눠라. "
        )

    completion_feedback = ""
    if "문장 완결 조건" in validation_error:
        completion_feedback = (
            "문장 완결 피드백: 마지막 어절이 '고', '며', '면서', '지만' 같은 연결 어미면 안 된다. "
            "'했다', '이다', '이어갔다', '보여줬다'처럼 문장이 닫히는 형태로 끝내라. "
        )

    style_feedback = ""
    if "문체 조건" in validation_error:
        style_feedback = "문체 피드백: 입니다체와 금지 표현을 버리고 했다체/이다체로 다시 써라. "

    headline_feedback = ""
    if "헤드라인 연결 조건" in validation_error:
        headline_feedback = "헤드라인 연결 피드백: selected_headline의 팀명, 선수명, 사건 단어 중 최소 1개를 직접 반영하라. "

    return (
        f"이전 출력은 줄바꿈 제외 전체 {total_without_newline}자, 각 줄 {line_lengths}자라서 실패했다. "
        f"{issue_feedback}"
        f"이전 출력값은 {normalized!r} 이다. "
        f"{first_line_feedback}{total_length_feedback}{break_feedback}{completion_feedback}{style_feedback}{headline_feedback}"
        "다음 출력은 이전 출력값을 반복하지 말고, 먼저 줄바꿈 없는 70~85자 완성 문장을 처음부터 다시 쓴 뒤 "
        "첫 줄 40~48자 지점에서 자연스럽게 2줄로 나누라. "
        "마지막 어절은 반드시 '했다', '이다', '이어갔다', '보여줬다'처럼 문장이 닫히는 형태로 끝내라. "
        "반드시 입니다체를 버리고, selected_headline의 핵심 단어와 연결되게 작성하라."
    )


def _is_complete_subheadline_sentence(text: str) -> bool:
    compact = _collapse_spaces(text.replace("\n", " ")).strip()
    if not compact:
        return False
    compact = compact.rstrip(" .!?,。")
    if not compact:
        return False
    last_token = compact.split()[-1]
    if last_token.endswith(
        (
            "고",
            "며",
            "면서",
            "지만",
            "하고",
            "하며",
            "통해",
            "위해",
            "속에서",
            "상황에서",
            "이후",
            "전",
            "뒤",
            "중",
        )
    ):
        return False
    if last_token.endswith(
        (
            "다",
            "했다",
            "였다",
            "었다",
            "았다",
            "됐다",
            "낸다",
            "간다",
            "온다",
            "준다",
            "냈다",
            "갔다",
            "왔다",
            "섰다",
        )
    ):
        return True
    return bool(re.search(r"(?:다|했다|였다|었다|았다|됐다|냈다|갔다|왔다|섰다)$", compact))


def _force_complete_subheadline_sentence(text: str) -> str:
    normalized = _normalize_subheadline_whitespace(text)
    if not normalized or _is_complete_subheadline_sentence(normalized):
        return normalized

    compact = _collapse_spaces(normalized.replace("\n", " ")).rstrip(" .!?,。")
    replacements: tuple[tuple[str, str], ...] = (
        (r"(.+)을 알렸고$", r"\1을 알렸다"),
        (r"(.+)를 알렸고$", r"\1를 알렸다"),
        (r"(.+)이 성공했고$", r"\1이 성공했다"),
        (r"(.+)가 성공했고$", r"\1가 성공했다"),
        (r"(.+)도 성공했고$", r"\1도 성공했다"),
        (r"(.+)까지 성공했고$", r"\1까지 성공했다"),
        (r"(.+)보여줬고$", r"\1보여줬다"),
        (r"(.+)보여주며$", r"\1보여줬다"),
        (r"(.+)이어가며$", r"\1이어갔다"),
        (r"(.+)이어가고$", r"\1이어갔다"),
        (r"(.+)마치며$", r"\1마쳤다"),
        (r"(.+)따내며$", r"\1따냈다"),
        (r"(.+)거두며$", r"\1거뒀다"),
        (r"(.+)바꾸며$", r"\1바꿨다"),
        (r"(.+)살리며$", r"\1살렸다"),
        (r"(.+)만들며$", r"\1만들었다"),
        (r"(.+)넘기며$", r"\1넘겼다"),
        (r"(.+)이끌며$", r"\1이끌었다"),
        (r"(.+)잡으며$", r"\1잡았다"),
        (r"(.+)알리며$", r"\1알렸다"),
        (r"(.+)증명하며$", r"\1증명했다"),
        (r"(.+)완주하며$", r"\1완주했다"),
        (r"(.+)했고$", r"\1했다"),
        (r"(.+)됐고$", r"\1됐다"),
        (r"(.+)였고$", r"\1였다"),
        (r"(.+)었고$", r"\1었다"),
        (r"(.+)았고$", r"\1았다"),
        (r"(.+)하며$", r"\1했다"),
    )
    for pattern, replacement in replacements:
        repaired = re.sub(pattern, replacement, compact)
        if repaired != compact and _is_complete_subheadline_sentence(repaired):
            return repaired

    return _append_completion_phrase(compact)


def _append_completion_phrase(text: str) -> str:
    base = _collapse_spaces(text).rstrip(" ,.!?。")
    if not base:
        return "핵심 장면을 완성했다"
    base = re.sub(r"(?:고|며|면서|지만|하고|하며|통해|위해)$", "", base).strip(" ,")
    phrase = "성과를 남겼다"
    max_base_length = max(0, SUBHEADLINE_TARGET_MAX_CHARS - len(phrase) - 1)
    while len(base) > max_base_length and " " in base:
        base = base.rsplit(" ", 1)[0].strip()
    return f"{base} {phrase}".strip()


def _has_natural_subheadline_line_break(line1: str, line2: str) -> bool:
    left = _collapse_spaces(line1).strip()
    right = _collapse_spaces(line2).strip()
    if not left or not right:
        return False
    if left.endswith((",", "，", "·", "-", "(", "[", "{", ":", ";")):
        return False
    last_token = left.split()[-1]
    if last_token in set(TEAM_KEYWORDS):
        return False
    if re.fullmatch(r"[A-Z]{2,4}", last_token):
        return False
    if re.fullmatch(r"\d+(?:-\d+)?", last_token):
        return False
    if last_token.endswith("의") and re.match(r"\d", right):
        return True
    if last_token.endswith(("은", "는", "이", "가", "을", "를", "의", "와", "과", "로", "으로", "에서", "부터")):
        return False
    if right.startswith(("선발", "투수", "타자", "감독", "선수", "교체", "부상")) and last_token in set(TEAM_KEYWORDS):
        return False
    return True


def _has_disallowed_subheadline_phrase(text: str) -> bool:
    compact = _collapse_spaces(text)
    disallowed_phrases = (
        "부상 교체를 틈타",
        "부상을 틈타",
        "교체를 틈타",
        "틈타",
        "최하위 탈출의 시동",
        "입니다",
        "했습니다",
        "합니다",
        "습니다",
        "예정입니다",
        "아닙니다",
        "됩니다",
        "됐습니다",
        "되었습니다",
    )
    if any(phrase in compact for phrase in disallowed_phrases):
        return True
    return re.search(r"(?:입|합|됩|했|됐|되었|였|겠|습|깁|옵)니다\b", compact) is not None


def _subheadline_matches_headline(text: str, *, headline: str, copy_input: TitleCopyInput) -> bool:
    anchors = _extract_headline_anchor_terms(headline, copy_input=copy_input)
    if not anchors:
        return True
    compact_text = text.replace(" ", "")
    for anchor in anchors:
        if anchor.replace(" ", "") in compact_text:
            return True
        for synonym in _headline_anchor_synonyms(anchor):
            if synonym in compact_text:
                return True
    return False


def _extract_headline_anchor_terms(headline: str, *, copy_input: TitleCopyInput) -> list[str]:
    compact_headline = _collapse_spaces(headline).replace(" ", "")
    if not compact_headline:
        return []
    anchors: list[str] = []

    def add(term: str) -> None:
        cleaned = _collapse_spaces(term).replace(" ", "")
        if len(cleaned) >= 2 and cleaned not in anchors:
            anchors.append(cleaned)

    if copy_input.team_name and copy_input.team_name in compact_headline:
        add(copy_input.team_name)
    for team in TEAM_KEYWORDS:
        if team in compact_headline:
            add(team)

    event_terms = (
        "연장승",
        "역전승",
        "결승포",
        "결승타",
        "홈런",
        "만루포",
        "쐐기포",
        "호투",
        "역투",
        "맹타",
        "연승",
        "연패",
        "선두",
        "탈환",
        "부상",
        "복귀",
        "비상",
        "호수비",
        "득점",
    )
    for term in event_terms:
        if term in compact_headline:
            add(term)

    for candidate in _extract_player_candidates(
        copy_input.topic_name,
        copy_input.draft_title,
        copy_input.draft_subtitle,
        copy_input.cover_headline,
        copy_input.cover_body,
        copy_input.overall_summary,
    ):
        if candidate in compact_headline:
            add(candidate)

    if not anchors:
        for token in re.findall(r"[A-Za-z가-힣0-9]{2,8}", headline):
            add(token)
    return anchors[:6]


def _headline_anchor_synonyms(anchor: str) -> tuple[str, ...]:
    synonym_map = {
        "결승포": ("결승홈런", "홈런", "장타", "결정적한방"),
        "결승타": ("적시타", "장타", "결정적한방"),
        "연장승": ("연장", "승리", "접전"),
        "역전승": ("역전", "승리"),
        "비상": ("위기", "공백", "이탈"),
        "부상": ("이탈", "공백", "재검진"),
        "맹타": ("장타", "안타", "타선"),
    }
    return synonym_map.get(anchor, ())


def _find_split_index(text: str, midpoint: int) -> int:
    left_space = text.rfind(" ", 0, midpoint + 1)
    right_space = text.find(" ", midpoint)

    if left_space == -1 and right_space == -1:
        return midpoint
    if left_space == -1:
        return right_space
    if right_space == -1:
        return left_space
    if midpoint - left_space <= right_space - midpoint:
        return left_space
    return right_space


def _trim_to_soft_limit(text: str, limit: int) -> str:
    cleaned = _collapse_spaces(text).strip(" .,")
    if len(cleaned) <= limit:
        return cleaned
    trimmed = cleaned[:limit + 1]
    last_break = max(trimmed.rfind(","), trimmed.rfind(" "), trimmed.rfind("·"))
    if last_break >= max(0, limit - 12):
        trimmed = trimmed[:last_break]
    else:
        trimmed = trimmed[:limit]
    return trimmed.rstrip(" ,·-.")


def _split_copy_fragments(text: str) -> list[str]:
    cleaned = _collapse_spaces(text)
    if not cleaned:
        return []
    parts = re.split(r"[,.!?]|(?:\s+·\s+)", cleaned)
    return [part.strip() for part in parts if part.strip()]


def _split_complete_sentences(text: str) -> list[str]:
    cleaned = _collapse_spaces(text)
    if not cleaned:
        return []
    parts = re.split(r"(?<=[.!?。])\s+|(?<=\n)\s*", cleaned)
    return [part.strip() for part in parts if part.strip()]


def _is_sentence_complete(text: str) -> bool:
    stripped = _collapse_spaces(text).strip()
    return bool(stripped) and stripped.endswith((".", "!", "?", "。"))


def _extract_prefix_number(text: str, suffix: str) -> str | None:
    marker = text.find(suffix)
    if marker <= 0:
        return None
    digits: list[str] = []
    for ch in reversed(text[:marker]):
        if ch.isdigit():
            digits.append(ch)
            continue
        if digits:
            break
    if not digits:
        return None
    return "".join(reversed(digits))


def _format_date_text(published_at: str | None) -> str:
    if not published_at:
        return datetime.now().strftime("%Y.%m.%d")

    try:
        parsed = datetime.fromisoformat(published_at.replace("Z", "+00:00"))
    except ValueError:
        return datetime.now().strftime("%Y.%m.%d")
    return parsed.strftime("%Y.%m.%d")


def _build_title_output_basename(
    *,
    date_text: str | None,
    title_text: str | None,
    fallback_text: str,
) -> str:
    date_part = _normalize_title_date_token(date_text)
    title_part = _slugify_title_token(title_text or fallback_text)
    return f"{date_part}_{title_part}"


def _build_editable_output_paths(output_path: Path) -> tuple[Path, Path]:
    return (
        output_path.with_suffix(".editable.html"),
        output_path.with_suffix(".editable.json"),
    )


def _build_spec_output_paths(output_path: Path) -> tuple[Path, Path]:
    return (
        output_path.with_suffix(".json"),
        output_path.with_suffix(".editable.json"),
    )


def _build_candidate_export_filename(*, index: int, asset_reference: str, selected: bool) -> str:
    label = "selected" if selected else "candidate"
    safe_ref = _slugify_title_token(asset_reference)[:32] or f"asset_{index}"
    return f"{index:02d}_{label}_{safe_ref}.png"


def _normalize_title_date_token(date_text: str | None) -> str:
    raw = (date_text or "").strip()
    digits = "".join(ch for ch in raw if ch.isdigit())
    if len(digits) >= 8:
        return digits[2:8]
    if len(digits) == 6:
        return digits
    return datetime.now().strftime("%y%m%d")


def _slugify_title_token(value: str) -> str:
    compact = re.sub(r"\s+", "_", (value or "").strip())
    compact = re.sub(r"[^0-9A-Za-z가-힣_-]+", "", compact)
    compact = re.sub(r"_+", "_", compact).strip("_-")
    return compact[:60] or "title"


def _resolve_title_team_color(team_name: str | None) -> str:
    if not team_name:
        return "#111111"
    return {
        "LG": "#C30452",
        "KIA": "#EA0029",
        "두산": "#131230",
        "삼성": "#0066B3",
        "롯데": "#041E42",
        "SSG": "#CE0E2D",
        "한화": "#FC4E00",
        "KT": "#231F20",
        "NC": "#315288",
        "키움": "#7A003C",
    }.get(team_name, "#111111")
