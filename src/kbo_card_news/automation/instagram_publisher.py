from __future__ import annotations

import json
import os
import re
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from kbo_card_news.automation.job_state import AutomationJob, utc_now
from kbo_card_news.config.env import load_default_env

INSTAGRAM_ACCESS_TOKEN_ENV = "INSTAGRAM_ACCESS_TOKEN"
INSTAGRAM_IG_USER_ID_ENV = "INSTAGRAM_IG_USER_ID"
INSTAGRAM_GRAPH_API_VERSION_ENV = "INSTAGRAM_GRAPH_API_VERSION"
DEFAULT_GRAPH_API_VERSION = "v24.0"
GRAPH_API_BASE_URL = "https://graph.facebook.com"
CAPTION_MAX_LENGTH = 2200
CAPTION_MAX_HASHTAGS = 30
CAPTION_MAX_MENTIONS = 20


@dataclass(slots=True)
class InstagramPublishConfig:
    ig_user_id: str
    access_token: str
    graph_api_version: str = DEFAULT_GRAPH_API_VERSION


@dataclass(slots=True)
class InstagramPublishPlan:
    job_id: str
    image_url: str
    caption: str
    alt_text: str | None
    graph_api_version: str
    caption_length: int
    hashtag_count: int
    mention_count: int


@dataclass(slots=True)
class InstagramPublishResult:
    ok: bool
    message: str
    plan: InstagramPublishPlan
    container_id: str | None = None
    media_id: str | None = None
    permalink: str | None = None
    response_payload: dict[str, Any] | None = None


def resolve_instagram_config(
    *,
    ig_user_id: str | None = None,
    access_token: str | None = None,
    graph_api_version: str | None = None,
) -> InstagramPublishConfig:
    load_default_env()
    resolved_ig_user_id = (ig_user_id or os.getenv(INSTAGRAM_IG_USER_ID_ENV) or "").strip()
    resolved_access_token = (access_token or os.getenv(INSTAGRAM_ACCESS_TOKEN_ENV) or "").strip()
    resolved_version = (
        graph_api_version
        or os.getenv(INSTAGRAM_GRAPH_API_VERSION_ENV)
        or DEFAULT_GRAPH_API_VERSION
    ).strip()
    if not resolved_ig_user_id:
        raise RuntimeError(f"{INSTAGRAM_IG_USER_ID_ENV} is required")
    if not resolved_access_token:
        raise RuntimeError(f"{INSTAGRAM_ACCESS_TOKEN_ENV} is required")
    return InstagramPublishConfig(
        ig_user_id=resolved_ig_user_id,
        access_token=resolved_access_token,
        graph_api_version=resolved_version,
    )


def build_instagram_publish_plan(
    job: AutomationJob,
    *,
    image_url: str,
    caption: str | None = None,
    caption_path: str | Path | None = None,
    alt_text: str | None = None,
    graph_api_version: str = DEFAULT_GRAPH_API_VERSION,
) -> InstagramPublishPlan:
    resolved_image_url = str(image_url or "").strip()
    if not _is_public_http_url(resolved_image_url):
        raise ValueError("Instagram image publish requires a public http(s) image_url")
    resolved_caption = _resolve_caption(job, caption=caption, caption_path=caption_path)
    _validate_caption(resolved_caption)
    return InstagramPublishPlan(
        job_id=job.job_id,
        image_url=resolved_image_url,
        caption=resolved_caption,
        alt_text=_clean_optional_text(alt_text),
        graph_api_version=graph_api_version,
        caption_length=len(resolved_caption),
        hashtag_count=_count_hashtags(resolved_caption),
        mention_count=_count_mentions(resolved_caption),
    )


def publish_instagram_image(
    job: AutomationJob,
    *,
    image_url: str,
    caption: str | None = None,
    caption_path: str | Path | None = None,
    alt_text: str | None = None,
    ig_user_id: str | None = None,
    access_token: str | None = None,
    graph_api_version: str | None = None,
    dry_run: bool = False,
    timeout_seconds: int = 30,
) -> InstagramPublishResult:
    version = (graph_api_version or os.getenv(INSTAGRAM_GRAPH_API_VERSION_ENV) or DEFAULT_GRAPH_API_VERSION).strip()
    plan = build_instagram_publish_plan(
        job,
        image_url=image_url,
        caption=caption,
        caption_path=caption_path,
        alt_text=alt_text,
        graph_api_version=version,
    )
    if dry_run:
        return InstagramPublishResult(ok=True, message="dry_run", plan=plan)

    config = resolve_instagram_config(
        ig_user_id=ig_user_id,
        access_token=access_token,
        graph_api_version=version,
    )
    container_payload = _post_graph_api(
        f"{config.ig_user_id}/media",
        config=config,
        params=_container_params(plan, config.access_token),
        timeout_seconds=timeout_seconds,
    )
    container_id = str(container_payload.get("id") or "").strip()
    if not container_id:
        return InstagramPublishResult(
            ok=False,
            message="Instagram did not return a container id",
            plan=plan,
            response_payload=container_payload,
        )

    publish_payload = _post_graph_api(
        f"{config.ig_user_id}/media_publish",
        config=config,
        params={"creation_id": container_id, "access_token": config.access_token},
        timeout_seconds=timeout_seconds,
    )
    media_id = str(publish_payload.get("id") or "").strip()
    permalink = _fetch_permalink(
        media_id,
        config=config,
        timeout_seconds=timeout_seconds,
    ) if media_id else None
    return InstagramPublishResult(
        ok=bool(media_id),
        message="published" if media_id else "Instagram did not return a media id",
        plan=plan,
        container_id=container_id,
        media_id=media_id or None,
        permalink=permalink,
        response_payload={
            "container": container_payload,
            "publish": publish_payload,
            "fetched_at": utc_now().isoformat(),
        },
    )


def publish_result_to_metadata(result: InstagramPublishResult) -> dict[str, Any]:
    return {
        "instagram_container_id": result.container_id,
        "instagram_media_id": result.media_id,
        "instagram_permalink": result.permalink,
        "instagram_published_at": utc_now().isoformat() if result.ok else None,
        "instagram_publish_message": result.message,
        "instagram_graph_api_version": result.plan.graph_api_version,
        "instagram_image_url": result.plan.image_url,
        "instagram_caption_length": result.plan.caption_length,
        "instagram_hashtag_count": result.plan.hashtag_count,
        "instagram_mention_count": result.plan.mention_count,
    }


def extract_caption_for_topic(markdown: str, topic_id: str) -> str | None:
    escaped = re.escape(topic_id)
    pattern = re.compile(
        rf"<!--\s*topic:{escaped}:start\s*-->\s*(.*?)\s*<!--\s*topic:{escaped}:end\s*-->",
        re.DOTALL,
    )
    match = pattern.search(markdown)
    if not match:
        return None
    block = match.group(1).strip()
    lines = block.splitlines()
    if lines and lines[0].lstrip().startswith("## "):
        lines = lines[1:]
    return "\n".join(lines).strip()


def _resolve_caption(
    job: AutomationJob,
    *,
    caption: str | None,
    caption_path: str | Path | None,
) -> str:
    if caption is not None:
        return caption.strip()
    raw_path = str(caption_path or job.social_copy_md_path or "").strip()
    if not raw_path:
        raise ValueError("caption or caption_path is required")
    path = Path(raw_path).expanduser()
    if not path.exists():
        raise ValueError(f"caption file not found: {path}")
    markdown = path.read_text(encoding="utf-8")
    extracted = extract_caption_for_topic(markdown, job.topic_id)
    if extracted:
        return extracted
    return markdown.strip()


def _validate_caption(caption: str) -> None:
    if not caption.strip():
        raise ValueError("caption is empty")
    if len(caption) > CAPTION_MAX_LENGTH:
        raise ValueError(f"caption is too long: {len(caption)} > {CAPTION_MAX_LENGTH}")
    hashtag_count = _count_hashtags(caption)
    if hashtag_count > CAPTION_MAX_HASHTAGS:
        raise ValueError(f"caption has too many hashtags: {hashtag_count} > {CAPTION_MAX_HASHTAGS}")
    mention_count = _count_mentions(caption)
    if mention_count > CAPTION_MAX_MENTIONS:
        raise ValueError(f"caption has too many mentions: {mention_count} > {CAPTION_MAX_MENTIONS}")


def _post_graph_api(
    path: str,
    *,
    config: InstagramPublishConfig,
    params: dict[str, str],
    timeout_seconds: int,
) -> dict[str, Any]:
    url = f"{GRAPH_API_BASE_URL}/{config.graph_api_version}/{path.strip('/')}"
    body = urllib.parse.urlencode(params).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=body,
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "User-Agent": "kbo-card-news-automation/phase10",
        },
        method="POST",
    )
    return _read_json_response(request, timeout_seconds=timeout_seconds)


def _fetch_permalink(
    media_id: str,
    *,
    config: InstagramPublishConfig,
    timeout_seconds: int,
) -> str | None:
    query = urllib.parse.urlencode(
        {
            "fields": "permalink",
            "access_token": config.access_token,
        }
    )
    request = urllib.request.Request(
        f"{GRAPH_API_BASE_URL}/{config.graph_api_version}/{media_id}?{query}",
        headers={"User-Agent": "kbo-card-news-automation/phase10"},
        method="GET",
    )
    payload = _read_json_response(request, timeout_seconds=timeout_seconds)
    value = str(payload.get("permalink") or "").strip()
    return value or None


def _read_json_response(request: urllib.request.Request, *, timeout_seconds: int) -> dict[str, Any]:
    try:
        with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
            raw = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        raw_error = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Instagram API HTTP {exc.code}: {raw_error}") from exc
    payload = json.loads(raw or "{}")
    if not isinstance(payload, dict):
        raise RuntimeError(f"Instagram API returned non-object JSON: {payload!r}")
    if "error" in payload:
        raise RuntimeError(f"Instagram API error: {payload['error']}")
    return payload


def _container_params(plan: InstagramPublishPlan, access_token: str) -> dict[str, str]:
    params = {
        "image_url": plan.image_url,
        "caption": plan.caption,
        "access_token": access_token,
    }
    if plan.alt_text:
        params["alt_text"] = plan.alt_text
    return params


def _is_public_http_url(value: str) -> bool:
    parsed = urllib.parse.urlparse(value)
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


def _count_hashtags(value: str) -> int:
    return len(re.findall(r"(?<!\w)#[^\s#@]+", value))


def _count_mentions(value: str) -> int:
    return len(re.findall(r"(?<!\w)@[A-Za-z0-9_.]+", value))


def _clean_optional_text(value: str | None) -> str | None:
    text = str(value or "").strip()
    return text or None
