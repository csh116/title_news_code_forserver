from __future__ import annotations

import json
import os
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Protocol

from kbo_card_news.automation.job_deduplication import (
    JobFingerprint,
    find_duplicate_job_by_fingerprint,
    fingerprint_metadata,
    hash_parts,
    normalize_topic_key,
    normalize_url,
)
from kbo_card_news.automation.job_state import (
    AUTOMATION_OUTPUT_DIR,
    AutomationJob,
    AutomationJobArticle,
    AutomationJobRepository,
    FreshWindowDecisionRecord,
    build_job_id,
    utc_now,
)
from kbo_card_news.automation.news_collection import build_news_collectors
from kbo_card_news.collectors.service import CollectorService
from kbo_card_news.config.env import load_default_env
from kbo_card_news.models.issue import BatchArticleCandidate
from kbo_card_news.pipeline.issue_feed import StoredArticleBatchBuilder
from kbo_card_news.pipeline.storage import (
    PersistedSourceItem,
    SQLiteSourceItemRepository,
    SourceItemIngestionService,
)
from kbo_card_news.runtime.model_fallback import build_model_fallback_policy, call_with_fallback
from kbo_card_news.scoring.engine import HttpTransport, UrllibHttpTransport
from kbo_card_news.workflows.approval_history import load_completed_topic_registry

ROOT_DIR = Path(__file__).resolve().parents[3]
OUTPUT_ROOT = ROOT_DIR / "outputs"
SOURCE_DB_PATH = OUTPUT_ROOT / "source_collection.db"
KST = timezone(timedelta(hours=9))
PROMPT_VERSION = "fresh-window-decision.v1"


FRESH_WINDOW_DECISION_JSON_SCHEMA = {
    "type": "object",
    "properties": {
        "topic_decisions": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "decision": {"type": "string"},
                    "issue_score": {"type": "number"},
                    "notification_level": {"type": "string"},
                    "topic_name": {"type": "string"},
                    "group_key": {"type": "string"},
                    "dedupe_key": {"type": "string"},
                    "representative_article_id": {"type": "string"},
                    "target_article_ids": {"type": "array", "items": {"type": "string"}},
                    "related_article_ids": {"type": "array", "items": {"type": "string"}},
                    "reason_summary": {"type": "string"},
                    "risk_flags": {"type": "array", "items": {"type": "string"}},
                    "matched_positive_examples": {"type": "array", "items": {"type": "string"}},
                    "matched_negative_examples": {"type": "array", "items": {"type": "string"}},
                },
                "required": [
                    "decision",
                    "issue_score",
                    "notification_level",
                    "topic_name",
                    "group_key",
                    "dedupe_key",
                    "representative_article_id",
                    "target_article_ids",
                    "related_article_ids",
                    "reason_summary",
                    "risk_flags",
                ],
            },
        }
    },
    "required": ["topic_decisions"],
}


@dataclass(slots=True)
class FreshWindowDecisionConfig:
    collection_window_minutes: int = 10
    context_window_hours: int = 24
    feedback_lookback_days: int = 90
    duplicate_lookback_hours: int = 72
    max_target_articles_per_call: int = 40
    max_context_articles: int = 180
    max_feedback_examples: int = 40
    model_name: str = "gemini-3.1-flash-lite-preview"


@dataclass(slots=True)
class FreshWindowTopicDecision:
    decision: str
    issue_score: float
    notification_level: str
    topic_name: str
    group_key: str
    dedupe_key: str
    representative_article_id: str
    target_article_ids: list[str]
    related_article_ids: list[str]
    reason_summary: str
    risk_flags: list[str]
    matched_positive_examples: list[str] = field(default_factory=list)
    matched_negative_examples: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class FreshWindowDecisionRequest:
    run_id: str
    collection_window_start: datetime
    collection_window_end: datetime
    context_window_start: datetime
    context_window_end: datetime
    target_articles: list[BatchArticleCandidate]
    historical_context_articles: list[BatchArticleCandidate]
    feedback_examples: dict[str, list[dict[str, Any]]]
    completed_topics: list[dict[str, Any]]


@dataclass(slots=True)
class FreshWindowDecisionModelResult:
    model_name: str
    prompt_version: str
    decisions: list[FreshWindowTopicDecision]
    raw_payload: dict[str, Any]
    prompt: str = ""
    response_text: str = ""
    status: str = "ok"
    error: str = ""


@dataclass(slots=True)
class FreshWindowDecisionResult:
    collection_window_start: datetime
    collection_window_end: datetime
    context_window_start: datetime
    context_window_end: datetime
    collected_count: int
    inserted_count: int
    duplicate_count: int
    target_article_count: int
    context_article_count: int
    decision_count: int
    created_jobs: list[AutomationJob]
    duplicate_jobs: list[AutomationJob]
    skipped_count: int
    collector_errors: list[str]
    report_path: Path
    choice_json_path: Path
    prompt_path: Path
    response_path: Path
    decisions: list[FreshWindowTopicDecision]
    model_name: str
    prompt_version: str
    model_call_status: str
    model_error: str = ""


class FreshWindowDecisionEngine(Protocol):
    def decide(self, request: FreshWindowDecisionRequest) -> FreshWindowDecisionModelResult:
        ...


class GeminiFreshWindowDecisionEngine:
    def __init__(
        self,
        *,
        api_key: str | None = None,
        model_name: str = "gemini-3.1-flash-lite-preview",
        prompt_version: str = PROMPT_VERSION,
        transport: HttpTransport | None = None,
        endpoint_base: str = "https://generativelanguage.googleapis.com/v1beta/models",
        max_attempts: int = 2,
        retry_delay_seconds: float = 10.0,
    ) -> None:
        load_default_env()
        self.api_key = api_key or os.getenv("GEMINI_API_KEY")
        self.model_name = model_name
        self.prompt_version = prompt_version
        self.transport = transport or UrllibHttpTransport()
        self.endpoint_base = endpoint_base.rstrip("/")
        self.model_policy = [
            candidate
            for candidate in build_model_fallback_policy(model_name)
            if candidate.startswith("gemini")
        ]
        self.max_attempts = max(1, int(max_attempts))
        self.retry_delay_seconds = max(0.0, float(retry_delay_seconds))

    def decide(self, request: FreshWindowDecisionRequest) -> FreshWindowDecisionModelResult:
        if not self.api_key:
            raise RuntimeError("GEMINI_API_KEY is required for the fresh window decision gate")
        call_result = call_with_fallback(
            self.model_policy,
            request,
            prompt_builder=build_fresh_window_decision_prompt,
            schema_name="fresh_window_decision",
            json_schema=FRESH_WINDOW_DECISION_JSON_SCHEMA,
            validator=lambda parsed: validate_fresh_window_response(parsed, request=request),
            transport=self.transport,
            gemini_api_key=self.api_key,
            openai_api_key=None,
            gemini_endpoint_base=self.endpoint_base,
            max_attempts_per_model=self.max_attempts,
            retry_delay_seconds=self.retry_delay_seconds,
        )
        decisions = parse_fresh_window_decisions(call_result.parsed, request=request)
        return FreshWindowDecisionModelResult(
            model_name=call_result.model_name,
            prompt_version=self.prompt_version,
            decisions=decisions,
            raw_payload=call_result.parsed,
            prompt=call_result.prompt,
            response_text=call_result.response_text,
            status="ok",
        )


def watch_fresh_window_once(
    *,
    job_repository: AutomationJobRepository,
    source_db_path: str | Path = SOURCE_DB_PATH,
    config: FreshWindowDecisionConfig | None = None,
    now: datetime | None = None,
    collection_window_start: datetime | None = None,
    collection_window_end: datetime | None = None,
    decision_engine: FreshWindowDecisionEngine | None = None,
) -> FreshWindowDecisionResult:
    config = config or FreshWindowDecisionConfig()
    current = _normalize_now(now)
    collection_start = (
        _normalize_now(collection_window_start)
        if collection_window_start
        else current - timedelta(minutes=max(1, int(config.collection_window_minutes)))
    )
    collection_end = _normalize_now(collection_window_end) if collection_window_end else current
    if collection_start >= collection_end:
        raise ValueError("collection_window_start must be earlier than collection_window_end")
    context_start = collection_start - timedelta(hours=max(1, int(config.context_window_hours)))
    run_id = _fresh_run_id(current)
    run_dir = AUTOMATION_OUTPUT_DIR / "fresh_watch_runs" / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    report_path = run_dir / "fresh_window_decision_report.json"
    prompt_path = run_dir / "fresh_window_decision_prompt.json"
    response_path = run_dir / "fresh_window_decision_response.json"
    choice_json_path = run_dir / "topic_selection_choice.json"
    candidate_report_path = run_dir / "topic_candidates_report.json"

    with SQLiteSourceItemRepository(str(Path(source_db_path).expanduser())) as source_repository:
        collector_result = CollectorService(build_news_collectors()).collect_all(
            window_start=collection_start,
            window_end=collection_end,
        )
        ingestion_result = SourceItemIngestionService(repository=source_repository).ingest(collector_result.items)
        source_repository.save_collection_window(
            window_start=collection_start,
            window_end=collection_end,
            status="completed" if not collector_result.errors else "partial",
            item_count=len(collector_result.items),
            inserted_count=len(ingestion_result.inserted),
            duplicate_count=len(ingestion_result.duplicates),
            errors=collector_result.errors,
        )
        target_items = source_repository.list_items_published_between(
            window_start=collection_start,
            window_end=collection_end,
            limit=max(1, int(config.max_target_articles_per_call) * 3),
        )
        context_items = source_repository.list_items_published_between(
            window_start=context_start,
            window_end=collection_start,
            limit=max(1, int(config.max_context_articles) * 3),
        )

    target_batch = StoredArticleBatchBuilder().build(
        target_items,
        batch_id=f"{run_id}:target",
        window_start=collection_start,
        window_end=collection_end,
    )
    context_batch = StoredArticleBatchBuilder().build(
        context_items,
        batch_id=f"{run_id}:context",
        window_start=context_start,
        window_end=collection_start,
    )
    target_articles = target_batch.articles[: max(1, int(config.max_target_articles_per_call))]
    context_articles = _select_context_articles(
        target_articles=target_articles,
        context_articles=context_batch.articles,
        limit=config.max_context_articles,
    )
    feedback_examples = _build_feedback_examples(
        job_repository,
        target_articles=target_articles,
        lookback_days=config.feedback_lookback_days,
        limit=config.max_feedback_examples,
    )
    completed_topics = _load_completed_topics(limit=config.max_feedback_examples)
    request = FreshWindowDecisionRequest(
        run_id=run_id,
        collection_window_start=collection_start,
        collection_window_end=collection_end,
        context_window_start=context_start,
        context_window_end=collection_start,
        target_articles=target_articles,
        historical_context_articles=context_articles,
        feedback_examples=feedback_examples,
        completed_topics=completed_topics,
    )
    prompt_path.write_text(
        json.dumps(build_fresh_window_payload(request), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    model_result = FreshWindowDecisionModelResult(
        model_name=config.model_name,
        prompt_version=PROMPT_VERSION,
        decisions=[],
        raw_payload={"topic_decisions": []},
        status="skipped_empty_target",
    )
    if target_articles:
        engine = decision_engine or GeminiFreshWindowDecisionEngine(model_name=config.model_name)
        try:
            model_result = engine.decide(request)
        except Exception as exc:
            model_result = FreshWindowDecisionModelResult(
                model_name=config.model_name,
                prompt_version=PROMPT_VERSION,
                decisions=[],
                raw_payload={"topic_decisions": []},
                status="no_decision",
                error=str(exc),
            )
    response_path.write_text(
        json.dumps(
            {
                "status": model_result.status,
                "model_name": model_result.model_name,
                "prompt_version": model_result.prompt_version,
                "error": model_result.error,
                "raw_payload": model_result.raw_payload,
                "response_text": model_result.response_text,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    article_lookup = {article.article_id: article for article in [*target_articles, *context_articles]}
    persisted_lookup = {item.item.id: item for item in [*target_items, *context_items]}
    created_jobs: list[AutomationJob] = []
    duplicate_jobs: list[AutomationJob] = []
    skipped_count = 0
    publish_decisions: list[FreshWindowTopicDecision] = []

    publish_sequence = 0
    for decision in model_result.decisions:
        stored_decision = job_repository.create_fresh_window_decision(
            FreshWindowDecisionRecord(
                id=None,
                run_id=run_id,
                decision=decision.decision,
                issue_score=decision.issue_score,
                notification_level=decision.notification_level,
                topic_name=decision.topic_name,
                group_key=decision.group_key,
                dedupe_key=decision.dedupe_key,
                representative_article_id=decision.representative_article_id,
                target_article_ids=decision.target_article_ids,
                related_article_ids=decision.related_article_ids,
                reason_summary=decision.reason_summary,
                risk_flags=decision.risk_flags,
                model_name=model_result.model_name,
                prompt_version=model_result.prompt_version,
                raw_decision=decision.metadata.get("raw_decision") if isinstance(decision.metadata.get("raw_decision"), dict) else asdict(decision),
            )
        )
        if decision.decision != "publish":
            skipped_count += 1
            continue
        publish_sequence += 1
        fingerprint = _decision_fingerprint(decision, article_lookup=article_lookup)
        topic_id = f"fresh-decision-{hash_parts([decision.dedupe_key, decision.group_key, decision.topic_name])}"
        existing, duplicate_reason = find_duplicate_job_by_fingerprint(
            job_repository,
            topic_id=topic_id,
            fingerprint=fingerprint,
            lookback_hours=config.duplicate_lookback_hours,
        )
        if existing is not None:
            job_repository.record_event(
                existing.job_id,
                "duplicate_fresh_window_decision_seen",
                message=f"duplicate fresh window decision seen: {duplicate_reason}",
                metadata={
                    **fingerprint_metadata(fingerprint, duplicate_lookback_hours=config.duplicate_lookback_hours),
                    "duplicate_match_reason": duplicate_reason,
                    "fresh_window_run_id": run_id,
                    "fresh_window_decision_id": stored_decision.id,
                },
            )
            duplicate_jobs.append(existing)
            continue
        job = _job_from_decision(
            decision,
            topic_id=topic_id,
            run_dir=run_dir,
            choice_json_path=choice_json_path,
            fingerprint=fingerprint,
            config=config,
            collection_start=collection_start,
            collection_end=collection_end,
            context_start=context_start,
            context_end=collection_start,
            article_lookup=article_lookup,
            sequence=publish_sequence,
        )
        created = job_repository.create_job(job)
        if stored_decision.id is not None:
            job_repository.update_fresh_window_decision_job(stored_decision.id, created.job_id)
        created_jobs.append(created)
        publish_decisions.append(decision)

    _write_compatible_candidate_report(
        candidate_report_path,
        decisions=publish_decisions,
        article_lookup=article_lookup,
        source_db_path=Path(source_db_path).expanduser(),
        collection_start=collection_start,
        collection_end=collection_end,
        model_result=model_result,
        run_id=run_id,
    )
    _write_choice_json(
        choice_json_path,
        decisions=publish_decisions,
        article_lookup=article_lookup,
        candidate_report_path=candidate_report_path,
    )
    result = FreshWindowDecisionResult(
        collection_window_start=collection_start,
        collection_window_end=collection_end,
        context_window_start=context_start,
        context_window_end=collection_start,
        collected_count=len(collector_result.items),
        inserted_count=len(ingestion_result.inserted),
        duplicate_count=len(ingestion_result.duplicates),
        target_article_count=len(target_articles),
        context_article_count=len(context_articles),
        decision_count=len(model_result.decisions),
        created_jobs=created_jobs,
        duplicate_jobs=duplicate_jobs,
        skipped_count=skipped_count,
        collector_errors=collector_result.errors,
        report_path=report_path,
        choice_json_path=choice_json_path,
        prompt_path=prompt_path,
        response_path=response_path,
        decisions=model_result.decisions,
        model_name=model_result.model_name,
        prompt_version=model_result.prompt_version,
        model_call_status=model_result.status,
        model_error=model_result.error,
    )
    report_path.write_text(json.dumps(fresh_window_decision_result_to_dict(result), ensure_ascii=False, indent=2), encoding="utf-8")
    return result


def build_fresh_window_decision_prompt(request: FreshWindowDecisionRequest) -> str:
    return (
        "You are the KBO fresh-window decision gate. Return JSON only.\n"
        "Decision unit: the target article batch whose published_at is inside the collection window.\n"
        "You may publish, hold, or reject topic groups. Return an empty topic_decisions array when nothing is worth tracking.\n"
        "Publish only when at least one target article supports the topic. Historical context is evidence only.\n"
        "Do not group articles merely because they share a team. Context URLs may be related_article_ids only when they are the same incident.\n"
        "Strong publish candidates: injury, surgery, long absence, first-team roster removal, discipline, controversy, apology, incident, trade, release, foreign-player replacement, manager change, playoff-impacting news, or multi-source spread.\n"
        "Default reject: previews, probable starters, standings-only, game result roundups, Futures League, repeated completed topics.\n"
        "Write topic_name and reason_summary in Korean. Use article IDs exactly as given.\n\n"
        "Payload JSON:\n"
        f"{json.dumps(build_fresh_window_payload(request), ensure_ascii=False, indent=2)}"
    )


def build_fresh_window_payload(request: FreshWindowDecisionRequest) -> dict[str, Any]:
    return {
        "task": "Analyze the target 10-minute KBO article batch with historical context and decide whether any publishable topic groups exist now.",
        "policy": {
            "decision_unit": "target_window_batch",
            "allowed_decisions": ["publish", "hold", "reject"],
            "publish_requires_target_article": True,
            "allow_empty_publish": True,
        },
        "collection_window": {
            "start": _serialize_datetime(request.collection_window_start),
            "end": _serialize_datetime(request.collection_window_end),
        },
        "target_articles": [_article_payload(article, excerpt_limit=500) for article in request.target_articles],
        "historical_context_articles": [
            _article_payload(article, excerpt_limit=350)
            for article in request.historical_context_articles
        ],
        "feedback_examples": request.feedback_examples,
        "completed_topics": request.completed_topics,
    }


def validate_fresh_window_response(parsed: dict[str, Any], *, request: FreshWindowDecisionRequest) -> None:
    parse_fresh_window_decisions(parsed, request=request)


def parse_fresh_window_decisions(
    parsed: dict[str, Any],
    *,
    request: FreshWindowDecisionRequest,
) -> list[FreshWindowTopicDecision]:
    raw_decisions = parsed.get("topic_decisions")
    if not isinstance(raw_decisions, list):
        raise ValueError("fresh window response missing topic_decisions array")
    target_ids = {article.article_id for article in request.target_articles}
    all_ids = target_ids | {article.article_id for article in request.historical_context_articles}
    decisions: list[FreshWindowTopicDecision] = []
    for raw in raw_decisions:
        if not isinstance(raw, dict):
            raise ValueError("topic_decisions items must be objects")
        decision = str(raw.get("decision") or "").strip().lower()
        if decision not in {"publish", "hold", "reject"}:
            raise ValueError(f"invalid fresh window decision: {decision}")
        target_article_ids = _string_list(raw.get("target_article_ids"))
        related_article_ids = _string_list(raw.get("related_article_ids"))
        representative_article_id = str(raw.get("representative_article_id") or "").strip()
        unknown_ids = [article_id for article_id in [*target_article_ids, *related_article_ids, representative_article_id] if article_id and article_id not in all_ids]
        if unknown_ids:
            raise ValueError(f"fresh window response referenced unknown article ids: {unknown_ids}")
        if decision == "publish":
            required = ["topic_name", "group_key", "dedupe_key", "representative_article_id"]
            missing = [key for key in required if not str(raw.get(key) or "").strip()]
            if missing:
                raise ValueError(f"publish decision missing required fields: {missing}")
            if not target_ids.intersection(target_article_ids):
                raise ValueError("publish decision must include at least one target article id")
            if representative_article_id not in all_ids:
                raise ValueError("publish representative_article_id must exist in input articles")
        decisions.append(
            FreshWindowTopicDecision(
                decision=decision,
                issue_score=float(raw.get("issue_score") or 0.0),
                notification_level=str(raw.get("notification_level") or "watch").strip() or "watch",
                topic_name=str(raw.get("topic_name") or "").strip(),
                group_key=str(raw.get("group_key") or "").strip(),
                dedupe_key=str(raw.get("dedupe_key") or "").strip(),
                representative_article_id=representative_article_id,
                target_article_ids=target_article_ids,
                related_article_ids=related_article_ids,
                reason_summary=str(raw.get("reason_summary") or "").strip(),
                risk_flags=_string_list(raw.get("risk_flags")),
                matched_positive_examples=_string_list(raw.get("matched_positive_examples")),
                matched_negative_examples=_string_list(raw.get("matched_negative_examples")),
                metadata={"raw_decision": raw},
            )
        )
    return decisions


def fresh_window_decision_result_to_dict(result: FreshWindowDecisionResult) -> dict[str, Any]:
    publish_count = sum(1 for decision in result.decisions if decision.decision == "publish")
    hold_count = sum(1 for decision in result.decisions if decision.decision == "hold")
    reject_count = sum(1 for decision in result.decisions if decision.decision == "reject")
    return {
        "collection_window_start": result.collection_window_start.isoformat(),
        "collection_window_end": result.collection_window_end.isoformat(),
        "context_window_start": result.context_window_start.isoformat(),
        "context_window_end": result.context_window_end.isoformat(),
        "collected_count": result.collected_count,
        "inserted_count": result.inserted_count,
        "duplicate_count": result.duplicate_count,
        "target_article_count": result.target_article_count,
        "context_article_count": result.context_article_count,
        "decision_count": result.decision_count,
        "publish_count": publish_count,
        "hold_count": hold_count,
        "reject_count": reject_count,
        "created_count": len(result.created_jobs),
        "duplicate_job_count": len(result.duplicate_jobs),
        "skipped_count": result.skipped_count,
        "collector_errors": result.collector_errors,
        "model_name": result.model_name,
        "prompt_version": result.prompt_version,
        "model_call_status": result.model_call_status,
        "model_error": result.model_error,
        "report_path": str(result.report_path),
        "choice_json_path": str(result.choice_json_path),
        "prompt_path": str(result.prompt_path),
        "response_path": str(result.response_path),
        "created_jobs": [_job_summary(job) for job in result.created_jobs],
        "duplicate_jobs": [_job_summary(job) for job in result.duplicate_jobs],
        "decisions": [asdict(decision) for decision in result.decisions],
    }


def _select_context_articles(
    *,
    target_articles: list[BatchArticleCandidate],
    context_articles: list[BatchArticleCandidate],
    limit: int,
) -> list[BatchArticleCandidate]:
    if not context_articles:
        return []
    tokens = _target_tokens(target_articles)
    scored: list[tuple[float, BatchArticleCandidate]] = []
    for article in context_articles:
        article_tokens = _target_tokens([article])
        overlap = len(tokens.intersection(article_tokens))
        reference = article.published_at or article.collected_at
        age_hours = max(0.0, (_normalize_now(datetime.now(timezone.utc)) - _normalize_now(reference)).total_seconds() / 3600)
        recency_score = max(0.0, 72.0 - age_hours) / 72.0
        scored.append((overlap * 10.0 + recency_score, article))
    scored.sort(key=lambda item: (item[0], item[1].published_at or item[1].collected_at), reverse=True)
    return [article for _score, article in scored[: max(0, int(limit))]]


def _build_feedback_examples(
    repository: AutomationJobRepository,
    *,
    target_articles: list[BatchArticleCandidate],
    lookback_days: int,
    limit: int,
) -> dict[str, list[dict[str, Any]]]:
    tokens = _target_tokens(target_articles)
    positive_statuses = {"approved", "pipeline_running", "editor_ready", "render_ready", "publish_approved", "published"}
    negative_statuses = {"skipped", "expired"}
    positive: list[dict[str, Any]] = []
    negative: list[dict[str, Any]] = []
    for job in repository.list_recent_jobs(hours=max(24, int(lookback_days) * 24), limit=max(100, int(limit) * 4)):
        row = {
            "job_id": job.job_id,
            "topic_name": job.topic_name,
            "status": job.status,
            "article_titles": [article.title for article in job.articles[:5]],
            "reason": job.recommendation_summary,
        }
        score = len(tokens.intersection(_tokens_for_text(" ".join([job.topic_name, job.recommendation_summary, *row["article_titles"]]))))
        row["_score"] = score
        if job.status in positive_statuses:
            positive.append(row)
        elif job.status in negative_statuses:
            negative.append(row)
    for decision in repository.list_recent_fresh_window_decisions(days=lookback_days, limit=max(50, int(limit) * 2)):
        if decision.decision == "reject":
            negative.append(
                {
                    "job_id": decision.run_id,
                    "topic_name": decision.topic_name,
                    "status": "fresh_window_reject",
                    "article_titles": [],
                    "reason": decision.reason_summary,
                    "_score": len(tokens.intersection(_tokens_for_text(f"{decision.topic_name} {decision.reason_summary}"))),
                }
            )
    positive = _rank_feedback(positive, limit=limit // 2)
    negative = _rank_feedback(negative, limit=limit - len(positive))
    return {"positive": positive, "negative": negative}


def _rank_feedback(rows: list[dict[str, Any]], *, limit: int) -> list[dict[str, Any]]:
    rows.sort(key=lambda row: int(row.get("_score") or 0), reverse=True)
    cleaned: list[dict[str, Any]] = []
    for row in rows[: max(0, int(limit))]:
        cleaned.append({key: value for key, value in row.items() if key != "_score"})
    return cleaned


def _load_completed_topics(*, limit: int) -> list[dict[str, Any]]:
    payload = load_completed_topic_registry()
    topics = payload.get("topics") if isinstance(payload, dict) else []
    if not isinstance(topics, list):
        return []
    rows = [item for item in topics if isinstance(item, dict)]
    return rows[-max(0, int(limit)) :]


def _decision_fingerprint(
    decision: FreshWindowTopicDecision,
    *,
    article_lookup: dict[str, BatchArticleCandidate],
) -> JobFingerprint:
    articles = _decision_articles(decision, article_lookup=article_lookup)
    article_urls = [normalize_url(article.source_url) for article in articles if normalize_url(article.source_url)]
    representative = article_lookup.get(decision.representative_article_id)
    return JobFingerprint(
        topic_fingerprint=f"fresh-decision:{normalize_topic_key(decision.dedupe_key)}",
        representative_article_url=normalize_url(representative.source_url if representative else ""),
        article_url_fingerprint=hash_parts(sorted(article_urls)),
        normalized_topic_key=normalize_topic_key(f"{decision.topic_name} {decision.dedupe_key}"),
        article_urls=article_urls,
    )


def _job_from_decision(
    decision: FreshWindowTopicDecision,
    *,
    topic_id: str,
    run_dir: Path,
    choice_json_path: Path,
    fingerprint: JobFingerprint,
    config: FreshWindowDecisionConfig,
    collection_start: datetime,
    collection_end: datetime,
    context_start: datetime,
    context_end: datetime,
    article_lookup: dict[str, BatchArticleCandidate],
    sequence: int,
) -> AutomationJob:
    articles = _decision_articles(decision, article_lookup=article_lookup)[:5]
    metadata = {
        "source": "watch_fresh_window_once",
        "fresh_decision_gate": True,
        "fresh_watch_run_dir": str(run_dir),
        "choice_json_path": str(choice_json_path),
        "issue_score": decision.issue_score,
        "decision": decision.decision,
        "notification_level": decision.notification_level,
        "group_key": decision.group_key,
        "dedupe_key": decision.dedupe_key,
        "risk_flags": decision.risk_flags,
        "target_article_count": len(decision.target_article_ids),
        "related_article_count": len(decision.related_article_ids),
        "target_article_ids": decision.target_article_ids,
        "related_article_ids": decision.related_article_ids,
        "collection_window_start": collection_start.isoformat(),
        "collection_window_end": collection_end.isoformat(),
        "context_window_start": context_start.isoformat(),
        "context_window_end": context_end.isoformat(),
        **fingerprint_metadata(fingerprint, duplicate_lookback_hours=config.duplicate_lookback_hours),
    }
    return AutomationJob(
        job_id=build_job_id(timestamp=datetime.now(KST), sequence=sequence),
        topic_id=topic_id,
        topic_name=decision.topic_name,
        status="detected",
        notification_level=decision.notification_level,
        virality_potential_score=decision.issue_score,
        account_fit_score=80.0,
        recommendation_summary=decision.reason_summary,
        metadata=metadata,
        articles=[
            AutomationJobArticle(
                article_id=article.article_id,
                title=article.title,
                source_type=article.source_type,
                source_url=article.source_url,
                published_at=article.published_at.isoformat() if article.published_at else None,
            )
            for article in articles
        ],
    )


def _decision_articles(
    decision: FreshWindowTopicDecision,
    *,
    article_lookup: dict[str, BatchArticleCandidate],
) -> list[BatchArticleCandidate]:
    ids = [*decision.target_article_ids, *decision.related_article_ids]
    if decision.representative_article_id and decision.representative_article_id not in ids:
        ids.insert(0, decision.representative_article_id)
    seen: set[str] = set()
    articles: list[BatchArticleCandidate] = []
    for article_id in ids:
        if article_id in seen:
            continue
        article = article_lookup.get(article_id)
        if article is None:
            continue
        seen.add(article_id)
        articles.append(article)
    articles.sort(key=lambda article: article.published_at or article.collected_at, reverse=True)
    return articles


def _write_choice_json(
    path: Path,
    *,
    decisions: list[FreshWindowTopicDecision],
    article_lookup: dict[str, BatchArticleCandidate],
    candidate_report_path: Path | None = None,
) -> None:
    payload = {
        "candidate_report_path": str(candidate_report_path) if candidate_report_path else None,
        "required_selection_count": 1 if decisions else 0,
        "selected_topic_ids": [],
        "candidates": [
            {
                "topic_id": f"fresh-decision-{hash_parts([decision.dedupe_key, decision.group_key, decision.topic_name])}",
                "topic_name": decision.topic_name,
                "importance_rank": index,
                "topic_score": decision.issue_score,
                "reason_summary": decision.reason_summary,
                "representative_article_id": decision.representative_article_id,
                "article_ids": decision.target_article_ids,
                "selected": False,
                "metadata": {
                    "article_publication_summary": {
                        "articles": [_article_summary(article) for article in _decision_articles(decision, article_lookup=article_lookup)[:5]],
                    },
                    "fresh_window_decision": asdict(decision),
                },
            }
            for index, decision in enumerate(decisions, start=1)
        ],
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _write_compatible_candidate_report(
    path: Path,
    *,
    decisions: list[FreshWindowTopicDecision],
    article_lookup: dict[str, BatchArticleCandidate],
    source_db_path: Path,
    collection_start: datetime,
    collection_end: datetime,
    model_result: FreshWindowDecisionModelResult,
    run_id: str,
) -> None:
    topics = []
    for index, decision in enumerate(decisions, start=1):
        topic_id = f"fresh-decision-{hash_parts([decision.dedupe_key, decision.group_key, decision.topic_name])}"
        topics.append(
            {
                "topic_id": topic_id,
                "topic_name": decision.topic_name,
                "importance_rank": index,
                "topic_score": decision.issue_score,
                "reason_summary": decision.reason_summary,
                "article_ids": list(decision.target_article_ids),
                "representative_article_id": decision.representative_article_id,
                "metadata": {
                    "article_publication_summary": {
                        "articles": [_article_summary(article) for article in _decision_articles(decision, article_lookup=article_lookup)[:5]],
                    },
                    "fresh_window_decision": asdict(decision),
                },
            }
        )
    payload = {
        "window_start_kst": collection_start.isoformat(),
        "window_end_kst": collection_end.isoformat(),
        "candidate_count": len(decisions),
        "collection_db_path": str(source_db_path),
        "collection_missing_windows": [],
        "collection_skipped_window_count": 0,
        "collected_count": None,
        "inserted_count": None,
        "duplicate_count": None,
        "collector_errors": [],
        "batch_article_count": len({article_id for decision in decisions for article_id in decision.target_article_ids}),
        "batch_metadata": {"source": "fresh_window_decision", "run_id": run_id},
        "completed_topic_registry_count": 0,
        "excluded_completed_topics": [],
        "selection_result": {
            "batch_id": f"{run_id}:fresh-window-publish",
            "model_name": model_result.model_name,
            "prompt_version": model_result.prompt_version,
            "topics": topics,
            "raw_payload": model_result.raw_payload,
            "created_at": datetime.now(KST).isoformat(),
        },
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _article_payload(article: BatchArticleCandidate, *, excerpt_limit: int) -> dict[str, Any]:
    return {
        "article_id": article.article_id,
        "title": article.title,
        "source_type": article.source_type,
        "source_url": article.source_url,
        "published_at": _serialize_datetime(article.published_at),
        "collected_at": _serialize_datetime(article.collected_at),
        "excerpt_text": _limit_text(article.excerpt_text or "", excerpt_limit),
        "metadata": article.metadata,
    }


def _article_summary(article: BatchArticleCandidate) -> dict[str, Any]:
    return {
        "article_id": article.article_id,
        "title": article.title,
        "source_type": article.source_type,
        "published_at": article.published_at.isoformat() if article.published_at else None,
        "source_url": article.source_url,
    }


def _job_summary(job: AutomationJob) -> dict[str, Any]:
    return {
        "job_id": job.job_id,
        "topic_id": job.topic_id,
        "topic_name": job.topic_name,
        "status": job.status,
        "notification_level": job.notification_level,
        "virality_potential_score": job.virality_potential_score,
    }


def _target_tokens(articles: list[BatchArticleCandidate]) -> set[str]:
    return _tokens_for_text(" ".join(f"{article.title} {article.excerpt_text or ''}" for article in articles))


def _tokens_for_text(value: str) -> set[str]:
    stopwords = {"프로야구", "야구", "기사", "경기", "선수", "감독", "구단", "시즌", "오늘", "오전", "오후", "기자"}
    tokens = {token.lower() for token in re.findall(r"[0-9A-Za-z가-힣]{2,}", value)}
    return {token for token in tokens if token not in stopwords}


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if str(item).strip()]


def _limit_text(value: str, limit: int) -> str:
    compact = " ".join(str(value or "").split()).strip()
    if len(compact) <= limit:
        return compact
    return compact[: max(0, limit - 1)].rstrip() + "..."


def _normalize_now(value: datetime | None) -> datetime:
    if value is None:
        return datetime.now(KST).replace(second=0, microsecond=0)
    if value.tzinfo is None:
        return value.replace(tzinfo=KST)
    return value.astimezone(KST)


def _serialize_datetime(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat()


def _fresh_run_id(now: datetime) -> str:
    return now.astimezone(KST).strftime("%Y%m%d_%H%M%S")
