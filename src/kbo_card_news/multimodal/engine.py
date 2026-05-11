from __future__ import annotations

import base64
import http.client
import http.cookiejar
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import deque
from dataclasses import asdict
from datetime import datetime, timezone
from typing import Protocol

from kbo_card_news.config.env import load_default_env
from kbo_card_news.feedback_memory import (
    FeedbackMemoryRepository,
    apply_multimodal_policies,
    build_asset_fingerprint,
    build_topic_fingerprint,
    extract_asset_features,
    extract_topic_features,
    format_multimodal_retrieval_summary,
    retrieve_similar_multimodal_edits,
)
from kbo_card_news.models.issue import (
    AssetMultimodalInsight,
    IssueMultimodalAnalysis,
    IssueMultimodalAnalysisInput,
)
from kbo_card_news.multimodal.prompts import (
    MULTIMODAL_SYSTEM_PROMPT,
    MULTIMODAL_TAG_DICTIONARY,
    build_multimodal_user_prompt,
)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


class MultimodalAnalysisEngine(Protocol):
    def analyze(self, analysis_input: IssueMultimodalAnalysisInput) -> IssueMultimodalAnalysis:
        ...


class HttpTransport(Protocol):
    def post_json(self, url: str, payload: dict, headers: dict[str, str]) -> dict:
        ...


class UrllibHttpTransport:
    def post_json(self, url: str, payload: dict, headers: dict[str, str]) -> dict:
        request = urllib.request.Request(
            url=url,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=180) as response:
            body = response.read().decode("utf-8")
        return json.loads(body)


class HeuristicMultimodalAnalysisEngine:
    def __init__(
        self,
        *,
        model_name: str = "heuristic-multimodal-v1",
        prompt_version: str = "phase3.3",
    ) -> None:
        self.model_name = model_name
        self.prompt_version = prompt_version

    def analyze(self, analysis_input: IssueMultimodalAnalysisInput) -> IssueMultimodalAnalysis:
        candidate = analysis_input.candidate
        insights: list[AssetMultimodalInsight] = []
        for asset in sorted(analysis_input.assets, key=lambda item: item.sort_order):
            reference = asset.asset_id or asset.origin_url
            text_hint = _primary_text_hint(asset, candidate.summary)
            usage_recommendation = _build_usage_recommendation(asset, candidate)
            caution_note = _build_caution_note(asset, candidate)
            confidence = _estimate_confidence(asset)
            subject_tags = _build_subject_tags(asset, candidate)
            event_tags = _build_event_tags(asset, candidate)
            emotion_tags = _build_emotion_tags(asset, candidate)
            composition_tags = _build_composition_tags(asset, candidate, usage_recommendation)
            risk_tags = _build_risk_tags(asset, candidate)
            tag_summary = _build_tag_summary(
                subject_tags=subject_tags,
                event_tags=event_tags,
                emotion_tags=emotion_tags,
                composition_tags=composition_tags,
                fallback=text_hint,
            )
            scene_description = _build_scene_description(tag_summary)
            humor_point = _build_humor_point(
                usage_recommendation=usage_recommendation,
                event_tags=event_tags,
                emotion_tags=emotion_tags,
            )
            insights.append(
                AssetMultimodalInsight(
                    asset_reference=reference,
                    asset_type=asset.asset_type,
                    scene_description=scene_description,
                    humor_point=humor_point,
                    usage_recommendation=usage_recommendation,
                    subject_tags=subject_tags,
                    event_tags=event_tags,
                    emotion_tags=emotion_tags,
                    composition_tags=composition_tags,
                    risk_tags=risk_tags,
                    tag_summary=tag_summary,
                    caution_note=caution_note,
                    confidence=confidence,
                    analysis_payload={
                        "text_hint": text_hint,
                        "source_type": candidate.source_type,
                    },
                )
            )

        overall_summary = _build_overall_summary(analysis_input, insights)
        return IssueMultimodalAnalysis(
            issue_id=candidate.issue_id,
            model_name=self.model_name,
            prompt_version=self.prompt_version,
            overall_summary=overall_summary,
            assets=insights,
            metadata={
                "source_type": candidate.source_type,
                "asset_count": len(analysis_input.assets),
                "analysis_mode": "metadata-grounded",
            },
            created_at=_utc_now(),
        )


class GeminiMultimodalAnalysisEngine:
    def __init__(
        self,
        *,
        api_key: str | None = None,
        model_name: str = "gemini-2.5-flash-lite",
        prompt_version: str = "phase3.3",
        transport: HttpTransport | None = None,
        endpoint_base: str = "https://generativelanguage.googleapis.com/v1beta/models",
    ) -> None:
        load_default_env()
        self.api_key = api_key or os.getenv("GEMINI_API_KEY")
        self.model_name = model_name
        self.prompt_version = prompt_version
        self.transport = transport or UrllibHttpTransport()
        self.endpoint_base = endpoint_base.rstrip("/")

    def analyze(self, analysis_input: IssueMultimodalAnalysisInput) -> IssueMultimodalAnalysis:
        if not self.api_key:
            raise ValueError("GEMINI_API_KEY is required for GeminiMultimodalAnalysisEngine")

        request_payload = self._build_request_payload(analysis_input)
        url = f"{self.endpoint_base}/{self.model_name}:generateContent"
        headers = {
            "Content-Type": "application/json",
            "x-goog-api-key": self.api_key,
        }

        try:
            response_payload = self.transport.post_json(url, request_payload, headers)
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"Gemini API request failed: HTTP {exc.code} {detail}") from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(f"Gemini API request failed: {exc.reason}") from exc

        parsed = self._parse_response_payload(response_payload)
        self._validate_asset_alignment(analysis_input, parsed)
        insights = [
            _build_insight_from_response_item(item)
            for item in parsed["assets"]
        ]
        return IssueMultimodalAnalysis(
            issue_id=analysis_input.candidate.issue_id,
            model_name=self.model_name,
            prompt_version=self.prompt_version,
            overall_summary=_normalize_overall_summary(parsed.get("overall_summary"), analysis_input, insights),
            assets=insights,
            metadata={
                "engine_type": "gemini",
                "request": request_payload,
                "response": response_payload,
                "analysis_mode": "metadata-grounded",
            },
            created_at=_utc_now(),
        )

    def _build_request_payload(self, analysis_input: IssueMultimodalAnalysisInput) -> dict:
        payload = asdict(analysis_input)
        payload["candidate"]["published_at"] = _serialize_datetime(
            analysis_input.candidate.published_at
        )
        payload["candidate"]["collected_at"] = _serialize_datetime(
            analysis_input.candidate.collected_at
        )
        if analysis_input.card_news_draft and analysis_input.card_news_draft.created_at:
            payload["card_news_draft"]["created_at"] = _serialize_datetime(
                analysis_input.card_news_draft.created_at
            )
        input_json = json.dumps(payload, ensure_ascii=False, indent=2)
        expected_asset_references = ", ".join(
            asset.asset_id or asset.origin_url
            for asset in sorted(analysis_input.assets, key=lambda item: item.sort_order)
        )
        return {
            "system_instruction": {
                "parts": [{"text": MULTIMODAL_SYSTEM_PROMPT}]
            },
            "contents": [
                {
                    "role": "user",
                    "parts": [
                        {
                            "text": build_multimodal_user_prompt(
                                input_json,
                                expected_asset_references=expected_asset_references,
                                has_memory_context=bool(
                                    analysis_input.memory_context_summary
                                    or analysis_input.memory_context_by_asset
                                ),
                            )
                        }
                    ],
                }
            ],
            "generationConfig": {
                "temperature": 0.3,
                "responseMimeType": "application/json",
                "responseSchema": {
                    "type": "OBJECT",
                    "properties": {
                        "overall_summary": {"type": "STRING"},
                        "assets": {
                            "type": "ARRAY",
                            "items": {
                                "type": "OBJECT",
                                "properties": {
                                    "asset_reference": {"type": "STRING"},
                                    "asset_type": {"type": "STRING"},
                                    "scene_description": {"type": "STRING"},
                                    "humor_point": {"type": "STRING"},
                                    "usage_recommendation": {"type": "STRING"},
                                    "subject_tags": {
                                        "type": "ARRAY",
                                        "items": {"type": "STRING"},
                                    },
                                    "event_tags": {
                                        "type": "ARRAY",
                                        "items": {"type": "STRING"},
                                    },
                                    "emotion_tags": {
                                        "type": "ARRAY",
                                        "items": {"type": "STRING"},
                                    },
                                    "composition_tags": {
                                        "type": "ARRAY",
                                        "items": {"type": "STRING"},
                                    },
                                    "risk_tags": {
                                        "type": "ARRAY",
                                        "items": {"type": "STRING"},
                                    },
                                    "tag_summary": {"type": "STRING"},
                                    "caution_note": {"type": "STRING"},
                                    "confidence": {"type": "NUMBER"},
                                },
                                "required": [
                                    "asset_reference",
                                    "asset_type",
                                    "scene_description",
                                    "humor_point",
                                    "usage_recommendation",
                                    "subject_tags",
                                    "event_tags",
                                    "emotion_tags",
                                    "composition_tags",
                                    "risk_tags",
                                    "tag_summary",
                                    "caution_note",
                                    "confidence",
                                ],
                            },
                        },
                    },
                    "required": ["overall_summary", "assets"],
                },
            },
        }

    @staticmethod
    def _parse_response_payload(response_payload: dict) -> dict:
        try:
            text = response_payload["candidates"][0]["content"]["parts"][0]["text"]
        except (KeyError, IndexError, TypeError) as exc:
            raise RuntimeError(f"Unexpected Gemini response shape: {response_payload}") from exc

        try:
            parsed = json.loads(text)
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"Gemini response was not valid JSON: {text}") from exc

        required = {"overall_summary", "assets"}
        missing = sorted(required - set(parsed))
        if missing:
            raise RuntimeError(f"Gemini response missing fields: {', '.join(missing)}")
        if not isinstance(parsed["assets"], list):
            raise RuntimeError("Gemini response assets must be a list")
        return parsed

    def _validate_asset_alignment(self, analysis_input: IssueMultimodalAnalysisInput, parsed: dict) -> None:
        expected_refs = [asset.asset_id or asset.origin_url for asset in analysis_input.assets]
        actual_refs = [str(item.get("asset_reference")) for item in parsed["assets"]]
        if len(actual_refs) != len(expected_refs):
            raise RuntimeError(
                "Gemini response violated asset-count rule: "
                f"expected {len(expected_refs)} assets, got {len(actual_refs)}"
            )
        if set(actual_refs) != set(expected_refs):
            raise RuntimeError(
                "Gemini response violated asset-reference rule: "
                f"expected {expected_refs}, got {actual_refs}"
            )


class OpenAIMultimodalAnalysisEngine:
    def __init__(
        self,
        *,
        api_key: str | None = None,
        model_name: str = "gpt-4o",
        prompt_version: str = "phase3.3",
        transport: HttpTransport | None = None,
        endpoint: str = "https://api.openai.com/v1/responses",
        image_detail: str = "low",
        max_attempts: int = 3,
        retry_delay_seconds: float = 1.0,
        max_assets_per_request: int = 8,
    ) -> None:
        load_default_env()
        self.api_key = api_key or os.getenv("OPENAI_API_KEY")
        self.model_name = model_name
        self.prompt_version = prompt_version
        self.transport = transport or UrllibHttpTransport()
        self.endpoint = endpoint
        self.image_detail = image_detail
        self.max_attempts = max(1, max_attempts)
        self.retry_delay_seconds = max(0.0, retry_delay_seconds)
        self.max_assets_per_request = max(1, max_assets_per_request)

    def analyze(self, analysis_input: IssueMultimodalAnalysisInput) -> IssueMultimodalAnalysis:
        if not self.api_key:
            raise ValueError("OPENAI_API_KEY is required for OpenAIMultimodalAnalysisEngine")

        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.api_key}",
        }
        sorted_assets = sorted(analysis_input.assets, key=lambda item: item.sort_order)
        insights_by_ref: dict[str, AssetMultimodalInsight] = {}
        request_records: list[dict[str, object]] = []
        overall_summaries: list[str] = []
        asset_batches = deque(_chunk_assets(sorted_assets, self.max_assets_per_request))
        batch_index = 0

        while asset_batches:
            batch_assets = asset_batches.popleft()
            batch_index += 1
            pending_assets = list(batch_assets)
            batch_completed = False

            for attempt in range(1, self.max_attempts + 1):
                if not pending_assets:
                    batch_completed = True
                    break

                subset_input = _subset_analysis_input(analysis_input, pending_assets)
                request_payload = self._build_request_payload(subset_input)
                try:
                    response_payload = self.transport.post_json(self.endpoint, request_payload, headers)
                except urllib.error.HTTPError as exc:
                    detail = exc.read().decode("utf-8", errors="replace")
                    if _is_openai_retryable_limit_error(exc.code, detail):
                        if attempt >= self.max_attempts:
                            raise RuntimeError(f"OpenAI API request failed: HTTP {exc.code} {detail}") from exc
                        time.sleep(max(_extract_retry_after_seconds(detail), self.retry_delay_seconds * attempt, 1.0))
                        continue
                    if _is_openai_request_too_large_error(exc.code, detail) and len(pending_assets) > 1:
                        split_batches = _split_assets_in_half(pending_assets)
                        for split_batch in reversed(split_batches):
                            asset_batches.appendleft(split_batch)
                        request_records.append(
                            {
                                "batch_index": batch_index,
                                "attempt": attempt,
                                "requested_asset_references": [
                                    asset.asset_id or asset.origin_url for asset in pending_assets
                                ],
                                "response_asset_references": [],
                                "request": request_payload,
                                "response": {"split_reason": "rate_limit_exceeded", "detail": detail},
                            }
                        )
                        pending_assets = []
                        batch_completed = True
                        break
                    if attempt >= self.max_attempts:
                        raise RuntimeError(f"OpenAI API request failed: HTTP {exc.code} {detail}") from exc
                    time.sleep(self.retry_delay_seconds * attempt)
                    continue
                except urllib.error.URLError as exc:
                    if attempt >= self.max_attempts:
                        raise RuntimeError(f"OpenAI API request failed: {exc.reason}") from exc
                    time.sleep(self.retry_delay_seconds * attempt)
                    continue

                parsed = self._parse_response_payload(response_payload)
                parsed_assets = self._collect_valid_assets(subset_input, parsed)
                parsed_refs = {item["asset_reference"] for item in parsed_assets}
                if parsed_assets:
                    summary = str(parsed["overall_summary"]).strip()
                    if summary:
                        overall_summaries.append(summary)
                    for item in parsed_assets:
                        insight = _build_insight_from_response_item(item)
                        insights_by_ref[item["asset_reference"]] = insight
                    request_records.append(
                        {
                            "batch_index": batch_index,
                            "attempt": attempt,
                            "requested_asset_references": [
                                asset.asset_id or asset.origin_url for asset in pending_assets
                            ],
                            "response_asset_references": sorted(parsed_refs),
                            "request": request_payload,
                            "response": response_payload,
                        }
                    )
                    pending_assets = [
                        asset
                        for asset in pending_assets
                        if (asset.asset_id or asset.origin_url) not in parsed_refs
                    ]
                    if not pending_assets:
                        batch_completed = True
                        break

                if attempt < self.max_attempts:
                    time.sleep(self.retry_delay_seconds * attempt)

            if not batch_completed and pending_assets:
                pending_refs = [asset.asset_id or asset.origin_url for asset in pending_assets]
                raise RuntimeError(
                    "OpenAI response violated asset-coverage rule: "
                    f"missing analyses for {pending_refs}"
                )

        insights = [
            insights_by_ref[asset.asset_id or asset.origin_url]
            for asset in sorted_assets
        ]
        return IssueMultimodalAnalysis(
            issue_id=analysis_input.candidate.issue_id,
            model_name=self.model_name,
            prompt_version=self.prompt_version,
            overall_summary=_normalize_overall_summary(
                _merge_overall_summaries(analysis_input, insights, overall_summaries),
                analysis_input,
                insights,
            ),
            assets=insights,
            metadata={
                "engine_type": "openai",
                "image_detail": self.image_detail,
                "asset_count": len(sorted_assets),
                "request_count": len(request_records),
                "max_assets_per_request": self.max_assets_per_request,
                "requests": request_records,
                "analysis_mode": (
                    "vision-grounded"
                    if any(
                        any(item.get("type") == "input_image" for item in record["request"]["input"][1]["content"])
                        for record in request_records
                    )
                    else "metadata-grounded"
                ),
            },
            created_at=_utc_now(),
        )

    def _build_request_payload(self, analysis_input: IssueMultimodalAnalysisInput) -> dict:
        serialized_input = _serialize_analysis_input(analysis_input)
        expected_asset_references = ", ".join(
            asset.asset_id or asset.origin_url
            for asset in sorted(analysis_input.assets, key=lambda item: item.sort_order)
        )
        user_content: list[dict[str, object]] = [
            {
                "type": "input_text",
                "text": build_multimodal_user_prompt(
                    serialized_input,
                    expected_asset_references=expected_asset_references,
                    has_memory_context=bool(
                        analysis_input.memory_context_summary
                        or analysis_input.memory_context_by_asset
                    ),
                ),
            }
        ]

        for asset in sorted(analysis_input.assets, key=lambda item: item.sort_order):
            reference = asset.asset_id or asset.origin_url
            asset_summary = {
                "asset_reference": reference,
                "asset_type": asset.asset_type,
                "origin_url": asset.origin_url,
                "caption": asset.caption,
                "vision_caption": asset.vision_caption,
                "ocr_text": asset.ocr_text,
                "mime_type": asset.mime_type,
                "width": asset.width,
                "height": asset.height,
                "sort_order": asset.sort_order,
            }
            user_content.append(
                {
                    "type": "input_text",
                    "text": "Asset context:\n" + json.dumps(asset_summary, ensure_ascii=False, indent=2),
                }
            )
            image_input_url = self._build_openai_image_url(
                asset.origin_url,
                mime_type=asset.mime_type,
                referer=analysis_input.candidate.source_url,
            )
            if image_input_url:
                user_content.append(
                    {
                        "type": "input_image",
                        "image_url": image_input_url,
                        "detail": self.image_detail,
                    }
                )

        return {
            "model": self.model_name,
            "input": [
                {
                    "role": "system",
                    "content": [
                        {"type": "input_text", "text": MULTIMODAL_SYSTEM_PROMPT},
                    ],
                },
                {
                    "role": "user",
                    "content": user_content,
                },
            ],
            "text": {
                "format": {
                    "type": "json_schema",
                    "name": "kbo_multimodal_analysis",
                    "strict": True,
                    "schema": _multimodal_response_schema(),
                }
            },
        }

    def _build_openai_image_url(
        self,
        origin_url: str | None,
        *,
        mime_type: str | None,
        referer: str | None,
    ) -> str | None:
        if not _supports_remote_image_input(origin_url):
            return None

        download = self._download_best_effort_image(origin_url, referer=referer)
        if download is None:
            return None

        image_bytes, content_type, final_url = download
        resolved_mime_type = _resolve_image_mime_type(content_type, mime_type, final_url or origin_url)
        encoded = base64.b64encode(image_bytes).decode("ascii")
        return f"data:{resolved_mime_type};base64,{encoded}"

    def _download_best_effort_image(
        self,
        origin_url: str,
        *,
        referer: str | None,
    ) -> tuple[bytes, str | None, str] | None:
        browser_headers = _build_browser_headers(referer=referer, accept_images_only=True)
        direct = _fetch_url_bytes(origin_url, headers=browser_headers)
        if direct is not None:
            image_bytes, content_type, final_url = direct
            if _detect_supported_image_mime_type(image_bytes, content_type):
                return image_bytes, content_type, final_url

        if not referer or not _supports_remote_image_input(referer):
            return None

        opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar())
        )
        article_html = _fetch_url_bytes(
            referer,
            headers=_build_browser_headers(referer=referer, accept_html=True),
            opener=opener,
        )
        if article_html is None:
            return direct if direct and _detect_supported_image_mime_type(direct[0], direct[1]) else None

        article_bytes, _, article_final_url = article_html
        retried_direct = _fetch_url_bytes(origin_url, headers=browser_headers, opener=opener)
        if retried_direct is not None:
            image_bytes, content_type, final_url = retried_direct
            if _detect_supported_image_mime_type(image_bytes, content_type):
                return image_bytes, content_type, final_url

        html_text = article_bytes.decode("utf-8", errors="replace")
        candidate_urls = _extract_candidate_image_urls(
            html_text,
            base_url=article_final_url,
            preferred_url=origin_url,
        )
        for candidate_url in candidate_urls:
            fetched = _fetch_url_bytes(candidate_url, headers=browser_headers, opener=opener)
            if fetched is None:
                continue
            image_bytes, content_type, final_url = fetched
            if _detect_supported_image_mime_type(image_bytes, content_type):
                return image_bytes, content_type, final_url

        return None

    @staticmethod
    def _parse_response_payload(response_payload: dict) -> dict:
        text = _extract_openai_response_text(response_payload)
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"OpenAI response was not valid JSON: {text}") from exc

        required = {"overall_summary", "assets"}
        missing = sorted(required - set(parsed))
        if missing:
            raise RuntimeError(f"OpenAI response missing fields: {', '.join(missing)}")
        if not isinstance(parsed["assets"], list):
            raise RuntimeError("OpenAI response assets must be a list")
        return parsed

    @staticmethod
    def _collect_valid_assets(analysis_input: IssueMultimodalAnalysisInput, parsed: dict) -> list[dict]:
        expected_refs = {asset.asset_id or asset.origin_url for asset in analysis_input.assets}
        valid_assets: list[dict] = []
        seen_refs: set[str] = set()
        for item in parsed["assets"]:
            asset_reference = str(item.get("asset_reference"))
            if asset_reference not in expected_refs:
                raise RuntimeError(
                    "OpenAI response violated asset-reference rule: "
                    f"unexpected asset_reference {asset_reference}"
                )
            if asset_reference in seen_refs:
                continue
            seen_refs.add(asset_reference)
            valid_assets.append(item)
        if not valid_assets:
            raise RuntimeError(
                "OpenAI response violated asset-coverage rule: "
                "no valid asset analyses were returned"
            )
        return valid_assets


class IssueMultimodalAnalysisService:
    def __init__(
        self,
        engine: MultimodalAnalysisEngine | None = None,
        *,
        feedback_repository: FeedbackMemoryRepository | None = None,
    ) -> None:
        self.engine = engine or build_default_multimodal_engine()
        self.feedback_repository = feedback_repository

    def analyze(self, analysis_input: IssueMultimodalAnalysisInput) -> IssueMultimodalAnalysis:
        prepared_input = self._with_memory_context(analysis_input)
        result = self.engine.analyze(prepared_input)
        topic_metadata = self._build_policy_topic_metadata(prepared_input)
        self._attach_policy_context(result, prepared_input, topic_metadata)
        policy_result = apply_multimodal_policies(
            analysis=result,
            input_assets=prepared_input.assets,
            topic_metadata=topic_metadata,
            repository=self.feedback_repository,
        )
        debug_metadata = {
            "memory_context_used": bool(prepared_input.referenced_memory_ids),
            "num_similar_cases": len(prepared_input.referenced_memory_ids),
            "referenced_memory_ids": list(prepared_input.referenced_memory_ids),
            "num_similar_cases_by_asset": {
                asset_reference: _count_summary_cases(summary)
                for asset_reference, summary in prepared_input.memory_context_by_asset.items()
            },
            "memory_context_by_asset": dict(prepared_input.memory_context_by_asset),
            "policy_correction_used": policy_result.policy_correction_used,
            "applied_policy_ids": list(policy_result.applied_policy_ids),
            "applied_policy_types": list(policy_result.applied_policy_types),
            "policy_correction_summary": policy_result.policy_correction_summary,
        }
        result.metadata = {
            **dict(result.metadata),
            **debug_metadata,
        }
        referenced_by_asset = _collect_referenced_ids_by_asset(
            prepared_input.assets,
            prepared_input.memory_context_by_asset,
            prepared_input.referenced_memory_ids,
        )
        for asset in result.assets:
            asset_policy_debug = policy_result.asset_debug.get(asset.asset_reference)
            asset.analysis_payload = {
                **dict(asset.analysis_payload),
                "memory_context_used": asset.asset_reference in prepared_input.memory_context_by_asset,
                "num_similar_cases": _count_summary_cases(
                    prepared_input.memory_context_by_asset.get(asset.asset_reference)
                ),
                "referenced_memory_ids": referenced_by_asset.get(asset.asset_reference, []),
                "policy_correction_used": bool(asset_policy_debug),
                "applied_policy_ids": list(asset_policy_debug.applied_policy_ids) if asset_policy_debug else [],
                "applied_policy_types": list(asset_policy_debug.applied_policy_types) if asset_policy_debug else [],
                "corrected_fields": list(asset_policy_debug.corrected_fields) if asset_policy_debug else [],
                "pre_correction_snapshot": dict(asset_policy_debug.pre_correction_snapshot) if asset_policy_debug else {},
            }
        return result

    def _with_memory_context(
        self,
        analysis_input: IssueMultimodalAnalysisInput,
    ) -> IssueMultimodalAnalysisInput:
        if analysis_input.memory_context_summary or analysis_input.memory_context_by_asset:
            return analysis_input

        topic_source = _build_multimodal_topic_source(analysis_input)
        asset_summaries: dict[str, str] = {}
        referenced_memory_ids: list[str] = []
        summary_blocks: list[str] = []

        for asset in sorted(analysis_input.assets, key=lambda item: item.sort_order):
            asset_reference = asset.asset_id or asset.origin_url
            try:
                rows = retrieve_similar_multimodal_edits(
                    asset,
                    topic_source=topic_source,
                    repository=self.feedback_repository,
                    top_k=2,
                )
            except Exception:
                rows = []
            if not rows:
                continue
            asset_summary = format_multimodal_retrieval_summary(rows)
            asset_summaries[asset_reference] = asset_summary
            summary_blocks.append(f"[asset {asset_reference}]\n{asset_summary}")
            for row in rows:
                memory_id = str(row.get("id") or "").strip()
                if memory_id and memory_id not in referenced_memory_ids:
                    referenced_memory_ids.append(memory_id)

        return IssueMultimodalAnalysisInput(
            candidate=analysis_input.candidate,
            assets=list(analysis_input.assets),
            card_news_draft=analysis_input.card_news_draft,
            memory_context_summary="\n\n".join(summary_blocks) if summary_blocks else None,
            referenced_memory_ids=referenced_memory_ids,
            memory_context_by_asset=asset_summaries,
            metadata=dict(analysis_input.metadata),
        )

    def _build_policy_topic_metadata(self, analysis_input: IssueMultimodalAnalysisInput) -> dict[str, Any]:
        topic_features = extract_topic_features(
            {
                "issue_id": analysis_input.candidate.issue_id,
                "topic_name": analysis_input.metadata.get("topic_name", analysis_input.candidate.title),
                **dict(analysis_input.metadata),
            },
            overrides={
                "topic_type": analysis_input.metadata.get("topic_type"),
                "entity_focus": analysis_input.metadata.get("entity_focus"),
                "event_type": analysis_input.metadata.get("event_type"),
                "angle_type": analysis_input.metadata.get("angle_type"),
                "article_count": analysis_input.metadata.get("article_count"),
                "asset_count": len(analysis_input.assets),
                "has_notable_numbers": analysis_input.metadata.get("has_notable_numbers"),
                "recommended_focus": analysis_input.metadata.get("recommended_focus"),
            },
        )
        return {
            **topic_features,
            "topic_fingerprint": build_topic_fingerprint(
                {
                    "issue_id": analysis_input.candidate.issue_id,
                    "topic_name": analysis_input.metadata.get("topic_name", analysis_input.candidate.title),
                    **dict(analysis_input.metadata),
                },
                overrides={
                    "topic_type": topic_features.get("topic_type"),
                    "entity_focus": topic_features.get("entity_focus"),
                    "event_type": topic_features.get("event_type"),
                    "angle_type": topic_features.get("angle_type"),
                },
            ),
        }

    def _attach_policy_context(
        self,
        result: IssueMultimodalAnalysis,
        analysis_input: IssueMultimodalAnalysisInput,
        topic_metadata: dict[str, Any],
    ) -> None:
        input_by_ref = {asset.asset_id or asset.origin_url: asset for asset in analysis_input.assets}
        for asset in result.assets:
            source_asset = input_by_ref.get(asset.asset_reference)
            if source_asset is None:
                continue
            asset_features = extract_asset_features(
                {
                    "asset_reference": asset.asset_reference,
                    "asset_type": source_asset.asset_type,
                    "caption": source_asset.caption,
                    "vision_caption": source_asset.vision_caption,
                    "ocr_text": source_asset.ocr_text,
                    "width": source_asset.width,
                    "height": source_asset.height,
                    "sort_order": source_asset.sort_order,
                    "subject_tags": list(asset.subject_tags),
                    "event_tags": list(asset.event_tags),
                    "emotion_tags": list(asset.emotion_tags),
                    "composition_tags": list(asset.composition_tags),
                    "risk_tags": list(asset.risk_tags),
                    "usage_recommendation": asset.usage_recommendation,
                    "tag_summary": asset.tag_summary,
                    "scene_description": asset.scene_description,
                    "humor_point": asset.humor_point,
                    "caution_note": asset.caution_note,
                }
            )
            asset.analysis_payload = {
                **dict(asset.analysis_payload),
                **asset_features,
                "asset_fingerprint": build_asset_fingerprint(
                    {
                        "asset_reference": asset.asset_reference,
                        "asset_type": source_asset.asset_type,
                        "caption": source_asset.caption,
                        "vision_caption": source_asset.vision_caption,
                        "ocr_text": source_asset.ocr_text,
                        "width": source_asset.width,
                        "height": source_asset.height,
                        "sort_order": source_asset.sort_order,
                        **asset_features,
                    }
                ),
                "topic_fingerprint": topic_metadata.get("topic_fingerprint"),
            }


def build_default_multimodal_engine() -> MultimodalAnalysisEngine:
    load_default_env()
    if os.getenv("OPENAI_API_KEY"):
        return OpenAIMultimodalAnalysisEngine()
    if os.getenv("GEMINI_API_KEY"):
        return GeminiMultimodalAnalysisEngine()
    return HeuristicMultimodalAnalysisEngine()


def _serialize_datetime(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    else:
        value = value.astimezone(timezone.utc)
    return value.isoformat()


def _primary_text_hint(asset, fallback: str) -> str:
    for candidate in [asset.vision_caption, asset.caption, asset.ocr_text, fallback]:
        if candidate and str(candidate).strip():
            return " ".join(str(candidate).split()).strip()
    return "visual context unavailable"


def _compact_text(*parts: object) -> str:
    return " ".join(" ".join(str(part).split()).strip() for part in parts if part).strip()


def _limit_tags(tags: list[str], *, group_name: str) -> list[str]:
    allowed = set(MULTIMODAL_TAG_DICTIONARY[group_name])
    unique: list[str] = []
    seen: set[str] = set()
    for tag in tags:
        if tag not in allowed or tag in seen:
            continue
        seen.add(tag)
        unique.append(tag)
    return unique


def _build_scene_description(tag_summary: str) -> str:
    summary = " ".join(str(tag_summary).split()).strip()
    if not summary:
        return "장면 핵심이 제한적으로만 파악된다."
    return f"{summary} 중심 장면으로 해석된다."


def _build_humor_point(
    *,
    usage_recommendation: str,
    event_tags: list[str],
    emotion_tags: list[str],
) -> str:
    combined = set(event_tags) | set(emotion_tags)
    if any(tag in combined for tag in ["환호", "열광", "기쁨", "응원", "리액션장면"]):
        return "현장 반응을 붙이면 카드뉴스 리듬이 살아난다."
    if any(tag in combined for tag in ["우천중단", "긴장", "비장함"]):
        return "날씨 변수 자체가 긴장 포인트라 짧고 재빠른 문장이 어울린다."
    if any(tag in combined for tag in ["끝내기", "결승타", "역전", "홈런", "결정적순간"]):
        return "결정적 순간을 한 줄로 치고 들어가면 임팩트가 좋다."
    if usage_recommendation == "reaction":
        return "표정이나 현장 반응을 짧게 붙이면 활용도가 높다."
    return "설명문은 담백하게 두고 제목에서 톤을 살리는 편이 안전하다."


def _build_usage_recommendation(asset, candidate) -> str:
    hint = _primary_text_hint(asset, candidate.summary).lower()
    issue_hint = " ".join([candidate.title, candidate.summary]).lower()
    if candidate.source_type == "kma_weather":
        return "quick_info"
    if candidate.source_type == "kbo_stats":
        return "data_context"
    if candidate.source_type == "dcinside" and any(
        token in hint for token in ["환호", "팬", "관중", "응원", "밈", "반복", "움짤", "gif"]
    ):
        return "reaction"
    decisive_tokens = ["끝내기", "결승", "역전", "홈런", "celebrat"]
    if any(token in hint for token in decisive_tokens):
        if asset.sort_order == 0:
            return "detail_b"
        if asset.sort_order == 1:
            return "cover"
        if asset.sort_order == 2:
            return "reaction"
        return "detail_a"
    if asset.sort_order == 0 and any(
        token in issue_hint for token in ["끝내기", "결승", "역전", "홈런", "walk-off"]
    ):
        return "detail_b"
    if any(token in hint for token in ["환호", "팬", "관중", "응원"]):
        if asset.sort_order == 0:
            return "cover"
        return "reaction"
    if asset.sort_order == 0:
        return "cover"
    return "detail_a"


def _build_subject_tags(asset, candidate) -> list[str]:
    hint = _compact_text(candidate.title, candidate.summary, asset.caption, asset.vision_caption, asset.ocr_text)
    tags: list[str] = []
    for team in MULTIMODAL_TAG_DICTIONARY["subject_tags"][:10]:
        if team in hint:
            tags.append(team)
    lowered = hint.lower()
    if any(token in lowered for token in ["관중", "팬", "응원단", "cheer", "crowd"]):
        tags.append("관중")
    if any(token in lowered for token in ["감독"]):
        tags.append("감독")
    if any(token in lowered for token in ["코치"]):
        tags.append("코치")
    if any(token in lowered for token in ["심판"]):
        tags.append("심판")
    if any(token in lowered for token in ["투수", "pitch", "선발", "불펜"]):
        tags.append("투수")
    elif any(token in lowered for token in ["포수"]):
        tags.append("포수")
    elif any(token in lowered for token in ["타자", "타석", "bat", "홈런", "안타", "결승타"]):
        tags.append("타자")
    return _limit_tags(tags[:4], group_name="subject_tags")


def _build_event_tags(asset, candidate) -> list[str]:
    hint = _compact_text(candidate.title, candidate.summary, asset.caption, asset.vision_caption, asset.ocr_text).lower()
    mapping = [
        ("끝내기", "끝내기"),
        ("결승타", "결승타"),
        ("적시타", "적시타"),
        ("안타", "안타"),
        ("홈런", "홈런"),
        ("역전", "역전"),
        ("득점", "득점"),
        ("실점", "실점"),
        ("삼진", "삼진"),
        ("세이브", "세이브"),
        ("호수비", "호수비"),
        ("송구", "송구"),
        ("세리머니", "세리머니"),
        ("벤치", "벤치반응"),
        ("더그아웃", "더그아웃"),
        ("인터뷰", "인터뷰"),
        ("응원", "응원"),
        ("우천", "우천중단"),
        ("비", "우천중단"),
    ]
    tags = [tag for keyword, tag in mapping if keyword in hint]
    if any(token in hint for token in ["환호", "팬", "관중", "응원"]) and "응원" not in tags:
        tags.append("리액션장면")
    if any(token in hint for token in ["결승", "끝내기", "역전", "홈런"]) and "결정적순간" not in tags:
        tags.append("결정적순간")
    if not tags and hint:
        tags.append("하이라이트장면")
    return _limit_tags(tags[:3], group_name="event_tags")


def _build_emotion_tags(asset, candidate) -> list[str]:
    hint = _compact_text(candidate.title, candidate.summary, asset.caption, asset.vision_caption, asset.ocr_text).lower()
    mapping = [
        ("환호", "환호"),
        ("기쁨", "기쁨"),
        ("포효", "포효"),
        ("열광", "열광"),
        ("흥분", "흥분"),
        ("자신감", "자신감"),
        ("집중", "집중"),
        ("긴장", "긴장"),
        ("비장", "비장함"),
        ("신중", "신중함"),
        ("침울", "침울"),
        ("아쉬", "아쉬움"),
        ("당황", "당황"),
        ("허탈", "허탈"),
        ("분노", "분노"),
        ("실망", "실망"),
        ("유쾌", "유쾌함"),
    ]
    tags = [tag for keyword, tag in mapping if keyword in hint]
    if any(token in hint for token in ["홈런", "결승", "끝내기", "역전"]) and not tags:
        tags.extend(["환호", "기쁨"])
    return _limit_tags(tags[:3], group_name="emotion_tags")


def _build_composition_tags(asset, candidate, usage_recommendation: str) -> list[str]:
    hint = _compact_text(candidate.title, candidate.summary, asset.caption, asset.vision_caption, asset.ocr_text).lower()
    tags: list[str] = []
    if usage_recommendation == "cover":
        tags.append("타이틀컷적합")
    elif usage_recommendation == "reaction":
        tags.append("리액션컷적합")
    else:
        tags.append("본문컷적합")
    if any(token in hint for token in ["관중", "팬", "응원단", "crowd"]):
        tags.extend(["관중포함", "다중인물"])
    if any(token in hint for token in ["더그아웃", "벤치"]):
        tags.append("더그아웃배경")
    if any(token in hint for token in ["그라운드", "마운드", "타석"]):
        tags.append("그라운드배경")
    if asset.width and asset.height:
        ratio = asset.width / max(asset.height, 1)
        if ratio >= 1.3:
            tags.append("텍스트안전영역넓음")
        elif ratio >= 0.9:
            tags.append("텍스트안전영역보통")
        else:
            tags.append("텍스트안전영역좁음")
    else:
        tags.append("텍스트안전영역보통")
    if any(token in hint for token in ["액션", "스윙", "투구", "타격", "송구", "세리머니"]):
        tags.append("액션샷")
    else:
        tags.append("정지장면")
    if any(token in hint for token in ["관중", "팬", "단체", "응원"]) and "단체샷" not in tags:
        tags.append("단체샷")
    if "관중포함" in tags:
        tags.append("배경복잡")
    else:
        tags.append("배경단순")
    if asset.sort_order == 0:
        tags.append("중앙구도")
    prioritized = []
    priority_order = [
        "타이틀컷적합",
        "본문컷적합",
        "리액션컷적합",
        "텍스트안전영역넓음",
        "텍스트안전영역보통",
        "텍스트안전영역좁음",
        "액션샷",
        "정지장면",
        "클로즈업",
        "상반신",
        "전신",
        "중앙구도",
        "측면구도",
        "관중포함",
        "다중인물",
        "단체샷",
        "더그아웃배경",
        "그라운드배경",
        "배경단순",
        "배경복잡",
    ]
    seen = set()
    for tag in priority_order + tags:
        if tag in tags and tag not in seen:
            seen.add(tag)
            prioritized.append(tag)
    return _limit_tags(prioritized[:4], group_name="composition_tags")


def _build_risk_tags(asset, candidate) -> list[str]:
    tags: list[str] = []
    if not any([asset.caption, asset.vision_caption]):
        tags.append("캡션근거약함")
    if not any([asset.caption, asset.vision_caption, asset.ocr_text]):
        tags.append("메타데이터부족")
        tags.append("상황해석불확실")
    if asset.ocr_text and len(asset.ocr_text.strip()) < 3:
        tags.append("ocr불명확")
    if asset.asset_type == "gif":
        tags.append("gif해석주의")
    if asset.width and asset.height and min(asset.width, asset.height) < 400:
        tags.append("저해상도")
    if candidate.source_type == "dcinside":
        tags.append("과한추정주의")
    return _limit_tags(tags[:3], group_name="risk_tags")


def _build_tag_summary(
    *,
    subject_tags: list[str],
    event_tags: list[str],
    emotion_tags: list[str],
    composition_tags: list[str],
    fallback: str,
) -> str:
    parts: list[str] = []
    if subject_tags:
        parts.append("/".join(subject_tags[:2]))
    if event_tags:
        parts.append("/".join(event_tags[:2]))
    if emotion_tags:
        parts.append("/".join(emotion_tags[:1]))
    if not parts:
        compact = " ".join(str(fallback).split()).strip()
        return compact[:40] if compact else "핵심 장면 단서가 제한적임"
    summary = " ".join(parts)
    if composition_tags:
        if "타이틀컷적합" in composition_tags:
            summary += " 타이틀컷"
        elif "리액션컷적합" in composition_tags:
            summary += " 반응컷"
    return summary[:40]


def _summarize_tags_for_text(
    *,
    subject_tags: list[str],
    event_tags: list[str],
    emotion_tags: list[str],
    composition_tags: list[str],
) -> str:
    parts: list[str] = []
    if subject_tags:
        parts.append("/".join(subject_tags[:2]))
    if event_tags:
        parts.append("/".join(event_tags[:2]))
    if emotion_tags:
        parts.append("/".join(emotion_tags[:1]))
    if composition_tags:
        if "타이틀컷적합" in composition_tags:
            parts.append("타이틀컷")
        elif "리액션컷적합" in composition_tags:
            parts.append("반응컷")
    return " ".join(part for part in parts if part).strip()


def _build_caution_note(asset, candidate) -> str | None:
    notes: list[str] = []
    if candidate.source_type == "dcinside":
        notes.append("커뮤니티 출처라 과한 단정 표현은 피하는 편이 좋다")
    if not any([asset.caption, asset.vision_caption, asset.ocr_text]):
        notes.append("시각 단서가 적어 해석 확신도가 높지 않다")
    if asset.asset_type == "gif":
        notes.append("움직임 해석은 실제 프레임 확인 전까지 보수적으로 쓰는 편이 안전하다")
    if not notes:
        return None
    return "; ".join(notes)


def _estimate_confidence(asset) -> float:
    score = 0.45
    if asset.caption:
        score += 0.2
    if asset.vision_caption:
        score += 0.2
    if asset.ocr_text:
        score += 0.1
    if asset.asset_type == "gif":
        score -= 0.1
    return max(0.2, min(0.95, round(score, 2)))


def _build_overall_summary(
    analysis_input: IssueMultimodalAnalysisInput,
    insights: list[AssetMultimodalInsight],
) -> str:
    candidate = analysis_input.candidate
    if not insights:
        return "분석 가능한 시각 자산이 없어 제목/본문 중심으로만 구성하는 편이 적합하다."
    key_slots = ", ".join(insight.usage_recommendation for insight in insights[:3])
    return (
        f"{candidate.source_type} 이슈 기준 자산 {len(insights)}개를 메타데이터 기반으로 해석했고, "
        f"우선 활용 슬롯은 {key_slots} 쪽이 적합하다."
    )


def _normalize_overall_summary(
    value: object,
    analysis_input: IssueMultimodalAnalysisInput,
    insights: list[AssetMultimodalInsight],
) -> str:
    compact = " ".join(str(value or "").split()).strip()
    sentences = [
        sentence.strip()
        for sentence in re.split(r"(?<=[.!?])\s+| / ", compact)
        if sentence.strip()
    ]
    deduped: list[str] = []
    seen: set[str] = set()
    for sentence in sentences:
        if sentence in seen:
            continue
        seen.add(sentence)
        deduped.append(sentence)
    if deduped:
        normalized = " ".join(deduped[:2]).strip()
        if len(normalized) <= 120:
            return normalized
    return _build_overall_summary(analysis_input, insights)


def _normalize_caution_note(value: object) -> str | None:
    if value is None:
        return None
    note = " ".join(str(value).split()).strip()
    if not note:
        return None
    lowered = note.lower()
    generic_notes = {
        "no specific cautions noted.",
        "no specific cautions noted",
        "no notable cautions.",
        "none",
        "n/a",
    }
    if lowered in generic_notes:
        return None
    return note


def _normalize_tag_list(values: object, *, group_name: str) -> list[str]:
    if not isinstance(values, list):
        return []
    normalized = []
    for value in values:
        if value is None:
            continue
        compact = " ".join(str(value).split()).strip()
        if compact:
            normalized.append(compact)
    return _limit_tags(normalized, group_name=group_name)


def _normalize_usage_recommendation(
    value: object,
    *,
    composition_tags: list[str],
    event_tags: list[str],
    emotion_tags: list[str],
) -> str:
    usage = " ".join(str(value or "").split()).strip()
    allowed = set(MULTIMODAL_TAG_DICTIONARY["usage_recommendation"])
    if usage not in allowed:
        usage = ""
    if "타이틀컷적합" in composition_tags:
        return "cover"
    if "리액션컷적합" in composition_tags or any(
        tag in event_tags for tag in ["응원", "리액션장면", "벤치반응"]
    ) or any(tag in emotion_tags for tag in ["환호", "열광", "유쾌함"]):
        return "reaction" if usage not in {"cover", "detail_a", "detail_b"} else usage
    if usage in {"data_context", "quick_info"} and not any(
        tag in event_tags for tag in ["인터뷰", "작전지시"]
    ):
        return "detail_a"
    if usage:
        return usage
    return "detail_a"


def _normalize_composition_tags(
    composition_tags: list[str],
    *,
    usage_recommendation: str,
) -> list[str]:
    tags = list(composition_tags)
    if usage_recommendation == "cover" and "타이틀컷적합" not in tags:
        tags.insert(0, "타이틀컷적합")
    elif usage_recommendation == "reaction" and "리액션컷적합" not in tags:
        tags.insert(0, "리액션컷적합")
    elif usage_recommendation in {"detail_a", "detail_b", "summary_cta", "quick_info", "data_context"} and "본문컷적합" not in tags and "리액션컷적합" not in tags and "타이틀컷적합" not in tags:
        tags.insert(0, "본문컷적합")
    if not any(tag.startswith("텍스트안전영역") for tag in tags):
        tags.append("텍스트안전영역보통")
    return _limit_tags(tags, group_name="composition_tags")


def _normalize_risk_tags(
    risk_tags: list[str],
    *,
    subject_tags: list[str],
    event_tags: list[str],
    confidence: float,
) -> list[str]:
    tags = list(risk_tags)
    if not event_tags and "상황해석불확실" not in tags and confidence < 0.9:
        tags.append("상황해석불확실")
    if len(subject_tags) <= 1 and confidence < 0.85 and "인물식별불확실" not in tags:
        tags.append("인물식별불확실")
    return _limit_tags(tags, group_name="risk_tags")


def _build_insight_from_response_item(item: dict) -> AssetMultimodalInsight:
    subject_tags = _normalize_tag_list(item.get("subject_tags"), group_name="subject_tags")
    event_tags = _normalize_tag_list(item.get("event_tags"), group_name="event_tags")
    emotion_tags = _normalize_tag_list(item.get("emotion_tags"), group_name="emotion_tags")
    composition_tags = _normalize_tag_list(item.get("composition_tags"), group_name="composition_tags")
    confidence = round(float(item["confidence"]), 2)
    usage_recommendation = _normalize_usage_recommendation(
        item.get("usage_recommendation"),
        composition_tags=composition_tags,
        event_tags=event_tags,
        emotion_tags=emotion_tags,
    )
    composition_tags = _normalize_composition_tags(
        composition_tags,
        usage_recommendation=usage_recommendation,
    )
    risk_tags = _normalize_risk_tags(
        _normalize_tag_list(item.get("risk_tags"), group_name="risk_tags"),
        subject_tags=subject_tags,
        event_tags=event_tags,
        confidence=confidence,
    )
    model_scene = " ".join(str(item.get("scene_description") or "").split()).strip()
    fallback_text = _summarize_tags_for_text(
        subject_tags=subject_tags,
        event_tags=event_tags,
        emotion_tags=emotion_tags,
        composition_tags=composition_tags,
    ) or model_scene
    tag_summary = _build_tag_summary(
        subject_tags=subject_tags,
        event_tags=event_tags,
        emotion_tags=emotion_tags,
        composition_tags=composition_tags,
        fallback=fallback_text,
    )
    scene_description = _build_scene_description(tag_summary)
    humor_point = _build_humor_point(
        usage_recommendation=usage_recommendation,
        event_tags=event_tags,
        emotion_tags=emotion_tags,
    )
    return AssetMultimodalInsight(
        asset_reference=str(item["asset_reference"]),
        asset_type=str(item["asset_type"]),
        scene_description=scene_description,
        humor_point=humor_point,
        usage_recommendation=usage_recommendation,
        subject_tags=subject_tags,
        event_tags=event_tags,
        emotion_tags=emotion_tags,
        composition_tags=composition_tags,
        risk_tags=risk_tags,
        tag_summary=tag_summary,
        caution_note=_normalize_caution_note(item.get("caution_note")),
        confidence=confidence,
        analysis_payload={},
    )


def _serialize_analysis_input(analysis_input: IssueMultimodalAnalysisInput) -> str:
    payload = asdict(analysis_input)
    payload["candidate"]["published_at"] = _serialize_datetime(analysis_input.candidate.published_at)
    payload["candidate"]["collected_at"] = _serialize_datetime(analysis_input.candidate.collected_at)
    if analysis_input.card_news_draft and analysis_input.card_news_draft.created_at:
        payload["card_news_draft"]["created_at"] = _serialize_datetime(
            analysis_input.card_news_draft.created_at
        )
    return json.dumps(payload, ensure_ascii=False, indent=2)


def _subset_analysis_input(
    analysis_input: IssueMultimodalAnalysisInput,
    assets,
) -> IssueMultimodalAnalysisInput:
    return IssueMultimodalAnalysisInput(
        candidate=analysis_input.candidate,
        assets=list(assets),
        card_news_draft=analysis_input.card_news_draft,
        memory_context_summary=analysis_input.memory_context_summary,
        referenced_memory_ids=list(analysis_input.referenced_memory_ids),
        memory_context_by_asset=dict(analysis_input.memory_context_by_asset),
        metadata=dict(analysis_input.metadata),
    )


def _build_multimodal_topic_source(
    analysis_input: IssueMultimodalAnalysisInput,
) -> dict[str, object]:
    draft = analysis_input.card_news_draft
    metadata = dict(analysis_input.metadata)
    candidate = analysis_input.candidate
    return {
        "topic_id": candidate.issue_id,
        "topic_name": metadata.get("topic_name") or candidate.title,
        "team_name": metadata.get("team_name"),
        "summary": candidate.summary,
        "overall_summary": candidate.summary,
        "topic_type": metadata.get("topic_type"),
        "entity_focus": metadata.get("entity_focus"),
        "event_type": metadata.get("event_type"),
        "angle_type": metadata.get("angle_type"),
        "article_count": metadata.get("article_count"),
        "asset_count": len(analysis_input.assets),
        "has_notable_numbers": metadata.get("has_notable_numbers"),
        "recommended_focus": metadata.get("recommended_focus"),
        "draft_title": draft.title if draft else None,
        "draft_subtitle": draft.subtitle if draft else None,
    }


def _count_summary_cases(summary: str | None) -> int:
    if not summary:
        return 0
    return sum(
        1
        for line in summary.splitlines()
        if re.match(r"^\d+\.\s", line.strip())
    )


def _collect_referenced_ids_by_asset(
    assets,
    memory_context_by_asset: dict[str, str],
    referenced_memory_ids: list[str],
) -> dict[str, list[str]]:
    ordered_refs = [
        asset.asset_id or asset.origin_url
        for asset in assets
        if (asset.asset_id or asset.origin_url) in memory_context_by_asset
    ]
    if not ordered_refs or not referenced_memory_ids:
        return {}
    result: dict[str, list[str]] = {}
    cursor = 0
    for asset_reference in ordered_refs:
        case_count = _count_summary_cases(memory_context_by_asset.get(asset_reference))
        if case_count <= 0:
            continue
        result[asset_reference] = referenced_memory_ids[cursor:cursor + case_count]
        cursor += case_count
    return result


def _chunk_assets(assets, chunk_size: int) -> list[list]:
    if chunk_size <= 0:
        return [list(assets)]
    materialized = list(assets)
    return [
        materialized[index:index + chunk_size]
        for index in range(0, len(materialized), chunk_size)
    ]


def _split_assets_in_half(assets) -> list[list]:
    materialized = list(assets)
    midpoint = max(1, len(materialized) // 2)
    left = materialized[:midpoint]
    right = materialized[midpoint:]
    return [batch for batch in [left, right] if batch]


def _is_openai_token_limit_error(status_code: int, detail: str) -> bool:
    normalized = detail.lower()
    return status_code == 429 and (
        "rate_limit_exceeded" in normalized
        or "tokens per min" in normalized
    )


def _is_openai_request_too_large_error(status_code: int, detail: str) -> bool:
    normalized = detail.lower()
    return status_code == 429 and "request too large" in normalized


def _is_openai_retryable_limit_error(status_code: int, detail: str) -> bool:
    return _is_openai_token_limit_error(status_code, detail)


def _extract_retry_after_seconds(detail: str) -> float:
    match = re.search(r"try again in\s+([0-9]+(?:\.[0-9]+)?)s", detail, flags=re.IGNORECASE)
    if not match:
        return 0.0
    try:
        return float(match.group(1))
    except ValueError:
        return 0.0


def _merge_overall_summaries(
    analysis_input: IssueMultimodalAnalysisInput,
    insights: list[AssetMultimodalInsight],
    summaries: list[str],
) -> str:
    normalized: list[str] = []
    seen: set[str] = set()
    for summary in summaries:
        compact = " ".join(summary.split()).strip()
        if not compact or compact in seen:
            continue
        seen.add(compact)
        normalized.append(compact)
    if not normalized:
        return _build_overall_summary(analysis_input, insights)
    return " / ".join(normalized[:3])


def _multimodal_response_schema() -> dict:
    return {
        "type": "object",
        "properties": {
            "overall_summary": {"type": "string"},
            "assets": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "asset_reference": {"type": "string"},
                        "asset_type": {"type": "string"},
                        "scene_description": {"type": "string"},
                        "humor_point": {"type": "string"},
                        "usage_recommendation": {"type": "string"},
                        "subject_tags": {
                            "type": "array",
                            "items": {"type": "string"},
                        },
                        "event_tags": {
                            "type": "array",
                            "items": {"type": "string"},
                        },
                        "emotion_tags": {
                            "type": "array",
                            "items": {"type": "string"},
                        },
                        "composition_tags": {
                            "type": "array",
                            "items": {"type": "string"},
                        },
                        "risk_tags": {
                            "type": "array",
                            "items": {"type": "string"},
                        },
                        "tag_summary": {"type": "string"},
                        "caution_note": {"type": "string"},
                        "confidence": {"type": "number"},
                    },
                    "required": [
                        "asset_reference",
                        "asset_type",
                        "scene_description",
                        "humor_point",
                        "usage_recommendation",
                        "subject_tags",
                        "event_tags",
                        "emotion_tags",
                        "composition_tags",
                        "risk_tags",
                        "tag_summary",
                        "caution_note",
                        "confidence",
                    ],
                    "additionalProperties": False,
                },
            },
        },
        "required": ["overall_summary", "assets"],
        "additionalProperties": False,
    }


def _supports_remote_image_input(origin_url: str | None) -> bool:
    if not origin_url:
        return False
    lowered = origin_url.lower()
    return lowered.startswith("http://") or lowered.startswith("https://")


def _build_browser_headers(
    *,
    referer: str | None,
    accept_images_only: bool = False,
    accept_html: bool = False,
) -> dict[str, str]:
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0 Safari/537.36"
        ),
    }
    if accept_images_only:
        headers["Accept"] = "image/jpeg,image/png,image/gif,image/webp,image/*;q=0.8,*/*;q=0.5"
    elif accept_html:
        headers["Accept"] = "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"
    if referer:
        headers["Referer"] = referer
    return headers


def _fetch_url_bytes(
    url: str,
    *,
    headers: dict[str, str],
    opener=None,
) -> tuple[bytes, str | None, str] | None:
    request = urllib.request.Request(url, headers=headers, method="GET")
    target = opener.open if opener is not None else urllib.request.urlopen
    try:
        with target(request, timeout=30) as response:
            content_type = response.headers.get_content_type()
            final_url = response.geturl()
            try:
                body = response.read()
            except http.client.IncompleteRead as exc:
                partial = exc.partial or b""
                if partial:
                    return partial, content_type, final_url
                return None
            return body, content_type, final_url
    except (urllib.error.HTTPError, urllib.error.URLError):
        return None


def _detect_supported_image_mime_type(image_bytes: bytes, content_type: str | None) -> str | None:
    if image_bytes.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if image_bytes.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if image_bytes.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if len(image_bytes) >= 12 and image_bytes[:4] == b"RIFF" and image_bytes[8:12] == b"WEBP":
        return "image/webp"
    if content_type in {"image/jpeg", "image/png", "image/gif", "image/webp"}:
        return content_type
    return None


def _extract_candidate_image_urls(
    html_text: str,
    *,
    base_url: str,
    preferred_url: str,
) -> list[str]:
    candidates: list[str] = []
    patterns = [
        r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\']([^"\']+)["\']',
        r'<meta[^>]+name=["\']twitter:image["\'][^>]+content=["\']([^"\']+)["\']',
        r'<img[^>]+src=["\']([^"\']+)["\']',
    ]
    for pattern in patterns:
        for match in re.findall(pattern, html_text, flags=re.IGNORECASE):
            absolute = urllib.parse.urljoin(base_url, match.strip())
            if absolute not in candidates:
                candidates.append(absolute)

    preferred_name = urllib.parse.urlparse(preferred_url).path.rsplit("/", 1)[-1]
    ranked = sorted(
        candidates,
        key=lambda item: (
            preferred_name not in urllib.parse.urlparse(item).path,
            item != preferred_url,
        ),
    )
    return ranked


def _resolve_image_mime_type(
    content_type: str | None,
    fallback_mime_type: str | None,
    origin_url: str | None,
) -> str:
    detected = _detect_supported_image_mime_type(b"", content_type)
    if detected:
        return detected
    for candidate in [content_type, fallback_mime_type]:
        if candidate and str(candidate).startswith("image/"):
            return str(candidate)
    lowered = (origin_url or "").lower()
    if lowered.endswith(".png"):
        return "image/png"
    if lowered.endswith(".gif"):
        return "image/gif"
    if lowered.endswith(".webp"):
        return "image/webp"
    return "image/jpeg"


def _extract_openai_response_text(response_payload: dict) -> str:
    output_text = response_payload.get("output_text")
    if isinstance(output_text, str) and output_text.strip():
        return output_text
    if isinstance(output_text, list):
        joined = "\n".join(str(item) for item in output_text if str(item).strip()).strip()
        if joined:
            return joined

    outputs = response_payload.get("output")
    if isinstance(outputs, list):
        fragments: list[str] = []
        for output_item in outputs:
            if not isinstance(output_item, dict):
                continue
            content_items = output_item.get("content")
            if not isinstance(content_items, list):
                continue
            for content_item in content_items:
                if not isinstance(content_item, dict):
                    continue
                text = content_item.get("text")
                if isinstance(text, str) and text.strip():
                    fragments.append(text)
        if fragments:
            return "\n".join(fragments)

    raise RuntimeError(f"Unexpected OpenAI response shape: {response_payload}")
