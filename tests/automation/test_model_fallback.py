from __future__ import annotations

import unittest

from kbo_card_news.runtime.model_fallback import call_openai


class _CaptureTransport:
    def __init__(self) -> None:
        self.payload: dict | None = None

    def post_json(self, url: str, payload: dict, headers: dict[str, str]) -> dict:
        self.payload = payload
        return {"output_text": '{"ok": "yes"}'}


class ModelFallbackTest(unittest.TestCase):
    def test_openai_call_adds_strict_schema_requirements_recursively(self) -> None:
        transport = _CaptureTransport()
        schema = {
            "type": "object",
            "properties": {
                "items": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "name": {"type": "string"},
                        },
                        "required": ["name"],
                    },
                },
            },
            "required": ["items"],
        }

        call_openai(
            model_name="gpt-4o-mini",
            prompt="Return JSON.",
            schema_name="test_schema",
            json_schema=schema,
            transport=transport,
            api_key="test-key",
            endpoint="https://example.test/responses",
        )

        assert transport.payload is not None
        strict_schema = transport.payload["text"]["format"]["schema"]
        nested = strict_schema["properties"]["items"]["items"]

        self.assertIs(strict_schema["additionalProperties"], False)
        self.assertEqual(strict_schema["required"], ["items"])
        self.assertIs(nested["additionalProperties"], False)
        self.assertEqual(nested["required"], ["name"])
        self.assertNotIn("additionalProperties", schema)


if __name__ == "__main__":
    unittest.main()
