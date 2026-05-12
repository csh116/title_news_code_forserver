from __future__ import annotations

import argparse
import json
import secrets
from datetime import datetime
from pathlib import Path
from typing import Any

from kbo_card_news.automation.job_state import (
    AUTOMATION_OUTPUT_DIR,
    AUTOMATION_STATE_DB_PATH,
    AutomationJob,
    AutomationJobArticle,
    AutomationJobRepository,
    build_job_id,
    event_to_dict,
    job_to_dict,
    utc_now,
)
from kbo_card_news.automation.pipeline_runner import (
    build_title_editor_run,
    confirm_topic_candidates,
    generate_topic_candidates,
    serve_title_editor,
)
from kbo_card_news.automation.news_watcher import (
    DEFAULT_WATCH_CANDIDATE_COUNT,
    DEFAULT_WATCH_MAX_CANDIDATES,
    watch_once,
)
from kbo_card_news.automation.fresh_issue_detector import (
    KST,
    SOURCE_DB_PATH,
)
from kbo_card_news.automation.fresh_window_decision import (
    FreshWindowDecisionConfig,
    fresh_window_decision_result_to_dict,
    watch_fresh_window_once,
)
from kbo_card_news.automation.topic_ranker import rank_candidate, rank_result_to_metadata
from kbo_card_news.automation.discord_bot import (
    send_editor_ready_notification,
    send_job_notification,
    send_render_notification,
)
from kbo_card_news.automation.discord_actions import handle_component_action
from kbo_card_news.automation.discord_bot_runner import ensure_discord_button_worker, run_discord_button_worker
from kbo_card_news.automation.operations import (
    AutomationLockBusy,
    build_digest_report,
    build_health_report,
    digest_report_to_dict,
    expire_old_pending_jobs,
    health_report_to_dict,
    recover_stale_pipeline_jobs,
    run_with_lock,
    write_failure_log,
)
from kbo_card_news.automation.instagram_publisher import (
    build_instagram_publish_plan,
    extract_caption_for_topic,
    publish_instagram_image,
    publish_result_to_metadata,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Manage local news automation jobs.")
    parser.add_argument(
        "--db",
        default=str(AUTOMATION_STATE_DB_PATH),
        help="Automation state SQLite DB path.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("init", help="Initialize the automation state DB.")

    create_parser = subparsers.add_parser("create", help="Create a job manually.")
    create_parser.add_argument("--job-id", help="Optional explicit job id.")
    create_parser.add_argument("--topic-id", required=True)
    create_parser.add_argument("--topic-name", required=True)
    create_parser.add_argument("--status", default="detected")
    create_parser.add_argument("--notification-level", default="watch")
    create_parser.add_argument("--virality", type=float, default=0.0)
    create_parser.add_argument("--account-fit", type=float, default=0.0)
    create_parser.add_argument("--summary", default="")
    create_parser.add_argument("--metadata-json", default="{}")
    create_parser.add_argument(
        "--article-json",
        action="append",
        default=[],
        help="Article JSON object. Can be provided multiple times.",
    )

    list_parser = subparsers.add_parser("list", help="List jobs.")
    list_parser.add_argument("--status")
    list_parser.add_argument("--limit", type=int, default=20)
    list_parser.add_argument("--json", action="store_true", help="Print JSON instead of a compact table.")

    show_parser = subparsers.add_parser("show", help="Show a job.")
    show_parser.add_argument("job_id")
    show_parser.add_argument("--events", action="store_true")

    status_parser = subparsers.add_parser("status", help="Update job status.")
    status_parser.add_argument("job_id")
    status_parser.add_argument("status")
    status_parser.add_argument("--message", default="")
    status_parser.add_argument("--failure-message")
    status_parser.add_argument("--metadata-json", default="{}")

    paths_parser = subparsers.add_parser("paths", help="Update job output paths.")
    paths_parser.add_argument("job_id")
    paths_parser.add_argument("--approval-run-dir")
    paths_parser.add_argument("--editor-url")
    paths_parser.add_argument("--render-png-path")
    paths_parser.add_argument("--social-copy-md-path")
    paths_parser.add_argument("--message", default="")

    event_parser = subparsers.add_parser("event", help="Record an event for a job.")
    event_parser.add_argument("job_id")
    event_parser.add_argument("event_type")
    event_parser.add_argument("--message", default="")
    event_parser.add_argument("--metadata-json", default="{}")

    candidates_parser = subparsers.add_parser("candidates", help="Run topic candidate generation.")
    candidates_parser.add_argument("--approval-run-dir")
    candidates_parser.add_argument("--window-start-kst", help="YYYY-MM-DD HH:MM. Default uses manual script default.")
    candidates_parser.add_argument("--window-end-kst", help="YYYY-MM-DD HH:MM. Default uses manual script default.")
    candidates_parser.add_argument("--candidate-count", type=int)
    candidates_parser.add_argument("--selection-engine", choices=["heuristic", "gemini"], default="heuristic")

    confirm_parser = subparsers.add_parser("confirm", help="Confirm selected topic candidates non-interactively.")
    confirm_parser.add_argument("choice_json_path")
    confirm_group = confirm_parser.add_mutually_exclusive_group(required=True)
    confirm_group.add_argument("--topic-id", action="append", default=[])
    confirm_group.add_argument("--index", type=int, action="append", default=[])
    confirm_parser.add_argument("--approval-run-dir")

    editor_parser = subparsers.add_parser("build-editor", help="Build the title HTML editor run.")
    editor_parser.add_argument("--approval-run-dir", required=True)
    editor_parser.add_argument("--confirmed-json-path")
    editor_parser.add_argument("--host", default="127.0.0.1")
    editor_parser.add_argument("--public-host")
    editor_parser.add_argument("--port", type=int, default=8787)

    serve_parser = subparsers.add_parser("serve-editor", help="Serve an existing title HTML editor manifest.")
    serve_parser.add_argument("--approval-run-dir", required=True)
    serve_parser.add_argument("--manifest-path")
    serve_parser.add_argument("--host", default="127.0.0.1")
    serve_parser.add_argument("--port", type=int, default=8787)
    serve_parser.add_argument("--shutdown-after-render", action="store_true")
    serve_parser.add_argument("--idle-timeout-seconds", type=int)

    watch_parser = subparsers.add_parser("watch-once", help="Generate or load candidates and store them as jobs.")
    watch_parser.add_argument("--choice-json-path")
    watch_parser.add_argument("--approval-run-dir")
    watch_parser.add_argument("--window-start-kst", help="YYYY-MM-DD HH:MM. Used only when generating candidates.")
    watch_parser.add_argument("--window-end-kst", help="YYYY-MM-DD HH:MM. Used only when generating candidates.")
    watch_parser.add_argument("--candidate-count", type=int)
    watch_parser.add_argument("--max-candidates", type=int)
    watch_parser.add_argument("--selection-engine", choices=["heuristic", "gemini"], default="heuristic")
    watch_parser.add_argument("--initial-status", default="detected")
    watch_parser.add_argument("--json", action="store_true")

    watch_cycle_parser = subparsers.add_parser("watch-cycle", help="Run one locked watcher cycle.")
    watch_cycle_parser.add_argument("--choice-json-path")
    watch_cycle_parser.add_argument("--approval-run-dir")
    watch_cycle_parser.add_argument("--window-start-kst", help="YYYY-MM-DD HH:MM. Used only when generating candidates.")
    watch_cycle_parser.add_argument("--window-end-kst", help="YYYY-MM-DD HH:MM. Used only when generating candidates.")
    watch_cycle_parser.add_argument(
        "--candidate-count",
        type=int,
        default=DEFAULT_WATCH_CANDIDATE_COUNT,
        help=f"Candidate count passed to the heavy topic-selection script. Default: {DEFAULT_WATCH_CANDIDATE_COUNT}.",
    )
    watch_cycle_parser.add_argument(
        "--max-candidates",
        type=int,
        default=DEFAULT_WATCH_MAX_CANDIDATES,
        help=f"Maximum candidates persisted as automation jobs. Default: {DEFAULT_WATCH_MAX_CANDIDATES}.",
    )
    watch_cycle_parser.add_argument("--initial-status", default="detected")
    watch_cycle_parser.add_argument("--selection-engine", choices=["heuristic", "gemini"], default="heuristic")
    watch_cycle_parser.add_argument("--notify", action="store_true")
    watch_cycle_parser.add_argument("--channel-id")
    watch_cycle_parser.add_argument("--bot-token")
    watch_cycle_parser.add_argument("--webhook-url", help=argparse.SUPPRESS)
    watch_cycle_parser.add_argument("--dry-run-notify", action="store_true")
    watch_cycle_parser.add_argument("--no-start-button-worker", action="store_true")
    watch_cycle_parser.add_argument("--button-worker-host", default="127.0.0.1")
    watch_cycle_parser.add_argument("--button-worker-public-host")
    watch_cycle_parser.add_argument("--button-worker-tailscale-funnel-base-url")
    watch_cycle_parser.add_argument("--button-worker-port", type=int, default=8787)
    watch_cycle_parser.add_argument("--lock-path")
    watch_cycle_parser.add_argument("--json", action="store_true")

    fresh_parser = subparsers.add_parser("watch-fresh-cycle", help="Run one locked fresh issue watcher cycle.")
    fresh_parser.add_argument("--source-db-path", default=str(SOURCE_DB_PATH))
    fresh_parser.add_argument("--collection-window-minutes", type=int, default=10)
    fresh_parser.add_argument("--context-window-hours", type=int, default=24)
    fresh_parser.add_argument("--duplicate-lookback-hours", type=int, default=72)
    fresh_parser.add_argument("--feedback-lookback-days", type=int, default=90)
    fresh_parser.add_argument("--fresh-decision-model", default="gemini-3.1-flash-lite-preview")
    fresh_parser.add_argument("--max-target-articles-per-call", type=int, default=40)
    fresh_parser.add_argument("--max-context-articles", type=int, default=180)
    fresh_parser.add_argument("--max-feedback-examples", type=int, default=40)
    fresh_parser.add_argument("--min-issue-score", type=float, default=None, help=argparse.SUPPRESS)
    fresh_parser.add_argument("--max-jobs", type=int, default=None, help=argparse.SUPPRESS)
    fresh_parser.add_argument("--gemini-review", dest="gemini_review", action="store_true", default=True, help=argparse.SUPPRESS)
    fresh_parser.add_argument("--no-gemini-review", dest="gemini_review", action="store_false", help=argparse.SUPPRESS)
    fresh_parser.add_argument("--notify", action="store_true")
    fresh_parser.add_argument("--channel-id")
    fresh_parser.add_argument("--bot-token")
    fresh_parser.add_argument("--dry-run-notify", action="store_true")
    fresh_parser.add_argument("--no-start-button-worker", action="store_true")
    fresh_parser.add_argument("--button-worker-host", default="127.0.0.1")
    fresh_parser.add_argument("--button-worker-public-host")
    fresh_parser.add_argument("--button-worker-tailscale-funnel-base-url")
    fresh_parser.add_argument("--button-worker-port", type=int, default=8787)
    fresh_parser.add_argument("--lock-path")
    fresh_parser.add_argument("--quiet-start-hour", type=int, default=0)
    fresh_parser.add_argument("--quiet-end-hour", type=int, default=7)
    fresh_parser.add_argument("--json", action="store_true")

    rank_parser = subparsers.add_parser("rank-candidates", help="Score candidates from a topic_selection_choice.json.")
    rank_parser.add_argument("choice_json_path")
    rank_parser.add_argument("--limit", type=int, default=20)
    rank_parser.add_argument("--json", action="store_true")

    notify_parser = subparsers.add_parser("notify-pending", help="Send Discord notifications for detected jobs.")
    notify_parser.add_argument("--status", default="detected")
    notify_parser.add_argument(
        "--level",
        action="append",
        default=[],
        help="Notification level to send. Defaults to immediate and watch.",
    )
    notify_parser.add_argument("--limit", type=int, default=10)
    notify_parser.add_argument("--channel-id")
    notify_parser.add_argument("--bot-token")
    notify_parser.add_argument("--webhook-url", help=argparse.SUPPRESS)
    notify_parser.add_argument("--dry-run", action="store_true")
    notify_parser.add_argument("--no-start-button-worker", action="store_true")
    notify_parser.add_argument("--button-worker-host", default="127.0.0.1")
    notify_parser.add_argument("--button-worker-public-host")
    notify_parser.add_argument("--button-worker-tailscale-funnel-base-url")
    notify_parser.add_argument("--button-worker-port", type=int, default=8787)

    approve_parser = subparsers.add_parser("approve", help="Approve a job for pipeline execution.")
    approve_parser.add_argument("job_id")
    approve_parser.add_argument("--message", default="approved by CLI")

    build_approved_parser = subparsers.add_parser("build-approved-editor", help="Confirm one approved job and build its editor run.")
    build_approved_parser.add_argument("job_id")
    build_approved_parser.add_argument("--approval-run-dir")
    build_approved_parser.add_argument("--host", default="127.0.0.1")
    build_approved_parser.add_argument("--public-host")
    build_approved_parser.add_argument("--port", type=int, default=8787)
    build_approved_parser.add_argument("--editor-token")
    build_approved_parser.add_argument("--notify", action="store_true")
    build_approved_parser.add_argument("--dry-run-notify", action="store_true")
    build_approved_parser.add_argument("--channel-id")
    build_approved_parser.add_argument("--bot-token")
    build_approved_parser.add_argument("--lock-path")
    build_approved_parser.add_argument("--allow-any-status", action="store_true")
    build_approved_parser.add_argument("--dry-run", action="store_true")

    build_next_parser = subparsers.add_parser("build-next-approved", help="Build the next approved job's editor run.")
    build_next_parser.add_argument("--approval-run-dir")
    build_next_parser.add_argument("--host", default="127.0.0.1")
    build_next_parser.add_argument("--public-host")
    build_next_parser.add_argument("--port", type=int, default=8787)
    build_next_parser.add_argument("--editor-token")
    build_next_parser.add_argument("--notify", action="store_true")
    build_next_parser.add_argument("--dry-run-notify", action="store_true")
    build_next_parser.add_argument("--channel-id")
    build_next_parser.add_argument("--bot-token")
    build_next_parser.add_argument("--lock-path")
    build_next_parser.add_argument("--dry-run", action="store_true")

    editor_notify_parser = subparsers.add_parser("notify-editor-ready", help="Send a Discord notification for an editor_ready job.")
    editor_notify_parser.add_argument("job_id")
    editor_notify_parser.add_argument("--channel-id")
    editor_notify_parser.add_argument("--bot-token")
    editor_notify_parser.add_argument("--dry-run", action="store_true")
    editor_notify_parser.add_argument("--allow-any-status", action="store_true")

    action_parser = subparsers.add_parser("handle-discord-action", help="Handle a Discord button custom_id.")
    action_parser.add_argument("custom_id")
    action_parser.add_argument("--actor", default="cli")
    action_parser.add_argument("--dry-run", action="store_true")

    button_worker_parser = subparsers.add_parser("discord-button-worker", help="Run the Discord button interaction worker.")
    button_worker_parser.add_argument("--channel-id")
    button_worker_parser.add_argument("--bot-token")
    button_worker_parser.add_argument("--auto-build", action="store_true")
    button_worker_parser.add_argument("--host", default="127.0.0.1")
    button_worker_parser.add_argument("--public-host")
    button_worker_parser.add_argument("--tailscale-funnel-base-url")
    button_worker_parser.add_argument("--port", type=int, default=8787)
    button_worker_parser.add_argument("--notify-build", action="store_true")
    button_worker_parser.add_argument("--build-lock-path")

    serve_job_parser = subparsers.add_parser("serve-job-editor", help="Serve the editor manifest recorded on a job.")
    serve_job_parser.add_argument("job_id")
    serve_job_parser.add_argument("--host", default="127.0.0.1")
    serve_job_parser.add_argument("--port", type=int, default=8787)
    serve_job_parser.add_argument("--editor-token")
    serve_job_parser.add_argument("--notify-render", action="store_true")
    serve_job_parser.add_argument("--no-render-attachments", action="store_true")
    serve_job_parser.add_argument("--keep-open-after-render", action="store_true")
    serve_job_parser.add_argument("--idle-timeout-seconds", type=int, default=1800)
    serve_job_parser.add_argument("--channel-id")
    serve_job_parser.add_argument("--bot-token")
    serve_job_parser.add_argument("--allow-any-status", action="store_true")

    render_parser = subparsers.add_parser("record-render", help="Record render output paths on a job.")
    render_parser.add_argument("job_id")
    render_parser.add_argument("--state-path", required=True)
    render_parser.add_argument("--png-path")
    render_parser.add_argument("--social-copy-md-path")
    render_parser.add_argument("--channel-id")
    render_parser.add_argument("--bot-token")
    render_parser.add_argument("--webhook-url", help=argparse.SUPPRESS)
    render_parser.add_argument("--notify", action="store_true")
    render_parser.add_argument("--no-attachments", action="store_true")
    render_parser.add_argument("--dry-run", action="store_true")
    render_parser.add_argument("--allow-any-status", action="store_true")

    approve_publish_parser = subparsers.add_parser("approve-publish", help="Approve a rendered job for Instagram publishing.")
    approve_publish_parser.add_argument("job_id")
    approve_publish_parser.add_argument("--message", default="approved for Instagram publishing by CLI")

    publish_parser = subparsers.add_parser("publish-instagram", help="Publish a render_ready job to Instagram.")
    publish_parser.add_argument("job_id")
    publish_parser.add_argument("--image-url", required=True, help="Public http(s) URL Instagram can fetch.")
    publish_parser.add_argument("--caption")
    publish_parser.add_argument("--caption-path")
    publish_parser.add_argument("--alt-text")
    publish_parser.add_argument("--ig-user-id")
    publish_parser.add_argument("--access-token")
    publish_parser.add_argument("--graph-api-version")
    publish_parser.add_argument("--dry-run", action="store_true")
    publish_parser.add_argument("--allow-any-status", action="store_true")

    health_parser = subparsers.add_parser("health", help="Print automation health and stale-job checks.")
    health_parser.add_argument("--disk-path")
    health_parser.add_argument("--min-free-gb", type=float, default=5.0)
    health_parser.add_argument("--pending-hours", type=int, default=12)
    health_parser.add_argument("--pipeline-hours", type=int, default=2)
    health_parser.add_argument("--json", action="store_true")

    recover_parser = subparsers.add_parser("recover-running", help="Recover stale pipeline_running jobs.")
    recover_parser.add_argument("--stale-hours", type=int, default=2)
    recover_parser.add_argument("--target-status", choices=["approved", "failed"], default="approved")
    recover_parser.add_argument("--json", action="store_true")

    expire_parser = subparsers.add_parser("expire-pending", help="Expire old approval-waiting jobs.")
    expire_parser.add_argument("--stale-hours", type=int, default=12)
    expire_parser.add_argument("--status", action="append", default=[])
    expire_parser.add_argument("--json", action="store_true")

    digest_parser = subparsers.add_parser("digest", help="Print an operations digest.")
    digest_parser.add_argument("--since-hours", type=int, default=24)
    digest_parser.add_argument("--json", action="store_true")

    skip_parser = subparsers.add_parser("skip", help="Skip a job and record a reason.")
    skip_parser.add_argument("job_id")
    skip_parser.add_argument("--reason", default="manual_skip")
    skip_parser.add_argument("--message", default="")

    return parser


def main() -> None:
    args = build_parser().parse_args()
    if args.command == "candidates":
        _handle_candidates(args)
        return
    if args.command == "confirm":
        _handle_confirm(args)
        return
    if args.command == "build-editor":
        _handle_build_editor(args)
        return
    if args.command == "serve-editor":
        _handle_serve_editor(args)
        return
    if args.command == "rank-candidates":
        _handle_rank_candidates(args)
        return
    if args.command == "discord-button-worker":
        _handle_discord_button_worker(args)
        return
    with AutomationJobRepository(Path(args.db)) as repository:
        if args.command == "watch-once":
            _handle_watch_once(repository, args)
            return
        if args.command == "watch-cycle":
            _handle_watch_cycle(repository, args)
            return
        if args.command == "watch-fresh-cycle":
            _handle_watch_fresh_cycle(repository, args)
            return
        if args.command == "notify-pending":
            _handle_notify_pending(repository, args)
            return
        if args.command == "approve":
            _handle_approve(repository, args)
            return
        if args.command == "build-approved-editor":
            _handle_build_approved_editor(repository, args)
            return
        if args.command == "build-next-approved":
            _handle_build_next_approved(repository, args)
            return
        if args.command == "notify-editor-ready":
            _handle_notify_editor_ready(repository, args)
            return
        if args.command == "handle-discord-action":
            _handle_discord_action(repository, args)
            return
        if args.command == "serve-job-editor":
            _handle_serve_job_editor(repository, args)
            return
        if args.command == "record-render":
            _handle_record_render(repository, args)
            return
        if args.command == "approve-publish":
            _handle_approve_publish(repository, args)
            return
        if args.command == "publish-instagram":
            _handle_publish_instagram(repository, args)
            return
        if args.command == "health":
            _handle_health(repository, args)
            return
        if args.command == "recover-running":
            _handle_recover_running(repository, args)
            return
        if args.command == "expire-pending":
            _handle_expire_pending(repository, args)
            return
        if args.command == "digest":
            _handle_digest(repository, args)
            return
        if args.command == "skip":
            _handle_skip(repository, args)
            return
        if args.command == "init":
            print(f"automation_state_db={repository.db_path}")
            return
        if args.command == "create":
            _handle_create(repository, args)
            return
        if args.command == "list":
            _handle_list(repository, args)
            return
        if args.command == "show":
            _handle_show(repository, args)
            return
        if args.command == "status":
            _handle_status(repository, args)
            return
        if args.command == "paths":
            _handle_paths(repository, args)
            return
        if args.command == "event":
            _handle_event(repository, args)
            return
    raise ValueError(f"unsupported command: {args.command}")


def _handle_create(repository: AutomationJobRepository, args: argparse.Namespace) -> None:
    metadata = _parse_json_object(args.metadata_json, label="metadata-json")
    articles = [
        _article_from_dict(_parse_json_object(raw, label="article-json"))
        for raw in args.article_json
    ]
    job = AutomationJob(
        job_id=args.job_id or build_job_id(),
        topic_id=args.topic_id,
        topic_name=args.topic_name,
        status=args.status,
        notification_level=args.notification_level,
        virality_potential_score=args.virality,
        account_fit_score=args.account_fit,
        recommendation_summary=args.summary,
        metadata=metadata,
        articles=articles,
    )
    created = repository.create_job(job)
    print(json.dumps(job_to_dict(created), ensure_ascii=False, indent=2))


def _handle_list(repository: AutomationJobRepository, args: argparse.Namespace) -> None:
    jobs = repository.list_jobs(status=args.status, limit=args.limit)
    if args.json:
        print(json.dumps([job_to_dict(job) for job in jobs], ensure_ascii=False, indent=2))
        return
    if not jobs:
        print("no jobs")
        return
    print("job_id                 status            level       virality  fit  topic")
    for job in jobs:
        print(
            f"{job.job_id:<22} {job.status:<17} {job.notification_level:<11} "
            f"{job.virality_potential_score:>7.1f} {job.account_fit_score:>4.1f}  "
            f"{job.topic_name}"
        )


def _handle_show(repository: AutomationJobRepository, args: argparse.Namespace) -> None:
    job = repository.get_job(args.job_id)
    if job is None:
        raise SystemExit(f"automation job not found: {args.job_id}")
    payload: dict[str, Any] = job_to_dict(job)
    if args.events:
        payload["events"] = [
            event_to_dict(event)
            for event in repository.list_events(args.job_id)
        ]
    print(json.dumps(payload, ensure_ascii=False, indent=2))


def _handle_status(repository: AutomationJobRepository, args: argparse.Namespace) -> None:
    metadata = _parse_json_object(args.metadata_json, label="metadata-json")
    job = repository.update_status(
        args.job_id,
        args.status,
        message=args.message,
        failure_message=args.failure_message,
        metadata=metadata,
    )
    print(json.dumps(job_to_dict(job), ensure_ascii=False, indent=2))


def _handle_paths(repository: AutomationJobRepository, args: argparse.Namespace) -> None:
    job = repository.update_job_paths(
        args.job_id,
        approval_run_dir=args.approval_run_dir,
        editor_url=args.editor_url,
        render_png_path=args.render_png_path,
        social_copy_md_path=args.social_copy_md_path,
        message=args.message,
    )
    print(json.dumps(job_to_dict(job), ensure_ascii=False, indent=2))


def _handle_event(repository: AutomationJobRepository, args: argparse.Namespace) -> None:
    metadata = _parse_json_object(args.metadata_json, label="metadata-json")
    event = repository.record_event(
        args.job_id,
        args.event_type,
        message=args.message,
        metadata=metadata,
    )
    print(json.dumps(event_to_dict(event), ensure_ascii=False, indent=2))


def _handle_candidates(args: argparse.Namespace) -> None:
    result = generate_topic_candidates(
        approval_run_dir=args.approval_run_dir,
        window_start_kst=args.window_start_kst,
        window_end_kst=args.window_end_kst,
        candidate_count=args.candidate_count,
        selection_engine=args.selection_engine,
    )
    print(
        json.dumps(
            {
                "approval_run_dir": str(result.approval_run_dir),
                "choice_json_path": str(result.choice_json_path),
                "report_path": str(result.report_path),
                "candidate_text_path": str(result.candidate_text_path),
            },
            ensure_ascii=False,
            indent=2,
        )
    )


def _handle_confirm(args: argparse.Namespace) -> None:
    result = confirm_topic_candidates(
        args.choice_json_path,
        selected_topic_ids=args.topic_id or None,
        selected_indices=args.index or None,
        approval_run_dir=args.approval_run_dir,
    )
    print(
        json.dumps(
            {
                "approval_run_dir": str(result.approval_run_dir),
                "choice_json_path": str(result.choice_json_path),
                "confirmed_json_path": str(result.confirmed_json_path),
                "selected_topic_ids": result.selected_topic_ids,
            },
            ensure_ascii=False,
            indent=2,
        )
    )


def _handle_build_editor(args: argparse.Namespace) -> None:
    result = build_title_editor_run(
        approval_run_dir=args.approval_run_dir,
        confirmed_json_path=args.confirmed_json_path,
        host=args.host,
        public_host=args.public_host,
        port=args.port,
    )
    print(
        json.dumps(
            {
                "approval_run_dir": str(result.approval_run_dir),
                "manifest_path": str(result.manifest_path),
                "report_path": str(result.report_path),
                "editor_url": result.editor_url,
                "topic_count": result.topic_count,
            },
            ensure_ascii=False,
            indent=2,
        )
    )


def _handle_serve_editor(args: argparse.Namespace) -> None:
    serve_title_editor(
        approval_run_dir=args.approval_run_dir,
        manifest_path=args.manifest_path,
        host=args.host,
        port=args.port,
        shutdown_after_render=args.shutdown_after_render,
        idle_timeout_seconds=args.idle_timeout_seconds,
    )


def _handle_watch_once(repository: AutomationJobRepository, args: argparse.Namespace) -> None:
    result = watch_once(
        repository=repository,
        choice_json_path=args.choice_json_path,
        approval_run_dir=args.approval_run_dir,
        window_start_kst=args.window_start_kst,
        window_end_kst=args.window_end_kst,
        candidate_count=args.candidate_count,
        max_candidates=args.max_candidates,
        initial_status=args.initial_status,
        selection_engine=args.selection_engine,
    )
    payload = {
        "choice_json_path": str(result.choice_json_path),
        "created_count": len(result.created_jobs),
        "duplicate_count": len(result.duplicate_jobs),
        "skipped_count": result.skipped_count,
        "created_jobs": [job_to_dict(job) for job in result.created_jobs],
        "duplicate_jobs": [job_to_dict(job) for job in result.duplicate_jobs],
    }
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return
    print(f"choice_json_path={payload['choice_json_path']}")
    print(f"created_count={payload['created_count']}")
    print(f"duplicate_count={payload['duplicate_count']}")
    print(f"skipped_count={payload['skipped_count']}")
    for job in result.created_jobs:
        print(f"created {job.job_id} | {job.notification_level} | {job.topic_name}")
    for job in result.duplicate_jobs:
        print(f"duplicate {job.job_id} | {job.status} | {job.topic_name}")


def _handle_watch_cycle(repository: AutomationJobRepository, args: argparse.Namespace) -> None:
    try:
        payload = run_with_lock(
            args.lock_path,
            lambda: _run_watch_cycle_under_lock(repository, args),
        )
    except AutomationLockBusy as exc:
        raise SystemExit(str(exc)) from exc
    except Exception as exc:
        log_path = write_failure_log(
            operation="watch_cycle",
            exc=exc,
            metadata={"db_path": str(repository.db_path)},
        )
        raise SystemExit(f"watch-cycle failed; failure_log={log_path}") from exc
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return
    print(f"choice_json_path={payload['choice_json_path']}")
    print(f"created_count={payload['created_count']}")
    print(f"duplicate_count={payload['duplicate_count']}")
    print(f"skipped_count={payload['skipped_count']}")
    print(f"notified_count={payload['notified_count']}")
    print(f"notification_failed_count={payload['notification_failed_count']}")


def _run_watch_cycle_under_lock(repository: AutomationJobRepository, args: argparse.Namespace) -> dict[str, Any]:
    result = watch_once(
        repository=repository,
        choice_json_path=args.choice_json_path,
        approval_run_dir=args.approval_run_dir,
        window_start_kst=args.window_start_kst,
        window_end_kst=args.window_end_kst,
        candidate_count=args.candidate_count,
        max_candidates=args.max_candidates,
        initial_status=args.initial_status,
        selection_engine=args.selection_engine,
    )
    notified_count = 0
    notification_failed_count = 0
    notification_payloads: list[dict[str, Any]] = []
    button_worker: dict[str, Any] | None = None
    if args.notify:
        for job in result.created_jobs:
            notification = send_job_notification(
                job,
                channel_id=args.channel_id,
                bot_token=args.bot_token,
                dry_run=args.dry_run_notify,
            )
            if args.dry_run_notify:
                notification_payloads.append({"job_id": job.job_id, "payload": notification.payload})
                continue
            if notification.ok:
                notified_count += 1
                repository.update_status(
                    job.job_id,
                    "notified",
                    message="Discord notification sent by watch-cycle",
                    metadata={"discord_status_code": notification.status_code},
                )
            else:
                notification_failed_count += 1
                repository.record_event(
                    job.job_id,
                    "discord_notification_failed",
                    message=notification.message,
                    metadata={"discord_status_code": notification.status_code},
                )
        if notified_count and not args.dry_run_notify and not args.no_start_button_worker:
            button_worker = _ensure_button_worker_from_args(repository, args)
    return {
        "choice_json_path": str(result.choice_json_path),
        "created_count": len(result.created_jobs),
        "duplicate_count": len(result.duplicate_jobs),
        "skipped_count": result.skipped_count,
        "notified_count": notified_count,
        "notification_failed_count": notification_failed_count,
        "created_jobs": [job_to_dict(job) for job in result.created_jobs],
        "duplicate_jobs": [job_to_dict(job) for job in result.duplicate_jobs],
        "notification_payloads": notification_payloads,
        "button_worker": button_worker,
    }


def _handle_watch_fresh_cycle(repository: AutomationJobRepository, args: argparse.Namespace) -> None:
    if args.gemini_review is False:
        raise SystemExit("watch-fresh-cycle requires Gemini fresh window decision gate; --no-gemini-review is no longer supported.")
    schedule = _fresh_cycle_schedule_decision(
        now=datetime.now(KST),
        quiet_start_hour=args.quiet_start_hour,
        quiet_end_hour=args.quiet_end_hour,
    )
    if schedule["mode"] == "skip":
        payload = {
            "status": "skipped_quiet_hours",
            "reason": schedule["reason"],
            "quiet_start_hour": args.quiet_start_hour,
            "quiet_end_hour": args.quiet_end_hour,
            "now": schedule["now"],
        }
        if args.json:
            print(json.dumps(payload, ensure_ascii=False, indent=2))
            return
        print("status=skipped_quiet_hours")
        print(f"reason={payload['reason']}")
        return

    try:
        payload = run_with_lock(
            args.lock_path,
            lambda: _run_watch_fresh_cycle_under_lock(repository, args, schedule=schedule),
        )
    except AutomationLockBusy as exc:
        raise SystemExit(str(exc)) from exc
    except Exception as exc:
        log_path = write_failure_log(
            operation="watch_fresh_cycle",
            exc=exc,
            metadata={"db_path": str(repository.db_path), "source_db_path": args.source_db_path},
        )
        raise SystemExit(f"watch-fresh-cycle failed; failure_log={log_path}") from exc
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return
    print(f"report_path={payload['report_path']}")
    print(f"choice_json_path={payload['choice_json_path']}")
    print(f"collected_count={payload['collected_count']}")
    print(f"inserted_count={payload['inserted_count']}")
    print(f"target_article_count={payload['target_article_count']}")
    print(f"decision_count={payload['decision_count']}")
    print(f"created_count={payload['created_count']}")
    print(f"duplicate_job_count={payload['duplicate_job_count']}")
    print(f"notified_count={payload['notified_count']}")
    print(f"notification_failed_count={payload['notification_failed_count']}")


def _run_watch_fresh_cycle_under_lock(
    repository: AutomationJobRepository,
    args: argparse.Namespace,
    *,
    schedule: dict[str, Any] | None = None,
) -> dict[str, Any]:
    schedule = schedule or _fresh_cycle_schedule_decision(
        now=datetime.now(KST),
        quiet_start_hour=args.quiet_start_hour,
        quiet_end_hour=args.quiet_end_hour,
    )
    config = FreshWindowDecisionConfig(
        collection_window_minutes=args.collection_window_minutes,
        context_window_hours=args.context_window_hours,
        duplicate_lookback_hours=args.duplicate_lookback_hours,
        feedback_lookback_days=args.feedback_lookback_days,
        max_target_articles_per_call=args.max_target_articles_per_call,
        max_context_articles=args.max_context_articles,
        max_feedback_examples=args.max_feedback_examples,
        model_name=args.fresh_decision_model,
    )
    marker_path = Path(str(schedule["marker_path"])) if schedule.get("marker_path") else None
    if marker_path is not None and marker_path.exists():
        current = datetime.now(KST).replace(second=0, microsecond=0)
        schedule = {
            "mode": "normal",
            "now": current.isoformat(),
            "analysis_now": current.isoformat(),
        }
        marker_path = None
    run_now = datetime.fromisoformat(str(schedule["analysis_now"])) if schedule.get("analysis_now") else None
    collection_window_start = (
        datetime.fromisoformat(str(schedule["collection_window_start"]))
        if schedule.get("collection_window_start")
        else None
    )
    collection_window_end = (
        datetime.fromisoformat(str(schedule["collection_window_end"]))
        if schedule.get("collection_window_end")
        else None
    )
    result = watch_fresh_window_once(
        job_repository=repository,
        source_db_path=args.source_db_path,
        config=config,
        now=run_now,
        collection_window_start=collection_window_start,
        collection_window_end=collection_window_end,
    )
    if marker_path is not None:
        marker_path.parent.mkdir(parents=True, exist_ok=True)
        marker_path.write_text(
            json.dumps({"completed_at": datetime.now(KST).isoformat()}, ensure_ascii=False),
            encoding="utf-8",
        )
    notified_count = 0
    notification_failed_count = 0
    notification_payloads: list[dict[str, Any]] = []
    button_worker: dict[str, Any] | None = None
    if args.notify:
        for job in result.created_jobs:
            notification = send_job_notification(
                job,
                channel_id=args.channel_id,
                bot_token=args.bot_token,
                dry_run=args.dry_run_notify,
            )
            if args.dry_run_notify:
                notification_payloads.append({"job_id": job.job_id, "payload": notification.payload})
                continue
            if notification.ok:
                notified_count += 1
                repository.update_status(
                    job.job_id,
                    "notified",
                    message="Discord notification sent by watch-fresh-cycle",
                    metadata={"discord_status_code": notification.status_code},
                )
            else:
                notification_failed_count += 1
                repository.record_event(
                    job.job_id,
                    "discord_notification_failed",
                    message=notification.message,
                    metadata={"discord_status_code": notification.status_code},
                )
        if notified_count and not args.dry_run_notify and not args.no_start_button_worker:
            button_worker = _ensure_button_worker_from_args(repository, args)
    payload = fresh_window_decision_result_to_dict(result)
    payload.update(
        {
            "status": "completed",
            "schedule_mode": schedule["mode"],
            "notified_count": notified_count,
            "notification_failed_count": notification_failed_count,
            "notification_payloads": notification_payloads,
            "button_worker": button_worker,
        }
    )
    return payload


def _fresh_cycle_schedule_decision(
    *,
    now: datetime,
    quiet_start_hour: int = 0,
    quiet_end_hour: int = 7,
) -> dict[str, Any]:
    current = now if now.tzinfo else now.replace(tzinfo=KST)
    current = current.astimezone(KST).replace(second=0, microsecond=0)
    start_hour = int(quiet_start_hour) % 24
    end_hour = int(quiet_end_hour) % 24
    if start_hour != 0 or end_hour != 7:
        if _hour_in_quiet_window(current.hour, start_hour=start_hour, end_hour=end_hour):
            return {
                "mode": "skip",
                "reason": f"quiet_hours_{start_hour:02d}_to_{end_hour:02d}",
                "now": current.isoformat(),
            }
        return {"mode": "normal", "now": current.isoformat(), "analysis_now": current.isoformat()}

    if 0 <= current.hour < 7:
        return {
            "mode": "skip",
            "reason": "quiet_hours_00_to_07",
            "now": current.isoformat(),
        }
    if current.hour == 7:
        marker_path = _fresh_morning_catchup_marker_path(current)
        if not marker_path.exists():
            midnight = current.replace(hour=0, minute=0)
            seven = current.replace(hour=7, minute=0)
            return {
                "mode": "morning_catchup",
                "now": current.isoformat(),
                "analysis_now": seven.isoformat(),
                "collection_window_start": midnight.isoformat(),
                "collection_window_end": seven.isoformat(),
                "marker_path": str(marker_path),
            }
    return {"mode": "normal", "now": current.isoformat(), "analysis_now": current.isoformat()}


def _hour_in_quiet_window(hour: int, *, start_hour: int, end_hour: int) -> bool:
    if start_hour == end_hour:
        return False
    if start_hour < end_hour:
        return start_hour <= hour < end_hour
    return hour >= start_hour or hour < end_hour


def _fresh_morning_catchup_marker_path(now: datetime) -> Path:
    date_key = now.astimezone(KST).strftime("%Y%m%d")
    return AUTOMATION_OUTPUT_DIR / "fresh_watch_runs" / f"morning_catchup_{date_key}.done"


def _handle_rank_candidates(args: argparse.Namespace) -> None:
    choice_path = Path(args.choice_json_path).expanduser()
    payload = json.loads(choice_path.read_text(encoding="utf-8"))
    candidates = payload.get("candidates") or []
    if not isinstance(candidates, list):
        raise SystemExit(f"candidates must be a list in {choice_path}")
    rows = []
    for index, candidate in enumerate(candidates[: max(1, int(args.limit))], start=1):
        if not isinstance(candidate, dict):
            continue
        result = rank_candidate(candidate)
        rows.append(
            {
                "index": index,
                "topic_id": candidate.get("topic_id"),
                "topic_name": candidate.get("topic_name"),
                "notification_level": result.notification_level,
                "virality_potential_score": result.virality_potential_score,
                "account_fit_score": result.account_fit_score,
                "recommendation_summary": result.recommendation_summary,
                **rank_result_to_metadata(result),
            }
        )
    if args.json:
        print(json.dumps(rows, ensure_ascii=False, indent=2))
        return
    print("idx  level      virality  fit    topic")
    for row in rows:
        print(
            f"{int(row['index']):>3}  {str(row['notification_level']):<10} "
            f"{float(row['virality_potential_score']):>7.1f} "
            f"{float(row['account_fit_score']):>5.1f}  "
            f"{row['topic_name']}"
        )


def _handle_notify_pending(repository: AutomationJobRepository, args: argparse.Namespace) -> None:
    send_limit = max(1, int(args.limit))
    fetch_limit = max(send_limit * 10, 100)
    jobs = repository.list_jobs(status=args.status, limit=fetch_limit)
    levels = set(args.level or ["immediate", "watch"])
    selected = sorted(
        [job for job in jobs if job.notification_level in levels],
        key=_notification_sort_key,
    )[:send_limit]
    sent_count = 0
    failed_count = 0
    for job in selected:
        result = send_job_notification(
            job,
            channel_id=args.channel_id,
            bot_token=args.bot_token,
            dry_run=args.dry_run,
        )
        if args.dry_run:
            print(json.dumps({"job_id": job.job_id, "payload": result.payload}, ensure_ascii=False, indent=2))
        elif result.ok:
            sent_count += 1
            repository.update_status(
                job.job_id,
                "notified",
                message="Discord notification sent",
                metadata={"discord_status_code": result.status_code},
            )
            print(f"notified {job.job_id} | {job.topic_name}")
        else:
            failed_count += 1
            repository.record_event(
                job.job_id,
                "discord_notification_failed",
                message=result.message,
                metadata={"discord_status_code": result.status_code},
            )
            print(f"failed {job.job_id} | {result.message}")
    button_worker: dict[str, Any] | None = None
    if sent_count and not args.dry_run and not args.no_start_button_worker:
        button_worker = _ensure_button_worker_from_args(repository, args)
    print(f"selected_count={len(selected)}")
    print(f"sent_count={sent_count}")
    print(f"failed_count={failed_count}")
    if button_worker:
        print(
            "button_worker="
            f"{button_worker['message']} pid={button_worker['pid']} log_path={button_worker['log_path']}"
        )


def _handle_approve(repository: AutomationJobRepository, args: argparse.Namespace) -> None:
    job = repository.update_status(
        args.job_id,
        "approved",
        message=args.message,
        metadata={"approved_by": "cli"},
    )
    print(json.dumps(job_to_dict(job), ensure_ascii=False, indent=2))


def _handle_build_approved_editor(repository: AutomationJobRepository, args: argparse.Namespace) -> None:
    lock_path = getattr(args, "lock_path", None)
    if lock_path:
        args.lock_path = None
        try:
            run_with_lock(
                lock_path,
                lambda: _handle_build_approved_editor(repository, args),
            )
        except AutomationLockBusy as exc:
            raise SystemExit(str(exc)) from exc
        finally:
            args.lock_path = lock_path
        return

    job = repository.get_job(args.job_id)
    if job is None:
        raise SystemExit(f"automation job not found: {args.job_id}")
    if job.status != "approved" and not args.allow_any_status:
        raise SystemExit(f"job must be approved before editor build: {job.job_id} status={job.status}")
    choice_json_path = str(job.metadata.get("choice_json_path") or "").strip()
    if not choice_json_path:
        raise SystemExit(f"job has no choice_json_path metadata: {job.job_id}")
    approval_run_dir = args.approval_run_dir or _approval_run_dir_from_choice_path(choice_json_path)
    plan = {
        "job_id": job.job_id,
        "topic_id": job.topic_id,
        "topic_name": job.topic_name,
        "choice_json_path": choice_json_path,
        "approval_run_dir": approval_run_dir,
        "host": args.host,
        "public_host": args.public_host,
        "port": args.port,
        "editor_token_present": bool(args.editor_token or job.metadata.get("editor_token")),
    }
    if args.dry_run:
        print(json.dumps({"dry_run": True, **plan}, ensure_ascii=False, indent=2))
        return

    repository.update_status(
        job.job_id,
        "pipeline_running",
        message="building title editor for approved job",
        metadata={"approval_run_dir": approval_run_dir},
    )
    editor_token = _resolve_editor_token(job, explicit_token=args.editor_token)
    try:
        confirmation = confirm_topic_candidates(
            choice_json_path,
            selected_topic_ids=[job.topic_id],
            approval_run_dir=approval_run_dir,
        )
        result = build_title_editor_run(
            approval_run_dir=confirmation.approval_run_dir,
            confirmed_json_path=confirmation.confirmed_json_path,
            host=args.host,
            public_host=args.public_host,
            port=args.port,
            editor_token=editor_token,
        )
    except Exception as exc:
        repository.update_status(
            job.job_id,
            "failed",
            message="editor build failed",
            failure_message=str(exc),
        )
        raise

    repository.update_job_paths(
        job.job_id,
        approval_run_dir=str(result.approval_run_dir),
        editor_url=result.editor_url,
        message="editor paths recorded",
    )
    updated = repository.update_status(
        job.job_id,
        "editor_ready",
        message="title editor ready",
        metadata={
            "confirmed_json_path": str(confirmation.confirmed_json_path),
            "title_editor_manifest_path": str(result.manifest_path),
            "title_editor_report_path": str(result.report_path),
            "topic_count": result.topic_count,
            "editor_host": args.host,
            "editor_public_host": args.public_host or args.host,
            "editor_port": args.port,
            "editor_token": editor_token,
        },
    )
    if args.notify:
        result = send_editor_ready_notification(
            updated,
            channel_id=args.channel_id,
            bot_token=args.bot_token,
            dry_run=args.dry_run_notify,
        )
        if args.dry_run_notify:
            print(json.dumps({"dry_run_notify": True, "payload": result.payload}, ensure_ascii=False, indent=2))
        elif result.ok:
            repository.record_event(
                job.job_id,
                "editor_ready_notification_sent",
                message="Discord editor-ready notification sent",
                metadata={"discord_status_code": result.status_code},
            )
        else:
            repository.record_event(
                job.job_id,
                "editor_ready_notification_failed",
                message=result.message,
                metadata={"discord_status_code": result.status_code},
            )
    print(json.dumps(job_to_dict(updated), ensure_ascii=False, indent=2))


def _handle_build_next_approved(repository: AutomationJobRepository, args: argparse.Namespace) -> None:
    approved_jobs = repository.list_jobs(status="approved", limit=20)
    if not approved_jobs:
        print(json.dumps({"built": False, "message": "no approved jobs"}, ensure_ascii=False, indent=2))
        return
    job = approved_jobs[-1]
    args.job_id = job.job_id
    args.allow_any_status = False
    try:
        run_with_lock(
            args.lock_path,
            lambda: _handle_build_approved_editor(repository, args),
        )
    except AutomationLockBusy as exc:
        raise SystemExit(str(exc)) from exc


def _handle_notify_editor_ready(repository: AutomationJobRepository, args: argparse.Namespace) -> None:
    job = repository.get_job(args.job_id)
    if job is None:
        raise SystemExit(f"automation job not found: {args.job_id}")
    if job.status != "editor_ready" and not args.allow_any_status:
        raise SystemExit(f"job editor is not ready: {job.job_id} status={job.status}")
    result = send_editor_ready_notification(
        job,
        channel_id=args.channel_id,
        bot_token=args.bot_token,
        dry_run=args.dry_run,
    )
    if args.dry_run:
        print(json.dumps({"job_id": job.job_id, "payload": result.payload}, ensure_ascii=False, indent=2))
        return
    if result.ok:
        repository.record_event(
            job.job_id,
            "editor_ready_notification_sent",
            message="Discord editor-ready notification sent",
            metadata={"discord_status_code": result.status_code},
        )
        print(f"notified {job.job_id} | editor_ready")
        return
    repository.record_event(
        job.job_id,
        "editor_ready_notification_failed",
        message=result.message,
        metadata={"discord_status_code": result.status_code},
    )
    raise SystemExit(f"editor-ready notification failed: {result.message}")


def _handle_discord_action(repository: AutomationJobRepository, args: argparse.Namespace) -> None:
    result = handle_component_action(
        repository,
        args.custom_id,
        actor=args.actor,
        dry_run=args.dry_run,
    )
    payload = {
        "ok": result.ok,
        "message": result.message,
        "dry_run": result.dry_run,
        "job": job_to_dict(result.job) if result.job else None,
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2))


def _handle_discord_button_worker(args: argparse.Namespace) -> None:
    run_discord_button_worker(
        db_path=args.db,
        bot_token=args.bot_token,
        channel_id=args.channel_id,
        auto_build=args.auto_build,
        build_host=args.host,
        build_public_host=args.public_host,
        build_port=args.port,
        tailscale_funnel_base_url=args.tailscale_funnel_base_url,
        notify_build=args.notify_build,
        build_lock_path=args.build_lock_path,
    )


def _handle_serve_job_editor(repository: AutomationJobRepository, args: argparse.Namespace) -> None:
    job = repository.get_job(args.job_id)
    if job is None:
        raise SystemExit(f"automation job not found: {args.job_id}")
    if job.status != "editor_ready" and not args.allow_any_status:
        raise SystemExit(f"job editor is not ready: {job.job_id} status={job.status}")
    approval_run_dir = job.approval_run_dir or str(job.metadata.get("approval_run_dir") or "").strip()
    manifest_path = str(job.metadata.get("title_editor_manifest_path") or "").strip()
    editor_token = args.editor_token or str(job.metadata.get("editor_token") or "").strip() or None
    if not approval_run_dir:
        raise SystemExit(f"job has no approval_run_dir: {job.job_id}")
    if not manifest_path:
        raise SystemExit(f"job has no title_editor_manifest_path metadata: {job.job_id}")
    db_path = repository.db_path

    def render_callback(render_result: dict[str, Any]) -> None:
        with AutomationJobRepository(db_path) as callback_repository:
            updated, notification = _record_render_result(
                callback_repository,
                job_id=job.job_id,
                state_path=Path(str(render_result.get("state_path") or "")),
                png_path=str(render_result.get("output_png_path") or ""),
                social_copy_md_path=str(render_result.get("social_copy_md_path") or ""),
                notify=args.notify_render,
                attach_files=not args.no_render_attachments,
                channel_id=args.channel_id,
                bot_token=args.bot_token,
            )
        render_result["automation_job_status"] = updated.status
        render_result["automation_job_id"] = updated.job_id
        if notification is not None:
            render_result["discord_notification_ok"] = notification.ok
            render_result["discord_notification_message"] = notification.message
            render_result["discord_status_code"] = notification.status_code

    serve_title_editor(
        approval_run_dir=approval_run_dir,
        manifest_path=manifest_path,
        host=args.host,
        port=args.port,
        editor_token=editor_token,
        render_callback=render_callback,
        shutdown_after_render=not args.keep_open_after_render,
        idle_timeout_seconds=args.idle_timeout_seconds,
    )


def _handle_record_render(repository: AutomationJobRepository, args: argparse.Namespace) -> None:
    job = repository.get_job(args.job_id)
    if job is None:
        raise SystemExit(f"automation job not found: {args.job_id}")
    if job.status not in {"editor_ready", "render_ready"} and not args.allow_any_status:
        raise SystemExit(f"job editor is not ready for render recording: {job.job_id} status={job.status}")
    state_path = Path(args.state_path).expanduser()
    if not state_path.exists():
        raise SystemExit(f"state file not found: {state_path}")
    state_payload = json.loads(state_path.read_text(encoding="utf-8"))
    png_path = args.png_path or str(state_payload.get("output_path") or "")
    if not png_path:
        raise SystemExit("png path is required via --png-path or state output_path")
    social_copy_md_path = args.social_copy_md_path or _infer_social_copy_md_path(state_path)
    plan = {
        "job_id": job.job_id,
        "state_path": str(state_path),
        "png_path": png_path,
        "social_copy_md_path": social_copy_md_path,
        "notify": bool(args.notify),
    }
    if args.dry_run:
        dry_job = job
        dry_job.render_png_path = png_path
        dry_job.social_copy_md_path = social_copy_md_path
        social_copy_text = _read_social_copy_for_job(dry_job, social_copy_md_path)
        payload = send_render_notification(
            dry_job,
            state_payload=state_payload,
            social_copy_text=social_copy_text,
            file_paths=[] if args.no_attachments else _render_attachment_paths(png_path, social_copy_md_path),
            dry_run=True,
        ).payload
        print(json.dumps({"dry_run": True, **plan, "payload": payload}, ensure_ascii=False, indent=2))
        return

    updated, _ = _record_render_result(
        repository,
        job_id=job.job_id,
        state_path=state_path,
        png_path=png_path,
        social_copy_md_path=social_copy_md_path,
        notify=args.notify,
        attach_files=not args.no_attachments,
        channel_id=args.channel_id,
        bot_token=args.bot_token,
    )
    print(json.dumps(job_to_dict(updated), ensure_ascii=False, indent=2))


def _handle_approve_publish(repository: AutomationJobRepository, args: argparse.Namespace) -> None:
    job = repository.get_job(args.job_id)
    if job is None:
        raise SystemExit(f"automation job not found: {args.job_id}")
    if job.status != "render_ready":
        raise SystemExit(f"job must be render_ready before publish approval: {job.job_id} status={job.status}")
    if not job.render_png_path:
        raise SystemExit(f"job has no render_png_path: {job.job_id}")
    if not job.social_copy_md_path:
        raise SystemExit(f"job has no social_copy_md_path: {job.job_id}")
    updated = repository.update_status(
        job.job_id,
        "publish_approved",
        message=args.message,
        metadata={"publish_approved_by": "cli"},
    )
    print(json.dumps(job_to_dict(updated), ensure_ascii=False, indent=2))


def _handle_publish_instagram(repository: AutomationJobRepository, args: argparse.Namespace) -> None:
    job = repository.get_job(args.job_id)
    if job is None:
        raise SystemExit(f"automation job not found: {args.job_id}")
    if job.status != "publish_approved" and not args.allow_any_status:
        raise SystemExit(f"job must be publish_approved before Instagram publish: {job.job_id} status={job.status}")
    caption_path = args.caption_path or job.social_copy_md_path
    if args.dry_run:
        try:
            plan = build_instagram_publish_plan(
                job,
                image_url=args.image_url,
                caption=args.caption,
                caption_path=caption_path,
                alt_text=args.alt_text,
                graph_api_version=args.graph_api_version or "v24.0",
            )
        except Exception as exc:
            raise SystemExit(str(exc)) from exc
        print(json.dumps({"dry_run": True, "plan": _publish_plan_to_dict(plan)}, ensure_ascii=False, indent=2))
        return
    try:
        result = publish_instagram_image(
            job,
            image_url=args.image_url,
            caption=args.caption,
            caption_path=caption_path,
            alt_text=args.alt_text,
            ig_user_id=args.ig_user_id,
            access_token=args.access_token,
            graph_api_version=args.graph_api_version,
        )
    except Exception as exc:
        repository.update_status(
            job.job_id,
            "failed",
            message="Instagram publish failed",
            failure_message=str(exc),
            metadata={"instagram_publish_failed_at": _utc_now_text()},
        )
        raise
    if not result.ok:
        updated = repository.update_status(
            job.job_id,
            "failed",
            message="Instagram publish failed",
            failure_message=result.message,
            metadata=publish_result_to_metadata(result),
        )
        print(json.dumps(job_to_dict(updated), ensure_ascii=False, indent=2))
        return
    updated = repository.update_status(
        job.job_id,
        "published",
        message="Instagram media published",
        metadata=publish_result_to_metadata(result),
    )
    repository.record_event(
        job.job_id,
        "instagram_published",
        message=result.permalink or result.media_id or "published",
        metadata=publish_result_to_metadata(result),
    )
    print(json.dumps(job_to_dict(updated), ensure_ascii=False, indent=2))


def _handle_health(repository: AutomationJobRepository, args: argparse.Namespace) -> None:
    report = build_health_report(
        repository,
        disk_path=args.disk_path or repository.db_path.parent,
        min_free_gb=args.min_free_gb,
        pending_hours=args.pending_hours,
        pipeline_hours=args.pipeline_hours,
    )
    payload = health_report_to_dict(report)
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return
    print(f"db_path={payload['db_path']}")
    print(f"total_jobs={payload['total_jobs']}")
    print(f"status_counts={json.dumps(payload['status_counts'], ensure_ascii=False, sort_keys=True)}")
    print(f"stale_pipeline_running={len(payload['stale_pipeline_running'])}")
    print(f"stale_pending_approval={len(payload['stale_pending_approval'])}")
    print(f"disk_path={payload['disk_path']}")
    print(f"disk_free_gb={payload['disk_free_gb']}")
    print(f"disk_ok={payload['disk_ok']}")
    print(f"log_dir={payload['log_dir']}")
    print(f"lock_path={payload['lock_path']}")


def _handle_recover_running(repository: AutomationJobRepository, args: argparse.Namespace) -> None:
    result = recover_stale_pipeline_jobs(
        repository,
        stale_hours=args.stale_hours,
        target_status=args.target_status,
    )
    payload = {
        "recovered_count": len(result.recovered_jobs),
        "skipped_count": len(result.skipped_jobs),
        "recovered_jobs": [job_to_dict(job) for job in result.recovered_jobs],
        "skipped_jobs": [job_to_dict(job) for job in result.skipped_jobs],
    }
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return
    print(f"recovered_count={payload['recovered_count']}")
    print(f"skipped_count={payload['skipped_count']}")
    for job in result.recovered_jobs:
        print(f"recovered {job.job_id} -> {job.status} | {job.topic_name}")


def _handle_expire_pending(repository: AutomationJobRepository, args: argparse.Namespace) -> None:
    statuses = set(args.status or []) or None
    result = expire_old_pending_jobs(
        repository,
        stale_hours=args.stale_hours,
        statuses=statuses,
    )
    payload = {
        "expired_count": len(result.expired_jobs),
        "skipped_count": len(result.skipped_jobs),
        "expired_jobs": [job_to_dict(job) for job in result.expired_jobs],
        "skipped_jobs": [job_to_dict(job) for job in result.skipped_jobs],
    }
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return
    print(f"expired_count={payload['expired_count']}")
    print(f"skipped_count={payload['skipped_count']}")
    for job in result.expired_jobs:
        print(f"expired {job.job_id} | {job.topic_name}")


def _handle_digest(repository: AutomationJobRepository, args: argparse.Namespace) -> None:
    report = build_digest_report(repository, since_hours=args.since_hours)
    payload = digest_report_to_dict(report)
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return
    print(f"since_hours={payload['since_hours']}")
    print(f"status_counts={json.dumps(payload['status_counts'], ensure_ascii=False, sort_keys=True)}")
    print(f"recent_count={payload['recent_count']}")
    print(f"failed_count={payload['failed_count']}")
    print(f"render_ready_count={payload['render_ready_count']}")
    for row in payload["recent_jobs"][:10]:
        print(f"{row['job_id']} | {row['status']} | {row['topic_name']}")


def _handle_skip(repository: AutomationJobRepository, args: argparse.Namespace) -> None:
    message = args.message or f"skipped by CLI: {args.reason}"
    job = repository.update_status(
        args.job_id,
        "skipped",
        message=message,
        metadata={"skip_reason": args.reason},
    )
    print(json.dumps(job_to_dict(job), ensure_ascii=False, indent=2))


def _ensure_button_worker_from_args(repository: AutomationJobRepository, args: argparse.Namespace) -> dict[str, Any]:
    result = ensure_discord_button_worker(
        db_path=repository.db_path,
        bot_token=getattr(args, "bot_token", None),
        channel_id=getattr(args, "channel_id", None),
        auto_build=True,
        build_host=getattr(args, "button_worker_host", "127.0.0.1"),
        build_public_host=getattr(args, "button_worker_public_host", None),
        build_port=getattr(args, "button_worker_port", 8787),
        tailscale_funnel_base_url=getattr(args, "button_worker_tailscale_funnel_base_url", None),
        notify_build=True,
    )
    return {
        "started": result.started,
        "pid": result.pid,
        "message": result.message,
        "pid_path": str(result.pid_path),
        "log_path": str(result.log_path),
    }


def _record_render_result(
    repository: AutomationJobRepository,
    *,
    job_id: str,
    state_path: Path,
    png_path: str,
    social_copy_md_path: str | None,
    notify: bool,
    attach_files: bool,
    channel_id: str | None = None,
    bot_token: str | None = None,
) -> tuple[AutomationJob, Any | None]:
    job = repository.get_job(job_id)
    if job is None:
        raise KeyError(f"automation job not found: {job_id}")
    if not str(state_path):
        raise ValueError("state_path is required")
    resolved_state_path = state_path.expanduser()
    if not resolved_state_path.exists() or not resolved_state_path.is_file():
        raise ValueError(f"state file not found: {resolved_state_path}")
    state_payload = json.loads(resolved_state_path.read_text(encoding="utf-8"))
    resolved_png_path = str(png_path or state_payload.get("output_path") or "").strip()
    if not resolved_png_path:
        raise ValueError("png path is required")
    resolved_social_copy_md_path = (
        str(social_copy_md_path or "").strip()
        or _infer_social_copy_md_path(resolved_state_path)
    )
    repository.update_job_paths(
        job.job_id,
        render_png_path=resolved_png_path,
        social_copy_md_path=resolved_social_copy_md_path,
        message="render output paths recorded",
    )
    updated = repository.update_status(
        job.job_id,
        "render_ready",
        message="render output recorded",
        metadata={
            "render_state_path": str(resolved_state_path),
            "render_title_text": state_payload.get("title_text"),
            "render_subheadline": state_payload.get("subheadline"),
            "render_photo_credit": state_payload.get("selected_image_source_name"),
        },
    )
    notification = None
    if notify:
        social_copy_text = _read_social_copy_for_job(updated, resolved_social_copy_md_path)
        notification = send_render_notification(
            updated,
            state_payload=state_payload,
            social_copy_text=social_copy_text,
            file_paths=[] if not attach_files else _render_attachment_paths(resolved_png_path, resolved_social_copy_md_path),
            channel_id=channel_id,
            bot_token=bot_token,
        )
        if notification.ok:
            repository.record_event(
                job.job_id,
                "render_notification_sent",
                message="Discord render notification sent",
                metadata={"discord_status_code": notification.status_code},
            )
        else:
            repository.record_event(
                job.job_id,
                "render_notification_failed",
                message=notification.message,
                metadata={"discord_status_code": notification.status_code},
            )
    return updated, notification


def _article_from_dict(value: dict[str, Any]) -> AutomationJobArticle:
    return AutomationJobArticle(
        article_id=_optional_string(value.get("article_id")),
        title=str(value.get("title") or ""),
        source_type=str(value.get("source_type") or ""),
        source_url=str(value.get("source_url") or ""),
        published_at=_optional_string(value.get("published_at")),
    )


def _notification_sort_key(job: AutomationJob) -> tuple[int, float, float, str]:
    level_rank = {
        "immediate": 0,
        "watch": 1,
        "digest": 2,
    }.get(job.notification_level, 9)
    return (
        level_rank,
        -float(job.virality_potential_score),
        -float(job.account_fit_score),
        job.created_at.isoformat(),
    )


def _resolve_editor_token(job: AutomationJob, *, explicit_token: str | None = None) -> str:
    token = (explicit_token or str(job.metadata.get("editor_token") or "")).strip()
    if token:
        return token
    return secrets.token_urlsafe(24)


def _parse_json_object(raw: str, *, label: str) -> dict[str, Any]:
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise SystemExit(f"invalid {label}: {exc}") from exc
    if not isinstance(parsed, dict):
        raise SystemExit(f"invalid {label}: expected JSON object")
    return parsed


def _optional_string(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _approval_run_dir_from_choice_path(choice_json_path: str) -> str:
    path = Path(choice_json_path).expanduser().resolve()
    for parent in [path.parent, *path.parents]:
        if parent.name.startswith("approval_run_"):
            return str(parent)
    if path.name == "topic_selection_choice.json" and path.parent.parent.name.startswith("fresh_watch_runs"):
        return str(path.parent)
    # Existing test/automation runs may not use approval_run_* names.
    if path.parent.name == "01_topic_candidates":
        return str(path.parent.parent)
    raise SystemExit(f"could not infer approval_run_dir from choice_json_path: {choice_json_path}")


def _infer_social_copy_md_path(state_path: Path) -> str | None:
    candidate = state_path.parent / "title_render_social_copy.md"
    if candidate.exists():
        return str(candidate)
    return None


def _read_social_copy_for_job(job: AutomationJob, social_copy_md_path: str | None) -> str | None:
    raw_path = str(social_copy_md_path or "").strip()
    if not raw_path:
        return None
    path = Path(raw_path).expanduser()
    if not path.exists():
        return None
    markdown = path.read_text(encoding="utf-8")
    extracted = extract_caption_for_topic(markdown, job.topic_id)
    return extracted


def _render_attachment_paths(png_path: str, social_copy_md_path: str | None) -> list[Path]:
    paths: list[Path] = []
    for raw_path in [png_path]:
        text = str(raw_path or "").strip()
        if not text:
            continue
        path = Path(text).expanduser()
        if path.exists() and path.is_file():
            paths.append(path)
    return paths


def _publish_plan_to_dict(plan: Any) -> dict[str, Any]:
    return {
        "job_id": plan.job_id,
        "image_url": plan.image_url,
        "caption": plan.caption,
        "alt_text": plan.alt_text,
        "graph_api_version": plan.graph_api_version,
        "caption_length": plan.caption_length,
        "hashtag_count": plan.hashtag_count,
        "mention_count": plan.mention_count,
    }


def _utc_now_text() -> str:
    return utc_now().isoformat()


if __name__ == "__main__":
    main()
