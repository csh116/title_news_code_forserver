from __future__ import annotations

import json
import math
import os
import re
import socket
import urllib.error
import urllib.request
from dataclasses import asdict
from datetime import datetime, timezone
from typing import Protocol

from kbo_card_news.config.env import load_default_env
from kbo_card_news.models.issue import IssueCandidate, IssueScore


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


class ScoringEngine(Protocol):
    def score(self, candidate: IssueCandidate) -> IssueScore:
        ...


class HttpTransport(Protocol):
    def post_json(self, url: str, payload: dict, headers: dict[str, str]) -> dict:
        ...


class UrllibHttpTransport:
    def __init__(self, timeout_seconds: int = 180) -> None:
        self.timeout_seconds = timeout_seconds

    def post_json(self, url: str, payload: dict, headers: dict[str, str]) -> dict:
        request = urllib.request.Request(
            url=url,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
            body = response.read().decode("utf-8")
        return json.loads(body)


class HeuristicIssueScoringEngine:
    def __init__(
        self,
        *,
        model_name: str = "heuristic-kbo-v1",
        prompt_version: str = "phase3.1",
        publish_threshold: float = 80.0,
    ) -> None:
        self.model_name = model_name
        self.prompt_version = prompt_version
        self.publish_threshold = publish_threshold

    def score(self, candidate: IssueCandidate) -> IssueScore:
        fun_score, fun_reasons = self._score_fun(candidate)
        timeliness_score, timeliness_reasons = self._score_timeliness(candidate)
        info_score, info_reasons = self._score_info(candidate)
        safety_score, safety_reasons = self._score_safety(candidate)

        weighted_total = (
            fun_score * 0.35
            + timeliness_score * 0.40
            + info_score * 0.25
        )
        safety_penalty = max(0.0, 60.0 - safety_score) * 0.35
        total_score = max(0.0, min(100.0, round(weighted_total - safety_penalty, 2)))
        should_publish = total_score >= self.publish_threshold and safety_score >= 50.0

        reason_summary = "; ".join(
            [
                f"fun: {', '.join(fun_reasons)}",
                f"timeliness: {', '.join(timeliness_reasons)}",
                f"info: {', '.join(info_reasons)}",
                f"safety: {', '.join(safety_reasons)}",
            ]
        )
        scoring_payload = {
            "candidate": asdict(candidate),
            "weights": {
                "fun": 0.35,
                "timeliness": 0.40,
                "info": 0.25,
                "safety_penalty_factor": 0.35,
            },
            "subscores": {
                "fun_score": fun_score,
                "timeliness_score": timeliness_score,
                "info_score": info_score,
                "safety_score": safety_score,
            },
            "threshold": self.publish_threshold,
            "engine_type": "heuristic",
        }
        return IssueScore(
            issue_id=candidate.issue_id,
            model_name=self.model_name,
            prompt_version=self.prompt_version,
            fun_score=fun_score,
            timeliness_score=timeliness_score,
            info_score=info_score,
            safety_score=safety_score,
            total_score=total_score,
            should_publish=should_publish,
            reason_summary=reason_summary,
            scoring_payload=scoring_payload,
            created_at=_utc_now(),
        )

    def _score_fun(self, candidate: IssueCandidate) -> tuple[float, list[str]]:
        text = f"{candidate.title} {candidate.summary}".lower()
        reasons: list[str] = []
        score = 45.0

        engagement_signal = candidate.engagement_like_count + candidate.engagement_comment_count * 2
        score += min(25.0, math.log10(engagement_signal + 1) * 10)
        if engagement_signal > 0:
            reasons.append("engagement reaction detected")

        fun_keywords = [
            "끝내기", "역전", "대기록", "연장", "만루홈런", "홈런", "호수비", "벤치클리어링",
            "walkoff", "grand slam", "comeback",
        ]
        if any(keyword in text for keyword in fun_keywords):
            score += 18.0
            reasons.append("high-drama keyword matched")

        if candidate.asset_count > 0:
            score += 6.0
            reasons.append("visual asset available")

        return min(100.0, round(score, 2)), reasons or ["baseline fun score"]

    def _score_timeliness(self, candidate: IssueCandidate) -> tuple[float, list[str]]:
        reference_at = candidate.published_at or candidate.collected_at
        normalized_reference_at = _normalize_datetime(reference_at)
        age_hours = max(0.0, (_utc_now() - normalized_reference_at).total_seconds() / 3600)
        score = 100.0 - min(80.0, age_hours * 6.0)
        reasons = [f"age_hours={round(age_hours, 2)}"]

        if age_hours <= 3:
            score += 6.0
            reasons.append("fresh issue")
        elif age_hours >= 24:
            reasons.append("stale issue")

        return max(0.0, min(100.0, round(score, 2))), reasons

    def _score_info(self, candidate: IssueCandidate) -> tuple[float, list[str]]:
        text = f"{candidate.title} {candidate.summary}"
        reasons: list[str] = []
        score = 35.0

        if candidate.source_type in {"kbo_stats", "kma_weather"}:
            score += 30.0
            reasons.append("official structured source")

        if candidate.asset_count > 0:
            score += 5.0
            reasons.append("supporting asset available")

        stat_matches = re.findall(r"\d+", text)
        if stat_matches:
            score += min(20.0, len(stat_matches) * 4.0)
            reasons.append("contains numeric detail")

        if len(text.strip()) >= 80:
            score += 10.0
            reasons.append("enough descriptive context")

        return min(100.0, round(score, 2)), reasons or ["baseline info score"]

    def _score_safety(self, candidate: IssueCandidate) -> tuple[float, list[str]]:
        text = f"{candidate.title} {candidate.summary}".lower()
        reasons = ["default safe"]
        score = 85.0

        risky_keywords = [
            "루머", "찌라시", "폭행", "도박", "음주", "사망", "논란", "고소",
            "rumor", "gambling", "assault", "lawsuit",
        ]
        matched = [keyword for keyword in risky_keywords if keyword in text]
        if matched:
            score -= min(55.0, len(matched) * 18.0)
            reasons = [f"risk keyword matched: {', '.join(matched)}"]

        if candidate.source_type == "dcinside":
            score -= 12.0
            reasons.append("community source requires caution")

        return max(0.0, min(100.0, round(score, 2))), reasons


class GeminiIssueScoringEngine:
    def __init__(
        self,
        *,
        api_key: str | None = None,
        model_name: str = "gemini-2.5-flash-lite",
        prompt_version: str = "phase3.1",
        publish_threshold: float = 80.0,
        transport: HttpTransport | None = None,
        endpoint_base: str = "https://generativelanguage.googleapis.com/v1beta/models",
    ) -> None:
        load_default_env()
        self.api_key = api_key or os.getenv("GEMINI_API_KEY")
        self.model_name = model_name
        self.prompt_version = prompt_version
        self.publish_threshold = publish_threshold
        self.transport = transport or UrllibHttpTransport()
        self.endpoint_base = endpoint_base.rstrip("/")

    def score(self, candidate: IssueCandidate) -> IssueScore:
        if not self.api_key:
            raise ValueError("GEMINI_API_KEY is required for GeminiIssueScoringEngine")

        request_payload = self._build_request_payload(candidate)
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
        except (TimeoutError, socket.timeout) as exc:
            raise RuntimeError("Gemini API request timed out while waiting for a response") from exc

        parsed = self._parse_response_payload(response_payload)
        total_score = max(0.0, min(100.0, round(float(parsed["total_score"]), 2)))
        safety_score = max(0.0, min(100.0, round(float(parsed["safety_score"]), 2)))
        should_publish = bool(parsed["should_publish"])
        if total_score < self.publish_threshold or safety_score < 50.0:
            should_publish = False

        return IssueScore(
            issue_id=candidate.issue_id,
            model_name=self.model_name,
            prompt_version=self.prompt_version,
            fun_score=round(float(parsed["fun_score"]), 2),
            timeliness_score=round(float(parsed["timeliness_score"]), 2),
            info_score=round(float(parsed["info_score"]), 2),
            safety_score=safety_score,
            total_score=total_score,
            should_publish=should_publish,
            reason_summary=str(parsed["reason_summary"]),
            scoring_payload={
                "candidate": asdict(candidate),
                "request": request_payload,
                "response": response_payload,
                "engine_type": "gemini",
                "threshold": self.publish_threshold,
            },
            created_at=_utc_now(),
        )

    def _build_request_payload(self, candidate: IssueCandidate) -> dict:
        candidate_payload = json.dumps(
            {
                **asdict(candidate),
                "published_at": _serialize_datetime(candidate.published_at),
                "collected_at": _serialize_datetime(candidate.collected_at),
            },
            ensure_ascii=False,
            indent=2,
        )
        return {
            "system_instruction": {
                "parts": [
                    {
                        "text": (
                            "You are scoring KBO card news issue candidates. "
                            "Return only JSON following the schema. "
                            "All numeric scores must be between 0 and 100. "
                            "Be conservative on safety. "
                            "Set should_publish true only when the issue is newsworthy and safe enough."
                        )
                    }
                ]
            },
            "contents": [
                {
                    "role": "user",
                    "parts": [
                        {
                            "text": (
                                "Score this issue candidate for card-news publication.\n"
                                "Fields:\n"
                                "- fun_score\n"
                                "- timeliness_score\n"
                                "- info_score\n"
                                "- safety_score\n"
                                "- total_score\n"
                                "- should_publish\n"
                                "- reason_summary\n\n"
                                "Issue candidate JSON:\n"
                                f"{candidate_payload}"
                            )
                        }
                    ],
                }
            ],
            "generationConfig": {
                "temperature": 0.2,
                "responseMimeType": "application/json",
                "responseSchema": {
                    "type": "OBJECT",
                    "properties": {
                        "fun_score": {"type": "NUMBER"},
                        "timeliness_score": {"type": "NUMBER"},
                        "info_score": {"type": "NUMBER"},
                        "safety_score": {"type": "NUMBER"},
                        "total_score": {"type": "NUMBER"},
                        "should_publish": {"type": "BOOLEAN"},
                        "reason_summary": {"type": "STRING"},
                    },
                    "required": [
                        "fun_score",
                        "timeliness_score",
                        "info_score",
                        "safety_score",
                        "total_score",
                        "should_publish",
                        "reason_summary",
                    ],
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

        required = {
            "fun_score",
            "timeliness_score",
            "info_score",
            "safety_score",
            "total_score",
            "should_publish",
            "reason_summary",
        }
        missing = sorted(required - set(parsed))
        if missing:
            raise RuntimeError(f"Gemini response missing fields: {', '.join(missing)}")
        return parsed


class IssueScoringService:
    def __init__(self, engine: ScoringEngine | None = None) -> None:
        self.engine = engine or build_default_engine()

    def score_candidates(self, candidates: list[IssueCandidate]) -> list[IssueScore]:
        return [self.engine.score(candidate) for candidate in candidates]


def build_default_engine() -> ScoringEngine:
    load_default_env()
    if os.getenv("GEMINI_API_KEY"):
        return GeminiIssueScoringEngine()
    return HeuristicIssueScoringEngine()


def _normalize_datetime(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _serialize_datetime(value: datetime | None) -> str | None:
    if value is None:
        return None
    return _normalize_datetime(value).isoformat()
