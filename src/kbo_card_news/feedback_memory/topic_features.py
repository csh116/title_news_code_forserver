from __future__ import annotations

import re
from dataclasses import asdict, dataclass, is_dataclass
from typing import Any, Mapping

TOPIC_FEATURE_FIELD_NAMES = (
    "topic_type",
    "entity_focus",
    "event_type",
    "angle_type",
    "article_count",
    "asset_count",
    "has_notable_numbers",
    "recommended_focus",
)

TOPIC_FEATURE_TEXT_FIELDS = {
    "topic_type",
    "entity_focus",
    "event_type",
    "angle_type",
    "recommended_focus",
}

TOPIC_FEATURE_INT_FIELDS = {
    "article_count",
    "asset_count",
}

TOPIC_FEATURE_BOOL_FIELDS = {
    "has_notable_numbers",
}


@dataclass(slots=True)
class TopicFeatures:
    topic_type: str | None = None
    entity_focus: str | None = None
    event_type: str | None = None
    angle_type: str | None = None
    article_count: int | None = None
    asset_count: int | None = None
    has_notable_numbers: bool | None = None
    recommended_focus: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def empty_topic_features() -> dict[str, Any]:
    return TopicFeatures().to_dict()


def extract_topic_features(
    source: Any = None,
    *,
    overrides: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    values: dict[str, Any] = empty_topic_features()
    metadata = _coerce_mapping(_read_value(source, "metadata"))
    raw_payload = _coerce_mapping(_read_value(source, "raw_payload"))
    normalized_overrides = dict(overrides or {})

    for field_name in TOPIC_FEATURE_FIELD_NAMES:
        raw_value = _coalesce_field_value(
            field_name,
            overrides=normalized_overrides,
            source=source,
            metadata=metadata,
            raw_payload=raw_payload,
        )
        values[field_name] = _normalize_topic_feature_value(field_name, raw_value)
    if values["topic_type"] is None:
        values["topic_type"] = _infer_topic_type(
            source=source,
            metadata=metadata,
            raw_payload=raw_payload,
        )
    topic_text = _build_topic_text(source=source, metadata=metadata, raw_payload=raw_payload)
    if values["entity_focus"] is None:
        values["entity_focus"] = _infer_entity_focus(topic_text, topic_type=values["topic_type"])
    if values["event_type"] is None:
        values["event_type"] = _infer_event_type(topic_text, topic_type=values["topic_type"])
    if values["angle_type"] is None:
        values["angle_type"] = _infer_angle_type(
            topic_text,
            topic_type=values["topic_type"],
            event_type=values["event_type"],
        )
    if values["article_count"] is None:
        values["article_count"] = _infer_article_count(source=source, metadata=metadata, raw_payload=raw_payload)
    if values["asset_count"] is None:
        values["asset_count"] = _infer_asset_count(source=source, metadata=metadata, raw_payload=raw_payload)
    if values["has_notable_numbers"] is None:
        values["has_notable_numbers"] = _infer_has_notable_numbers(
            source=source,
            metadata=metadata,
            raw_payload=raw_payload,
            topic_text=topic_text,
        )
    if values["recommended_focus"] is None:
        values["recommended_focus"] = _infer_recommended_focus(
            topic_text,
            topic_type=values["topic_type"],
            event_type=values["event_type"],
            angle_type=values["angle_type"],
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


def _normalize_topic_feature_value(field_name: str, value: Any) -> Any:
    if field_name in TOPIC_FEATURE_TEXT_FIELDS:
        return _normalize_text(value)
    if field_name in TOPIC_FEATURE_INT_FIELDS:
        return _normalize_int(value)
    if field_name in TOPIC_FEATURE_BOOL_FIELDS:
        return _normalize_bool(value)
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


def _infer_topic_type(
    *,
    source: Any,
    metadata: Mapping[str, Any],
    raw_payload: Mapping[str, Any],
) -> str | None:
    source_type = _normalize_text(_coalesce_text_value(source, metadata, raw_payload, "source_type"))
    issue_category = _normalize_text(_coalesce_text_value(source, metadata, raw_payload, "issue_category"))
    article_kind = _normalize_text(_coalesce_text_value(source, metadata, raw_payload, "article_kind"))
    text = _build_topic_text(source=source, metadata=metadata, raw_payload=raw_payload)

    if not text and not source_type and not issue_category and not article_kind:
        return None

    if article_kind == "probable_starters":
        return "preview_or_schedule"
    if article_kind in {"standings", "results_summary"}:
        return "record"
    if issue_category == "game":
        return "game_result"

    if _contains_keyword(
        text,
        (
            "프리뷰",
            "preview",
            "선발투수 예고",
            "선발 예고",
            "예고",
            "맞대결",
            "일정",
            "schedule",
        ),
    ):
        return "preview_or_schedule"
    if _contains_keyword(
        text,
        (
            "부상",
            "injury",
            "재활",
            "통증",
            "결장",
            "이탈",
            "복귀전 무산",
        ),
    ):
        return "injury"
    if _contains_keyword(
        text,
        (
            "트레이드",
            "trade",
            "엔트리",
            "1군 등록",
            "1군 말소",
            "등록",
            "말소",
            "콜업",
            "승격",
            "강등",
            "방출",
            "영입",
            "합류",
            "로스터",
        ),
    ):
        return "trade_or_roster"
    if _contains_keyword(
        text,
        (
            "논란",
            "controversy",
            "징계",
            "오심",
            "비판",
            "충돌",
            "벤치클리어링",
            "파문",
        ),
    ):
        return "controversy"
    if _looks_like_record_topic(text):
        return "record"
    if _looks_like_game_result_topic(text):
        return "game_result"
    if _looks_like_player_highlight_topic(text):
        return "player_highlight"
    if source_type or issue_category or text:
        return "general_news"
    return None


def _build_topic_text(
    *,
    source: Any,
    metadata: Mapping[str, Any],
    raw_payload: Mapping[str, Any],
) -> str:
    parts = [
        _coalesce_text_value(source, metadata, raw_payload, "topic_name"),
        _coalesce_text_value(source, metadata, raw_payload, "title"),
        _coalesce_text_value(source, metadata, raw_payload, "draft_title"),
        _coalesce_text_value(source, metadata, raw_payload, "draft_subtitle"),
        _coalesce_text_value(source, metadata, raw_payload, "summary"),
        _coalesce_text_value(source, metadata, raw_payload, "overall_summary"),
        _coalesce_text_value(source, metadata, raw_payload, "angle_summary"),
        _coalesce_text_value(source, metadata, raw_payload, "reason_summary"),
        _coalesce_text_value(source, metadata, raw_payload, "cover_headline"),
        _coalesce_text_value(source, metadata, raw_payload, "cover_body"),
        _coalesce_text_value(source, metadata, raw_payload, "recommended_focus"),
    ]
    compact = " ".join(part for part in parts if part)
    return compact.lower().strip()


def _coalesce_text_value(
    source: Any,
    metadata: Mapping[str, Any],
    raw_payload: Mapping[str, Any],
    field_name: str,
) -> str:
    value = _read_value(source, field_name)
    if value is None and field_name in metadata:
        value = metadata.get(field_name)
    if value is None and field_name in raw_payload:
        value = raw_payload.get(field_name)
    return _normalize_text(value) or ""


def _contains_keyword(text: str, keywords: tuple[str, ...]) -> bool:
    return any(keyword.lower() in text for keyword in keywords)


def _looks_like_record_topic(text: str) -> bool:
    if not text:
        return False
    record_keywords = (
        "신기록",
        "기록",
        "최초",
        "최다",
        "통산",
        "역대",
    )
    if _contains_keyword(text, record_keywords):
        return True
    if re.search(r"\d+\s*(호|번째|승|세이브|탈삼진)", text):
        return True
    return False


def _looks_like_game_result_topic(text: str) -> bool:
    if not text:
        return False
    result_keywords = (
        "승리",
        "패배",
        "완승",
        "역전승",
        "끝내기",
        "연승",
        "연패",
        "스윕",
        "전적",
        "결과",
        "대파",
        "제압",
    )
    if _contains_keyword(text, result_keywords):
        return True
    if re.search(r"\d+\s*-\s*\d+", text):
        return True
    return False


def _looks_like_player_highlight_topic(text: str) -> bool:
    if not text:
        return False
    player_keywords = (
        "홈런",
        "멀티히트",
        "호투",
        "완투",
        "세이브",
        "맹타",
        "결승타",
        "타점",
        "삼진쇼",
        "에이스",
        "mvp",
    )
    return _contains_keyword(text, player_keywords)


def _infer_article_count(
    *,
    source: Any,
    metadata: Mapping[str, Any],
    raw_payload: Mapping[str, Any],
) -> int | None:
    for field_name in ("article_count", "source_article_count"):
        count = _normalize_int(_coalesce_numeric_value(source, metadata, raw_payload, field_name))
        if count is not None:
            return count
    for field_name in ("articles", "source_articles"):
        items = _coalesce_sequence_value(source, metadata, raw_payload, field_name)
        if items is not None:
            return len(items)
    return None


def _infer_asset_count(
    *,
    source: Any,
    metadata: Mapping[str, Any],
    raw_payload: Mapping[str, Any],
) -> int | None:
    for field_name in ("asset_count", "source_asset_count"):
        count = _normalize_int(_coalesce_numeric_value(source, metadata, raw_payload, field_name))
        if count is not None:
            return count
    for field_name in ("assets", "source_assets", "candidates"):
        items = _coalesce_sequence_value(source, metadata, raw_payload, field_name)
        if items is not None:
            return len(items)
    return None


def _infer_has_notable_numbers(
    *,
    source: Any,
    metadata: Mapping[str, Any],
    raw_payload: Mapping[str, Any],
    topic_text: str,
) -> bool | None:
    explicit = _normalize_bool(_coalesce_numeric_value(source, metadata, raw_payload, "has_notable_numbers"))
    if explicit is not None:
        return explicit

    notable_numbers = _coalesce_sequence_value(source, metadata, raw_payload, "notable_numbers")
    if notable_numbers is not None:
        return len(notable_numbers) > 0

    if topic_text:
        return bool(re.search(r"\d", topic_text))
    return None


def _infer_recommended_focus(
    topic_text: str,
    *,
    topic_type: str | None,
    event_type: str | None,
    angle_type: str | None,
) -> str | None:
    if not topic_text:
        return None
    if event_type == "walkoff":
        return "끝내기 장면과 승부처 중심"
    if event_type == "home_run":
        return "핵심 홈런 장면과 임팩트 중심"
    if event_type == "pitching":
        return "투구 내용과 흐름 제어 중심"
    if event_type == "injury":
        return "부상 상태와 이탈 영향 중심"
    if event_type == "return":
        return "복귀 배경과 반등 포인트 중심"
    if event_type == "ranking":
        return "순위 변화와 경쟁 구도 중심"
    if topic_type == "preview_or_schedule":
        return "관전 포인트와 매치업 중심"
    if topic_type == "trade_or_roster":
        return "로스터 변화와 역할 영향 중심"
    if topic_type == "record":
        return "기록 의미와 달성 맥락 중심"
    if angle_type == "drama":
        return "경기 전환점과 드라마 장면 중심"
    if angle_type == "celebration":
        return "활약 장면과 분위기 상승 포인트 중심"
    if angle_type == "setback":
        return "악재 배경과 전력 영향 중심"
    return "핵심 장면과 의미 중심"


def _infer_entity_focus(text: str, *, topic_type: str | None) -> str | None:
    if not text:
        return None
    if _contains_keyword(text, ("감독", "사령탑", "coach", "감독대행")):
        return "coach"
    if _contains_keyword(text, ("kbo", "리그", "league", "올스타", "위원회")):
        return "league"
    if topic_type == "preview_or_schedule":
        return "team"
    if _looks_like_player_focus(text, topic_type=topic_type):
        return "player"
    if _looks_like_team_focus(text, topic_type=topic_type):
        return "team"
    return None


def _infer_event_type(text: str, *, topic_type: str | None) -> str | None:
    if not text:
        return None
    if _contains_keyword(text, ("끝내기", "walkoff")):
        return "walkoff"
    if _contains_keyword(text, ("부상", "재활", "통증", "이탈", "결장")):
        return "injury"
    if _contains_keyword(text, ("복귀", "컴백", "돌아온")):
        return "return"
    if _contains_keyword(text, ("홈런", "포", "아치")):
        return "home_run"
    if _contains_keyword(text, ("호투", "완투", "세이브", "삼진", "마운드", "불펜", "선발")):
        return "pitching"
    if _contains_keyword(text, ("연승", "연패")):
        return "win_streak"
    if _contains_keyword(text, ("순위", "랭킹", "1위", "2위", "중간 순위")):
        return "ranking"
    if topic_type == "preview_or_schedule":
        return "preview"
    if topic_type == "trade_or_roster":
        return "roster_move"
    return None


def _infer_angle_type(
    text: str,
    *,
    topic_type: str | None,
    event_type: str | None,
) -> str | None:
    if not text:
        return None
    if _contains_keyword(text, ("세리머니", "환호", "축하", "celebration")):
        return "celebration"
    if _contains_keyword(text, ("분석", "해설", "짚어보면", "전망")):
        return "analysis"
    if _contains_keyword(text, ("역전", "끝내기", "드라마", "접전")):
        return "drama"
    if _contains_keyword(text, ("기록", "신기록", "최초", "통산", "역대")):
        return "record_chase"
    if _contains_keyword(text, ("부상", "악재", "이탈", "충격", "패배", "연패")):
        return "setback"
    if _contains_keyword(text, ("복귀", "컴백", "반등", "부활", "돌아와")):
        return "comeback"

    if event_type == "walkoff":
        return "drama"
    if event_type in {"home_run", "win_streak"} and topic_type in {"game_result", "player_highlight"}:
        return "celebration"
    if event_type == "injury" or topic_type == "injury":
        return "setback"
    if event_type == "return":
        return "comeback"
    if topic_type == "record":
        return "record_chase"
    return None


def _looks_like_team_focus(text: str, *, topic_type: str | None) -> bool:
    team_keywords = (
        "lg",
        "kia",
        "ssg",
        "kt",
        "nc",
        "두산",
        "롯데",
        "삼성",
        "한화",
        "키움",
        "트윈스",
        "타이거즈",
        "랜더스",
        "위즈",
        "다이노스",
        "베어스",
        "자이언츠",
        "라이온즈",
        "이글스",
        "히어로즈",
        "구단",
        "팀",
    )
    if _contains_keyword(text, team_keywords):
        return True
    return topic_type in {"game_result", "preview_or_schedule"}


def _looks_like_player_focus(text: str, *, topic_type: str | None) -> bool:
    if topic_type in {"player_highlight", "record", "injury"}:
        return True
    player_keywords = (
        "투수",
        "타자",
        "포수",
        "내야수",
        "외야수",
        "에이스",
        "주전",
        "신인",
        "베테랑",
    )
    if _contains_keyword(text, player_keywords):
        return True
    if re.search(r"[가-힣]{2,4}\s*(홈런|호투|부상|복귀|세이브|맹타|완투)", text):
        return True
    return False


def _coalesce_numeric_value(
    source: Any,
    metadata: Mapping[str, Any],
    raw_payload: Mapping[str, Any],
    field_name: str,
) -> Any:
    value = _read_value(source, field_name)
    if value is None and field_name in metadata:
        value = metadata.get(field_name)
    if value is None and field_name in raw_payload:
        value = raw_payload.get(field_name)
    return value


def _coalesce_sequence_value(
    source: Any,
    metadata: Mapping[str, Any],
    raw_payload: Mapping[str, Any],
    field_name: str,
) -> list[Any] | tuple[Any, ...] | None:
    value = _coalesce_numeric_value(source, metadata, raw_payload, field_name)
    if isinstance(value, (list, tuple)):
        return value
    return None
