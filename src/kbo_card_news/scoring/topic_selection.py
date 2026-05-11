from __future__ import annotations

import json
import math
import os
import re
from dataclasses import asdict
from datetime import datetime, timezone
from typing import Any, Protocol

from kbo_card_news.config.env import load_default_env
from kbo_card_news.models.issue import (
    BatchArticleCandidate,
    BatchIssueSelectionInput,
    BatchIssueSelectionResult,
    TopicCandidate,
)
from kbo_card_news.runtime.model_fallback import build_model_fallback_policy, call_with_fallback
from kbo_card_news.scoring.engine import HttpTransport, UrllibHttpTransport


TOPIC_SELECTION_JSON_SCHEMA = {
    "type": "object",
    "properties": {
        "topics": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "topic_name": {"type": "string"},
                    "importance_rank": {"type": "integer"},
                    "topic_score": {"type": "number"},
                    "reason_summary": {"type": "string"},
                    "article_ids": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                    "representative_article_id": {"type": "string"},
                },
                "required": [
                    "topic_name",
                    "importance_rank",
                    "topic_score",
                    "reason_summary",
                    "article_ids",
                    "representative_article_id",
                ],
                "additionalProperties": False,
            },
        }
    },
    "required": ["topics"],
    "additionalProperties": False,
}


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


class BatchIssueSelectionEngine(Protocol):
    def select_topics(self, batch_input: BatchIssueSelectionInput) -> BatchIssueSelectionResult:
        ...


class HeuristicBatchIssueSelectionEngine:
    def __init__(
        self,
        *,
        model_name: str = "heuristic-topic-selector-v1",
        prompt_version: str = "phase3.batch.v1",
        top_k: int = 5,
    ) -> None:
        self.model_name = model_name
        self.prompt_version = prompt_version
        self.top_k = top_k

    def select_topics(self, batch_input: BatchIssueSelectionInput) -> BatchIssueSelectionResult:
        ranked_articles = sorted(
            batch_input.articles,
            key=self._article_priority,
            reverse=True,
        )
        clusters: list[dict] = []
        for article in ranked_articles:
            keywords = self._extract_keywords(article)
            assigned_cluster = None
            best_similarity = 0.0
            for cluster in clusters:
                similarity = self._article_similarity(
                    article_keywords=keywords,
                    cluster_keywords=cluster["keywords"],
                    same_team=self._team_code(article) == cluster["team_code"],
                )
                same_team = self._team_code(article) == cluster["team_code"]
                if similarity >= 2.0 or (same_team and similarity >= 1.0):
                    if similarity > best_similarity:
                        best_similarity = similarity
                        assigned_cluster = cluster
            if assigned_cluster is None:
                assigned_cluster = {
                    "articles": [],
                    "keywords": set(),
                    "team_code": self._team_code(article),
                }
                clusters.append(assigned_cluster)
            assigned_cluster["articles"].append(article)
            assigned_cluster["keywords"].update(keywords[:6])

        topics: list[TopicCandidate] = []
        for cluster in clusters:
            cluster_articles = sorted(
                cluster["articles"],
                key=self._article_priority,
                reverse=True,
            )
            representative = cluster_articles[0]
            unique_sources = sorted({article.source_type for article in cluster_articles})
            keywords = [keyword for keyword in cluster["keywords"] if keyword not in _GENERIC_TOPIC_WORDS]
            topic_score = self._cluster_score(cluster_articles, source_count=len(unique_sources))
            reasons = [
                f"article_count={len(cluster_articles)}",
                f"source_diversity={len(unique_sources)}",
            ]
            if keywords:
                reasons.append(f"keywords={', '.join(keywords[:4])}")
            topics.append(
                TopicCandidate(
                    topic_id=f"{batch_input.batch_id}:{representative.article_id}",
                    topic_name=representative.title,
                    importance_rank=0,
                    topic_score=round(topic_score, 2),
                    reason_summary="; ".join(reasons),
                    article_ids=[article.article_id for article in cluster_articles],
                    representative_article_id=representative.article_id,
                    metadata={
                        "keywords": keywords[:6],
                        "source_types": unique_sources,
                        "team_code": cluster["team_code"],
                    },
                )
            )

        topics.sort(
            key=lambda topic: (
                self._topic_priority(topic, article_lookup=self._article_lookup(batch_input.articles)),
                topic.topic_score,
                len(topic.article_ids),
            ),
            reverse=True,
        )
        limited_topics = self._fill_to_top_k(
            topics,
            batch_input=batch_input,
            existing_article_ids={article_id for topic in topics for article_id in topic.article_ids},
        )[: self.top_k]
        for index, topic in enumerate(limited_topics, start=1):
            topic.importance_rank = index

        return BatchIssueSelectionResult(
            batch_id=batch_input.batch_id,
            model_name=self.model_name,
            prompt_version=self.prompt_version,
            topics=limited_topics,
            raw_payload={
                "engine_type": "heuristic",
                "article_count": len(batch_input.articles),
                "cluster_count": len(clusters),
            },
            created_at=_utc_now(),
        )

    def _article_priority(self, article: BatchArticleCandidate) -> float:
        reference_at = article.published_at or article.collected_at
        age_hours = max(0.0, (_utc_now() - _normalize_datetime(reference_at)).total_seconds() / 3600)
        recency_score = max(0.0, 72.0 - age_hours * 3.0)
        view_score = min(12.0, math.log10(max(article.engagement_view_count, 0) + 1) * 4.0)
        keyword_bonus = min(len(self._extract_keywords(article)), 6) * 1.5
        return recency_score + view_score + keyword_bonus

    def _cluster_score(self, articles: list[BatchArticleCandidate], *, source_count: int) -> float:
        article_count = len(articles)
        recency = max(self._article_priority(article) for article in articles)
        view_signal = max(article.engagement_view_count for article in articles) if articles else 0
        view_score = min(10.0, math.log10(view_signal + 1) * 3.0)
        return article_count * 18.0 + source_count * 8.0 + recency + view_score

    def _extract_keywords(self, article: BatchArticleCandidate) -> list[str]:
        text = " ".join(
            part for part in [article.title, article.excerpt_text or ""] if part
        ).lower()
        tokens = re.findall(r"[0-9a-zA-Z가-힣]{2,}", text)
        weighted: dict[str, float] = {}
        title_tokens = re.findall(r"[0-9a-zA-Z가-힣]{2,}", article.title.lower())
        excerpt_tokens = re.findall(r"[0-9a-zA-Z가-힣]{2,}", (article.excerpt_text or "").lower())

        for token in title_tokens:
            normalized = _normalize_token(token)
            if not normalized or normalized in _STOPWORDS:
                continue
            weighted[normalized] = weighted.get(normalized, 0.0) + 2.2
        for token in excerpt_tokens:
            normalized = _normalize_token(token)
            if not normalized or normalized in _STOPWORDS:
                continue
            weighted[normalized] = weighted.get(normalized, 0.0) + 1.0

        if not weighted:
            for token in tokens:
                normalized = _normalize_token(token)
                if normalized and normalized not in _STOPWORDS:
                    weighted[normalized] = weighted.get(normalized, 0.0) + 1.0

        return [
            token
            for token, _score in sorted(
                weighted.items(),
                key=lambda item: (item[1], item[0]),
                reverse=True,
            )[:8]
        ]

    @staticmethod
    def _article_lookup(articles: list[BatchArticleCandidate]) -> dict[str, BatchArticleCandidate]:
        return {article.article_id: article for article in articles}

    @staticmethod
    def _article_similarity(
        *,
        article_keywords: list[str],
        cluster_keywords: set[str],
        same_team: bool,
    ) -> float:
        if not article_keywords or not cluster_keywords:
            return 0.0
        article_set = set(article_keywords)
        shared = article_set & cluster_keywords
        similarity = float(len(shared))
        if same_team:
            similarity += 0.75
        return similarity

    @staticmethod
    def _team_code(article: BatchArticleCandidate) -> str:
        haystack = f"{article.title} {article.excerpt_text or ''}".lower()
        for team_code, aliases in _TEAM_ALIASES.items():
            if any(alias in haystack for alias in aliases):
                return team_code
        return "KBO"

    def _topic_priority(
        self,
        topic: TopicCandidate,
        *,
        article_lookup: dict[str, BatchArticleCandidate],
    ) -> float:
        priority = topic.topic_score
        if self._is_roundup_topic(topic, article_lookup=article_lookup):
            priority -= 45.0
        if self._is_futures_topic(topic, article_lookup=article_lookup):
            priority -= 35.0
        if len(topic.article_ids) == 1:
            priority -= 8.0
        return priority

    def _fill_to_top_k(
        self,
        topics: list[TopicCandidate],
        *,
        batch_input: BatchIssueSelectionInput,
        existing_article_ids: set[str],
    ) -> list[TopicCandidate]:
        filled = list(topics)
        if len(filled) >= self.top_k:
            return filled

        article_lookup = self._article_lookup(batch_input.articles)
        remaining_articles = [
            article
            for article in batch_input.articles
            if article.article_id not in existing_article_ids
        ]
        remaining_articles.sort(key=self._article_priority, reverse=True)
        for article in remaining_articles:
            filled.append(self._build_single_article_topic(article, batch_id=batch_input.batch_id))
            if len(filled) >= self.top_k:
                break

        if len(filled) < self.top_k:
            used_topic_names = {topic.topic_name for topic in filled}
            supplemental_articles = sorted(
                batch_input.articles,
                key=lambda article: (
                    self._article_priority(article) - self._single_article_penalty(article),
                    self._article_priority(article),
                ),
                reverse=True,
            )
            for article in supplemental_articles:
                if any(topic.representative_article_id == article.article_id for topic in filled):
                    continue
                supplemental_topic = self._build_single_article_topic(
                    article,
                    batch_id=batch_input.batch_id,
                    selection_mode="supplemental",
                )
                if supplemental_topic.topic_name in used_topic_names:
                    continue
                filled.append(supplemental_topic)
                used_topic_names.add(supplemental_topic.topic_name)
                if len(filled) >= self.top_k:
                    break

        filled.sort(
            key=lambda topic: (
                self._topic_priority(topic, article_lookup=article_lookup),
                topic.topic_score,
                len(topic.article_ids),
            ),
            reverse=True,
        )
        return filled

    def _build_single_article_topic(
        self,
        article: BatchArticleCandidate,
        *,
        batch_id: str,
        selection_mode: str = "fallback",
    ) -> TopicCandidate:
        return TopicCandidate(
            topic_id=f"{batch_id}:{selection_mode}:{article.article_id}",
            topic_name=article.title,
            importance_rank=0,
            topic_score=round(
                max(0.0, self._article_priority(article) - self._single_article_penalty(article)),
                2,
            ),
            reason_summary=self._fallback_reason(article),
            article_ids=[article.article_id],
            representative_article_id=article.article_id,
            metadata={
                "article_kind": article.metadata.get("article_kind"),
                "league_tier": article.metadata.get("league_tier"),
                "selection_mode": selection_mode,
            },
        )

    def _fallback_reason(self, article: BatchArticleCandidate) -> str:
        parts = ["개별 기사 보강 후보"]
        article_kind = str(article.metadata.get("article_kind") or "")
        league_tier = str(article.metadata.get("league_tier") or "")
        if article_kind:
            parts.append(f"article_kind={article_kind}")
        if league_tier:
            parts.append(f"league_tier={league_tier}")
        return "; ".join(parts)

    @staticmethod
    def _single_article_penalty(article: BatchArticleCandidate) -> float:
        penalty = 0.0
        article_kind = str(article.metadata.get("article_kind") or "")
        league_tier = str(article.metadata.get("league_tier") or "")
        if article_kind in {"standings", "results_summary", "probable_starters", "roundup"}:
            penalty += 30.0
        if league_tier == "futures":
            penalty += 25.0
        return penalty

    @staticmethod
    def _is_roundup_topic(
        topic: TopicCandidate,
        *,
        article_lookup: dict[str, BatchArticleCandidate],
    ) -> bool:
        article_kinds = {
            str(article_lookup[article_id].metadata.get("article_kind") or "")
            for article_id in topic.article_ids
            if article_id in article_lookup
        }
        return bool(article_kinds) and article_kinds <= {
            "standings",
            "results_summary",
            "probable_starters",
            "roundup",
        }

    @staticmethod
    def _is_futures_topic(
        topic: TopicCandidate,
        *,
        article_lookup: dict[str, BatchArticleCandidate],
    ) -> bool:
        tiers = {
            str(article_lookup[article_id].metadata.get("league_tier") or "")
            for article_id in topic.article_ids
            if article_id in article_lookup
        }
        return tiers == {"futures"}


class GeminiBatchIssueSelectionEngine(HeuristicBatchIssueSelectionEngine):
    def __init__(
        self,
        *,
        api_key: str | None = None,
        model_name: str | None = None,
        prompt_version: str = "phase3.batch.v1",
        top_k: int = 5,
        max_attempts: int = 3,
        retry_delay_seconds: float = 1.0,
        transport: HttpTransport | None = None,
        endpoint_base: str = "https://generativelanguage.googleapis.com/v1beta/models",
    ) -> None:
        load_default_env()
        resolved_model_name = (
            model_name
            or os.getenv("GEMINI_TOPIC_SELECTION_MODEL")
            or "gemini-2.5-flash-lite"
        )
        super().__init__(
            model_name=resolved_model_name,
            prompt_version=prompt_version,
            top_k=top_k,
        )
        self.api_key = api_key or os.getenv("GEMINI_API_KEY")
        self.openai_api_key = os.getenv("OPENAI_API_KEY")
        self.transport = transport or UrllibHttpTransport()
        self.endpoint_base = endpoint_base.rstrip("/")
        self.model_policy = build_model_fallback_policy(resolved_model_name)
        self.max_attempts = max(1, max_attempts)
        self.retry_delay_seconds = max(0.0, retry_delay_seconds)
        self.excerpt_limit = 400

    def select_topics(self, batch_input: BatchIssueSelectionInput) -> BatchIssueSelectionResult:
        call_result = call_with_fallback(
            self.model_policy,
            batch_input,
            prompt_builder=lambda input_data: build_prompt(
                input_data,
                top_k=self.top_k,
                excerpt_limit=self.excerpt_limit,
            ),
            schema_name="batch_issue_selection",
            json_schema=TOPIC_SELECTION_JSON_SCHEMA,
            transport=self.transport,
            gemini_api_key=self.api_key,
            openai_api_key=self.openai_api_key,
            gemini_endpoint_base=self.endpoint_base,
            max_attempts_per_model=self.max_attempts,
            retry_delay_seconds=self.retry_delay_seconds,
        )

        topics = self._parse_topics_payload(call_result.parsed, batch_input.batch_id)
        topics = self._post_process_topics(topics, batch_input=batch_input)
        return BatchIssueSelectionResult(
            batch_id=batch_input.batch_id,
            model_name=call_result.model_name,
            prompt_version=self.prompt_version,
            topics=topics[: self.top_k],
            raw_payload={
                "prompt": call_result.prompt,
                "response_text": call_result.response_text,
                "engine_type": call_result.provider,
            },
            created_at=_utc_now(),
        )

    @staticmethod
    def _parse_topics_payload(parsed: dict, batch_id: str) -> list[TopicCandidate]:
        raw_topics = parsed.get("topics")
        if not isinstance(raw_topics, list):
            raise RuntimeError(f"Model response missing topics array: {parsed}")

        topics: list[TopicCandidate] = []
        for index, raw_topic in enumerate(raw_topics, start=1):
            article_ids = [str(article_id) for article_id in raw_topic.get("article_ids", [])]
            representative_article_id = raw_topic.get("representative_article_id")
            topics.append(
                TopicCandidate(
                    topic_id=f"{batch_id}:{index}",
                    topic_name=str(raw_topic["topic_name"]),
                    importance_rank=int(raw_topic.get("importance_rank", index)),
                    topic_score=float(raw_topic.get("topic_score", 0.0)),
                    reason_summary=str(raw_topic.get("reason_summary", "")),
                    article_ids=article_ids,
                    representative_article_id=str(representative_article_id) if representative_article_id else None,
                )
            )
        topics.sort(key=lambda topic: (topic.importance_rank, -topic.topic_score))
        return topics

    def _post_process_topics(
        self,
        topics: list[TopicCandidate],
        *,
        batch_input: BatchIssueSelectionInput,
    ) -> list[TopicCandidate]:
        article_lookup = {article.article_id: article for article in batch_input.articles}
        normalized_topics = list(topics)
        for topic in normalized_topics:
            topic.metadata["priority_score"] = self._topic_priority(topic, article_lookup=article_lookup)
            topic.metadata["contains_roundup_only"] = self._is_roundup_topic(
                topic,
                article_lookup=article_lookup,
            )
            topic.metadata["contains_futures_only"] = self._is_futures_topic(
                topic,
                article_lookup=article_lookup,
            )

        normalized_topics.sort(
            key=lambda topic: (
                float(topic.metadata.get("priority_score", topic.topic_score)),
                topic.topic_score,
                len(topic.article_ids),
            ),
            reverse=True,
        )
        filled = self._fill_to_top_k(
            normalized_topics,
            batch_input=batch_input,
            existing_article_ids={article_id for topic in normalized_topics for article_id in topic.article_ids},
        )
        limited = filled[: self.top_k]
        for index, topic in enumerate(limited, start=1):
            topic.importance_rank = index
        return limited


def build_prompt(
    batch_input: BatchIssueSelectionInput,
    *,
    top_k: int,
    excerpt_limit: int = 400,
) -> str:
    batch_payload = json.dumps(
        {
            **asdict(batch_input),
            "window_start": _serialize_datetime(batch_input.window_start),
            "window_end": _serialize_datetime(batch_input.window_end),
            "articles": [
                {
                    **asdict(article),
                    "published_at": _serialize_datetime(article.published_at),
                    "collected_at": _serialize_datetime(article.collected_at),
                    "excerpt_text": _limit_text(article.excerpt_text or "", excerpt_limit),
                }
                for article in batch_input.articles
            ],
        },
        ensure_ascii=False,
        indent=2,
    )
    return (
        "You are selecting the top KBO news topics from a batch of collected articles. "
        "Group similar articles into topics, rank the most newsworthy topics, and 반드시 JSON만 출력. "
        "Write topic_name and reason_summary in Korean. "
        "Use article_ids exactly as given. "
        f"If there are at least {top_k} meaningful articles, return exactly {top_k} topics. "
        "Strongly deprioritize generic roundup articles such as standings, aggregate results, and probable starter announcements. "
        "Strongly deprioritize Futures League / 2군 topics unless there are not enough first-team KBO topics.\n\n"
        "Analyze this KBO article batch.\n"
        "Tasks:\n"
        "1. Group related articles into topics.\n"
        "2. Rank topics by current issue importance.\n"
        f"3. Select exactly {top_k} topics if the batch contains at least {top_k} usable articles.\n"
        "4. For each topic, provide article_ids and one representative_article_id.\n"
        "5. 반드시 JSON만 출력.\n\n"
        "Batch JSON:\n"
        f"{batch_payload}"
    )


class BatchIssueSelectionService:
    def __init__(self, engine: BatchIssueSelectionEngine | None = None) -> None:
        self.engine = engine or build_default_topic_selection_engine()

    def select_topics(self, batch_input: BatchIssueSelectionInput) -> BatchIssueSelectionResult:
        return self.engine.select_topics(batch_input)

    def select_topic_candidates(
        self,
        batch_input: BatchIssueSelectionInput,
        *,
        candidate_count: int,
        completed_topics: list[dict[str, Any]] | None = None,
    ) -> BatchIssueSelectionResult:
        selection_result, _excluded_topics = self.select_topic_candidates_with_history(
            batch_input,
            candidate_count=candidate_count,
            completed_topics=completed_topics,
        )
        return selection_result

    def select_topic_candidates_with_history(
        self,
        batch_input: BatchIssueSelectionInput,
        *,
        candidate_count: int,
        completed_topics: list[dict[str, Any]] | None = None,
    ) -> tuple[BatchIssueSelectionResult, list[TopicCandidate]]:
        if candidate_count <= 0:
            raise ValueError("candidate_count must be positive")
        if not completed_topics:
            engine = _clone_topic_selection_engine_with_top_k(self.engine, top_k=candidate_count)
            return engine.select_topics(batch_input), []

        requested_count = min(max(candidate_count, 1), max(1, len(batch_input.articles)))
        excluded_topics: list[TopicCandidate] = []
        filtered_topics: list[TopicCandidate] = []
        last_topic_ids: tuple[str, ...] = ()

        while True:
            engine = _clone_topic_selection_engine_with_top_k(self.engine, top_k=requested_count)
            selection_result = engine.select_topics(batch_input)
            excluded_topics = [
                topic for topic in selection_result.topics if _is_completed_topic(topic, completed_topics)
            ]
            filtered_topics = [
                topic for topic in selection_result.topics if not _is_completed_topic(topic, completed_topics)
            ]
            current_topic_ids = tuple(topic.topic_id for topic in selection_result.topics)
            if len(filtered_topics) >= candidate_count:
                break
            if requested_count >= len(batch_input.articles) or current_topic_ids == last_topic_ids:
                break
            last_topic_ids = current_topic_ids
            requested_count = min(len(batch_input.articles), max(requested_count + 5, requested_count * 2))

        selection_result.topics = filtered_topics[:candidate_count]
        for index, topic in enumerate(selection_result.topics, start=1):
            topic.importance_rank = index
        selection_result.raw_payload = {
            **dict(selection_result.raw_payload),
            "completed_topic_registry_count": len(completed_topics),
            "excluded_completed_topic_count": len(excluded_topics),
            "requested_candidate_count": candidate_count,
            "selection_request_count": requested_count,
        }
        return selection_result, excluded_topics


def build_default_topic_selection_engine() -> BatchIssueSelectionEngine:
    load_default_env()
    if os.getenv("GEMINI_API_KEY"):
        return GeminiBatchIssueSelectionEngine()
    return HeuristicBatchIssueSelectionEngine()


def _clone_topic_selection_engine_with_top_k(
    engine: BatchIssueSelectionEngine,
    *,
    top_k: int,
) -> BatchIssueSelectionEngine:
    if isinstance(engine, GeminiBatchIssueSelectionEngine):
        return GeminiBatchIssueSelectionEngine(
            api_key=engine.api_key,
            model_name=engine.model_name,
            prompt_version=engine.prompt_version,
            top_k=top_k,
            max_attempts=engine.max_attempts,
            retry_delay_seconds=engine.retry_delay_seconds,
            transport=engine.transport,
            endpoint_base=engine.endpoint_base,
        )
    if isinstance(engine, HeuristicBatchIssueSelectionEngine):
        return HeuristicBatchIssueSelectionEngine(
            model_name=engine.model_name,
            prompt_version=engine.prompt_version,
            top_k=top_k,
        )
    raise TypeError(f"Unsupported topic selection engine type: {type(engine)!r}")


def _is_completed_topic(topic: TopicCandidate, completed_topics: list[dict[str, Any]]) -> bool:
    from kbo_card_news.workflows.approval_history import is_completed_topic

    return is_completed_topic(topic, completed_topics)


def _normalize_datetime(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _serialize_datetime(value: datetime | None) -> str | None:
    if value is None:
        return None
    return _normalize_datetime(value).isoformat()


def _normalize_token(token: str) -> str:
    token = token.strip().lower()
    if not token:
        return ""
    return token


def _limit_text(text: str, limit: int) -> str:
    compact = " ".join(text.split()).strip()
    if len(compact) <= limit:
        return compact
    return compact[: max(0, limit - 1)].rstrip() + "…"


_TEAM_ALIASES = {
    "lg": ("lg", "트윈스"),
    "kia": ("kia", "타이거즈"),
    "ssg": ("ssg", "랜더스"),
    "kt": ("kt", "위즈"),
    "nc": ("nc", "다이노스"),
    "두산": ("두산", "베어스"),
    "롯데": ("롯데", "자이언츠"),
    "삼성": ("삼성", "라이온즈"),
    "한화": ("한화", "이글스"),
    "키움": ("키움", "히어로즈"),
}

_GENERIC_TOPIC_WORDS = {
    "kbo",
    "프로야구",
    "야구",
    "기사",
    "경기",
    "선수",
    "감독",
}

_STOPWORDS = _GENERIC_TOPIC_WORDS | {
    "하다",
    "했다",
    "하며",
    "있는",
    "위해",
    "통해",
    "관련",
    "대한",
    "이번",
    "오늘",
    "오전",
    "오후",
    "공식",
    "발표",
    "기자",
    "뉴스",
    "스포츠",
    "구단",
    "시즌",
    "에서",
    "으로",
    "까지",
    "the",
    "and",
    "for",
    "with",
}
