from __future__ import annotations

import json
from typing import Any

JSON_EMPTY_LIST = "[]"
JSON_EMPTY_OBJECT = "{}"


def serialize_json_text(value: Any) -> str | None:
    if value is None:
        return None
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def deserialize_json_text(
    raw: str | None,
    *,
    default: Any = None,
) -> Any:
    if raw is None:
        return default
    text = str(raw).strip()
    if not text:
        return default
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return default


def serialize_string_list(value: list[Any] | tuple[Any, ...] | None) -> str:
    if not value:
        return JSON_EMPTY_LIST
    normalized = [str(item).strip() for item in value if str(item).strip()]
    return serialize_json_text(normalized) or JSON_EMPTY_LIST


def deserialize_string_list(raw: str | None) -> list[str]:
    parsed = deserialize_json_text(raw, default=[])
    if not isinstance(parsed, list):
        return []
    normalized: list[str] = []
    for item in parsed:
        text = str(item).strip()
        if text:
            normalized.append(text)
    return normalized


def serialize_bool_flag(value: bool | None) -> int | None:
    if value is None:
        return None
    return 1 if value else 0


def deserialize_bool_flag(value: Any) -> bool | None:
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


def serialize_plain_text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def serialize_record(
    record: dict[str, Any],
    *,
    json_fields: set[str] | None = None,
    bool_fields: set[str] | None = None,
    plain_text_fields: set[str] | None = None,
) -> dict[str, Any]:
    json_fields = json_fields or set()
    bool_fields = bool_fields or set()
    plain_text_fields = plain_text_fields or set()

    serialized: dict[str, Any] = {}
    for key, value in record.items():
        if key in json_fields:
            if isinstance(value, (list, tuple)):
                serialized[key] = serialize_string_list(value)
            else:
                serialized[key] = serialize_json_text(value)
        elif key in bool_fields:
            serialized[key] = serialize_bool_flag(value if isinstance(value, bool) or value is None else bool(value))
        elif key in plain_text_fields:
            serialized[key] = serialize_plain_text(value)
        else:
            serialized[key] = value
    return serialized


def deserialize_record(
    record: dict[str, Any],
    *,
    json_fields: set[str] | None = None,
    bool_fields: set[str] | None = None,
) -> dict[str, Any]:
    json_fields = json_fields or set()
    bool_fields = bool_fields or set()

    deserialized: dict[str, Any] = {}
    for key, value in record.items():
        if key in json_fields:
            default: Any = None
            if value == JSON_EMPTY_LIST:
                default = []
            elif value == JSON_EMPTY_OBJECT:
                default = {}
            deserialized[key] = deserialize_json_text(value, default=default)
        elif key in bool_fields:
            deserialized[key] = deserialize_bool_flag(value)
        else:
            deserialized[key] = value
    return deserialized
