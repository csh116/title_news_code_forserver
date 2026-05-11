from __future__ import annotations

import json
import os
import re
import urllib.request
from dataclasses import asdict
from datetime import datetime, timezone
from typing import Protocol

from kbo_card_news.config.env import load_default_env
from kbo_card_news.models.issue import (
    CardNewsDraft,
    CardNewsPageDraft,
    IssueStructuringInput,
)
from kbo_card_news.runtime.model_fallback import build_model_fallback_policy, call_with_fallback
from kbo_card_news.structuring.prompts import (
    STRUCTURING_SYSTEM_PROMPT,
    build_structuring_user_prompt,
)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


class StructuringEngine(Protocol):
    def build_draft(self, structuring_input: IssueStructuringInput) -> CardNewsDraft:
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


class HeuristicCardNewsStructuringEngine:
    def __init__(
        self,
        *,
        prompt_version: str = "phase3.2",
    ) -> None:
        self.prompt_version = prompt_version

    def build_draft(self, structuring_input: IssueStructuringInput) -> CardNewsDraft:
        candidate = structuring_input.candidate
        score = structuring_input.score
        template_name = self._select_template(structuring_input)
        team_code = str(candidate.metadata.get("team_code", "")).strip() or "KBO"
        visual_hint = self._build_visual_hint(structuring_input)
        pages = (
            self._build_compact_pages(structuring_input, team_code, visual_hint)
            if template_name == "compact_2p"
            else self._build_standard_pages(structuring_input, team_code, visual_hint)
        )
        subtitle = self._build_subtitle(candidate, score)
        planning_notes = (
            f"template={template_name}; total_score={score.total_score}; "
            f"should_publish={score.should_publish}; source_type={candidate.source_type}"
        )
        return CardNewsDraft(
            issue_id=candidate.issue_id,
            template_name=template_name,
            title=_limit_text(candidate.title, 34),
            subtitle=subtitle,
            pages=pages,
            prompt_version=self.prompt_version,
            source_asset_count=len(structuring_input.assets),
            planning_notes=planning_notes,
            metadata={
                "team_code": team_code,
                "source_type": candidate.source_type,
                "score_total": score.total_score,
                "score_reason_summary": score.reason_summary,
            },
            created_at=_utc_now(),
        )

    def _select_template(self, structuring_input: IssueStructuringInput) -> str:
        candidate = structuring_input.candidate
        issue_category = str(candidate.metadata.get("issue_category", "")).lower()
        if candidate.source_type == "kma_weather" or issue_category in {"weather", "breaking"}:
            return "compact_2p"
        return "standard_5p"

    def _build_standard_pages(
        self,
        structuring_input: IssueStructuringInput,
        team_code: str,
        visual_hint: str,
    ) -> list[CardNewsPageDraft]:
        candidate = structuring_input.candidate
        score = structuring_input.score
        key_numbers = _extract_number_phrases(f"{candidate.title} {candidate.summary}")
        score_line = (
            f"AI 평가 {int(round(score.total_score))}점, "
            f"{'발행 추천' if score.should_publish else '보류 권장'} 이슈."
        )
        detail_text = _sentences_from_summary(candidate.summary, fallback=candidate.title)
        context_line = self._build_context_line(candidate)

        return [
            CardNewsPageDraft(
                page_number=1,
                page_role="cover",
                headline=_limit_text(candidate.title, 28),
                body=_limit_text(self._build_subtitle(candidate, score), 48),
                image_prompt=(
                    f"{team_code} card-news cover, {visual_hint}, dramatic sports editorial style, "
                    "square composition, bold headline space, high energy stadium atmosphere"
                ),
                asset_reference=self._first_asset_reference(structuring_input),
            ),
            CardNewsPageDraft(
                page_number=2,
                page_role="detail_a",
                headline="무슨 장면이었나",
                body=_limit_text(detail_text, 110),
                image_prompt=(
                    f"{team_code} in-game detail scene, {visual_hint}, "
                    "player action or dugout reaction, editorial photo treatment"
                ),
                asset_reference=self._first_asset_reference(structuring_input),
            ),
            CardNewsPageDraft(
                page_number=3,
                page_role="detail_b",
                headline="왜 더 화제가 됐나",
                body=_limit_text(
                    f"{context_line} 반응 지표는 좋아요 {candidate.engagement_like_count}개, "
                    f"댓글 {candidate.engagement_comment_count}개 기준으로 정리했다.",
                    110,
                ),
                image_prompt=(
                    f"{team_code} fan reaction collage, {visual_hint}, "
                    "crowd energy, social buzz, sports card-news visual"
                ),
                asset_reference=self._second_asset_reference(structuring_input),
            ),
            CardNewsPageDraft(
                page_number=4,
                page_role="data_context",
                headline="숫자로 보는 포인트",
                body=_limit_text(
                    f"{score_line} 핵심 수치: {key_numbers or '기사 본문과 점수 요약 중심 정리'}",
                    110,
                ),
                image_prompt=(
                    f"{team_code} infographic sports card, {visual_hint}, "
                    "clean stat blocks, scoreboard motif, editorial layout"
                ),
            ),
            CardNewsPageDraft(
                page_number=5,
                page_role="summary_cta",
                headline="한 줄 정리",
                body=_limit_text(
                    f"{_limit_text(candidate.summary or candidate.title, 58)} "
                    "여러분은 오늘 경기 포인트를 어떻게 봤나요?",
                    110,
                ),
                image_prompt=(
                    f"{team_code} closing card, {visual_hint}, "
                    "clean brand finish, subtle stadium background, CTA friendly composition"
                ),
            ),
        ]

    def _build_compact_pages(
        self,
        structuring_input: IssueStructuringInput,
        team_code: str,
        visual_hint: str,
    ) -> list[CardNewsPageDraft]:
        candidate = structuring_input.candidate
        score = structuring_input.score
        return [
            CardNewsPageDraft(
                page_number=1,
                page_role="quick_info",
                headline=_limit_text(candidate.title, 28),
                body=_limit_text(
                    f"{candidate.summary} AI 평가 {int(round(score.total_score))}점 기준으로 핵심만 빠르게 정리.",
                    100,
                ),
                image_prompt=(
                    f"{team_code} quick update sports card, {visual_hint}, "
                    "minimal layout, bold key fact, instant update style"
                ),
                asset_reference=self._first_asset_reference(structuring_input),
            ),
            CardNewsPageDraft(
                page_number=2,
                page_role="summary_cta",
                headline="체크 포인트",
                body=_limit_text(
                    f"{self._build_context_line(candidate)} 여러분이 가장 먼저 확인한 포인트는 무엇인가요?",
                    100,
                ),
                image_prompt=(
                    f"{team_code} concise summary sports card, {visual_hint}, "
                    "clean CTA composition, mobile-first readability"
                ),
            ),
        ]

    def _build_visual_hint(self, structuring_input: IssueStructuringInput) -> str:
        if not structuring_input.assets:
            return "no source photo available, use symbolic baseball scene"
        asset = sorted(structuring_input.assets, key=lambda item: item.sort_order)[0]
        if asset.caption:
            return _limit_text(asset.caption, 80)
        if asset.asset_type == "image":
            return "reference the collected article photo"
        return f"reference the collected {asset.asset_type} asset"

    def _build_subtitle(self, candidate, score) -> str:
        freshness = "방금 올라온 이슈" if score.timeliness_score >= 80 else "맥락 정리가 필요한 이슈"
        return f"{freshness} · {candidate.source_type} 기준"

    def _build_context_line(self, candidate) -> str:
        if candidate.source_type == "kbo_stats":
            return "공식 기록 기반이라 수치 설명에 힘을 실을 수 있다."
        if candidate.source_type == "kma_weather":
            return "날씨형 이슈라 현장 컨디션 전달이 핵심이다."
        if candidate.source_type == "dcinside":
            return "커뮤니티 반응형 이슈라 표현은 보수적으로 정리한다."
        return "기사 핵심과 현장 반응을 함께 엮는 흐름이 적합하다."

    @staticmethod
    def _first_asset_reference(structuring_input: IssueStructuringInput) -> str | None:
        if not structuring_input.assets:
            return None
        asset = sorted(structuring_input.assets, key=lambda item: item.sort_order)[0]
        return asset.origin_url

    @staticmethod
    def _second_asset_reference(structuring_input: IssueStructuringInput) -> str | None:
        if len(structuring_input.assets) < 2:
            return HeuristicCardNewsStructuringEngine._first_asset_reference(structuring_input)
        asset = sorted(structuring_input.assets, key=lambda item: item.sort_order)[1]
        return asset.origin_url


class GeminiCardNewsStructuringEngine:
    def __init__(
        self,
        *,
        api_key: str | None = None,
        model_name: str = "gemini-2.5-flash-lite",
        prompt_version: str = "phase3.2",
        max_attempts: int = 3,
        retry_delay_seconds: float = 1.0,
        transport: HttpTransport | None = None,
        endpoint_base: str = "https://generativelanguage.googleapis.com/v1beta/models",
    ) -> None:
        load_default_env()
        self.api_key = api_key or os.getenv("GEMINI_API_KEY")
        self.model_name = model_name
        self.prompt_version = prompt_version
        self.transport = transport or UrllibHttpTransport()
        self.endpoint_base = endpoint_base.rstrip("/")
        self.openai_api_key = os.getenv("OPENAI_API_KEY")
        self.model_policy = build_model_fallback_policy(self.model_name)
        self.max_attempts = max(1, max_attempts)
        self.retry_delay_seconds = max(0.0, retry_delay_seconds)

    def build_draft(self, structuring_input: IssueStructuringInput) -> CardNewsDraft:
        call_result = call_with_fallback(
            self.model_policy,
            structuring_input,
            prompt_builder=build_prompt,
            schema_name="card_news_structuring",
            json_schema=CARD_NEWS_STRUCTURING_JSON_SCHEMA,
            transport=self.transport,
            gemini_api_key=self.api_key,
            openai_api_key=self.openai_api_key,
            gemini_endpoint_base=self.endpoint_base,
            validator=lambda parsed: self._validate_template_rules(structuring_input, parsed),
            max_attempts_per_model=self.max_attempts,
            retry_delay_seconds=self.retry_delay_seconds,
        )

        pages = [
            CardNewsPageDraft(
                page_number=int(page["page_number"]),
                page_role=str(page["page_role"]),
                headline=str(page["headline"]),
                body=str(page["body"]),
                image_prompt=str(page["image_prompt"]),
                asset_reference=page.get("asset_reference"),
            )
            for page in call_result.parsed["pages"]
        ]
        return CardNewsDraft(
            issue_id=structuring_input.candidate.issue_id,
            template_name=str(call_result.parsed["template_name"]),
            title=str(call_result.parsed["title"]),
            subtitle=str(call_result.parsed["subtitle"]),
            pages=pages,
            prompt_version=self.prompt_version,
            source_asset_count=len(structuring_input.assets),
            planning_notes=str(call_result.parsed["planning_notes"]),
            metadata={
                "engine_type": call_result.provider,
                "prompt": call_result.prompt,
                "response_text": call_result.response_text,
            },
            created_at=_utc_now(),
        )

    def _validate_template_rules(self, structuring_input: IssueStructuringInput, parsed: dict) -> None:
        expected_template = _expected_template_name(structuring_input)
        actual_template = str(parsed["template_name"])
        page_count = len(parsed["pages"])
        expected_page_count = 2 if expected_template == "compact_2p" else 5

        if actual_template != expected_template:
            raise RuntimeError(
                "Gemini response violated template rule: "
                f"expected {expected_template}, got {actual_template}"
            )
        if page_count != expected_page_count:
            raise RuntimeError(
                "Gemini response violated page-count rule: "
                f"template {actual_template} requires {expected_page_count} pages, got {page_count}"
            )


CARD_NEWS_STRUCTURING_JSON_SCHEMA = {
    "type": "object",
    "properties": {
        "template_name": {"type": "string"},
        "title": {"type": "string"},
        "subtitle": {"type": "string"},
        "planning_notes": {"type": "string"},
        "pages": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "page_number": {"type": "integer"},
                    "page_role": {"type": "string"},
                    "headline": {"type": "string"},
                    "body": {"type": "string"},
                    "image_prompt": {"type": "string"},
                    "asset_reference": {"type": "string"},
                },
                "required": [
                    "page_number",
                    "page_role",
                    "headline",
                    "body",
                    "image_prompt",
                    "asset_reference",
                ],
                "additionalProperties": False,
            },
        },
    },
    "required": [
        "template_name",
        "title",
        "subtitle",
        "planning_notes",
        "pages",
    ],
    "additionalProperties": False,
}


def build_prompt(structuring_input: IssueStructuringInput) -> str:
    payload = asdict(structuring_input)
    payload["candidate"]["published_at"] = _serialize_datetime(
        structuring_input.candidate.published_at
    )
    payload["candidate"]["collected_at"] = _serialize_datetime(
        structuring_input.candidate.collected_at
    )
    payload["score"]["created_at"] = _serialize_datetime(structuring_input.score.created_at)
    input_json = json.dumps(payload, ensure_ascii=False, indent=2)
    expected_template_name = _expected_template_name(structuring_input)
    expected_page_roles = ", ".join(_expected_page_roles(expected_template_name))
    user_prompt = build_structuring_user_prompt(
        input_json,
        expected_template_name=expected_template_name,
        expected_page_roles=expected_page_roles,
    )
    return (
        f"{STRUCTURING_SYSTEM_PROMPT}\n\n"
        f"{user_prompt}\n\n"
        "반드시 JSON만 출력."
    )


class IssueStructuringService:
    def __init__(self, engine: StructuringEngine | None = None) -> None:
        self.engine = engine or build_default_structuring_engine()

    def build_draft(self, structuring_input: IssueStructuringInput) -> CardNewsDraft:
        return self.engine.build_draft(structuring_input)

    def build_drafts(
        self,
        structuring_inputs: list[IssueStructuringInput],
    ) -> list[CardNewsDraft]:
        return [self.build_draft(item) for item in structuring_inputs]


def build_default_structuring_engine() -> StructuringEngine:
    load_default_env()
    if os.getenv("GEMINI_API_KEY"):
        return GeminiCardNewsStructuringEngine()
    return HeuristicCardNewsStructuringEngine()


def _expected_template_name(structuring_input: IssueStructuringInput) -> str:
    candidate = structuring_input.candidate
    issue_category = str(candidate.metadata.get("issue_category", "")).lower()
    if candidate.source_type == "kma_weather" or issue_category in {"weather", "breaking"}:
        return "compact_2p"
    return "standard_5p"


def _expected_page_roles(template_name: str) -> list[str]:
    if template_name == "compact_2p":
        return ["quick_info", "summary_cta"]
    return ["cover", "detail_a", "detail_b", "data_context", "summary_cta"]


def _serialize_datetime(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    else:
        value = value.astimezone(timezone.utc)
    return value.isoformat()


def _sentences_from_summary(summary: str, *, fallback: str) -> str:
    cleaned = " ".join(summary.split()).strip()
    if not cleaned:
        return fallback
    return cleaned


def _extract_number_phrases(text: str) -> str:
    matches = re.findall(r"\d+[^\s,]*", text)
    unique_matches: list[str] = []
    for match in matches:
        if match not in unique_matches:
            unique_matches.append(match)
    return ", ".join(unique_matches[:4])


def _limit_text(value: str, limit: int) -> str:
    compact = " ".join(value.split()).strip()
    if len(compact) <= limit:
        return compact
    return compact[: max(0, limit - 1)].rstrip() + "…"
