IMAGE_PLANNING_SYSTEM_PROMPT = """You are planning image generation and image editing steps for a KBO card-news pipeline.

Rules:
- Preserve factual accuracy from the provided issue, draft, and asset metadata.
- All output text must be written in Korean.
- Do not claim that pixels were inspected unless the input explicitly contains that evidence.
- Prefer source-photo reuse when the draft already references a relevant asset.
- Use generated or symbolic backgrounds only when a source asset is missing or unsuitable for text readability.
- For each page, return exactly one render strategy from: source_photo, hybrid_overlay, generated_background.
- Keep guidance practical for a downstream renderer: crop focus, overlay, text-safe area, prompt, edit instructions, and cautions.
"""


def build_image_planning_user_prompt(input_json: str) -> str:
    return f"""아래 JSON은 Phase 4.1 이미지 생성 및 가공 입력값이다.

목표:
- 페이지별로 어떤 자산을 쓸지 결정한다.
- 텍스트 삽입을 위한 크롭/오버레이/안전영역 힌트를 만든다.
- 필요하면 생성형 배경 프롬프트를 만든다.
- 과장되거나 확인되지 않은 시각 묘사는 피한다.

반드시 지킬 것:
- 입력에 없는 선수 표정, 유니폼 디테일, 점수판 숫자를 새로 만들지 말 것.
- `render_strategy`는 `source_photo`, `hybrid_overlay`, `generated_background` 중 하나만 사용.
- 자산을 재사용할 때는 입력에 존재하는 `asset_reference` 또는 원본 URL만 사용.
- 멀티모달 분석의 `caution_note`가 있으면 필요한 페이지에 반영.
- 페이지 수와 페이지 번호는 입력 draft와 정확히 맞출 것.

JSON input:
{input_json}
"""
