from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[2]
SRC_DIR = ROOT_DIR / "src"
OUTPUT_ROOT = ROOT_DIR / "outputs"
PERSISTENT_COLLECTION_DB_PATH = OUTPUT_ROOT / "source_collection.db"

if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from kbo_card_news.collectors.news_sites import NEWS_SITE_DEFINITIONS, NewsSiteCollector, NewsSiteCollectorConfig  # noqa: E402
from kbo_card_news.collectors.service import CollectorService  # noqa: E402
from kbo_card_news.config.env import load_default_env  # noqa: E402
from kbo_card_news.pipeline import SQLiteSourceItemRepository, SourceItemIngestionService, StoredArticleBatchBuilder  # noqa: E402
from kbo_card_news.scoring import BatchIssueSelectionService, GeminiBatchIssueSelectionEngine, HeuristicBatchIssueSelectionEngine  # noqa: E402
from kbo_card_news.workflows import (  # noqa: E402
    build_topic_selection_template,
    ensure_stage_dir,
    load_completed_topic_registry,
    serialize_for_json,
)


KST = timezone(timedelta(hours=9))


def _default_window() -> tuple[datetime, datetime]:
    window_end = datetime.now(KST).replace(second=0, microsecond=0)
    window_start = window_end - timedelta(hours=24)
    return window_start, window_end


DEFAULT_START, DEFAULT_END = _default_window()
DEFAULT_CANDIDATE_COUNT = 10


def build_news_collectors() -> list[NewsSiteCollector]:
    return [
        NewsSiteCollector(
            NewsSiteCollectorConfig(
                definition=definition,
                default_page_limit=1,
                window_page_limit_min=8,
                window_page_limit_per_day=8,
                window_page_limit_max=40,
            )
        )
        for definition in NEWS_SITE_DEFINITIONS.values()
    ]


def choose_selection_engine():
    load_default_env(ROOT_DIR)
    if os.getenv("GEMINI_API_KEY"):
        return GeminiBatchIssueSelectionEngine()
    return HeuristicBatchIssueSelectionEngine()


def _parse_kst_datetime_input(raw: str, *, default_value: datetime) -> datetime:
    text = raw.strip()
    if not text:
        return default_value
    normalized = text.replace("T", " ")
    parsed = datetime.strptime(normalized, "%Y-%m-%d %H:%M")
    return parsed.replace(tzinfo=KST)


def _prompt_window() -> tuple[datetime, datetime]:
    print("window 입력 형식: YYYY-MM-DD HH:MM")
    print(f"start 기본값           : {DEFAULT_START.strftime('%Y-%m-%d %H:%M')} KST")
    print(f"end 기본값             : {DEFAULT_END.strftime('%Y-%m-%d %H:%M')} KST")

    try:
        start_raw = input("window_start_kst 입력 [엔터=기본값]: ")
    except EOFError:
        start_raw = ""
    try:
        end_raw = input("window_end_kst 입력   [엔터=기본값]: ")
    except EOFError:
        end_raw = ""

    window_start = _parse_kst_datetime_input(start_raw, default_value=DEFAULT_START)
    window_end = _parse_kst_datetime_input(end_raw, default_value=DEFAULT_END)
    if window_end <= window_start:
        raise ValueError("window_end_kst must be later than window_start_kst")
    return window_start, window_end


def _prompt_candidate_count() -> int:
    print(f"candidate_count 기본값 : {DEFAULT_CANDIDATE_COUNT}")
    try:
        raw = input("candidate_count 입력 [엔터=기본값]: ").strip()
    except EOFError:
        raw = ""
    if not raw:
        return DEFAULT_CANDIDATE_COUNT
    candidate_count = int(raw)
    if candidate_count <= 0:
        raise ValueError("candidate_count must be positive")
    return candidate_count


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Collect fresh news and select topic candidates.")
    parser.add_argument("--window-start-kst", help="YYYY-MM-DD HH:MM. Defaults to now-24h in KST.")
    parser.add_argument("--window-end-kst", help="YYYY-MM-DD HH:MM. Defaults to now in KST.")
    parser.add_argument("--candidate-count", type=int, help=f"Candidate count. Default: {DEFAULT_CANDIDATE_COUNT}.")
    parser.add_argument("--non-interactive", action="store_true", help="Use defaults instead of prompting.")
    return parser.parse_args()


def _resolve_window_and_count(args: argparse.Namespace) -> tuple[datetime, datetime, int]:
    has_explicit_value = bool(args.window_start_kst or args.window_end_kst or args.candidate_count is not None)
    if args.non_interactive or has_explicit_value:
        window_start = _parse_kst_datetime_input(args.window_start_kst or "", default_value=DEFAULT_START)
        window_end = _parse_kst_datetime_input(args.window_end_kst or "", default_value=DEFAULT_END)
        if window_end <= window_start:
            raise ValueError("window_end_kst must be later than window_start_kst")
        candidate_count = int(args.candidate_count or DEFAULT_CANDIDATE_COUNT)
        if candidate_count <= 0:
            raise ValueError("candidate_count must be positive")
        return window_start, window_end, candidate_count
    window_start, window_end = _prompt_window()
    candidate_count = _prompt_candidate_count()
    return window_start, window_end, candidate_count


def _normalize_datetime(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _missing_collection_windows(
    *,
    window_start: datetime,
    window_end: datetime,
    completed_windows: list[object],
) -> list[tuple[datetime, datetime]]:
    requested_start = _normalize_datetime(window_start)
    requested_end = _normalize_datetime(window_end)
    covered: list[tuple[datetime, datetime]] = []
    for record in completed_windows:
        start = _normalize_datetime(record.window_start)
        end = _normalize_datetime(record.window_end)
        if end <= requested_start or start >= requested_end:
            continue
        covered.append((max(start, requested_start), min(end, requested_end)))
    covered.sort(key=lambda item: item[0])

    missing: list[tuple[datetime, datetime]] = []
    cursor = requested_start
    for start, end in covered:
        if start > cursor:
            missing.append((cursor, start))
        if end > cursor:
            cursor = end
    if cursor < requested_end:
        missing.append((cursor, requested_end))
    return missing


def _format_window_label(window_start: datetime, window_end: datetime) -> str:
    return f"{window_start.astimezone(KST).strftime('%Y-%m-%d %H:%M')} KST -> {window_end.astimezone(KST).strftime('%Y-%m-%d %H:%M')} KST"


def _format_article_time(value: datetime | None) -> str:
    if value is None:
        return "-"
    return value.astimezone(KST).strftime("%Y-%m-%d %H:%M KST")


def _enrich_topics_publication_metadata(topics, batch_input) -> None:
    article_lookup = {article.article_id: article for article in batch_input.articles}
    for topic in topics:
        topic_articles = [
            article_lookup[article_id]
            for article_id in topic.article_ids
            if article_id in article_lookup
        ]
        if not topic_articles:
            continue
        topic_articles.sort(key=lambda article: article.published_at or article.collected_at, reverse=True)
        published_values = [article.published_at for article in topic_articles if article.published_at]
        representative = article_lookup.get(str(topic.representative_article_id or ""))
        summary = {
            "time_basis": "published_at",
            "representative_published_at": representative.published_at.isoformat()
            if representative and representative.published_at
            else None,
            "representative_published_at_kst": _format_article_time(representative.published_at)
            if representative
            else "-",
            "latest_published_at": max(published_values).isoformat() if published_values else None,
            "earliest_published_at": min(published_values).isoformat() if published_values else None,
            "published_range_kst": (
                f"{_format_article_time(min(published_values))} -> {_format_article_time(max(published_values))}"
                if published_values
                else "-"
            ),
            "articles": [
                {
                    "article_id": article.article_id,
                    "title": article.title,
                    "source_type": article.source_type,
                    "published_at": article.published_at.isoformat() if article.published_at else None,
                    "published_at_kst": _format_article_time(article.published_at),
                    "source_url": article.source_url,
                }
                for article in topic_articles
            ],
        }
        topic.metadata["article_publication_summary"] = summary


def _enrich_topic_publication_metadata(selection_result, batch_input) -> None:
    _enrich_topics_publication_metadata(selection_result.topics, batch_input)


def _write_candidate_text(run_dir: Path, selection_result) -> Path:
    lines = ["topic_candidates:"]
    for index, topic in enumerate(selection_result.topics, start=1):
        source_types = topic.metadata.get("source_types") if isinstance(topic.metadata, dict) else None
        keywords = topic.metadata.get("keywords") if isinstance(topic.metadata, dict) else None
        publication_summary = (
            topic.metadata.get("article_publication_summary")
            if isinstance(topic.metadata, dict)
            else None
        )
        lines.append(
            (
                f"[{index}] rank={topic.importance_rank} "
                f"score={topic.topic_score:.1f} "
                f"articles={len(topic.article_ids)} "
                f"name={topic.topic_name}"
            )
        )
        lines.append(f"reason={topic.reason_summary}")
        if isinstance(publication_summary, dict):
            lines.append(f"published_range={publication_summary.get('published_range_kst') or '-'}")
            lines.append(
                f"representative_published_at={publication_summary.get('representative_published_at_kst') or '-'}"
            )
            article_summaries = publication_summary.get("articles") or []
            if isinstance(article_summaries, list):
                for article in article_summaries[:5]:
                    if not isinstance(article, dict):
                        continue
                    lines.append(
                        "  article="
                        f"{article.get('published_at_kst') or '-'} | "
                        f"{article.get('source_type') or '-'} | "
                        f"{article.get('title') or '-'}"
                    )
        if keywords:
            lines.append(f"keywords={', '.join(str(keyword) for keyword in keywords)}")
        if source_types:
            lines.append(f"source_types={', '.join(str(source) for source in source_types)}")
        lines.append(f"representative_article_id={topic.representative_article_id or '-'}")
        lines.append("")
    output_path = run_dir / "topic_candidates.txt"
    output_path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
    return output_path


def main() -> None:
    load_default_env(ROOT_DIR)
    args = _parse_args()
    window_start, window_end, candidate_count = _resolve_window_and_count(args)
    completed_registry = load_completed_topic_registry()
    completed_topics = completed_registry.get("topics") if isinstance(completed_registry, dict) else []
    if not isinstance(completed_topics, list):
        completed_topics = []

    run_dir = ensure_stage_dir("01_topic_candidates")
    db_path = PERSISTENT_COLLECTION_DB_PATH
    batch_suffix = datetime.now(KST).strftime("%Y%m%d_%H%M%S")

    print("[MANUAL CHECK] Fresh collection -> topic candidate selection")
    print("=" * 96)
    print(f"window_start_kst         : {window_start.isoformat()}")
    print(f"window_end_kst           : {window_end.isoformat()}")
    print(f"candidate_count          : {candidate_count}")

    with SQLiteSourceItemRepository(str(db_path)) as repository:
        completed_windows = repository.list_completed_collection_windows()
        missing_windows = _missing_collection_windows(
            window_start=window_start,
            window_end=window_end,
            completed_windows=completed_windows,
        )
        collectors = build_news_collectors()
        all_collected_items = []
        all_collector_errors: list[str] = []
        total_inserted_count = 0
        total_duplicate_count = 0
        skipped_window_count = 0 if missing_windows else 1
        if missing_windows:
            print("collection_missing_windows:")
            for missing_start, missing_end in missing_windows:
                print(f"  - {_format_window_label(missing_start, missing_end)}")
                collector_result = CollectorService(collectors).collect_all(
                    window_start=missing_start,
                    window_end=missing_end,
                )
                ingestion_result = SourceItemIngestionService(repository=repository).ingest(collector_result.items)
                all_collected_items.extend(collector_result.items)
                all_collector_errors.extend(collector_result.errors)
                total_inserted_count += len(ingestion_result.inserted)
                total_duplicate_count += len(ingestion_result.duplicates)
                repository.save_collection_window(
                    window_start=missing_start,
                    window_end=missing_end,
                    status="completed" if not collector_result.errors else "partial",
                    item_count=len(collector_result.items),
                    inserted_count=len(ingestion_result.inserted),
                    duplicate_count=len(ingestion_result.duplicates),
                    errors=collector_result.errors,
                )
        else:
            print("collection_missing_windows: none (requested window already covered)")

        persisted_items = repository.list_items()
        batch_input = StoredArticleBatchBuilder().build(
            persisted_items,
            batch_id=f"kbo-news-{batch_suffix}",
            window_start=window_start.astimezone(timezone.utc),
            window_end=window_end.astimezone(timezone.utc),
        )
        selection_engine = choose_selection_engine()
        selection_service = BatchIssueSelectionService(engine=selection_engine)
        selection_result, excluded_topics = selection_service.select_topic_candidates_with_history(
            batch_input=batch_input,
            candidate_count=candidate_count,
            completed_topics=completed_topics,
        )
        _enrich_topic_publication_metadata(selection_result, batch_input)
        _enrich_topics_publication_metadata(excluded_topics, batch_input)

    report = {
        "window_start_kst": window_start.isoformat(),
        "window_end_kst": window_end.isoformat(),
        "candidate_count": candidate_count,
        "collection_db_path": str(db_path),
        "collection_missing_windows": [
            {
                "window_start": start.isoformat(),
                "window_end": end.isoformat(),
            }
            for start, end in missing_windows
        ],
        "collection_skipped_window_count": skipped_window_count,
        "collected_count": len(all_collected_items),
        "inserted_count": total_inserted_count,
        "duplicate_count": total_duplicate_count,
        "collector_errors": all_collector_errors,
        "batch_article_count": len(batch_input.articles),
        "batch_metadata": serialize_for_json(batch_input.metadata),
        "completed_topic_registry_count": len(completed_topics),
        "excluded_completed_topics": serialize_for_json(excluded_topics),
        "selection_result": serialize_for_json(selection_result),
    }
    report_path = run_dir / "topic_candidates_report.json"
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    candidates_text_path = _write_candidate_text(run_dir, selection_result)
    selection_template_path = run_dir / "topic_selection_choice.json"
    selection_template_path.write_text(
        json.dumps(
            build_topic_selection_template(
                selection_result,
                candidate_report_path=str(report_path),
            ),
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    print(f"selection_model_name     : {selection_result.model_name}")
    print(f"candidate_topic_count    : {len(selection_result.topics)}")
    print(f"collection_db_path       : {db_path}")
    print(f"collection_missing_count : {len(missing_windows)}")
    print(f"collected_count          : {len(all_collected_items)}")
    print(f"inserted_count           : {total_inserted_count}")
    print(f"duplicate_count          : {total_duplicate_count}")
    print(f"completed_topic_count    : {len(completed_topics)}")
    print(f"excluded_completed_count : {len(excluded_topics)}")
    print(f"report_path              : {report_path}")
    print(f"candidate_text_path      : {candidates_text_path}")
    print(f"selection_template_path  : {selection_template_path}")
    if selection_result.topics:
        print()
        print("topic_candidates:")
        for index, topic in enumerate(selection_result.topics, start=1):
            publication_summary = (
                topic.metadata.get("article_publication_summary")
                if isinstance(topic.metadata, dict)
                else None
            )
            published_text = ""
            if isinstance(publication_summary, dict):
                published_text = f" | published={publication_summary.get('published_range_kst') or '-'}"
            print(
                f"  [{index}] {topic.topic_name} | score={topic.topic_score:.1f}"
                f"{published_text} | reason={topic.reason_summary}"
            )


if __name__ == "__main__":
    main()
