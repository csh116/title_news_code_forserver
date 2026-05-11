from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


@dataclass(slots=True)
class IssueCandidate:
    issue_id: str
    title: str
    summary: str
    source_type: str
    source_item_type: str
    source_url: str
    published_at: datetime | None
    collected_at: datetime
    asset_count: int = 0
    engagement_view_count: int = 0
    engagement_like_count: int = 0
    engagement_comment_count: int = 0
    engagement_share_count: int = 0
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class BatchArticleCandidate:
    article_id: str
    title: str
    source_type: str
    source_url: str
    published_at: datetime | None
    collected_at: datetime
    engagement_view_count: int = 0
    excerpt_text: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class BatchIssueSelectionInput:
    batch_id: str
    window_start: datetime
    window_end: datetime
    articles: list[BatchArticleCandidate] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class TopicCandidate:
    topic_id: str
    topic_name: str
    importance_rank: int
    topic_score: float
    reason_summary: str
    article_ids: list[str] = field(default_factory=list)
    representative_article_id: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class BatchIssueSelectionResult:
    batch_id: str
    model_name: str
    prompt_version: str
    topics: list[TopicCandidate] = field(default_factory=list)
    raw_payload: dict[str, Any] = field(default_factory=dict)
    created_at: datetime | None = None


@dataclass(slots=True)
class TopicResearchArticle:
    article_id: str
    title: str
    source_type: str
    source_url: str
    published_at: datetime | None
    collected_at: datetime
    author_name: str | None = None
    excerpt_text: str | None = None
    body_text: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class TopicResearchAsset:
    asset_id: str | None
    article_id: str
    asset_type: str
    origin_url: str
    caption: str | None = None
    mime_type: str | None = None
    width: int | None = None
    height: int | None = None
    sort_order: int = 0
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class TopicDeepResearchInput:
    batch_id: str
    topic: TopicCandidate
    articles: list[TopicResearchArticle] = field(default_factory=list)
    assets: list[TopicResearchAsset] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class TopicDeepResearchResult:
    batch_id: str
    topic_id: str
    topic_name: str
    model_name: str
    prompt_version: str
    representative_article_id: str | None
    angle_summary: str
    key_points: list[str] = field(default_factory=list)
    timeline: list[str] = field(default_factory=list)
    notable_numbers: list[str] = field(default_factory=list)
    source_article_ids: list[str] = field(default_factory=list)
    source_asset_ids: list[str] = field(default_factory=list)
    risk_flags: list[str] = field(default_factory=list)
    recommended_focus: str = ""
    raw_payload: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)
    created_at: datetime | None = None


@dataclass(slots=True)
class IssueScore:
    issue_id: str
    model_name: str
    prompt_version: str
    fun_score: float
    timeliness_score: float
    info_score: float
    safety_score: float
    total_score: float
    should_publish: bool
    reason_summary: str
    scoring_payload: dict[str, Any]
    created_at: datetime


@dataclass(slots=True)
class IssueAssetContext:
    asset_id: str | None
    asset_type: str
    origin_url: str
    storage_path: str | None = None
    caption: str | None = None
    vision_caption: str | None = None
    ocr_text: str | None = None
    mime_type: str | None = None
    width: int | None = None
    height: int | None = None
    sort_order: int = 0


@dataclass(slots=True)
class IssueStructuringInput:
    candidate: IssueCandidate
    score: IssueScore
    assets: list[IssueAssetContext] = field(default_factory=list)


@dataclass(slots=True)
class CardNewsPageDraft:
    page_number: int
    page_role: str
    headline: str
    body: str
    image_prompt: str
    asset_reference: str | None = None


@dataclass(slots=True)
class CardNewsDraft:
    issue_id: str
    template_name: str
    title: str
    subtitle: str
    pages: list[CardNewsPageDraft]
    prompt_version: str
    source_asset_count: int
    planning_notes: str
    metadata: dict[str, Any] = field(default_factory=dict)
    created_at: datetime | None = None


@dataclass(slots=True)
class IssueMultimodalAnalysisInput:
    candidate: IssueCandidate
    assets: list[IssueAssetContext] = field(default_factory=list)
    card_news_draft: CardNewsDraft | None = None
    memory_context_summary: str | None = None
    referenced_memory_ids: list[str] = field(default_factory=list)
    memory_context_by_asset: dict[str, str] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class AssetMultimodalInsight:
    asset_reference: str
    asset_type: str
    scene_description: str
    humor_point: str
    usage_recommendation: str
    subject_tags: list[str] = field(default_factory=list)
    event_tags: list[str] = field(default_factory=list)
    emotion_tags: list[str] = field(default_factory=list)
    composition_tags: list[str] = field(default_factory=list)
    risk_tags: list[str] = field(default_factory=list)
    tag_summary: str = ""
    caution_note: str | None = None
    confidence: float = 0.0
    analysis_payload: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class IssueMultimodalAnalysis:
    issue_id: str
    model_name: str
    prompt_version: str
    overall_summary: str
    assets: list[AssetMultimodalInsight]
    metadata: dict[str, Any] = field(default_factory=dict)
    created_at: datetime | None = None


@dataclass(slots=True)
class CardImagePlanningInput:
    candidate: IssueCandidate
    draft: CardNewsDraft
    assets: list[IssueAssetContext] = field(default_factory=list)
    multimodal_analysis: IssueMultimodalAnalysis | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class CardImagePlanPage:
    page_number: int
    page_role: str
    render_strategy: str
    primary_asset_reference: str | None
    crop_focus: str
    overlay_style: str
    text_layout_hint: str
    generation_prompt: str
    edit_instructions: str
    caution_note: str | None = None


@dataclass(slots=True)
class CardImagePlan:
    issue_id: str
    template_name: str
    model_name: str
    prompt_version: str
    overall_art_direction: str
    pages: list[CardImagePlanPage]
    metadata: dict[str, Any] = field(default_factory=dict)
    created_at: datetime | None = None


@dataclass(slots=True)
class CardDesignAutomationInput:
    draft: CardNewsDraft
    image_plan: CardImagePlan
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class CardDesignPage:
    page_number: int
    page_role: str
    layout_variant: str
    headline: str
    body: str
    primary_asset_reference: str | None
    render_strategy: str
    accent_color: str
    background_style: str
    component_order: list[str] = field(default_factory=list)
    html_fragment: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class CardDesignBundle:
    issue_id: str
    template_name: str
    team_code: str
    width: int
    height: int
    overall_art_direction: str
    pages: list[CardDesignPage]
    html_document: str
    metadata: dict[str, Any] = field(default_factory=dict)
    created_at: datetime | None = None


@dataclass(slots=True)
class CardDesignConsistencyPageResult:
    page_number: int
    page_role: str
    quality_score: float
    text_density: str
    warnings: list[str] = field(default_factory=list)
    adjustments: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class CardDesignConsistencyReport:
    issue_id: str
    team_code: str
    passed: bool
    consistency_score: float
    bundle: CardDesignBundle
    pages: list[CardDesignConsistencyPageResult] = field(default_factory=list)
    global_warnings: list[str] = field(default_factory=list)
    created_at: datetime | None = None
