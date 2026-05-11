"""Workflow helpers for approval-driven manual operations."""

from kbo_card_news.workflows.approval_flow import (
    build_topic_selection_template,
    card_news_draft_from_dict,
    confirm_topic_selection,
    issue_asset_contexts_from_list,
    issue_candidate_from_dict,
    multimodal_analysis_from_dict,
    selection_result_from_dict,
    serialize_for_json,
)
from kbo_card_news.workflows.approval_history import (
    append_completed_topic_entries,
    build_completed_topic_entry,
    completed_topic_registry_path,
    is_completed_topic,
    load_completed_topic_registry,
    normalize_topic_name,
)
from kbo_card_news.workflows.approval_paths import (
    build_approval_run_name,
    ensure_stage_dir,
    resolve_approval_run_dir,
)

__all__ = [
    "build_topic_selection_template",
    "append_completed_topic_entries",
    "build_completed_topic_entry",
    "card_news_draft_from_dict",
    "completed_topic_registry_path",
    "confirm_topic_selection",
    "build_approval_run_name",
    "issue_asset_contexts_from_list",
    "issue_candidate_from_dict",
    "is_completed_topic",
    "load_completed_topic_registry",
    "multimodal_analysis_from_dict",
    "normalize_topic_name",
    "ensure_stage_dir",
    "resolve_approval_run_dir",
    "selection_result_from_dict",
    "serialize_for_json",
]
