from __future__ import annotations

from dataclasses import asdict, is_dataclass
from datetime import datetime, timezone
from typing import Any

from kbo_card_news.models.issue import (
    AssetMultimodalInsight,
    BatchIssueSelectionResult,
    CardNewsDraft,
    CardNewsPageDraft,
    IssueAssetContext,
    IssueCandidate,
    IssueMultimodalAnalysis,
    TopicCandidate,
)


def serialize_for_json(value: Any) -> Any:
    if is_dataclass(value):
        return serialize_for_json(asdict(value))
    if isinstance(value, dict):
        return {key: serialize_for_json(item) for key, item in value.items()}
    if isinstance(value, list):
        return [serialize_for_json(item) for item in value]
    if isinstance(value, tuple):
        return [serialize_for_json(item) for item in value]
    if isinstance(value, datetime):
        return value.isoformat()
    return value


def build_topic_selection_template(
    selection_result: BatchIssueSelectionResult,
    *,
    candidate_report_path: str | None = None,
) -> dict[str, Any]:
    return {
        "candidate_report_path": candidate_report_path,
        "required_selection_count": None,
        "selected_topic_ids": [],
        "candidates": [
            {
                "topic_id": topic.topic_id,
                "importance_rank": topic.importance_rank,
                "topic_name": topic.topic_name,
                "topic_score": topic.topic_score,
                "reason_summary": topic.reason_summary,
                "article_ids": list(topic.article_ids),
                "representative_article_id": topic.representative_article_id,
                "metadata": dict(topic.metadata),
                "selected": False,
            }
            for topic in selection_result.topics
        ],
    }


def confirm_topic_selection(
    selection_payload: dict[str, Any],
    *,
    required_count: int | None = None,
) -> dict[str, Any]:
    candidates = selection_payload.get("candidates")
    if not isinstance(candidates, list) or not candidates:
        raise ValueError("selection payload must include non-empty candidates")

    explicit_ids = selection_payload.get("selected_topic_ids") or []
    selected_ids = {str(topic_id).strip() for topic_id in explicit_ids if str(topic_id).strip()}
    for candidate in candidates:
        if candidate.get("selected") is True:
            topic_id = str(candidate.get("topic_id") or "").strip()
            if topic_id:
                selected_ids.add(topic_id)

    ordered_selected = [
        candidate for candidate in candidates if str(candidate.get("topic_id") or "").strip() in selected_ids
    ]
    if required_count is None:
        required_count = len(ordered_selected)
    if required_count <= 0:
        raise ValueError("at least one topic must be selected")
    if len(ordered_selected) != required_count:
        raise ValueError(
            f"exactly {required_count} topics must be selected, got {len(ordered_selected)}"
        )

    return {
        "candidate_report_path": selection_payload.get("candidate_report_path"),
        "required_selection_count": required_count,
        "selected_topic_ids": [str(candidate["topic_id"]) for candidate in ordered_selected],
        "selected_topics": ordered_selected,
    }


def selection_result_from_dict(payload: dict[str, Any]) -> BatchIssueSelectionResult:
    topics = [topic_candidate_from_dict(item) for item in payload.get("topics", [])]
    return BatchIssueSelectionResult(
        batch_id=str(payload.get("batch_id") or ""),
        model_name=str(payload.get("model_name") or ""),
        prompt_version=str(payload.get("prompt_version") or ""),
        topics=topics,
        raw_payload=dict(payload.get("raw_payload") or {}),
        created_at=_parse_datetime(payload.get("created_at")),
    )


def topic_candidate_from_dict(payload: dict[str, Any]) -> TopicCandidate:
    return TopicCandidate(
        topic_id=str(payload.get("topic_id") or ""),
        topic_name=str(payload.get("topic_name") or ""),
        importance_rank=int(payload.get("importance_rank") or 0),
        topic_score=float(payload.get("topic_score") or 0.0),
        reason_summary=str(payload.get("reason_summary") or ""),
        article_ids=[str(item) for item in payload.get("article_ids", [])],
        representative_article_id=_optional_text(payload.get("representative_article_id")),
        metadata=dict(payload.get("metadata") or {}),
    )


def issue_candidate_from_dict(payload: dict[str, Any]) -> IssueCandidate:
    return IssueCandidate(
        issue_id=str(payload.get("issue_id") or ""),
        title=str(payload.get("title") or ""),
        summary=str(payload.get("summary") or ""),
        source_type=str(payload.get("source_type") or ""),
        source_item_type=str(payload.get("source_item_type") or ""),
        source_url=str(payload.get("source_url") or ""),
        published_at=_parse_datetime(payload.get("published_at")),
        collected_at=_parse_datetime(payload.get("collected_at")) or datetime.now(timezone.utc),
        asset_count=int(payload.get("asset_count") or 0),
        engagement_view_count=int(payload.get("engagement_view_count") or 0),
        engagement_like_count=int(payload.get("engagement_like_count") or 0),
        engagement_comment_count=int(payload.get("engagement_comment_count") or 0),
        engagement_share_count=int(payload.get("engagement_share_count") or 0),
        metadata=dict(payload.get("metadata") or {}),
    )


def issue_asset_contexts_from_list(items: list[dict[str, Any]]) -> list[IssueAssetContext]:
    return [
        IssueAssetContext(
            asset_id=_optional_text(item.get("asset_id")),
            asset_type=str(item.get("asset_type") or ""),
            origin_url=str(item.get("origin_url") or ""),
            storage_path=_optional_text(item.get("storage_path")),
            caption=_optional_text(item.get("caption")),
            vision_caption=_optional_text(item.get("vision_caption")),
            ocr_text=_optional_text(item.get("ocr_text")),
            mime_type=_optional_text(item.get("mime_type")),
            width=_optional_int(item.get("width")),
            height=_optional_int(item.get("height")),
            sort_order=int(item.get("sort_order") or 0),
        )
        for item in items
    ]


def card_news_draft_from_dict(payload: dict[str, Any]) -> CardNewsDraft:
    return CardNewsDraft(
        issue_id=str(payload.get("issue_id") or ""),
        template_name=str(payload.get("template_name") or ""),
        title=str(payload.get("title") or ""),
        subtitle=str(payload.get("subtitle") or ""),
        pages=[
            CardNewsPageDraft(
                page_number=int(page.get("page_number") or 0),
                page_role=str(page.get("page_role") or ""),
                headline=str(page.get("headline") or ""),
                body=str(page.get("body") or ""),
                image_prompt=str(page.get("image_prompt") or ""),
                asset_reference=_optional_text(page.get("asset_reference")),
            )
            for page in payload.get("pages", [])
        ],
        prompt_version=str(payload.get("prompt_version") or ""),
        source_asset_count=int(payload.get("source_asset_count") or 0),
        planning_notes=str(payload.get("planning_notes") or ""),
        metadata=dict(payload.get("metadata") or {}),
        created_at=_parse_datetime(payload.get("created_at")),
    )


def multimodal_analysis_from_dict(payload: dict[str, Any]) -> IssueMultimodalAnalysis:
    return IssueMultimodalAnalysis(
        issue_id=str(payload.get("issue_id") or ""),
        model_name=str(payload.get("model_name") or ""),
        prompt_version=str(payload.get("prompt_version") or ""),
        overall_summary=str(payload.get("overall_summary") or ""),
        assets=[
            AssetMultimodalInsight(
                asset_reference=str(asset.get("asset_reference") or ""),
                asset_type=str(asset.get("asset_type") or ""),
                scene_description=str(asset.get("scene_description") or ""),
                humor_point=str(asset.get("humor_point") or ""),
                usage_recommendation=str(asset.get("usage_recommendation") or ""),
                subject_tags=[str(tag) for tag in asset.get("subject_tags", [])],
                event_tags=[str(tag) for tag in asset.get("event_tags", [])],
                emotion_tags=[str(tag) for tag in asset.get("emotion_tags", [])],
                composition_tags=[str(tag) for tag in asset.get("composition_tags", [])],
                risk_tags=[str(tag) for tag in asset.get("risk_tags", [])],
                tag_summary=str(asset.get("tag_summary") or ""),
                caution_note=_optional_text(asset.get("caution_note")),
                confidence=float(asset.get("confidence") or 0.0),
                analysis_payload=dict(asset.get("analysis_payload") or {}),
            )
            for asset in payload.get("assets", [])
        ],
        metadata=dict(payload.get("metadata") or {}),
        created_at=_parse_datetime(payload.get("created_at")),
    )


def _parse_datetime(value: Any) -> datetime | None:
    if not value:
        return None
    if isinstance(value, datetime):
        return value
    return datetime.fromisoformat(str(value))


def _optional_text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _optional_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    return int(value)
