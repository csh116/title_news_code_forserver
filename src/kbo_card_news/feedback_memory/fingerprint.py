from __future__ import annotations

import re
from dataclasses import asdict, is_dataclass
from typing import Any, Mapping

from kbo_card_news.feedback_memory.asset_features import extract_asset_features
from kbo_card_news.feedback_memory.topic_features import extract_topic_features

_TEXT_TOKEN_RE = re.compile(r"[0-9a-zA-Z]+|[가-힣]+")
_PLAYER_STOPWORDS = {
    "kbo",
    "lg",
    "kia",
    "ssg",
    "nc",
    "kt",
    "samsung",
    "lotte",
    "doosan",
    "hanwha",
    "kiwoom",
    "ktwiz",
    "dinos",
    "twins",
    "tigers",
    "wyverns",
    "landers",
    "lions",
    "giants",
    "bears",
    "eagles",
    "heroes",
    "경기",
    "승리",
    "패배",
    "무승부",
    "홈런",
    "끝내기",
    "역전",
    "프리뷰",
    "예고",
    "기록",
    "부상",
    "복귀",
    "세리머니",
    "활약",
    "이슈",
    "뉴스",
    "쇼",
}
_TOPIC_ANCHOR_STOPWORDS = {
    "kbo",
    "리그",
    "뉴스",
    "이슈",
    "경기",
    "기사",
}
_GENERIC_ASSET_HINT_STOPWORDS = {
    "이미지",
    "사진",
    "장면",
    "컷",
    "포인트",
    "중심",
    "강조",
    "활용",
    "추천",
    "용도",
}
_TEAM_ALIASES = {
    "lg": "lg",
    "lg트윈스": "lg",
    "트윈스": "lg",
    "kia": "kia",
    "kia타이거즈": "kia",
    "기아": "kia",
    "타이거즈": "kia",
    "ssg": "ssg",
    "ssg랜더스": "ssg",
    "랜더스": "ssg",
    "nc": "nc",
    "nc다이노스": "nc",
    "다이노스": "nc",
    "kt": "kt",
    "kt위즈": "kt",
    "위즈": "kt",
    "samsung": "samsung",
    "삼성": "samsung",
    "삼성라이온즈": "samsung",
    "라이온즈": "samsung",
    "lotte": "lotte",
    "롯데": "lotte",
    "롯데자이언츠": "lotte",
    "자이언츠": "lotte",
    "doosan": "doosan",
    "두산": "doosan",
    "두산베어스": "doosan",
    "베어스": "doosan",
    "hanwha": "hanwha",
    "한화": "hanwha",
    "한화이글스": "hanwha",
    "이글스": "hanwha",
    "kiwoom": "kiwoom",
    "키움": "kiwoom",
    "키움히어로즈": "kiwoom",
    "히어로즈": "kiwoom",
}


def build_topic_fingerprint(
    source: Any = None,
    *,
    overrides: Mapping[str, Any] | None = None,
) -> str | None:
    features = extract_topic_features(source, overrides=overrides)
    metadata = _coerce_mapping(_read_value(source, "metadata"))
    raw_payload = _coerce_mapping(_read_value(source, "raw_payload"))

    topic_name = _coalesce_text_value(source, metadata, raw_payload, ("topic_name", "title", "headline"))
    team_name = _coalesce_text_value(source, metadata, raw_payload, ("team_name", "team", "team_code"))
    player_name = _coalesce_text_value(source, metadata, raw_payload, ("player_name", "entity_name", "player"))

    entity_tokens = _extract_topic_entity_tokens(
        topic_name=topic_name,
        team_name=team_name,
        player_name=player_name,
        entity_focus=features.get("entity_focus"),
    )
    topic_anchor = _build_topic_anchor(topic_name, entity_tokens=entity_tokens)

    components = [
        _slug_component(features.get("topic_type")),
        _slug_component(features.get("entity_focus")),
        _slug_component(features.get("event_type")),
        _slug_component(features.get("angle_type")),
        _join_component(entity_tokens),
        topic_anchor,
    ]
    if not any(component for component in components):
        return None
    return "topic:" + "|".join(component or "-" for component in components)


def build_asset_fingerprint(
    source: Any = None,
    *,
    overrides: Mapping[str, Any] | None = None,
) -> str | None:
    features = extract_asset_features(source, overrides=overrides)
    metadata = _coerce_mapping(_read_value(source, "metadata"))
    raw_payload = _coerce_mapping(_read_value(source, "raw_payload"))

    usage_hint = _build_asset_usage_hint(source, metadata=metadata, raw_payload=raw_payload)
    ratio_bucket = _build_aspect_ratio_bucket(features.get("aspect_ratio"))
    components = [
        _slug_component(features.get("shot_type")),
        _slug_component(features.get("subject_role")),
        _slug_component(features.get("person_count_bucket")),
        _slug_component("action" if features.get("is_action_shot") else "static"),
        _slug_component("postgame" if features.get("is_post_game") else "ingame"),
        ratio_bucket,
        _slug_component(features.get("caption_signal")),
        usage_hint,
    ]
    meaningful_components = [component for component in components if component and component != "-"]
    if not meaningful_components:
        return None
    return "asset:" + "|".join(component or "-" for component in components)


def _build_topic_anchor(topic_name: str | None, *, entity_tokens: list[str]) -> str | None:
    tokens = _normalize_token_list(topic_name)
    entity_set = set(entity_tokens)
    filtered = [
        token for token in tokens
        if token not in entity_set and token not in _TOPIC_ANCHOR_STOPWORDS
    ]
    if not filtered:
        filtered = [token for token in tokens if token not in entity_set]
    return _join_component(filtered[:2])


def _extract_topic_entity_tokens(
    *,
    topic_name: str | None,
    team_name: str | None,
    player_name: str | None,
    entity_focus: Any,
) -> list[str]:
    entities: list[str] = []
    for text in (team_name, topic_name):
        for token in _extract_team_tokens(text):
            if token not in entities:
                entities.append(token)

    focus = _slug_component(entity_focus)
    if focus == "player":
        player_token = _extract_player_token(player_name or topic_name)
        if player_token and player_token not in entities:
            entities.insert(0, player_token)
    return entities[:2]


def _extract_team_tokens(text: str | None) -> list[str]:
    if not text:
        return []
    normalized_text = str(text).strip().lower()
    found: list[str] = []
    for alias, canonical in _TEAM_ALIASES.items():
        if alias in normalized_text and canonical not in found:
            found.append(canonical)
    if found:
        return found
    return []


def _extract_player_token(text: str | None) -> str | None:
    for token in _normalize_token_list(text):
        if token in _PLAYER_STOPWORDS:
            continue
        if token in _TEAM_ALIASES.values():
            continue
        if token.isdigit():
            continue
        return token
    return None


def _build_asset_usage_hint(
    source: Any,
    *,
    metadata: Mapping[str, Any],
    raw_payload: Mapping[str, Any],
) -> str | None:
    values: list[str] = []
    usage_recommendation = _coalesce_text_value(
        source,
        metadata,
        raw_payload,
        ("usage_recommendation", "before_usage_recommendation", "after_usage_recommendation"),
    )
    if usage_recommendation:
        values.extend(_normalize_token_list(usage_recommendation))

    for field_name in ("event_tags", "composition_tags", "subject_tags"):
        values.extend(_normalize_tag_values(_coalesce_value(source, metadata, raw_payload, field_name)))

    normalized = sorted({
        token for token in values
        if token and token not in _GENERIC_ASSET_HINT_STOPWORDS
    })
    return _join_component(normalized[:3])


def _normalize_tag_values(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return _normalize_token_list(value)
    if isinstance(value, Mapping):
        return _normalize_token_list(" ".join(str(item) for item in value.values()))
    if isinstance(value, (list, tuple, set)):
        tokens: list[str] = []
        for item in value:
            tokens.extend(_normalize_token_list(item))
        return tokens
    return _normalize_token_list(value)


def _build_aspect_ratio_bucket(value: Any) -> str | None:
    if value is None:
        return None
    try:
        ratio = float(value)
    except (TypeError, ValueError):
        return None
    normalized_ratio = round(ratio, 2)
    if normalized_ratio < 0.95:
        orientation = "portrait"
    elif normalized_ratio > 1.05:
        orientation = "landscape"
    else:
        orientation = "square"
    return f"{orientation}-{normalized_ratio:.2f}".rstrip("0").rstrip(".")


def _join_component(tokens: list[str]) -> str | None:
    normalized = [token for token in tokens if token]
    if not normalized:
        return None
    return "-".join(normalized[:3])


def _slug_component(value: Any) -> str | None:
    tokens = _normalize_token_list(value)
    if not tokens:
        return None
    return "-".join(tokens[:3])


def _normalize_token_list(value: Any) -> list[str]:
    if value is None:
        return []
    text = str(value).strip().lower()
    if not text:
        return []
    tokens = [match.group(0) for match in _TEXT_TOKEN_RE.finditer(text)]
    normalized: list[str] = []
    for token in tokens:
        compact = token.strip("-_ ")
        if compact:
            normalized.append(compact)
    return normalized


def _read_value(source: Any, field_name: str) -> Any:
    if source is None:
        return None
    if isinstance(source, Mapping):
        return source.get(field_name)
    if is_dataclass(source):
        return getattr(source, field_name, None)
    return getattr(source, field_name, None)


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


def _coalesce_value(
    source: Any,
    metadata: Mapping[str, Any],
    raw_payload: Mapping[str, Any],
    field_name: str,
) -> Any:
    direct_value = _read_value(source, field_name)
    if direct_value is not None:
        return direct_value
    if field_name in metadata and metadata[field_name] is not None:
        return metadata[field_name]
    if field_name in raw_payload and raw_payload[field_name] is not None:
        return raw_payload[field_name]
    return None


def _coalesce_text_value(
    source: Any,
    metadata: Mapping[str, Any],
    raw_payload: Mapping[str, Any],
    field_names: tuple[str, ...],
) -> str | None:
    for field_name in field_names:
        value = _coalesce_value(source, metadata, raw_payload, field_name)
        if value is None:
            continue
        text = str(value).strip()
        if text:
            return text
    return None
