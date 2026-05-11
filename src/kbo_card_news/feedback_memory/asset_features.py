from __future__ import annotations

from dataclasses import asdict, dataclass, is_dataclass
from typing import Any, Mapping

ASSET_FEATURE_FIELD_NAMES = (
    "shot_type",
    "subject_role",
    "person_count_bucket",
    "is_action_shot",
    "is_post_game",
    "width",
    "height",
    "aspect_ratio",
    "caption_signal",
)

ASSET_FEATURE_TEXT_FIELDS = {
    "shot_type",
    "subject_role",
    "person_count_bucket",
    "caption_signal",
}

ASSET_FEATURE_INT_FIELDS = {
    "width",
    "height",
}

ASSET_FEATURE_BOOL_FIELDS = {
    "is_action_shot",
    "is_post_game",
}

ASSET_FEATURE_FLOAT_FIELDS = {
    "aspect_ratio",
}


@dataclass(slots=True)
class AssetFeatures:
    shot_type: str | None = None
    subject_role: str | None = None
    person_count_bucket: str | None = None
    is_action_shot: bool | None = None
    is_post_game: bool | None = None
    width: int | None = None
    height: int | None = None
    aspect_ratio: float | None = None
    caption_signal: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def empty_asset_features() -> dict[str, Any]:
    return AssetFeatures().to_dict()


def extract_asset_features(
    source: Any = None,
    *,
    overrides: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    values: dict[str, Any] = empty_asset_features()
    metadata = _coerce_mapping(_read_value(source, "metadata"))
    raw_payload = _coerce_mapping(_read_value(source, "raw_payload"))
    normalized_overrides = dict(overrides or {})

    for field_name in ASSET_FEATURE_FIELD_NAMES:
        raw_value = _coalesce_field_value(
            field_name,
            overrides=normalized_overrides,
            source=source,
            metadata=metadata,
            raw_payload=raw_payload,
        )
        values[field_name] = _normalize_asset_feature_value(field_name, raw_value)

    asset_text = _build_asset_text(source=source, metadata=metadata, raw_payload=raw_payload)
    asset_type = _normalize_text(_coalesce_alias_value(source, metadata, raw_payload, ("asset_type",)))
    caption_text = _collect_caption_text(source=source, metadata=metadata, raw_payload=raw_payload)

    if values["width"] is None:
        values["width"] = _infer_width(source=source, metadata=metadata, raw_payload=raw_payload)
    if values["height"] is None:
        values["height"] = _infer_height(source=source, metadata=metadata, raw_payload=raw_payload)
    if values["aspect_ratio"] is None:
        values["aspect_ratio"] = _infer_aspect_ratio(
            source=source,
            metadata=metadata,
            raw_payload=raw_payload,
            width=values["width"],
            height=values["height"],
        )
    if values["caption_signal"] is None:
        values["caption_signal"] = _infer_caption_signal(caption_text, asset_text=asset_text, asset_type=asset_type)
    if values["person_count_bucket"] is None:
        values["person_count_bucket"] = _infer_person_count_bucket(
            source=source,
            metadata=metadata,
            raw_payload=raw_payload,
            asset_text=asset_text,
            asset_type=asset_type,
        )
    if values["subject_role"] is None:
        values["subject_role"] = _infer_subject_role(
            asset_text,
            asset_type=asset_type,
            person_count_bucket=values["person_count_bucket"],
        )
    if values["shot_type"] is None:
        values["shot_type"] = _infer_shot_type(
            asset_text,
            asset_type=asset_type,
            aspect_ratio=values["aspect_ratio"],
            person_count_bucket=values["person_count_bucket"],
            subject_role=values["subject_role"],
        )
    if values["is_action_shot"] is None:
        values["is_action_shot"] = _infer_is_action_shot(
            asset_text,
            shot_type=values["shot_type"],
            subject_role=values["subject_role"],
        )
    if values["is_post_game"] is None:
        values["is_post_game"] = _infer_is_post_game(
            asset_text,
            shot_type=values["shot_type"],
            is_action_shot=values["is_action_shot"],
        )
    return values


def _coalesce_field_value(
    field_name: str,
    *,
    overrides: Mapping[str, Any],
    source: Any,
    metadata: Mapping[str, Any],
    raw_payload: Mapping[str, Any],
) -> Any:
    if field_name in overrides:
        return overrides[field_name]

    direct_value = _read_value(source, field_name)
    if direct_value is not None:
        return direct_value

    if field_name in metadata and metadata[field_name] is not None:
        return metadata[field_name]

    if field_name in raw_payload and raw_payload[field_name] is not None:
        return raw_payload[field_name]

    return None


def _coalesce_alias_value(
    source: Any,
    metadata: Mapping[str, Any],
    raw_payload: Mapping[str, Any],
    field_names: tuple[str, ...],
) -> Any:
    for field_name in field_names:
        value = _read_value(source, field_name)
        if value is None and field_name in metadata:
            value = metadata.get(field_name)
        if value is None and field_name in raw_payload:
            value = raw_payload.get(field_name)
        if value is not None:
            return value
    return None


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


def _normalize_asset_feature_value(field_name: str, value: Any) -> Any:
    if field_name in ASSET_FEATURE_TEXT_FIELDS:
        return _normalize_text(value)
    if field_name in ASSET_FEATURE_INT_FIELDS:
        return _normalize_int(value)
    if field_name in ASSET_FEATURE_BOOL_FIELDS:
        return _normalize_bool(value)
    if field_name in ASSET_FEATURE_FLOAT_FIELDS:
        return _normalize_float(value)
    return value


def _normalize_text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _normalize_int(value: Any) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    text = str(value).strip()
    if not text:
        return None
    try:
        return int(float(text))
    except ValueError:
        return None


def _normalize_bool(value: Any) -> bool | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(int(value))
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "y"}:
        return True
    if text in {"0", "false", "no", "n"}:
        return False
    return None


def _normalize_float(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return float(int(value))
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip()
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def _build_asset_text(
    *,
    source: Any,
    metadata: Mapping[str, Any],
    raw_payload: Mapping[str, Any],
) -> str:
    text_parts: list[str] = []
    for field_name in (
        "asset_type",
        "asset_reference",
        "caption",
        "vision_caption",
        "ocr_text",
        "scene_description",
        "usage_recommendation",
        "tag_summary",
        "humor_point",
        "caution_note",
        "image_file",
    ):
        value = _coalesce_alias_value(source, metadata, raw_payload, (field_name,))
        normalized = _normalize_text(value)
        if normalized:
            text_parts.append(normalized)
    for field_name in (
        "subject_tags",
        "event_tags",
        "emotion_tags",
        "composition_tags",
        "risk_tags",
    ):
        values = _coalesce_alias_value(source, metadata, raw_payload, (field_name,))
        if isinstance(values, (list, tuple)):
            text_parts.extend(_normalize_text(item) or "" for item in values)
    return " ".join(part for part in text_parts if part).lower().strip()


def _collect_caption_text(
    *,
    source: Any,
    metadata: Mapping[str, Any],
    raw_payload: Mapping[str, Any],
) -> str:
    parts: list[str] = []
    for field_name in ("caption", "vision_caption", "ocr_text"):
        value = _coalesce_alias_value(source, metadata, raw_payload, (field_name,))
        normalized = _normalize_text(value)
        if normalized:
            parts.append(normalized)
    return " ".join(parts).strip()


def _infer_width(
    *,
    source: Any,
    metadata: Mapping[str, Any],
    raw_payload: Mapping[str, Any],
) -> int | None:
    return _normalize_int(
        _coalesce_alias_value(
            source,
            metadata,
            raw_payload,
            ("width", "image_width", "pixel_width"),
        )
    )


def _infer_height(
    *,
    source: Any,
    metadata: Mapping[str, Any],
    raw_payload: Mapping[str, Any],
) -> int | None:
    return _normalize_int(
        _coalesce_alias_value(
            source,
            metadata,
            raw_payload,
            ("height", "image_height", "pixel_height"),
        )
    )


def _infer_aspect_ratio(
    *,
    source: Any,
    metadata: Mapping[str, Any],
    raw_payload: Mapping[str, Any],
    width: int | None,
    height: int | None,
) -> float | None:
    explicit = _normalize_float(
        _coalesce_alias_value(
            source,
            metadata,
            raw_payload,
            ("aspect_ratio", "image_aspect_ratio"),
        )
    )
    if explicit is not None:
        return round(explicit, 4)
    if width and height:
        return round(width / height, 4)
    return None


def _infer_caption_signal(caption_text: str, *, asset_text: str, asset_type: str | None) -> str | None:
    if asset_type == "gif":
        return "motion_only"
    if not caption_text:
        if asset_type in {"graphic", "scoreboard"}:
            return "graphic_text"
        return None
    lowered = caption_text.lower()
    if _contains_keyword(lowered, ("전광판", "scoreboard", "라인업", "순위표", "기록표", "box score")):
        return "scoreboard_text"
    if _contains_keyword(lowered, ("인터뷰", "코멘트", "quote", "멘트", "소감")):
        return "quote_text"
    if _contains_keyword(asset_text, ("자막", "캡션", "오버레이", "텍스트")):
        return "overlay_text"
    return "caption_present"


def _infer_person_count_bucket(
    *,
    source: Any,
    metadata: Mapping[str, Any],
    raw_payload: Mapping[str, Any],
    asset_text: str,
    asset_type: str | None,
) -> str | None:
    explicit_count = _normalize_int(
        _coalesce_alias_value(
            source,
            metadata,
            raw_payload,
            ("person_count", "people_count", "detected_person_count", "face_count"),
        )
    )
    if explicit_count is not None:
        if explicit_count <= 0:
            return "0"
        if explicit_count == 1:
            return "1"
        if explicit_count <= 3:
            return "2_to_3"
        return "4_plus"

    if asset_type in {"graphic", "scoreboard"}:
        return "0"
    if _contains_keyword(asset_text, ("전광판", "scoreboard", "라인업", "기록표", "box score")):
        return "0"
    if _contains_keyword(asset_text, ("관중", "팬들", "응원단", "더그아웃", "단체", "팀 세리머니", "crowd", "team group")):
        return "4_plus"
    if _contains_keyword(asset_text, ("배터리", "듀오", "투타", "2명", "세 명", "three players")):
        return "2_to_3"
    if _contains_keyword(asset_text, ("단독", "클로즈업", "closeup", "초상", "원샷")):
        return "1"
    return None


def _infer_subject_role(
    asset_text: str,
    *,
    asset_type: str | None,
    person_count_bucket: str | None,
) -> str | None:
    if asset_type in {"graphic", "scoreboard"}:
        return "unknown"
    if _contains_keyword(asset_text, ("전광판", "scoreboard", "라인업", "기록표", "box score")):
        return "unknown"
    if _contains_keyword(asset_text, ("마스코트", "mascot")):
        return "mascot"
    if _contains_keyword(asset_text, ("감독", "사령탑", "코치", "coach", "manager")):
        return "coach"
    if _contains_keyword(asset_text, ("관중", "팬", "응원석", "crowd")):
        return "crowd"
    if person_count_bucket == "4_plus" and _contains_keyword(asset_text, ("선수단", "팀", "더그아웃", "세리머니", "하이파이브")):
        return "team_group"
    if _contains_keyword(asset_text, ("투수", "선발", "마운드", "불펜", "투구", "pitcher", "세이브", "삼진")):
        return "pitcher"
    if _contains_keyword(asset_text, ("타자", "타석", "스윙", "홈런", "안타", "배트", "batter", "타격")):
        return "batter"
    if _contains_keyword(asset_text, ("포수", "내야수", "외야수", "수비", "송구", "포구", "fielder", "호수비")):
        return "fielder"
    if person_count_bucket in {"2_to_3", "4_plus"}:
        return "team_group"
    return None


def _infer_shot_type(
    asset_text: str,
    *,
    asset_type: str | None,
    aspect_ratio: float | None,
    person_count_bucket: str | None,
    subject_role: str | None,
) -> str | None:
    if _contains_keyword(asset_text, ("전광판", "scoreboard", "라인업", "기록표", "box score")):
        return "scoreboard"
    if asset_type == "graphic" or _contains_keyword(asset_text, ("그래픽", "graphic", "합성", "썸네일")):
        return "graphic"
    if _contains_keyword(asset_text, ("혼합", "콜라주", "여러 장면", "split", "mixed")):
        return "mixed"
    if _contains_keyword(asset_text, ("클로즈업", "closeup", "얼굴", "표정", "헤드샷", "상반신")):
        return "closeup"
    if person_count_bucket == "4_plus" or subject_role in {"team_group", "crowd"}:
        return "wide"
    if aspect_ratio is not None and aspect_ratio >= 1.6:
        return "wide"
    if aspect_ratio is not None and aspect_ratio <= 0.85 and person_count_bucket == "1":
        return "closeup"
    if person_count_bucket in {"1", "2_to_3"}:
        return "midshot"
    return None


def _infer_is_action_shot(
    asset_text: str,
    *,
    shot_type: str | None,
    subject_role: str | None,
) -> bool | None:
    if shot_type in {"graphic", "scoreboard"}:
        return False
    if _contains_keyword(
        asset_text,
        (
            "투구",
            "타격",
            "스윙",
            "주루",
            "슬라이딩",
            "송구",
            "포구",
            "점프",
            "다이빙",
            "호수비",
            "역투",
            "pitch",
            "swing",
            "throw",
            "catch",
            "running",
        ),
    ):
        return True
    if _contains_keyword(asset_text, ("인터뷰", "기자회견", "포즈", "브리핑", "수훈선수", "시상식")):
        return False
    if subject_role in {"pitcher", "batter", "fielder"} and _contains_keyword(asset_text, ("경기 장면", "플레이")):
        return True
    return None


def _infer_is_post_game(
    asset_text: str,
    *,
    shot_type: str | None,
    is_action_shot: bool | None,
) -> bool | None:
    if _contains_keyword(asset_text, ("경기 후", "postgame", "인터뷰", "기자회견", "수훈선수", "승장", "패장", "브리핑")):
        return True
    if shot_type in {"scoreboard", "graphic"}:
        return False
    if is_action_shot is True:
        return False
    return None


def _contains_keyword(text: str, keywords: tuple[str, ...]) -> bool:
    return any(keyword.lower() in text for keyword in keywords)
