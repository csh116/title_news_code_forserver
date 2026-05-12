from __future__ import annotations

import tempfile
import unittest
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from kbo_card_news.automation.fresh_window_decision import (
    FreshWindowDecisionConfig,
    GeminiFreshWindowDecisionEngine,
    FreshWindowDecisionModelResult,
    FreshWindowDecisionRequest,
    FreshWindowTopicDecision,
    build_fresh_window_decision_prompt,
    parse_fresh_window_decisions,
    watch_fresh_window_once,
)
from kbo_card_news.automation.job_state import AutomationJobRepository
from kbo_card_news.pipeline.storage import PersistedSourceItem, SQLiteSourceItemRepository, SourceItemRecord
from kbo_card_news.runtime.model_fallback import build_model_fallback_policy


@dataclass(slots=True)
class _CollectorResult:
    items: list[object]
    errors: list[str]


class _NoopCollectorService:
    def __init__(self, collectors: object) -> None:
        self.collectors = collectors

    def collect_all(self, *, window_start: datetime, window_end: datetime) -> _CollectorResult:
        return _CollectorResult(items=[], errors=[])


class _FakeDecisionEngine:
    def __init__(self, decisions: list[FreshWindowTopicDecision] | None = None, error: Exception | None = None) -> None:
        self.decisions = decisions or []
        self.error = error

    def decide(self, request: FreshWindowDecisionRequest) -> FreshWindowDecisionModelResult:
        if self.error is not None:
            raise self.error
        return FreshWindowDecisionModelResult(
            model_name="fake-model",
            prompt_version="test",
            decisions=self.decisions,
            raw_payload={"topic_decisions": [decision.metadata.get("raw_decision", {}) for decision in self.decisions]},
            status="ok",
        )


class FreshWindowDecisionTest(unittest.TestCase):
    def test_default_fresh_decision_fallback_policy_tries_31_then_25_then_openai(self) -> None:
        self.assertEqual(
            build_model_fallback_policy("gemini-3.1-flash-lite-preview"),
            ["gemini-3.1-flash-lite-preview", "gemini-2.5-flash-lite", "gpt-4o-mini"],
        )

    def test_fresh_decision_engine_keeps_openai_final_fallback(self) -> None:
        with patch.dict("os.environ", {"OPENAI_API_KEY": "openai-key"}, clear=False):
            engine = GeminiFreshWindowDecisionEngine(
                api_key="gemini-key",
                model_name="gemini-3.1-flash-lite-preview",
            )

        self.assertEqual(
            engine.model_policy,
            ["gemini-3.1-flash-lite-preview", "gemini-2.5-flash-lite", "gpt-4o-mini"],
        )
        self.assertEqual(engine.max_attempts, 5)
        self.assertEqual(engine.openai_api_key, "openai-key")

    def test_prompt_separates_target_and_context_article_id_fields(self) -> None:
        request = FreshWindowDecisionRequest(
            run_id="run",
            collection_window_start=datetime(2026, 5, 12, 10, 0, tzinfo=timezone.utc),
            collection_window_end=datetime(2026, 5, 12, 10, 10, tzinfo=timezone.utc),
            context_window_start=datetime(2026, 5, 12, 9, 0, tzinfo=timezone.utc),
            context_window_end=datetime(2026, 5, 12, 10, 0, tzinfo=timezone.utc),
            target_articles=[],
            historical_context_articles=[],
            feedback_examples={"positive": [], "negative": []},
            completed_topics=[],
        )

        prompt = build_fresh_window_decision_prompt(request)

        self.assertIn("target_article_ids may contain ONLY IDs from target_articles", prompt)
        self.assertIn("target_article_ids is required and must include at least one target_articles ID", prompt)
        self.assertIn("Never copy an ID from historical_context_articles into target_article_ids", prompt)

    def test_publish_only_creates_job_but_logs_all_decisions(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            source_db = tmp_path / "source.db"
            state_db = tmp_path / "state.db"
            window_start = datetime(2026, 5, 12, 10, 0, tzinfo=timezone.utc)
            window_end = window_start + timedelta(minutes=10)
            context_time = window_start - timedelta(hours=1)
            _seed_source_items(
                source_db,
                [
                    _item("a1", "LG 오스틴 1군 말소, 검진 예정", window_start + timedelta(minutes=1)),
                    _item("a2", "프로야구 선발투수 예고", window_start + timedelta(minutes=2)),
                    _item("c1", "LG 오스틴 선수 전날 햄스트링 통증 호소", context_time),
                ],
            )
            decisions = [
                FreshWindowTopicDecision(
                    decision="publish",
                    issue_score=86,
                    notification_level="immediate",
                    topic_name="LG 오스틴 부상 말소",
                    group_key="lg:austin:injury-roster",
                    dedupe_key="lg:austin:injury",
                    representative_article_id="a1",
                    target_article_ids=["a1"],
                    related_article_ids=["c1"],
                    reason_summary="10분 batch 안에서 1군 말소 보도가 확인됐다.",
                    risk_flags=[],
                    metadata={"raw_decision": {"decision": "publish"}},
                ),
                FreshWindowTopicDecision(
                    decision="reject",
                    issue_score=20,
                    notification_level="watch",
                    topic_name="선발 예고",
                    group_key="probable-starters",
                    dedupe_key="probable-starters",
                    representative_article_id="a2",
                    target_article_ids=["a2"],
                    related_article_ids=[],
                    reason_summary="선발 예고성 기사다.",
                    risk_flags=["probable_starter"],
                    metadata={"raw_decision": {"decision": "reject"}},
                ),
            ]

            with (
                patch("kbo_card_news.automation.fresh_window_decision.CollectorService", _NoopCollectorService),
                patch("kbo_card_news.automation.fresh_window_decision.build_news_collectors", lambda: []),
                AutomationJobRepository(state_db) as repository,
            ):
                result = watch_fresh_window_once(
                    job_repository=repository,
                    source_db_path=source_db,
                    config=FreshWindowDecisionConfig(),
                    now=window_end,
                    collection_window_start=window_start,
                    collection_window_end=window_end,
                    decision_engine=_FakeDecisionEngine(decisions),
                )
                logged = repository.list_recent_fresh_window_decisions(days=1, limit=10)

            self.assertEqual(result.decision_count, 2)
            self.assertEqual(len(result.created_jobs), 1)
            self.assertEqual(result.created_jobs[0].topic_name, "LG 오스틴 부상 말소")
            self.assertEqual([article.article_id for article in result.created_jobs[0].articles], ["a1", "c1"])
            self.assertEqual({record.decision for record in logged}, {"publish", "reject"})

    def test_invalid_publish_without_target_article_is_rejected(self) -> None:
        request = FreshWindowDecisionRequest(
            run_id="run",
            collection_window_start=datetime(2026, 5, 12, 10, 0, tzinfo=timezone.utc),
            collection_window_end=datetime(2026, 5, 12, 10, 10, tzinfo=timezone.utc),
            context_window_start=datetime(2026, 5, 12, 9, 0, tzinfo=timezone.utc),
            context_window_end=datetime(2026, 5, 12, 10, 0, tzinfo=timezone.utc),
            target_articles=[],
            historical_context_articles=[],
            feedback_examples={"positive": [], "negative": []},
            completed_topics=[],
        )
        decisions = parse_fresh_window_decisions(
            {
                "topic_decisions": [
                    {
                        "decision": "publish",
                        "issue_score": 80,
                        "notification_level": "immediate",
                        "topic_name": "context only",
                        "group_key": "context",
                        "dedupe_key": "context",
                        "representative_article_id": "",
                        "target_article_ids": [],
                        "related_article_ids": [],
                        "reason_summary": "",
                        "risk_flags": [],
                    }
                ]
            },
            request=request,
        )

        self.assertEqual(decisions[0].decision, "reject")

    def test_publish_article_ids_are_normalized_without_changing_decision(self) -> None:
        request = FreshWindowDecisionRequest(
            run_id="run",
            collection_window_start=datetime(2026, 5, 12, 10, 0, tzinfo=timezone.utc),
            collection_window_end=datetime(2026, 5, 12, 10, 10, tzinfo=timezone.utc),
            context_window_start=datetime(2026, 5, 12, 9, 0, tzinfo=timezone.utc),
            context_window_end=datetime(2026, 5, 12, 10, 0, tzinfo=timezone.utc),
            target_articles=[SimpleNamespace(article_id="target-1")],
            historical_context_articles=[SimpleNamespace(article_id="context-1")],
            feedback_examples={"positive": [], "negative": []},
            completed_topics=[],
        )

        decisions = parse_fresh_window_decisions(
            {
                "topic_decisions": [
                    {
                        "decision": "publish",
                        "issue_score": 80,
                        "notification_level": "immediate",
                        "topic_name": "최정 기록",
                        "group_key": "choi-record",
                        "dedupe_key": "choi-record",
                        "representative_article_id": "target-1",
                        "target_article_ids": ["context-1"],
                        "related_article_ids": ["target-1", "context-1"],
                        "reason_summary": "target window 기사에 근거가 있다.",
                        "risk_flags": [],
                    }
                ]
            },
            request=request,
        )

        self.assertEqual(decisions[0].decision, "publish")
        self.assertEqual(decisions[0].target_article_ids, ["target-1"])
        self.assertEqual(decisions[0].related_article_ids, ["context-1"])

    def test_publish_without_any_target_article_id_is_downgraded_to_reject(self) -> None:
        request = FreshWindowDecisionRequest(
            run_id="run",
            collection_window_start=datetime(2026, 5, 12, 10, 0, tzinfo=timezone.utc),
            collection_window_end=datetime(2026, 5, 12, 10, 10, tzinfo=timezone.utc),
            context_window_start=datetime(2026, 5, 12, 9, 0, tzinfo=timezone.utc),
            context_window_end=datetime(2026, 5, 12, 10, 0, tzinfo=timezone.utc),
            target_articles=[SimpleNamespace(article_id="target-1")],
            historical_context_articles=[SimpleNamespace(article_id="context-1")],
            feedback_examples={"positive": [], "negative": []},
            completed_topics=[],
        )

        decisions = parse_fresh_window_decisions(
            {
                "topic_decisions": [
                    {
                        "decision": "publish",
                        "issue_score": 80,
                        "notification_level": "immediate",
                        "topic_name": "context only",
                        "group_key": "context",
                        "dedupe_key": "context",
                        "representative_article_id": "context-1",
                        "target_article_ids": ["context-1"],
                        "related_article_ids": [],
                        "reason_summary": "context 기사만 있다.",
                        "risk_flags": [],
                    }
                ]
            },
            request=request,
        )

        self.assertEqual(decisions[0].decision, "reject")
        self.assertEqual(decisions[0].target_article_ids, [])
        self.assertEqual(decisions[0].related_article_ids, ["context-1"])

    def test_model_failure_creates_no_job_and_writes_no_decision_report(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            source_db = tmp_path / "source.db"
            state_db = tmp_path / "state.db"
            window_start = datetime(2026, 5, 12, 10, 0, tzinfo=timezone.utc)
            window_end = window_start + timedelta(minutes=10)
            _seed_source_items(
                source_db,
                [_item("a1", "LG 오스틴 1군 말소, 검진 예정", window_start + timedelta(minutes=1))],
            )
            with (
                patch("kbo_card_news.automation.fresh_window_decision.CollectorService", _NoopCollectorService),
                patch("kbo_card_news.automation.fresh_window_decision.build_news_collectors", lambda: []),
                AutomationJobRepository(state_db) as repository,
            ):
                result = watch_fresh_window_once(
                    job_repository=repository,
                    source_db_path=source_db,
                    now=window_end,
                    collection_window_start=window_start,
                    collection_window_end=window_end,
                    decision_engine=_FakeDecisionEngine(error=RuntimeError("quota exhausted")),
                )

            self.assertEqual(result.model_call_status, "no_decision")
            self.assertIn("quota exhausted", result.model_error)
            self.assertEqual(len(result.created_jobs), 0)
            self.assertTrue(result.report_path.exists())


def _seed_source_items(path: Path, items: list[PersistedSourceItem]) -> None:
    with SQLiteSourceItemRepository(str(path)) as repository:
        for item in items:
            repository.insert(item)


def _item(item_id: str, title: str, published_at: datetime) -> PersistedSourceItem:
    return PersistedSourceItem(
        item=SourceItemRecord(
            id=item_id,
            source_type="news",
            source_item_type="article",
            source_url=f"https://example.com/{item_id}",
            source_external_id=None,
            title=title,
            body_text=title,
            author_name=None,
            published_at=published_at,
            collected_at=published_at,
            language_code="ko",
            engagement_view_count=0,
            engagement_like_count=0,
            engagement_comment_count=0,
            engagement_share_count=0,
            status="collected",
            raw_payload={},
            content_hash=f"hash-{item_id}",
            created_at=published_at,
            updated_at=published_at,
            excerpt_text=title,
        ),
        assets=[],
    )


if __name__ == "__main__":
    unittest.main()
