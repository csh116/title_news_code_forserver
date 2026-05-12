from __future__ import annotations

import json
import mimetypes
import os
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from kbo_card_news.automation.job_state import AutomationJob
from kbo_card_news.config.env import load_default_env


DISCORD_BOT_TOKEN_ENV = "DISCORD_BOT_TOKEN"
DISCORD_CHANNEL_ID_ENV = "DISCORD_CHANNEL_ID"
DISCORD_USERNAME_ENV = "DISCORD_USERNAME"
DISCORD_API_BASE_URL = "https://discord.com/api/v10"


@dataclass(slots=True)
class DiscordBotConfig:
    bot_token: str
    channel_id: str
    username: str = ""


@dataclass(slots=True)
class DiscordNotificationResult:
    ok: bool
    status_code: int | None
    message: str
    payload: dict[str, Any]


def resolve_discord_bot_config(
    *,
    bot_token: str | None = None,
    channel_id: str | None = None,
    username: str | None = None,
) -> DiscordBotConfig:
    load_default_env()
    resolved_token = (bot_token or os.getenv(DISCORD_BOT_TOKEN_ENV) or "").strip()
    resolved_channel_id = (channel_id or os.getenv(DISCORD_CHANNEL_ID_ENV) or "").strip()
    resolved_username = (username or os.getenv(DISCORD_USERNAME_ENV) or "").strip()
    if not resolved_token:
        raise RuntimeError(f"{DISCORD_BOT_TOKEN_ENV} is required to send Discord bot notifications")
    if not resolved_channel_id:
        raise RuntimeError(f"{DISCORD_CHANNEL_ID_ENV} is required to send Discord bot notifications")
    return DiscordBotConfig(
        bot_token=resolved_token,
        channel_id=resolved_channel_id,
        username=resolved_username,
    )


def build_job_message(job: AutomationJob) -> str:
    if (job.metadata or {}).get("source") == "watch_fresh_window_once":
        return build_fresh_window_decision_job_message(job)
    if (job.metadata or {}).get("source") == "watch_fresh_once":
        return build_fresh_issue_job_message(job)
    level_label = {
        "immediate": "즉시 확인",
        "watch": "지켜볼 만함",
        "digest": "묶어서 확인",
    }.get(job.notification_level, job.notification_level)
    article_lines = []
    for index, article in enumerate(job.articles[:3], start=1):
        title = _truncate(article.title, 70)
        if article.source_url:
            article_lines.append(f"{index}. {title}\n{article.source_url}")
        else:
            article_lines.append(f"{index}. {title}")
    if not article_lines:
        article_lines.append("근거 기사 정보 없음")

    return "\n".join(
        [
            f"[{level_label}]",
            _truncate(job.topic_name, 90),
            "",
            f"판단: 화제성 {_score_label(job.virality_potential_score)} · 계정핏 {_score_label(job.account_fit_score)}",
            "",
            "기사",
            *article_lines,
        ]
    )[:1700]


def build_fresh_issue_job_message(job: AutomationJob) -> str:
    metadata = job.metadata or {}
    level_label = {
        "immediate": "강한 이슈",
        "watch": "확인 후보",
        "digest": "묶어서 확인",
    }.get(job.notification_level, job.notification_level)
    reasons = metadata.get("score_reasons") if isinstance(metadata.get("score_reasons"), list) else []
    risks = metadata.get("risk_flags") if isinstance(metadata.get("risk_flags"), list) else []
    matched_keywords = metadata.get("matched_keywords") if isinstance(metadata.get("matched_keywords"), list) else []
    time_bits = []
    fresh_count = metadata.get("fresh_article_count")
    context_count = metadata.get("context_article_count")
    source_diversity = metadata.get("source_diversity")
    if fresh_count is not None:
        time_bits.append(f"신규 {fresh_count}건")
    if context_count is not None:
        time_bits.append(f"24시간 관련 {context_count}건")
    if source_diversity is not None:
        time_bits.append(f"{source_diversity}개 매체")
    if matched_keywords:
        time_bits.append("키워드 " + ", ".join(str(value) for value in matched_keywords[:4]))

    article_lines = []
    for index, article in enumerate(job.articles[:3], start=1):
        title = _truncate(article.title, 70)
        if article.source_url:
            article_lines.append(f"{index}. {title}\n{article.source_url}")
        else:
            article_lines.append(f"{index}. {title}")
    if not article_lines:
        article_lines.append("근거 기사 정보 없음")

    lines = [
        f"[{level_label}]",
        _truncate(job.topic_name, 90),
        "",
        f"점수: {job.virality_potential_score:.0f}",
    ]
    gemini_decision = metadata.get("gemini_decision")
    if gemini_decision:
        confidence = metadata.get("gemini_confidence")
        confidence_text = f" ({float(confidence):.2f})" if isinstance(confidence, (int, float)) else ""
        lines.append(f"Gemini 판단: {gemini_decision}{confidence_text}")
    if time_bits:
        lines.append("근거: " + " / ".join(time_bits))
    if reasons:
        lines.append("세부: " + " / ".join(str(value) for value in reasons[:3]))
    if risks:
        lines.append("리스크: " + " / ".join(str(value) for value in risks[:3]))
    lines.extend(["", "기사", *article_lines])
    return "\n".join(lines)[:1700]


def build_fresh_window_decision_job_message(job: AutomationJob) -> str:
    metadata = job.metadata or {}
    level_label = {
        "immediate": "강한 이슈",
        "watch": "확인 후보",
        "digest": "묶어서 확인",
    }.get(job.notification_level, job.notification_level)
    risks = metadata.get("risk_flags") if isinstance(metadata.get("risk_flags"), list) else []
    target_count = metadata.get("target_article_count", 0)
    related_count = metadata.get("related_article_count", 0)

    article_lines = []
    for index, article in enumerate(job.articles[:5], start=1):
        title = _truncate(article.title, 70)
        if article.source_url:
            article_lines.append(f"{index}. {title}\n{article.source_url}")
        else:
            article_lines.append(f"{index}. {title}")
    if not article_lines:
        article_lines.append("근거 기사 정보 없음")

    lines = [
        f"[{level_label}]",
        _truncate(job.topic_name, 90),
        "",
        f"점수: {job.virality_potential_score:.0f}",
        "판단: Gemini fresh window gate",
    ]
    if job.recommendation_summary:
        lines.append("근거: " + _truncate(job.recommendation_summary, 240))
    if risks:
        lines.append("리스크: " + " / ".join(str(value) for value in risks[:4]))
    lines.append(f"target 기사: {target_count}건")
    lines.append(f"관련 기사: {related_count}건")
    lines.extend(["", "기사", *article_lines])
    return "\n".join(lines)[:1700]


def build_render_message(
    job: AutomationJob,
    *,
    state_payload: dict[str, Any] | None = None,
    social_copy_text: str | None = None,
) -> str:
    state_payload = state_payload or {}
    title_text = str(state_payload.get("title_text") or "").strip()
    subheadline = str(state_payload.get("subheadline") or "").strip()
    photo_credit = str(state_payload.get("selected_image_source_name") or "").strip()
    lines = [
        "[렌더 완료]",
        f"job_id: {job.job_id}",
        f"주제: {job.topic_name}",
    ]
    if title_text:
        lines.append(f"타이틀: {title_text}")
    if subheadline:
        lines.extend(["", "부제:", subheadline])
    if photo_credit:
        lines.append(f"사진출처: {photo_credit}")
    if social_copy_text:
        lines.extend(["", "인스타 본문:", _truncate_multiline(social_copy_text, 1300)])
    lines.extend(
        [
            "",
            f"PNG: {job.render_png_path or '-'}",
            "",
            "수정 필요:",
            f"PYTHONPATH=src python -m kbo_card_news.automation.cli status {job.job_id} editor_ready --message \"render needs revision\"",
        ]
    )
    return "\n".join(lines)[:1900]


def build_editor_ready_message(job: AutomationJob) -> str:
    lines = [
        "[에디터 준비 완료]",
        f"job_id: {job.job_id}",
        f"주제: {job.topic_name}",
        "",
        f"Editor: {job.editor_url or '-'}",
        "",
        "렌더 저장 후 기록:",
        f"PYTHONPATH=src python -m kbo_card_news.automation.cli record-render {job.job_id} --state-path <state.json> --notify",
    ]
    return "\n".join(lines)[:1900]


def build_channel_message_payload(
    content: str,
    *,
    components: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "content": content[:2000],
        "allowed_mentions": {"parse": []},
    }
    if components:
        payload["components"] = components
    return payload


def build_job_notification_payload(job: AutomationJob) -> dict[str, Any]:
    return build_channel_message_payload(
        build_job_message(job),
        components=build_job_action_components(job),
    )


def build_job_action_components(job: AutomationJob) -> list[dict[str, Any]]:
    return [
        {
            "type": 1,
            "components": [
                {
                    "type": 2,
                    "style": 3,
                    "label": "제작",
                    "custom_id": _job_action_custom_id("produce", job.job_id),
                },
                {
                    "type": 2,
                    "style": 4,
                    "label": "폐기",
                    "custom_id": _job_action_custom_id("discard", job.job_id),
                },
            ],
        }
    ]


def build_render_notification_payload(
    job: AutomationJob,
    *,
    state_payload: dict[str, Any] | None = None,
    social_copy_text: str | None = None,
) -> dict[str, Any]:
    return build_channel_message_payload(
        build_render_message(job, state_payload=state_payload, social_copy_text=social_copy_text)
    )


def build_editor_ready_notification_payload(job: AutomationJob) -> dict[str, Any]:
    return build_channel_message_payload(build_editor_ready_message(job))


def send_channel_message(
    content: str,
    *,
    channel_id: str | None = None,
    bot_token: str | None = None,
    username: str | None = None,
    dry_run: bool = False,
    timeout_seconds: int = 20,
) -> DiscordNotificationResult:
    payload = build_channel_message_payload(content)
    return send_channel_payload(
        payload,
        channel_id=channel_id,
        bot_token=bot_token,
        username=username,
        dry_run=dry_run,
        timeout_seconds=timeout_seconds,
    )


def send_channel_payload(
    payload: dict[str, Any],
    *,
    channel_id: str | None = None,
    bot_token: str | None = None,
    username: str | None = None,
    dry_run: bool = False,
    timeout_seconds: int = 20,
) -> DiscordNotificationResult:
    if dry_run:
        return DiscordNotificationResult(
            ok=True,
            status_code=None,
            message="dry_run",
            payload=payload,
        )
    config = resolve_discord_bot_config(
        bot_token=bot_token,
        channel_id=channel_id,
        username=username,
    )
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        f"{DISCORD_API_BASE_URL}/channels/{config.channel_id}/messages",
        data=body,
        headers={
            "Authorization": f"Bot {config.bot_token}",
            "Content-Type": "application/json; charset=utf-8",
            "User-Agent": "kbo-card-news-automation/discord-bot",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
            status_code = int(response.status)
            response.read()
    except urllib.error.HTTPError as exc:
        message = _read_error_message(exc)
        return DiscordNotificationResult(
            ok=False,
            status_code=int(exc.code),
            message=message,
            payload=payload,
        )
    except Exception as exc:
        return DiscordNotificationResult(
            ok=False,
            status_code=None,
            message=str(exc),
            payload=payload,
        )
    return DiscordNotificationResult(
        ok=200 <= status_code < 300,
        status_code=status_code,
        message="sent",
        payload=payload,
    )


def send_channel_payload_with_files(
    payload: dict[str, Any],
    *,
    file_paths: list[str | Path],
    channel_id: str | None = None,
    bot_token: str | None = None,
    username: str | None = None,
    dry_run: bool = False,
    timeout_seconds: int = 60,
) -> DiscordNotificationResult:
    existing_paths = [Path(path).expanduser() for path in file_paths if Path(path).expanduser().exists()]
    if dry_run:
        dry_payload = dict(payload)
        dry_payload["attachments"] = [str(path) for path in existing_paths]
        return DiscordNotificationResult(
            ok=True,
            status_code=None,
            message="dry_run",
            payload=dry_payload,
        )
    if not existing_paths:
        return send_channel_payload(
            payload,
            channel_id=channel_id,
            bot_token=bot_token,
            username=username,
            dry_run=False,
            timeout_seconds=timeout_seconds,
        )
    config = resolve_discord_bot_config(
        bot_token=bot_token,
        channel_id=channel_id,
        username=username,
    )
    boundary = f"----kbo-card-news-{uuid.uuid4().hex}"
    body = _build_multipart_body(payload, existing_paths, boundary=boundary)
    request = urllib.request.Request(
        f"{DISCORD_API_BASE_URL}/channels/{config.channel_id}/messages",
        data=body,
        headers={
            "Authorization": f"Bot {config.bot_token}",
            "Content-Type": f"multipart/form-data; boundary={boundary}",
            "User-Agent": "kbo-card-news-automation/discord-bot",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
            status_code = int(response.status)
            response.read()
    except urllib.error.HTTPError as exc:
        message = _read_error_message(exc)
        return DiscordNotificationResult(
            ok=False,
            status_code=int(exc.code),
            message=message,
            payload=payload,
        )
    except Exception as exc:
        return DiscordNotificationResult(
            ok=False,
            status_code=None,
            message=str(exc),
            payload=payload,
        )
    return DiscordNotificationResult(
        ok=200 <= status_code < 300,
        status_code=status_code,
        message="sent",
        payload=payload,
    )


def send_job_notification(
    job: AutomationJob,
    *,
    channel_id: str | None = None,
    bot_token: str | None = None,
    username: str | None = None,
    dry_run: bool = False,
    timeout_seconds: int = 20,
) -> DiscordNotificationResult:
    return send_channel_payload(
        build_job_notification_payload(job),
        channel_id=channel_id,
        bot_token=bot_token,
        username=username,
        dry_run=dry_run,
        timeout_seconds=timeout_seconds,
    )


def send_render_notification(
    job: AutomationJob,
    *,
    state_payload: dict[str, Any] | None = None,
    social_copy_text: str | None = None,
    file_paths: list[str | Path] | None = None,
    channel_id: str | None = None,
    bot_token: str | None = None,
    username: str | None = None,
    dry_run: bool = False,
    timeout_seconds: int = 20,
) -> DiscordNotificationResult:
    payload = build_render_notification_payload(
        job,
        state_payload=state_payload,
        social_copy_text=social_copy_text,
    )
    if file_paths:
        return send_channel_payload_with_files(
            payload,
            file_paths=file_paths,
            channel_id=channel_id,
            bot_token=bot_token,
            username=username,
            dry_run=dry_run,
            timeout_seconds=max(timeout_seconds, 60),
        )
    return send_channel_payload(
        payload,
        channel_id=channel_id,
        bot_token=bot_token,
        username=username,
        dry_run=dry_run,
        timeout_seconds=timeout_seconds,
    )


def send_editor_ready_notification(
    job: AutomationJob,
    *,
    channel_id: str | None = None,
    bot_token: str | None = None,
    username: str | None = None,
    dry_run: bool = False,
    timeout_seconds: int = 20,
) -> DiscordNotificationResult:
    return send_channel_message(
        build_editor_ready_message(job),
        channel_id=channel_id,
        bot_token=bot_token,
        username=username,
        dry_run=dry_run,
        timeout_seconds=timeout_seconds,
    )


def _read_error_message(exc: urllib.error.HTTPError) -> str:
    try:
        raw = exc.read().decode("utf-8", errors="replace")
    except Exception:
        raw = ""
    text = " ".join(raw.split())
    if not text:
        return str(exc)
    return f"{exc.reason}: {text}"


def _truncate(value: str, max_length: int) -> str:
    text = " ".join(str(value or "").split())
    if len(text) <= max_length:
        return text
    return text[: max(0, max_length - 1)].rstrip() + "..."


def _truncate_multiline(value: str, max_length: int) -> str:
    text = str(value or "").strip()
    if len(text) <= max_length:
        return text
    return text[: max(0, max_length - 4)].rstrip() + "\n..."


def _score_label(score: float) -> str:
    value = float(score or 0.0)
    if value >= 80:
        return "높음"
    if value >= 55:
        return "보통"
    return "낮음"


def _job_action_custom_id(action: str, job_id: str) -> str:
    return f"kbo:{action}:{job_id}"[:100]


def _build_multipart_body(payload: dict[str, Any], file_paths: list[Path], *, boundary: str) -> bytes:
    chunks: list[bytes] = []

    def add_text_part(name: str, value: str, content_type: str = "text/plain; charset=utf-8") -> None:
        chunks.extend(
            [
                f"--{boundary}\r\n".encode("utf-8"),
                f'Content-Disposition: form-data; name="{name}"\r\n'.encode("utf-8"),
                f"Content-Type: {content_type}\r\n\r\n".encode("utf-8"),
                value.encode("utf-8"),
                b"\r\n",
            ]
        )

    add_text_part("payload_json", json.dumps(payload, ensure_ascii=False), "application/json; charset=utf-8")
    for index, path in enumerate(file_paths):
        content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        chunks.extend(
            [
                f"--{boundary}\r\n".encode("utf-8"),
                (
                    f'Content-Disposition: form-data; name="files[{index}]"; '
                    f'filename="{path.name}"\r\n'
                ).encode("utf-8"),
                f"Content-Type: {content_type}\r\n\r\n".encode("utf-8"),
                path.read_bytes(),
                b"\r\n",
            ]
        )
    chunks.append(f"--{boundary}--\r\n".encode("utf-8"))
    return b"".join(chunks)
