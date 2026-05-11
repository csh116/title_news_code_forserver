from __future__ import annotations

import json
import socket
import time
import urllib.error
from dataclasses import dataclass
from typing import Any, Callable, Protocol, Sequence

RETRYABLE_HTTP_STATUS_CODES = {429, 500, 502, 503, 504}
GEMINI_MODEL_FALLBACKS = {
    "gemini-2.5-flash-lite": ["gemini-3.1-flash-lite-preview"],
    "gemini-2.5-flash": ["gemini-3-flash-preview"],
}
OPENAI_FINAL_FALLBACK_MODEL = "gpt-4o"


class RetryableModelError(RuntimeError):
    pass


class NonRetryableModelError(RuntimeError):
    pass


class PromptBuilder(Protocol):
    def __call__(self, input_data: Any) -> str:
        ...


class HttpTransport(Protocol):
    def post_json(self, url: str, payload: dict, headers: dict[str, str]) -> dict:
        ...


class ParsedValidator(Protocol):
    def __call__(self, parsed: dict[str, Any]) -> None:
        ...


@dataclass(frozen=True)
class ModelCallResult:
    model_name: str
    prompt: str
    response_text: str
    parsed: dict[str, Any]
    provider: str


def build_prompt(input_data: Any, *, prompt_builder: PromptBuilder) -> str:
    return prompt_builder(input_data)


def parse_json(response: str) -> dict[str, Any]:
    try:
        parsed = json.loads(response)
    except json.JSONDecodeError as exc:
        raise NonRetryableModelError(f"Model response was not valid JSON: {response}") from exc
    if not isinstance(parsed, dict):
        raise NonRetryableModelError(f"Model response must be a JSON object: {parsed}")
    return parsed


def is_retryable_error(error: Exception) -> bool:
    return isinstance(error, RetryableModelError)


def build_model_fallback_policy(primary_model: str) -> list[str]:
    policy: list[str] = []
    candidates = [primary_model, *GEMINI_MODEL_FALLBACKS.get(primary_model, [])]
    if str(primary_model).startswith("gemini"):
        candidates.append(OPENAI_FINAL_FALLBACK_MODEL)
    for candidate in candidates:
        normalized = str(candidate).strip()
        if not normalized or normalized in policy:
            continue
        policy.append(normalized)
    return policy


def call_model(
    model_name: str,
    prompt: str,
    *,
    schema_name: str,
    json_schema: dict[str, Any],
    transport: HttpTransport,
    gemini_api_key: str | None = None,
    openai_api_key: str | None = None,
    gemini_endpoint_base: str = "https://generativelanguage.googleapis.com/v1beta/models",
    openai_endpoint: str = "https://api.openai.com/v1/responses",
) -> str:
    if model_name.startswith("gemini"):
        return call_gemini(
            model_name=model_name,
            prompt=prompt,
            schema_name=schema_name,
            json_schema=json_schema,
            transport=transport,
            api_key=gemini_api_key,
            endpoint_base=gemini_endpoint_base,
        )
    if model_name.startswith("gpt"):
        return call_openai(
            model_name=model_name,
            prompt=prompt,
            schema_name=schema_name,
            json_schema=json_schema,
            transport=transport,
            api_key=openai_api_key,
            endpoint=openai_endpoint,
        )
    raise ValueError("Unsupported model")


def call_with_fallback(
    models: Sequence[str],
    input_data: Any,
    *,
    prompt_builder: PromptBuilder,
    schema_name: str,
    json_schema: dict[str, Any],
    transport: HttpTransport,
    gemini_api_key: str | None = None,
    openai_api_key: str | None = None,
    gemini_endpoint_base: str = "https://generativelanguage.googleapis.com/v1beta/models",
    openai_endpoint: str = "https://api.openai.com/v1/responses",
    validator: ParsedValidator | None = None,
    max_attempts_per_model: int = 3,
    retry_delay_seconds: float = 1.0,
) -> ModelCallResult:
    prompt = build_prompt(input_data, prompt_builder=prompt_builder)
    last_error: Exception | None = None
    total_models = len(models)
    normalized_max_attempts = max(1, max_attempts_per_model)
    normalized_retry_delay = max(0.0, retry_delay_seconds)

    for model_index, model in enumerate(models):
        for attempt in range(normalized_max_attempts):
            try:
                print(f"[MODEL TRY] {model}")
                response = call_model(
                    model_name=model,
                    prompt=prompt,
                    schema_name=schema_name,
                    json_schema=json_schema,
                    transport=transport,
                    gemini_api_key=gemini_api_key,
                    openai_api_key=openai_api_key,
                    gemini_endpoint_base=gemini_endpoint_base,
                    openai_endpoint=openai_endpoint,
                )
                parsed = parse_json(response)
                if validator is not None:
                    validator(parsed)
                return ModelCallResult(
                    model_name=model,
                    prompt=prompt,
                    response_text=response,
                    parsed=parsed,
                    provider="gemini" if model.startswith("gemini") else "openai",
                )
            except Exception as exc:
                last_error = exc
                if is_retryable_error(exc) and attempt < normalized_max_attempts - 1:
                    print(f"[RETRY] {model} attempt {attempt + 1}")
                    if normalized_retry_delay > 0:
                        time.sleep(normalized_retry_delay * (attempt + 1))
                    continue
                break

        if model_index < total_models - 1:
            print(f"[FALLBACK] switching model")

    raise RuntimeError("All models failed") from last_error


def call_gemini(
    *,
    model_name: str,
    prompt: str,
    schema_name: str,
    json_schema: dict[str, Any],
    transport: HttpTransport,
    api_key: str | None,
    endpoint_base: str,
) -> str:
    if not api_key:
        raise NonRetryableModelError("GEMINI_API_KEY is required")

    payload = {
        "contents": [
            {
                "role": "user",
                "parts": [{"text": prompt}],
            }
        ],
        "generationConfig": {
            "temperature": 0.2,
            "responseMimeType": "application/json",
            "responseSchema": _convert_schema_types(json_schema, upper=True),
        },
    }
    headers = {
        "Content-Type": "application/json",
        "x-goog-api-key": api_key,
    }
    url = f"{endpoint_base.rstrip('/')}/{model_name}:generateContent"
    try:
        response_payload = transport.post_json(url, payload, headers)
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        message = f"Gemini API request failed: HTTP {exc.code} {detail}"
        if exc.code in RETRYABLE_HTTP_STATUS_CODES:
            raise RetryableModelError(message) from exc
        raise NonRetryableModelError(message) from exc
    except urllib.error.URLError as exc:
        raise RetryableModelError(f"Gemini API request failed: {exc.reason}") from exc
    except (TimeoutError, socket.timeout) as exc:
        raise RetryableModelError("Gemini request timed out while waiting for a response") from exc

    try:
        return str(response_payload["candidates"][0]["content"]["parts"][0]["text"])
    except (KeyError, IndexError, TypeError) as exc:
        raise NonRetryableModelError(f"Unexpected Gemini response shape: {response_payload}") from exc


def call_openai(
    *,
    model_name: str,
    prompt: str,
    schema_name: str,
    json_schema: dict[str, Any],
    transport: HttpTransport,
    api_key: str | None,
    endpoint: str,
) -> str:
    if not api_key:
        raise NonRetryableModelError("OPENAI_API_KEY is required")

    payload = {
        "model": model_name,
        "input": [
            {
                "role": "user",
                "content": [{"type": "input_text", "text": prompt}],
            }
        ],
        "text": {
            "format": {
                "type": "json_schema",
                "name": schema_name,
                "strict": True,
                "schema": json_schema,
            }
        },
    }
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {api_key}",
    }
    try:
        response_payload = transport.post_json(endpoint, payload, headers)
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        message = f"OpenAI API request failed: HTTP {exc.code} {detail}"
        if exc.code in RETRYABLE_HTTP_STATUS_CODES:
            raise RetryableModelError(message) from exc
        raise NonRetryableModelError(message) from exc
    except urllib.error.URLError as exc:
        raise RetryableModelError(f"OpenAI API request failed: {exc.reason}") from exc
    except (TimeoutError, socket.timeout) as exc:
        raise RetryableModelError("OpenAI request timed out while waiting for a response") from exc

    output_text = response_payload.get("output_text")
    if isinstance(output_text, str) and output_text.strip():
        return output_text

    fragments: list[str] = []
    for output_item in response_payload.get("output") or []:
        if not isinstance(output_item, dict):
            continue
        for content_item in output_item.get("content") or []:
            if isinstance(content_item, dict) and isinstance(content_item.get("text"), str):
                fragments.append(content_item["text"])
    if fragments:
        return "\n".join(fragments)
    raise NonRetryableModelError(f"Unexpected OpenAI response shape: {response_payload}")


def _convert_schema_types(value: Any, *, upper: bool) -> Any:
    if isinstance(value, dict):
        converted: dict[str, Any] = {}
        for key, item in value.items():
            if key == "additionalProperties":
                continue
            if key == "type" and isinstance(item, str):
                converted[key] = item.upper() if upper else item.lower()
            else:
                converted[key] = _convert_schema_types(item, upper=upper)
        return converted
    if isinstance(value, list):
        return [_convert_schema_types(item, upper=upper) for item in value]
    return value
