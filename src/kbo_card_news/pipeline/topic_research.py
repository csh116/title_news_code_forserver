from __future__ import annotations

from dataclasses import dataclass

from kbo_card_news.models.issue import (
    BatchIssueSelectionInput,
    BatchIssueSelectionResult,
    TopicDeepResearchInput,
    TopicResearchArticle,
    TopicResearchAsset,
)
from kbo_card_news.pipeline.storage import PersistedSourceItem


@dataclass(slots=True)
class StoredTopicDeepResearchBundle:
    topic_input: TopicDeepResearchInput
    persisted_items: list[PersistedSourceItem]


class StoredTopicDeepResearchBuilder:
    def build(
        self,
        persisted_items: list[PersistedSourceItem],
        *,
        batch_input: BatchIssueSelectionInput,
        selection_result: BatchIssueSelectionResult,
    ) -> list[StoredTopicDeepResearchBundle]:
        persisted_by_id = {item.item.id: item for item in persisted_items}
        article_lookup = {article.article_id: article for article in batch_input.articles}
        bundles: list[StoredTopicDeepResearchBundle] = []

        for topic in selection_result.topics:
            selected_items: list[PersistedSourceItem] = []
            articles: list[TopicResearchArticle] = []
            assets: list[TopicResearchAsset] = []

            for article_id in topic.article_ids:
                persisted = persisted_by_id.get(article_id)
                if persisted is None:
                    continue

                selected_items.append(persisted)
                item = persisted.item
                batch_article = article_lookup.get(article_id)
                articles.append(
                    TopicResearchArticle(
                        article_id=item.id,
                        title=item.title or "(untitled)",
                        source_type=item.source_type,
                        source_url=item.source_url,
                        published_at=item.published_at,
                        collected_at=item.collected_at,
                        author_name=item.author_name,
                        excerpt_text=item.excerpt_text,
                        body_text=item.body_text,
                        metadata={
                            "article_kind": batch_article.metadata.get("article_kind") if batch_article else None,
                            "league_tier": batch_article.metadata.get("league_tier") if batch_article else None,
                            "engagement_view_count": item.engagement_view_count,
                        },
                    )
                )
                assets.extend(
                    TopicResearchAsset(
                        asset_id=asset.id,
                        article_id=item.id,
                        asset_type=asset.asset_type,
                        origin_url=asset.origin_url,
                        caption=asset.vision_caption,
                        mime_type=asset.mime_type,
                        width=asset.width,
                        height=asset.height,
                        sort_order=asset.sort_order,
                        metadata={"source_url": item.source_url},
                    )
                    for asset in persisted.assets
                )

            articles.sort(
                key=lambda article: article.published_at or article.collected_at,
                reverse=True,
            )
            assets.sort(key=lambda asset: (asset.article_id, asset.sort_order, asset.asset_id or ""))
            topic_input = TopicDeepResearchInput(
                batch_id=batch_input.batch_id,
                topic=topic,
                articles=articles,
                assets=assets,
                metadata={
                    "selection_model_name": selection_result.model_name,
                    "selection_prompt_version": selection_result.prompt_version,
                    "window_start": batch_input.window_start.isoformat(),
                    "window_end": batch_input.window_end.isoformat(),
                    "topic_rank": topic.importance_rank,
                    "topic_score": topic.topic_score,
                },
            )
            bundles.append(
                StoredTopicDeepResearchBundle(
                    topic_input=topic_input,
                    persisted_items=selected_items,
                )
            )
        return bundles
