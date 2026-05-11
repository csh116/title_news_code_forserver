from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone

from kbo_card_news.models.issue import (
    BatchArticleCandidate,
    BatchIssueSelectionInput,
    IssueAssetContext,
    IssueCandidate,
)
from kbo_card_news.pipeline.storage import PersistedSourceItem


@dataclass(slots=True)
class StoredIssueCandidateBundle:
    candidate: IssueCandidate
    assets: list[IssueAssetContext]
    source_item: PersistedSourceItem


class StoredIssueFeedAdapter:
    def convert(self, persisted: PersistedSourceItem) -> StoredIssueCandidateBundle:
        item = persisted.item
        title = _compact_text(item.title) or "(untitled)"
        body_text = _compact_text(item.body_text)
        summary = body_text or title
        metadata = {
            "team_code": _infer_team_code(title=title, body_text=body_text, source_url=item.source_url),
            "issue_category": _infer_issue_category(
                source_type=item.source_type,
                title=title,
                body_text=body_text,
            ),
            "author_name": item.author_name,
            "published_at": item.published_at.isoformat() if item.published_at else None,
            "collected_at": item.collected_at.isoformat(),
        }
        candidate = IssueCandidate(
            issue_id=item.id,
            title=title,
            summary=summary,
            source_type=item.source_type,
            source_item_type=item.source_item_type,
            source_url=item.source_url,
            published_at=item.published_at,
            collected_at=item.collected_at,
            asset_count=len(persisted.assets),
            engagement_view_count=item.engagement_view_count,
            engagement_like_count=item.engagement_like_count,
            engagement_comment_count=item.engagement_comment_count,
            engagement_share_count=item.engagement_share_count,
            metadata=metadata,
        )
        assets = [
            IssueAssetContext(
                asset_id=asset.id,
                asset_type=asset.asset_type,
                origin_url=asset.origin_url,
                storage_path=asset.storage_path,
                caption=asset.vision_caption,
                vision_caption=asset.vision_caption,
                ocr_text=asset.ocr_text,
                mime_type=asset.mime_type,
                width=asset.width,
                height=asset.height,
                sort_order=asset.sort_order,
            )
            for asset in persisted.assets
        ]
        return StoredIssueCandidateBundle(candidate=candidate, assets=assets, source_item=persisted)


class StoredArticleBatchBuilder:
    def build(
        self,
        persisted_items: list[PersistedSourceItem],
        *,
        batch_id: str,
        window_start: datetime,
        window_end: datetime,
    ) -> BatchIssueSelectionInput:
        articles: list[BatchArticleCandidate] = []
        normalized_start = _normalize_datetime(window_start)
        normalized_end = _normalize_datetime(window_end)
        excluded_missing_published_at_count = 0
        excluded_out_of_window_count = 0

        for persisted in persisted_items:
            item = persisted.item
            if item.source_item_type != "article":
                continue

            if item.published_at is None:
                excluded_missing_published_at_count += 1
                continue

            normalized_reference = _normalize_datetime(item.published_at)
            if normalized_reference < normalized_start or normalized_reference >= normalized_end:
                excluded_out_of_window_count += 1
                continue

            title = _compact_text(item.title) or "(untitled)"
            excerpt_text = (_compact_text(item.excerpt_text) or _compact_text(item.body_text))[:600]
            if not _looks_like_kbo_article(title=title, body_text=excerpt_text):
                continue
            articles.append(
                BatchArticleCandidate(
                    article_id=item.id,
                    title=title,
                    source_type=item.source_type,
                    source_url=item.source_url,
                    published_at=item.published_at,
                    collected_at=item.collected_at,
                    engagement_view_count=item.engagement_view_count,
                    excerpt_text=excerpt_text,
                    metadata={
                        "author_name": item.author_name,
                        "article_kind": _infer_article_kind(title=title, body_text=excerpt_text),
                        "league_tier": _infer_league_tier(title=title, body_text=excerpt_text),
                    },
                )
            )

        articles.sort(
            key=lambda article: article.published_at or article.collected_at,
            reverse=True,
        )
        return BatchIssueSelectionInput(
            batch_id=batch_id,
            window_start=window_start,
            window_end=window_end,
            articles=articles,
            metadata={
                "article_count": len(articles),
                "time_filter_basis": "published_at",
                "window_end_inclusive": False,
                "excluded_missing_published_at_count": excluded_missing_published_at_count,
                "excluded_out_of_window_count": excluded_out_of_window_count,
            },
        )


def _compact_text(value: str | None) -> str:
    if not value:
        return ""
    return " ".join(str(value).split()).strip()


def _infer_team_code(*, title: str, body_text: str, source_url: str) -> str:
    haystack = f"{title} {body_text} {source_url}".lower()
    team_aliases = {
        "LG": ["lg", "트윈스", "lgtwins"],
        "KIA": ["kia", "타이거즈"],
        "SSG": ["ssg", "랜더스"],
        "KT": ["kt", "위즈"],
        "NC": ["nc", "다이노스"],
        "두산": ["두산", "베어스"],
        "롯데": ["롯데", "자이언츠"],
        "삼성": ["삼성", "라이온즈"],
        "한화": ["한화", "이글스"],
        "키움": ["키움", "히어로즈"],
    }
    for team_code, aliases in team_aliases.items():
        if any(alias.lower() in haystack for alias in aliases):
            return team_code
    return "KBO"


def _infer_issue_category(*, source_type: str, title: str, body_text: str) -> str:
    text = f"{title} {body_text}".lower()
    if source_type == "kma_weather" or any(keyword in text for keyword in ["우천", "비", "weather", "rain"]):
        return "weather"
    if any(keyword in text for keyword in ["속보", "breaking"]):
        return "breaking"
    if source_type == "dcinside":
        return "community"
    if re.search(r"\d", text):
        return "game"
    return "general"


def _infer_article_kind(*, title: str, body_text: str) -> str:
    text = f"{title} {body_text}".lower()
    if "중간 순위" in text or "순위(" in text:
        return "standings"
    if "전적 종합" in text or ("전적" in text and "종합" in text):
        return "results_summary"
    if "선발투수 예고" in text or ("선발" in text and "예고" in text):
        return "probable_starters"
    if "종합" in text:
        return "roundup"
    return "report"


def _infer_league_tier(*, title: str, body_text: str) -> str:
    text = f"{title} {body_text}".lower()
    if any(keyword in text for keyword in ["퓨처스", "2군", "퓨처스리그"]):
        return "futures"
    return "kbo"


def _looks_like_kbo_article(*, title: str, body_text: str) -> bool:
    text = f"{title} {body_text}".lower()

    baseball_keywords = (
        "kbo",
        "프로야구",
        "야구",
        "투수",
        "타자",
        "홈런",
        "선발",
        "불펜",
        "마운드",
        "타선",
        "등판",
        "삼진",
        "볼넷",
        "안타",
        "득점",
        "승리",
        "패배",
        "연승",
        "연패",
        "엔트리",
        "말소",
        "콜업",
        "타율",
        "출루율",
        "장타율",
        "ops",
        "era",
        "war",
        "퓨처스",
        "포수",
        "유격수",
        "내야수",
        "외야수",
        "지명타자",
        "마무리",
        "세이브",
        "트레이드",
    )
    team_keywords = (
        "lg",
        "트윈스",
        "kia",
        "타이거즈",
        "ssg",
        "랜더스",
        "kt",
        "위즈",
        "nc",
        "다이노스",
        "두산",
        "베어스",
        "롯데",
        "자이언츠",
        "삼성",
        "라이온즈",
        "한화",
        "이글스",
        "키움",
        "히어로즈",
    )
    entertainment_noise = (
        "귀여워",
        "눈망울",
        "고양이귀",
        "사슴",
        "배우",
        "가수",
        "아이돌",
        "드라마",
        "영화",
        "공항패션",
        "화보",
        "베드신",
    )

    if any(keyword in text for keyword in baseball_keywords):
        return True

    if any(keyword in text for keyword in entertainment_noise):
        return False

    return any(keyword in text for keyword in team_keywords) and any(
        keyword in text for keyword in ("경기", "리그", "시즌", "구단", "선수")
    )


def _normalize_datetime(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)
