from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from kbo_card_news.models.issue import (
    IssueAssetContext,
    IssueCandidate,
    IssueScore,
    IssueStructuringInput,
    TopicDeepResearchInput,
    TopicDeepResearchResult,
)
from kbo_card_news.pipeline.issue_feed import _infer_issue_category, _infer_team_code


@dataclass(slots=True)
class TopicStructuringBundle:
    topic_input: TopicDeepResearchInput
    research_result: TopicDeepResearchResult
    structuring_input: IssueStructuringInput


class TopicStructuringInputBuilder:
    def build(
        self,
        topic_input: TopicDeepResearchInput,
        research_result: TopicDeepResearchResult,
    ) -> IssueStructuringInput:
        representative_article = self._select_representative_article(topic_input)
        title = self._build_title(topic_input, research_result)
        summary = self._build_summary(topic_input, research_result)
        source_type = representative_article.source_type if representative_article else "news_article_batch"
        source_url = representative_article.source_url if representative_article else ""
        published_at = representative_article.published_at if representative_article else None
        collected_at = self._resolve_collected_at(topic_input)
        team_code = _infer_team_code(
            title=title,
            body_text=self._build_team_context(topic_input, research_result),
            source_url=source_url,
        )
        issue_category = _infer_issue_category(
            source_type=source_type,
            title=title,
            body_text=summary,
        )
        total_score = _clamp_score(
            research_result.metadata.get("topic_score", topic_input.topic.topic_score)
        )
        timeliness_score = self._estimate_timeliness_score(topic_input, collected_at)
        info_score = _clamp_score(58 + len(research_result.key_points) * 7 + len(research_result.notable_numbers) * 4)
        safety_score = _clamp_score(92 - len(research_result.risk_flags) * 12)
        fun_score = _clamp_score(total_score + len(topic_input.assets) * 2 + len(research_result.key_points) * 1.5 - 5)
        should_publish = total_score >= 60 and safety_score >= 45
        score = IssueScore(
            issue_id=topic_input.topic.topic_id,
            model_name=research_result.model_name,
            prompt_version="phase3.2.topic_bridge.v1",
            fun_score=fun_score,
            timeliness_score=timeliness_score,
            info_score=info_score,
            safety_score=safety_score,
            total_score=total_score,
            should_publish=should_publish,
            reason_summary=self._build_reason_summary(topic_input, research_result),
            scoring_payload={
                "topic_score": topic_input.topic.topic_score,
                "risk_flags": research_result.risk_flags,
                "article_count": len(topic_input.articles),
                "asset_count": len(topic_input.assets),
            },
            created_at=research_result.created_at or _utc_now(),
        )
        candidate = IssueCandidate(
            issue_id=topic_input.topic.topic_id,
            title=title,
            summary=summary,
            source_type=source_type,
            source_item_type="article_batch_topic",
            source_url=source_url,
            published_at=published_at,
            collected_at=collected_at,
            asset_count=len(topic_input.assets),
            engagement_view_count=sum(
                int(article.metadata.get("engagement_view_count", 0))
                for article in topic_input.articles
            ),
            metadata={
                "team_code": team_code,
                "issue_category": issue_category,
                "topic_id": topic_input.topic.topic_id,
                "topic_rank": topic_input.topic.importance_rank,
                "topic_score": topic_input.topic.topic_score,
                "article_count": len(topic_input.articles),
                "source_article_ids": research_result.source_article_ids,
                "source_asset_ids": research_result.source_asset_ids,
                "risk_flags": research_result.risk_flags,
                "recommended_focus": research_result.recommended_focus,
                "representative_article_id": research_result.representative_article_id,
            },
        )
        assets = [
            IssueAssetContext(
                asset_id=asset.asset_id,
                asset_type=asset.asset_type,
                origin_url=asset.origin_url,
                caption=asset.caption,
                vision_caption=asset.caption,
                mime_type=asset.mime_type,
                width=asset.width,
                height=asset.height,
                sort_order=asset.sort_order,
            )
            for asset in topic_input.assets
        ]
        return IssueStructuringInput(candidate=candidate, score=score, assets=assets)

    def build_many(
        self,
        topic_inputs: list[TopicDeepResearchInput],
        research_results: list[TopicDeepResearchResult],
    ) -> list[TopicStructuringBundle]:
        results_by_topic_id = {result.topic_id: result for result in research_results}
        bundles: list[TopicStructuringBundle] = []
        for topic_input in topic_inputs:
            result = results_by_topic_id.get(topic_input.topic.topic_id)
            if result is None:
                continue
            bundles.append(
                TopicStructuringBundle(
                    topic_input=topic_input,
                    research_result=result,
                    structuring_input=self.build(topic_input, result),
                )
            )
        return bundles

    @staticmethod
    def _select_representative_article(topic_input: TopicDeepResearchInput):
        article_id = topic_input.topic.representative_article_id
        if article_id:
            for article in topic_input.articles:
                if article.article_id == article_id:
                    return article
        if topic_input.articles:
            return topic_input.articles[0]
        return None

    @staticmethod
    def _build_title(
        topic_input: TopicDeepResearchInput,
        research_result: TopicDeepResearchResult,
    ) -> str:
        lead = research_result.key_points[0] if research_result.key_points else topic_input.topic.topic_name
        compact = " ".join(str(lead).split()).strip()
        return compact or topic_input.topic.topic_name

    @staticmethod
    def _build_summary(
        topic_input: TopicDeepResearchInput,
        research_result: TopicDeepResearchResult,
    ) -> str:
        parts = [research_result.angle_summary.strip()]
        if research_result.key_points:
            parts.append("핵심 포인트: " + " / ".join(research_result.key_points[:3]))
        if research_result.notable_numbers:
            parts.append("주요 수치: " + ", ".join(research_result.notable_numbers[:4]))
        if research_result.recommended_focus:
            parts.append("구조화 포커스: " + research_result.recommended_focus.strip())
        summary = " ".join(part for part in parts if part).strip()
        return summary or topic_input.topic.reason_summary

    @staticmethod
    def _build_reason_summary(
        topic_input: TopicDeepResearchInput,
        research_result: TopicDeepResearchResult,
    ) -> str:
        reasons = [
            topic_input.topic.reason_summary.strip(),
            research_result.recommended_focus.strip(),
        ]
        if research_result.risk_flags:
            reasons.append("리스크: " + ", ".join(research_result.risk_flags[:2]))
        compact = " / ".join(part for part in reasons if part)
        return compact[:220] if len(compact) > 220 else compact

    @staticmethod
    def _build_team_context(
        topic_input: TopicDeepResearchInput,
        research_result: TopicDeepResearchResult,
    ) -> str:
        article_text = " ".join(article.title for article in topic_input.articles[:3])
        return f"{topic_input.topic.topic_name} {research_result.angle_summary} {article_text}"

    @staticmethod
    def _resolve_collected_at(topic_input: TopicDeepResearchInput) -> datetime:
        if not topic_input.articles:
            return _utc_now()
        return max(_normalize_datetime(article.collected_at) for article in topic_input.articles)

    @staticmethod
    def _estimate_timeliness_score(
        topic_input: TopicDeepResearchInput,
        collected_at: datetime,
    ) -> float:
        if not topic_input.articles:
            return 55.0
        latest_reference = max(
            _normalize_datetime(article.published_at or article.collected_at)
            for article in topic_input.articles
        )
        age = _normalize_datetime(collected_at) - latest_reference
        if age <= timedelta(hours=2):
            return 92.0
        if age <= timedelta(hours=6):
            return 84.0
        if age <= timedelta(hours=12):
            return 76.0
        if age <= timedelta(hours=24):
            return 68.0
        return 55.0


def _clamp_score(value: object) -> float:
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return 0.0
    return max(0.0, min(100.0, numeric))


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _normalize_datetime(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)
