from __future__ import annotations

from dataclasses import asdict, is_dataclass
from datetime import datetime
from typing import Any, Mapping, Sequence

from kbo_card_news.feedback_memory.asset_features import extract_asset_features
from kbo_card_news.feedback_memory.fingerprint import build_asset_fingerprint, build_topic_fingerprint
from kbo_card_news.feedback_memory.storage import FeedbackMemoryRepository
from kbo_card_news.feedback_memory.topic_features import extract_topic_features

_HEADLINE_JSON_FIELDS = {"referenced_memory_ids"}
_HEADLINE_BOOL_FIELDS = {"memory_context_used", "has_notable_numbers"}
_MULTIMODAL_JSON_FIELDS = {
    "before_subject_tags",
    "after_subject_tags",
    "before_event_tags",
    "after_event_tags",
    "before_emotion_tags",
    "after_emotion_tags",
    "before_composition_tags",
    "after_composition_tags",
    "before_risk_tags",
    "after_risk_tags",
    "referenced_memory_ids",
}
_MULTIMODAL_BOOL_FIELDS = {
    "memory_context_used",
    "has_notable_numbers",
    "is_action_shot",
    "is_post_game",
}


def retrieve_similar_headline_edits(
    source: Any = None,
    *,
    repository: FeedbackMemoryRepository | None = None,
    overrides: Mapping[str, Any] | None = None,
    top_k: int = 3,
    candidate_limit: int = 50,
) -> list[dict[str, Any]]:
    topic_features = extract_topic_features(source, overrides=overrides)
    topic_fingerprint = build_topic_fingerprint(source, overrides=topic_features)
    filters = {
        "topic_type": topic_features.get("topic_type"),
        "entity_focus": topic_features.get("entity_focus"),
        "event_type": topic_features.get("event_type"),
    }
    query, params = _build_filter_query(
        table_name="headline_edit_memory",
        filters=filters,
        candidate_limit=candidate_limit,
    )
    rows = _select_records(
        query=query,
        params=params,
        repository=repository,
        json_fields=_HEADLINE_JSON_FIELDS,
        bool_fields=_HEADLINE_BOOL_FIELDS,
    )
    scored_rows: list[dict[str, Any]] = []
    for row in rows:
        score = _score_headline_row(
            row=row,
            current_features=topic_features,
            topic_fingerprint=topic_fingerprint,
        )
        enriched_row = dict(row)
        enriched_row["retrieval_score"] = round(score, 4)
        enriched_row["retrieval_common_features"] = _headline_common_features(row, topic_features)
        scored_rows.append(enriched_row)
    return _sort_and_limit(scored_rows, top_k=top_k)


def retrieve_similar_multimodal_edits(
    source: Any = None,
    *,
    topic_source: Any = None,
    repository: FeedbackMemoryRepository | None = None,
    asset_overrides: Mapping[str, Any] | None = None,
    topic_overrides: Mapping[str, Any] | None = None,
    top_k: int = 3,
    candidate_limit: int = 50,
) -> list[dict[str, Any]]:
    asset_features = extract_asset_features(source, overrides=asset_overrides)
    topic_features = extract_topic_features(topic_source if topic_source is not None else source, overrides=topic_overrides)
    asset_payload = {
        **_coerce_mapping(source),
        **asset_features,
    }
    topic_payload = topic_source if topic_source is not None else source
    asset_fingerprint = build_asset_fingerprint(asset_payload, overrides=asset_features)
    topic_fingerprint = build_topic_fingerprint(topic_payload, overrides=topic_features)
    filters = {
        "shot_type": asset_features.get("shot_type"),
        "subject_role": asset_features.get("subject_role"),
        "is_action_shot": asset_features.get("is_action_shot"),
    }
    query, params = _build_filter_query(
        table_name="multimodal_edit_memory",
        filters=filters,
        candidate_limit=candidate_limit,
    )
    rows = _select_records(
        query=query,
        params=params,
        repository=repository,
        json_fields=_MULTIMODAL_JSON_FIELDS,
        bool_fields=_MULTIMODAL_BOOL_FIELDS,
    )
    scored_rows: list[dict[str, Any]] = []
    for row in rows:
        score = _score_multimodal_row(
            row=row,
            asset_features=asset_features,
            topic_features=topic_features,
            asset_fingerprint=asset_fingerprint,
            topic_fingerprint=topic_fingerprint,
        )
        enriched_row = dict(row)
        enriched_row["retrieval_score"] = round(score, 4)
        enriched_row["retrieval_common_features"] = _multimodal_common_features(row, asset_features, topic_features)
        scored_rows.append(enriched_row)
    return _sort_and_limit(scored_rows, top_k=top_k)


def format_headline_retrieval_summary(rows: Sequence[Mapping[str, Any]]) -> str:
    lines: list[str] = []
    for index, row in enumerate(rows, start=1):
        topic_label = _compose_topic_label(row)
        before_title = _display_text(row.get("before_title_text"))
        after_title = _display_text(row.get("after_title_text"))
        before_subheadline = _display_text(row.get("before_subheadline"))
        after_subheadline = _display_text(row.get("after_subheadline"))
        common_features = ", ".join(row.get("retrieval_common_features") or []) or "-"
        lines.append(
            f"{index}. topic={topic_label} score={_format_score(row.get('retrieval_score'))} common={common_features}"
        )
        lines.append(f"   title: {before_title} -> {after_title}")
        lines.append(f"   subheadline: {before_subheadline} -> {after_subheadline}")
    return "\n".join(lines)


def format_multimodal_retrieval_summary(rows: Sequence[Mapping[str, Any]]) -> str:
    lines: list[str] = []
    for index, row in enumerate(rows, start=1):
        asset_label = _compose_asset_label(row)
        usage_before = _display_text(row.get("before_usage_recommendation"))
        usage_after = _display_text(row.get("after_usage_recommendation"))
        scene_after = _display_text(row.get("after_scene_description"))
        humor_after = _display_text(row.get("after_humor_point"))
        tag_after = _display_text(row.get("after_tag_summary"))
        common_features = ", ".join(row.get("retrieval_common_features") or []) or "-"
        lines.append(
            f"{index}. asset={asset_label} score={_format_score(row.get('retrieval_score'))} common={common_features}"
        )
        lines.append(f"   usage: {usage_before} -> {usage_after}")
        lines.append(f"   scene/humor/tag: {scene_after} / {humor_after} / {tag_after}")
    return "\n".join(lines)


def _build_filter_query(
    *,
    table_name: str,
    filters: Mapping[str, Any],
    candidate_limit: int,
) -> tuple[str, list[Any]]:
    clauses: list[str] = []
    params: list[Any] = []
    for field_name, value in filters.items():
        if value is None:
            continue
        clauses.append(f"{field_name} = ?")
        params.append(value)
    where_sql = " AND ".join(clauses) if clauses else "1=1"
    query = (
        f"SELECT * FROM {table_name} "
        f"WHERE {where_sql} "
        f"ORDER BY created_at DESC "
        f"LIMIT ?"
    )
    params.append(max(int(candidate_limit), 1))
    return query, params


def _select_records(
    *,
    query: str,
    params: Sequence[Any],
    repository: FeedbackMemoryRepository | None,
    json_fields: set[str],
    bool_fields: set[str],
) -> list[dict[str, Any]]:
    owns_repository = repository is None
    active_repository = repository or FeedbackMemoryRepository()
    try:
        result = active_repository.safe_select_records(
            query,
            params,
            json_fields=json_fields,
            bool_fields=bool_fields,
            operation_name="feedback_memory_retrieve",
        )
        return list(result.rows) if result.ok else []
    finally:
        if owns_repository:
            active_repository.close()


def _score_headline_row(
    *,
    row: Mapping[str, Any],
    current_features: Mapping[str, Any],
    topic_fingerprint: str | None,
) -> float:
    score = 0.0
    if topic_fingerprint and row.get("topic_fingerprint") == topic_fingerprint:
        score += 3.0
    if _matches(row.get("angle_type"), current_features.get("angle_type")):
        score += 2.0
    if _matches(row.get("recommended_focus"), current_features.get("recommended_focus")):
        score += 1.5
    if _matches(row.get("has_notable_numbers"), current_features.get("has_notable_numbers")):
        score += 1.0
    score += _count_similarity(row.get("article_count"), current_features.get("article_count"), weight=0.8)
    score += _count_similarity(row.get("asset_count"), current_features.get("asset_count"), weight=0.7)
    score += _recency_bonus(row.get("created_at"), weight=0.3)
    return score


def _score_multimodal_row(
    *,
    row: Mapping[str, Any],
    asset_features: Mapping[str, Any],
    topic_features: Mapping[str, Any],
    asset_fingerprint: str | None,
    topic_fingerprint: str | None,
) -> float:
    score = 0.0
    if asset_fingerprint and row.get("asset_fingerprint") == asset_fingerprint:
        score += 3.0
    if topic_fingerprint and row.get("topic_fingerprint") == topic_fingerprint:
        score += 1.0
    if _matches(row.get("person_count_bucket"), asset_features.get("person_count_bucket")):
        score += 1.0
    score += _aspect_ratio_similarity(row.get("aspect_ratio"), asset_features.get("aspect_ratio"), weight=1.2)
    if _matches(row.get("caption_signal"), asset_features.get("caption_signal")):
        score += 1.0
    if _matches(row.get("is_post_game"), asset_features.get("is_post_game")):
        score += 0.8
    for field_name in ("topic_type", "entity_focus", "event_type"):
        if _matches(row.get(field_name), topic_features.get(field_name)):
            score += 0.5
    score += _recency_bonus(row.get("created_at"), weight=0.2)
    return score


def _headline_common_features(row: Mapping[str, Any], current_features: Mapping[str, Any]) -> list[str]:
    labels: list[str] = []
    for field_name in ("topic_type", "entity_focus", "event_type", "angle_type"):
        if _matches(row.get(field_name), current_features.get(field_name)):
            labels.append(field_name)
    if _matches(row.get("recommended_focus"), current_features.get("recommended_focus")):
        labels.append("recommended_focus")
    if _matches(row.get("has_notable_numbers"), current_features.get("has_notable_numbers")):
        labels.append("has_notable_numbers")
    return labels


def _multimodal_common_features(
    row: Mapping[str, Any],
    asset_features: Mapping[str, Any],
    topic_features: Mapping[str, Any],
) -> list[str]:
    labels: list[str] = []
    for field_name in ("shot_type", "subject_role", "person_count_bucket", "caption_signal"):
        if _matches(row.get(field_name), asset_features.get(field_name)):
            labels.append(field_name)
    if _matches(row.get("is_action_shot"), asset_features.get("is_action_shot")):
        labels.append("is_action_shot")
    if _matches(row.get("is_post_game"), asset_features.get("is_post_game")):
        labels.append("is_post_game")
    for field_name in ("topic_type", "entity_focus", "event_type"):
        if _matches(row.get(field_name), topic_features.get(field_name)):
            labels.append(field_name)
    return labels


def _sort_and_limit(rows: list[dict[str, Any]], *, top_k: int) -> list[dict[str, Any]]:
    sorted_rows = sorted(
        rows,
        key=lambda row: (
            float(row.get("retrieval_score") or 0.0),
            _parse_datetime(row.get("created_at")),
            str(row.get("id") or ""),
        ),
        reverse=True,
    )
    return sorted_rows[: max(int(top_k), 0)]


def _count_similarity(raw_left: Any, raw_right: Any, *, weight: float) -> float:
    left = _to_int(raw_left)
    right = _to_int(raw_right)
    if left is None or right is None:
        return 0.0
    difference = abs(left - right)
    return weight / (1.0 + difference)


def _aspect_ratio_similarity(raw_left: Any, raw_right: Any, *, weight: float) -> float:
    left = _to_float(raw_left)
    right = _to_float(raw_right)
    if left is None or right is None:
        return 0.0
    difference = abs(left - right)
    return max(0.0, weight - (difference * weight))


def _recency_bonus(raw_created_at: Any, *, weight: float) -> float:
    created_at = _parse_datetime(raw_created_at)
    if created_at == datetime.min:
        return 0.0
    age_days = max((datetime.now() - created_at).total_seconds() / 86400.0, 0.0)
    return weight / (1.0 + age_days)


def _parse_datetime(value: Any) -> datetime:
    if isinstance(value, datetime):
        return value
    text = str(value or "").strip()
    if not text:
        return datetime.min
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return datetime.min


def _matches(left: Any, right: Any) -> bool:
    if left is None or right is None:
        return False
    return left == right


def _to_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _to_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _compose_topic_label(row: Mapping[str, Any]) -> str:
    parts = [
        str(row.get("topic_name") or "").strip(),
        str(row.get("team_name") or "").strip(),
        str(row.get("topic_type") or "").strip(),
    ]
    normalized = [part for part in parts if part]
    return " / ".join(normalized) or "-"


def _compose_asset_label(row: Mapping[str, Any]) -> str:
    parts = [
        str(row.get("asset_reference") or "").strip(),
        str(row.get("shot_type") or "").strip(),
        str(row.get("subject_role") or "").strip(),
    ]
    normalized = [part for part in parts if part]
    return " / ".join(normalized) or "-"


def _display_text(value: Any) -> str:
    text = str(value or "").strip()
    return text or "-"


def _format_score(value: Any) -> str:
    numeric = _to_float(value)
    if numeric is None:
        return "0.00"
    return f"{numeric:.2f}"


def _coerce_mapping(value: Any) -> dict[str, Any]:
    if value is None:
        return {}
    if isinstance(value, Mapping):
        return dict(value)
    if is_dataclass(value):
        return asdict(value)
    if hasattr(value, "items"):
        try:
            return dict(value.items())
        except Exception:
            return {}
    return {}
