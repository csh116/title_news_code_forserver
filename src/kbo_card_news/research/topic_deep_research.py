from __future__ import annotations

import json
import os
import re
from dataclasses import asdict
from datetime import datetime, timezone
from typing import Protocol

from kbo_card_news.config.env import load_default_env
from kbo_card_news.models.issue import TopicDeepResearchInput, TopicDeepResearchResult
from kbo_card_news.runtime.model_fallback import build_model_fallback_policy, call_with_fallback
from kbo_card_news.scoring.engine import HttpTransport, UrllibHttpTransport


TOPIC_DEEP_RESEARCH_JSON_SCHEMA = {
    "type": "object",
    "properties": {
        "angle_summary": {"type": "string"},
        "key_points": {
            "type": "array",
            "items": {"type": "string"},
        },
        "timeline": {
            "type": "array",
            "items": {"type": "string"},
        },
        "notable_numbers": {
            "type": "array",
            "items": {"type": "string"},
        },
        "risk_flags": {
            "type": "array",
            "items": {"type": "string"},
        },
        "recommended_focus": {"type": "string"},
    },
    "required": [
        "angle_summary",
        "key_points",
        "timeline",
        "notable_numbers",
        "risk_flags",
        "recommended_focus",
    ],
    "additionalProperties": False,
}


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


class TopicDeepResearchEngine(Protocol):
    def research_topic(self, topic_input: TopicDeepResearchInput) -> TopicDeepResearchResult:
        ...


class HeuristicTopicDeepResearchEngine:
    def __init__(
        self,
        *,
        model_name: str = "heuristic-topic-deep-research-v1",
        prompt_version: str = "phase3.deep_research.v1",
    ) -> None:
        self.model_name = model_name
        self.prompt_version = prompt_version

    def research_topic(self, topic_input: TopicDeepResearchInput) -> TopicDeepResearchResult:
        articles = topic_input.articles
        top_article = articles[0] if articles else None
        source_article_ids = [article.article_id for article in articles]
        source_asset_ids = [asset.asset_id for asset in topic_input.assets if asset.asset_id]
        key_points = []
        timeline = []
        notable_numbers = []

        for article in articles[:3]:
            key_points.append(_limit_text(article.title, 120))
            if article.published_at:
                timeline.append(
                    f"{article.published_at.astimezone(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')} {article.title}"
                )
            notable_numbers.extend(_extract_number_phrases(article.excerpt_text or article.body_text or article.title))

        angle_summary = _limit_text(
            " / ".join(point for point in key_points[:2] if point) or topic_input.topic.topic_name,
            220,
        )
        risk_flags = self._build_risk_flags(topic_input)
        recommended_focus = (
            "기사 간 공통 사실과 경기 맥락을 중심으로 카드뉴스 2~4페이지를 설계"
            if len(articles) >= 2
            else "대표 기사 1건 기준으로 핵심 사실과 숫자를 먼저 고정"
        )

        return TopicDeepResearchResult(
            batch_id=topic_input.batch_id,
            topic_id=topic_input.topic.topic_id,
            topic_name=topic_input.topic.topic_name,
            model_name=self.model_name,
            prompt_version=self.prompt_version,
            representative_article_id=topic_input.topic.representative_article_id or (top_article.article_id if top_article else None),
            angle_summary=angle_summary,
            key_points=key_points or [topic_input.topic.reason_summary],
            timeline=timeline,
            notable_numbers=notable_numbers[:6],
            source_article_ids=source_article_ids,
            source_asset_ids=source_asset_ids,
            risk_flags=risk_flags,
            recommended_focus=recommended_focus,
            raw_payload={
                "engine_type": "heuristic",
                "article_count": len(articles),
                "asset_count": len(topic_input.assets),
            },
            metadata={
                "topic_score": topic_input.topic.topic_score,
                "topic_rank": topic_input.topic.importance_rank,
            },
            created_at=_utc_now(),
        )

    @staticmethod
    def _build_risk_flags(topic_input: TopicDeepResearchInput) -> list[str]:
        flags: list[str] = []
        if len(topic_input.articles) <= 1:
            flags.append("단일 기사 기반 토픽")
        if any(str(article.metadata.get("league_tier") or "") == "futures" for article in topic_input.articles):
            flags.append("퓨처스/2군 기사 포함")
        if any(str(article.metadata.get("article_kind") or "") in {"standings", "results_summary", "probable_starters"} for article in topic_input.articles):
            flags.append("범용 기사 포함")
        return flags


class GeminiTopicDeepResearchEngine:
    def __init__(
        self,
        *,
        api_key: str | None = None,
        model_name: str | None = None,
        prompt_version: str = "phase3.deep_research.v1",
        max_attempts: int = 3,
        retry_delay_seconds: float = 1.0,
        transport: HttpTransport | None = None,
        endpoint_base: str = "https://generativelanguage.googleapis.com/v1beta/models",
    ) -> None:
        load_default_env()
        self.api_key = api_key or os.getenv("GEMINI_API_KEY")
        self.model_name = (
            model_name
            or os.getenv("GEMINI_DEEP_RESEARCH_MODEL")
            or "gemini-2.5-flash"
        )
        self.prompt_version = prompt_version
        self.transport = transport or UrllibHttpTransport()
        self.endpoint_base = endpoint_base.rstrip("/")
        self.openai_api_key = os.getenv("OPENAI_API_KEY")
        self.model_policy = build_model_fallback_policy(self.model_name)
        self.max_attempts = max(1, max_attempts)
        self.retry_delay_seconds = max(0.0, retry_delay_seconds)

    def research_topic(self, topic_input: TopicDeepResearchInput) -> TopicDeepResearchResult:
        call_result = call_with_fallback(
            self.model_policy,
            topic_input,
            prompt_builder=build_prompt,
            schema_name="topic_deep_research",
            json_schema=TOPIC_DEEP_RESEARCH_JSON_SCHEMA,
            transport=self.transport,
            gemini_api_key=self.api_key,
            openai_api_key=self.openai_api_key,
            gemini_endpoint_base=self.endpoint_base,
            max_attempts_per_model=self.max_attempts,
            retry_delay_seconds=self.retry_delay_seconds,
        )
        return self._parse_result(
            parsed=call_result.parsed,
            prompt=call_result.prompt,
            response_text=call_result.response_text,
            model_name=call_result.model_name,
            provider=call_result.provider,
            topic_input=topic_input,
        )

    def _parse_result(
        self,
        *,
        parsed: dict,
        prompt: str,
        response_text: str,
        model_name: str,
        provider: str,
        topic_input: TopicDeepResearchInput,
    ) -> TopicDeepResearchResult:
        return TopicDeepResearchResult(
            batch_id=topic_input.batch_id,
            topic_id=topic_input.topic.topic_id,
            topic_name=topic_input.topic.topic_name,
            model_name=model_name,
            prompt_version=self.prompt_version,
            representative_article_id=topic_input.topic.representative_article_id,
            angle_summary=str(parsed.get("angle_summary", "")).strip(),
            key_points=[str(item) for item in parsed.get("key_points", [])],
            timeline=[str(item) for item in parsed.get("timeline", [])],
            notable_numbers=[str(item) for item in parsed.get("notable_numbers", [])],
            source_article_ids=[article.article_id for article in topic_input.articles],
            source_asset_ids=[asset.asset_id for asset in topic_input.assets if asset.asset_id],
            risk_flags=[str(item) for item in parsed.get("risk_flags", [])],
            recommended_focus=str(parsed.get("recommended_focus", "")).strip(),
            raw_payload={
                "prompt": prompt,
                "response_text": response_text,
                "engine_type": provider,
            },
            metadata={
                "topic_score": topic_input.topic.topic_score,
                "topic_rank": topic_input.topic.importance_rank,
                "article_count": len(topic_input.articles),
                "asset_count": len(topic_input.assets),
            },
            created_at=_utc_now(),
        )


def build_prompt(topic_input: TopicDeepResearchInput) -> str:
    research_payload = json.dumps(
        {
            "batch_id": topic_input.batch_id,
            "topic": {
                **asdict(topic_input.topic),
                "importance_rank": topic_input.topic.importance_rank,
            },
            "articles": [
                {
                    **asdict(article),
                    "published_at": _serialize_datetime(article.published_at),
                    "collected_at": _serialize_datetime(article.collected_at),
                    "body_text": _limit_text(article.body_text or "", 4000),
                    "excerpt_text": _limit_text(article.excerpt_text or "", 800),
                }
                for article in topic_input.articles
            ],
            "assets": [asdict(asset) for asset in topic_input.assets[:8]],
            "metadata": topic_input.metadata,
        },
        ensure_ascii=False,
        indent=2,
    )
    return (
        "You are performing second-stage deep research for a KBO card-news pipeline. "
        "Use the selected topic and the linked full article texts to build a research brief for editorial production. "
        "반드시 JSON만 출력. Write every user-facing field in Korean.\n\n"
        "다음은 1차 선정이 끝난 KBO 토픽의 2차 심층 리서치 입력이다.\n"
        "해야 할 일:\n"
        "1. 기사 원문과 발췌문을 읽고 핵심 각도를 2~3문장으로 정리\n"
        "2. 카드뉴스 기획에 바로 쓸 핵심 포인트 3~5개 정리\n"
        "3. 시간 흐름이나 경기 전개가 보이면 timeline 정리\n"
        "4. 기사에 나온 주요 숫자/기록을 notable_numbers 로 정리\n"
        "5. 과장/추정/범용 기사 의존 같은 리스크를 risk_flags 에 적기\n"
        "6. 다음 단계에서 어떤 포인트를 강조해야 하는지 recommended_focus 작성\n"
        "7. 반드시 JSON만 출력\n\n"
        "입력 JSON:\n"
        f"{research_payload}"
    )


class TopicDeepResearchService:
    def __init__(self, engine: TopicDeepResearchEngine | None = None) -> None:
        self.engine = engine or build_default_topic_deep_research_engine()

    def research_topic(self, topic_input: TopicDeepResearchInput) -> TopicDeepResearchResult:
        return self.engine.research_topic(topic_input)


def build_default_topic_deep_research_engine() -> TopicDeepResearchEngine:
    load_default_env()
    if os.getenv("GEMINI_API_KEY"):
        return GeminiTopicDeepResearchEngine()
    return HeuristicTopicDeepResearchEngine()


def _serialize_datetime(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc).isoformat()
    return value.astimezone(timezone.utc).isoformat()


def _extract_number_phrases(text: str) -> list[str]:
    if not text:
        return []
    matches = re.findall(r"[0-9]+(?:\.[0-9]+)?[%이닝안타홈런타점승패점개명차회]?", text)
    deduped: list[str] = []
    for match in matches:
        if match not in deduped:
            deduped.append(match)
    return deduped[:8]


def _limit_text(text: str, limit: int) -> str:
    compact = " ".join(text.split()).strip()
    if len(compact) <= limit:
        return compact
    return compact[: max(0, limit - 1)].rstrip() + "…"
