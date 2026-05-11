from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from dataclasses import asdict
from datetime import datetime, timezone
from typing import Protocol

from kbo_card_news.config.env import load_default_env
from kbo_card_news.imaging.prompts import (
    IMAGE_PLANNING_SYSTEM_PROMPT,
    build_image_planning_user_prompt,
)
from kbo_card_news.models.issue import (
    AssetMultimodalInsight,
    CardImagePlan,
    CardImagePlanPage,
    CardImagePlanningInput,
)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


class CardImagePlanningEngine(Protocol):
    def build_plan(self, planning_input: CardImagePlanningInput) -> CardImagePlan:
        ...


class HttpTransport(Protocol):
    def post_json(self, url: str, payload: dict, headers: dict[str, str]) -> dict:
        ...


class UrllibHttpTransport:
    def post_json(self, url: str, payload: dict, headers: dict[str, str]) -> dict:
        request = urllib.request.Request(
            url=url,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=180) as response:
            body = response.read().decode("utf-8")
        return json.loads(body)


class HeuristicCardImagePlanningEngine:
    def __init__(
        self,
        *,
        model_name: str = "heuristic-image-plan-v1",
        prompt_version: str = "phase4.1",
    ) -> None:
        self.model_name = model_name
        self.prompt_version = prompt_version

    def build_plan(self, planning_input: CardImagePlanningInput) -> CardImagePlan:
        pages = [
            self._build_page_plan(planning_input, page)
            for page in planning_input.draft.pages
        ]
        return CardImagePlan(
            issue_id=planning_input.candidate.issue_id,
            template_name=planning_input.draft.template_name,
            model_name=self.model_name,
            prompt_version=self.prompt_version,
            overall_art_direction=self._build_art_direction(planning_input),
            pages=pages,
            metadata={
                "source_asset_count": len(planning_input.assets),
                "has_multimodal_analysis": planning_input.multimodal_analysis is not None,
                "team_code": planning_input.draft.metadata.get("team_code"),
            },
            created_at=_utc_now(),
        )

    def _build_page_plan(
        self,
        planning_input: CardImagePlanningInput,
        page,
    ) -> CardImagePlanPage:
        effective_page_role = _resolve_effective_page_role(page.page_role, page.page_number)
        insight = _find_best_insight(planning_input, effective_page_role)
        primary_asset_reference = _select_primary_asset_reference(planning_input, page, insight)
        render_strategy = _select_render_strategy(effective_page_role, primary_asset_reference)
        if render_strategy == "generated_background":
            primary_asset_reference = None
        crop_focus = _build_crop_focus(effective_page_role, insight, planning_input.candidate.title)
        overlay_style = _build_overlay_style(effective_page_role, render_strategy)
        text_layout_hint = _build_text_layout_hint(effective_page_role, insight)
        generation_prompt = _build_generation_prompt(
            planning_input=planning_input,
            page=page,
            effective_page_role=effective_page_role,
            render_strategy=render_strategy,
            insight=insight,
        )
        edit_instructions = _build_edit_instructions(
            page_role=effective_page_role,
            render_strategy=render_strategy,
            insight=insight,
        )
        caution_note = _combine_cautions(effective_page_role, insight)
        return CardImagePlanPage(
            page_number=page.page_number,
            page_role=page.page_role,
            render_strategy=render_strategy,
            primary_asset_reference=primary_asset_reference,
            crop_focus=crop_focus,
            overlay_style=overlay_style,
            text_layout_hint=text_layout_hint,
            generation_prompt=generation_prompt,
            edit_instructions=edit_instructions,
            caution_note=caution_note,
        )

    def _build_art_direction(self, planning_input: CardImagePlanningInput) -> str:
        team_code = str(planning_input.draft.metadata.get("team_code", "KBO")).strip() or "KBO"
        source_type = planning_input.candidate.source_type
        if planning_input.draft.template_name == "compact_2p":
            return (
                f"{team_code} 구단 포인트 컬러를 살리되 모바일 우선 가독성을 유지하는 "
                f"간결한 속보형 톤. source_type={source_type}."
            )
        return (
            f"{team_code} 구단 포인트 컬러와 스포츠 에디토리얼 톤을 유지하고, "
            f"표지와 상세 페이지는 현장감, 데이터 페이지는 정돈된 정보성을 우선한다. "
            f"source_type={source_type}."
        )


class GeminiCardImagePlanningEngine:
    def __init__(
        self,
        *,
        api_key: str | None = None,
        model_name: str = "gemini-2.5-flash-lite",
        prompt_version: str = "phase4.1",
        transport: HttpTransport | None = None,
        endpoint_base: str = "https://generativelanguage.googleapis.com/v1beta/models",
    ) -> None:
        load_default_env()
        self.api_key = api_key or os.getenv("GEMINI_API_KEY")
        self.model_name = model_name
        self.prompt_version = prompt_version
        self.transport = transport or UrllibHttpTransport()
        self.endpoint_base = endpoint_base.rstrip("/")

    def build_plan(self, planning_input: CardImagePlanningInput) -> CardImagePlan:
        if not self.api_key:
            raise ValueError("GEMINI_API_KEY is required for GeminiCardImagePlanningEngine")

        request_payload = self._build_request_payload(planning_input)
        url = f"{self.endpoint_base}/{self.model_name}:generateContent"
        headers = {
            "Content-Type": "application/json",
            "x-goog-api-key": self.api_key,
        }

        try:
            response_payload = self.transport.post_json(url, request_payload, headers)
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"Gemini API request failed: HTTP {exc.code} {detail}") from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(f"Gemini API request failed: {exc.reason}") from exc

        parsed = self._parse_response_payload(response_payload)
        self._validate_response(planning_input, parsed)
        pages = [
            CardImagePlanPage(
                page_number=int(item["page_number"]),
                page_role=str(item["page_role"]),
                render_strategy=str(item["render_strategy"]),
                primary_asset_reference=_normalize_optional_text(item.get("primary_asset_reference")),
                crop_focus=str(item["crop_focus"]),
                overlay_style=str(item["overlay_style"]),
                text_layout_hint=str(item["text_layout_hint"]),
                generation_prompt=str(item["generation_prompt"]),
                edit_instructions=str(item["edit_instructions"]),
                caution_note=_normalize_optional_text(item.get("caution_note")),
            )
            for item in parsed["pages"]
        ]
        return CardImagePlan(
            issue_id=planning_input.candidate.issue_id,
            template_name=planning_input.draft.template_name,
            model_name=self.model_name,
            prompt_version=self.prompt_version,
            overall_art_direction=str(parsed["overall_art_direction"]),
            pages=pages,
            metadata={
                "engine_type": "gemini",
                "request": request_payload,
                "response": response_payload,
            },
            created_at=_utc_now(),
        )

    def _build_request_payload(self, planning_input: CardImagePlanningInput) -> dict:
        payload = asdict(planning_input)
        payload["candidate"]["published_at"] = _serialize_datetime(
            planning_input.candidate.published_at
        )
        payload["candidate"]["collected_at"] = _serialize_datetime(
            planning_input.candidate.collected_at
        )
        if planning_input.draft.created_at:
            payload["draft"]["created_at"] = _serialize_datetime(planning_input.draft.created_at)
        if planning_input.multimodal_analysis and planning_input.multimodal_analysis.created_at:
            payload["multimodal_analysis"]["created_at"] = _serialize_datetime(
                planning_input.multimodal_analysis.created_at
            )
        input_json = json.dumps(payload, ensure_ascii=False, indent=2)
        return {
            "system_instruction": {"parts": [{"text": IMAGE_PLANNING_SYSTEM_PROMPT}]},
            "contents": [
                {
                    "role": "user",
                    "parts": [{"text": build_image_planning_user_prompt(input_json)}],
                }
            ],
            "generationConfig": {
                "temperature": 0.3,
                "responseMimeType": "application/json",
                "responseSchema": {
                    "type": "OBJECT",
                    "properties": {
                        "overall_art_direction": {"type": "STRING"},
                        "pages": {
                            "type": "ARRAY",
                            "items": {
                                "type": "OBJECT",
                                "properties": {
                                    "page_number": {"type": "INTEGER"},
                                    "page_role": {"type": "STRING"},
                                    "render_strategy": {"type": "STRING"},
                                    "primary_asset_reference": {"type": "STRING"},
                                    "crop_focus": {"type": "STRING"},
                                    "overlay_style": {"type": "STRING"},
                                    "text_layout_hint": {"type": "STRING"},
                                    "generation_prompt": {"type": "STRING"},
                                    "edit_instructions": {"type": "STRING"},
                                    "caution_note": {"type": "STRING"},
                                },
                                "required": [
                                    "page_number",
                                    "page_role",
                                    "render_strategy",
                                    "primary_asset_reference",
                                    "crop_focus",
                                    "overlay_style",
                                    "text_layout_hint",
                                    "generation_prompt",
                                    "edit_instructions",
                                    "caution_note",
                                ],
                            },
                        },
                    },
                    "required": ["overall_art_direction", "pages"],
                },
            },
        }

    @staticmethod
    def _parse_response_payload(response_payload: dict) -> dict:
        try:
            text = response_payload["candidates"][0]["content"]["parts"][0]["text"]
        except (KeyError, IndexError, TypeError) as exc:
            raise RuntimeError(f"Unexpected Gemini response shape: {response_payload}") from exc

        try:
            parsed = json.loads(text)
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"Gemini response was not valid JSON: {text}") from exc

        required = {"overall_art_direction", "pages"}
        missing = sorted(required - set(parsed))
        if missing:
            raise RuntimeError(f"Gemini response missing fields: {', '.join(missing)}")
        if not isinstance(parsed["pages"], list) or not parsed["pages"]:
            raise RuntimeError("Gemini response pages must be a non-empty list")
        return parsed

    def _validate_response(self, planning_input: CardImagePlanningInput, parsed: dict) -> None:
        allowed_strategies = {"source_photo", "hybrid_overlay", "generated_background"}
        expected_pages = planning_input.draft.pages
        actual_pages = parsed["pages"]

        if len(actual_pages) != len(expected_pages):
            raise RuntimeError(
                "Gemini response violated page-count rule: "
                f"expected {len(expected_pages)} pages, got {len(actual_pages)}"
            )

        expected_refs: set[str] = set()
        for asset in planning_input.assets:
            if asset.asset_id:
                expected_refs.add(asset.asset_id)
            expected_refs.add(asset.origin_url)

        for expected, actual in zip(expected_pages, actual_pages):
            if int(actual.get("page_number")) != expected.page_number:
                raise RuntimeError(
                    "Gemini response violated page-number rule: "
                    f"expected {expected.page_number}, got {actual.get('page_number')}"
                )
            if str(actual.get("page_role")) != expected.page_role:
                raise RuntimeError(
                    "Gemini response violated page-role rule: "
                    f"expected {expected.page_role}, got {actual.get('page_role')}"
                )

            strategy = str(actual.get("render_strategy"))
            if strategy not in allowed_strategies:
                raise RuntimeError(
                    "Gemini response violated render-strategy rule: "
                    f"got {strategy}"
                )

            asset_reference = _normalize_optional_text(actual.get("primary_asset_reference"))
            if asset_reference and asset_reference not in expected_refs:
                raise RuntimeError(
                    "Gemini response violated asset-reference rule: "
                    f"got {asset_reference}"
                )


class CardImagePlanningService:
    def __init__(self, engine: CardImagePlanningEngine | None = None) -> None:
        self.engine = engine or build_default_card_image_planning_engine()

    def build_plan(self, planning_input: CardImagePlanningInput) -> CardImagePlan:
        return self.engine.build_plan(planning_input)


def build_default_card_image_planning_engine() -> CardImagePlanningEngine:
    load_default_env()
    if os.getenv("GEMINI_API_KEY"):
        return GeminiCardImagePlanningEngine()
    return HeuristicCardImagePlanningEngine()


def _serialize_datetime(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    else:
        value = value.astimezone(timezone.utc)
    return value.isoformat()


def _normalize_optional_text(value: object) -> str | None:
    if value is None:
        return None
    normalized = " ".join(str(value).split()).strip()
    return normalized or None


def _find_best_insight(
    planning_input: CardImagePlanningInput,
    page_role: str,
) -> AssetMultimodalInsight | None:
    analysis = planning_input.multimodal_analysis
    if analysis is None:
        return None

    role_preferences = {
        "cover": ["cover", "detail_b", "detail_a", "reaction"],
        "detail_a": ["detail_a", "cover", "detail_b", "reaction"],
        "detail_b": ["detail_b", "reaction", "cover", "detail_a"],
        "data_context": ["data_context", "cover", "detail_a"],
        "summary_cta": ["reaction", "cover", "detail_b", "detail_a"],
        "quick_info": ["quick_info", "cover", "detail_a"],
    }
    preferred_slots = role_preferences.get(page_role, [page_role, "cover", "detail_a"])

    for slot in preferred_slots:
        for insight in analysis.assets:
            if insight.usage_recommendation == slot:
                return insight
    return analysis.assets[0] if analysis.assets else None


def _resolve_effective_page_role(page_role: str, page_number: int) -> str:
    if page_role == "body":
        mapping = {
            2: "detail_a",
            3: "detail_b",
            4: "data_context",
        }
        return mapping.get(page_number, "detail_a")
    if page_role == "footer":
        return "summary_cta"
    return page_role


def _select_primary_asset_reference(planning_input, page, insight) -> str | None:
    if insight is not None and _is_known_asset_reference(planning_input, insight.asset_reference):
        return insight.asset_reference
    if page.asset_reference:
        return page.asset_reference
    if planning_input.assets:
        return planning_input.assets[0].asset_id or planning_input.assets[0].origin_url
    return None


def _is_known_asset_reference(planning_input: CardImagePlanningInput, reference: str | None) -> bool:
    if not reference:
        return False
    for asset in planning_input.assets:
        if reference == asset.asset_id or reference == asset.origin_url:
            return True
    return False


def _select_render_strategy(page_role: str, asset_reference: str | None) -> str:
    if page_role == "data_context":
        return "generated_background"
    if asset_reference is None:
        return "generated_background"
    if page_role in {"cover", "quick_info", "summary_cta"}:
        return "hybrid_overlay"
    return "source_photo"


def _build_crop_focus(page_role: str, insight: AssetMultimodalInsight | None, fallback: str) -> str:
    subject_hint = fallback
    if insight is not None and insight.scene_description:
        subject_hint = insight.scene_description
    manual_focus = _manual_guidance_value(insight, "crop_focus_note")
    avoid_region = _manual_guidance_value(insight, "avoid_region_note")

    if page_role in {"cover", "quick_info"}:
        base = f"핵심 피사체를 중앙 또는 약간 하단에 두고 상단 제목 안전영역을 확보. 기준 장면: {subject_hint}"
    elif page_role == "data_context":
        base = "복잡한 배경은 비우고 숫자 카드와 본문이 들어갈 중앙 안전영역을 넓게 확보."
    elif page_role == "summary_cta":
        base = f"과한 디테일보다는 분위기 전달 위주로 크롭. 기준 장면: {subject_hint}"
    else:
        base = f"설명 대상 장면이 잘 보이도록 인물/행동 중심으로 타이트하게 크롭. 기준 장면: {subject_hint}"
    extras: list[str] = []
    if manual_focus:
        extras.append(f"사용자 포커스 힌트: {manual_focus}")
    if avoid_region:
        extras.append(f"제외/회피 구역: {avoid_region}")
    if extras:
        return f"{base} {' / '.join(extras)}"
    return base


def _build_overlay_style(page_role: str, render_strategy: str) -> str:
    if render_strategy == "generated_background":
        return "구단 컬러 기반의 그라데이션 배경과 얕은 텍스처를 사용"
    if page_role in {"cover", "quick_info"}:
        return "상단 또는 좌측에 짙은 그라데이션 오버레이를 적용해 제목 대비를 확보"
    if page_role == "summary_cta":
        return "하단 반투명 오버레이로 CTA 문구 대비를 확보"
    return "필요 시 본문 뒤에만 부분 오버레이를 적용해 사진 현장감을 최대한 유지"


def _build_text_layout_hint(page_role: str, insight: AssetMultimodalInsight | None = None) -> str:
    mapping = {
        "cover": "상단 타이틀 2줄, 하단 보조문구 1줄, 중앙 피사체 보호",
        "detail_a": "상단 소제목, 좌하단 또는 우하단 본문 3~5줄",
        "detail_b": "상단 소제목, 반응 블록과 본문이 겹치지 않게 측면 여백 확보",
        "data_context": "중앙 숫자 블록 1~3개와 보조 설명이 들어갈 카드형 레이아웃",
        "summary_cta": "중앙 또는 하단에 한 줄 요약과 CTA 문구 배치",
        "quick_info": "상단 핵심 제목, 중앙 굵은 수치 또는 상태, 하단 보조 설명",
    }
    base = mapping.get(page_role, "제목과 본문이 사진 핵심 피사체를 가리지 않게 배치")
    layout_hint = _manual_guidance_value(insight, "layout_focus_hint")
    if layout_hint:
        return f"{base} / 사용자 레이아웃 힌트: {layout_hint}"
    return base


def _build_generation_prompt(
    *,
    planning_input: CardImagePlanningInput,
    page,
    effective_page_role: str,
    render_strategy: str,
    insight: AssetMultimodalInsight | None,
) -> str:
    team_code = str(planning_input.draft.metadata.get("team_code", "KBO")).strip() or "KBO"
    insight_hint = insight.scene_description if insight is not None else page.image_prompt
    if render_strategy == "generated_background":
        return (
            f"{team_code} baseball card-news background, {effective_page_role}, {page.image_prompt}, "
            f"{insight_hint}, clean editorial square layout, strong text-safe area, no readable text"
        )
    return (
        f"{team_code} sports card-news image treatment, {effective_page_role}, {page.image_prompt}, "
        f"preserve source-photo realism, emphasize {insight_hint}, square composition"
    )


def _build_edit_instructions(
    *,
    page_role: str,
    render_strategy: str,
    insight: AssetMultimodalInsight | None,
) -> str:
    base = {
        "source_photo": "원본 사진의 장면성을 유지하고 색보정과 크롭만 최소 범위로 적용",
        "hybrid_overlay": "원본 사진 위에 텍스트 가독성 확보용 오버레이와 배경 보정을 함께 적용",
        "generated_background": "실사 단정 대신 상징적 야구 배경을 만들고 정보 카드가 잘 읽히게 단순화",
    }[render_strategy]
    if insight is None:
        return base
    extras: list[str] = [f"멀티모달 해석 포인트: {insight.humor_point}"]
    manual_focus = _manual_guidance_value(insight, "crop_focus_note")
    avoid_region = _manual_guidance_value(insight, "avoid_region_note")
    layout_hint = _manual_guidance_value(insight, "layout_focus_hint")
    if manual_focus:
        extras.append(f"사용자 crop 지시: {manual_focus}")
    if avoid_region:
        extras.append(f"피해야 할 구역: {avoid_region}")
    if layout_hint:
        extras.append(f"텍스트/레이아웃 지시: {layout_hint}")
    return f"{base}. {' '.join(extras)}"


def _manual_guidance_value(insight: AssetMultimodalInsight | None, key: str) -> str:
    if insight is None:
        return ""
    value = insight.analysis_payload.get(key) if isinstance(insight.analysis_payload, dict) else None
    return " ".join(str(value or "").split()).strip()


def _combine_cautions(page_role: str, insight: AssetMultimodalInsight | None) -> str | None:
    notes: list[str] = []
    if insight is not None and insight.caution_note:
        notes.append(insight.caution_note)
    if page_role == "data_context":
        notes.append("수치 페이지는 실제 데이터 텍스트를 렌더 단계에서 별도 삽입하고 배경에는 숫자를 박아 넣지 않는 편이 안전하다")
    if page_role == "cover":
        notes.append("제목과 피사체 얼굴 또는 공이 겹치지 않도록 안전영역을 먼저 확보")
    if not notes:
        return None
    return "; ".join(notes)
