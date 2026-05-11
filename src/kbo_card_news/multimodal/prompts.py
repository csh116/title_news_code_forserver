from __future__ import annotations

import json


MULTIMODAL_TAG_DICTIONARY = {
    "subject_tags": [
        "KIA타이거즈",
        "삼성라이온즈",
        "LG트윈스",
        "두산베어스",
        "KT위즈",
        "SSG랜더스",
        "롯데자이언츠",
        "한화이글스",
        "NC다이노스",
        "키움히어로즈",
        "타자",
        "투수",
        "포수",
        "내야수",
        "외야수",
        "감독",
        "코치",
        "관중",
        "응원단",
        "심판",
    ],
    "event_tags": [
        "안타",
        "적시타",
        "결승타",
        "홈런",
        "투런홈런",
        "만루홈런",
        "끝내기",
        "득점",
        "역전",
        "추가득점",
        "삼진",
        "호투",
        "세이브",
        "실점",
        "호수비",
        "병살",
        "송구",
        "세리머니",
        "환영",
        "벤치반응",
        "작전지시",
        "인터뷰",
        "경기전",
        "경기후",
        "더그아웃",
        "응원",
        "우천중단",
        "결정적순간",
        "하이라이트장면",
        "리액션장면",
    ],
    "emotion_tags": [
        "환호",
        "기쁨",
        "포효",
        "열광",
        "흥분",
        "자신감",
        "집중",
        "긴장",
        "비장함",
        "신중함",
        "침울",
        "아쉬움",
        "당황",
        "허탈",
        "분노",
        "실망",
        "엄숙함",
        "유쾌함",
    ],
    "composition_tags": [
        "클로즈업",
        "상반신",
        "전신",
        "다중인물",
        "단체샷",
        "액션샷",
        "정지장면",
        "포즈중심",
        "중앙구도",
        "측면구도",
        "관중포함",
        "더그아웃배경",
        "그라운드배경",
        "전광판포함",
        "배경복잡",
        "배경단순",
        "텍스트안전영역넓음",
        "텍스트안전영역보통",
        "텍스트안전영역좁음",
        "타이틀컷적합",
        "본문컷적합",
        "리액션컷적합",
    ],
    "risk_tags": [
        "인물식별불확실",
        "팀식별불확실",
        "상황해석불확실",
        "이벤트판단불확실",
        "ocr불명확",
        "캡션근거약함",
        "메타데이터부족",
        "저해상도",
        "움직임흐림",
        "부분가림",
        "원거리장면",
        "gif해석주의",
        "중복컷가능성",
        "저작권주의",
        "과한추정주의",
    ],
    "usage_recommendation": [
        "cover",
        "detail_a",
        "detail_b",
        "reaction",
        "data_context",
        "quick_info",
        "summary_cta",
    ],
}


MULTIMODAL_SYSTEM_PROMPT = (
    "You are analyzing KBO card-news visual assets for structured editorial selection. "
    "Return only JSON. "
    "All output text must be written in Korean unless the input explicitly requires a quoted English phrase. "
    "Use only the provided asset metadata, captions, OCR text, issue context, and any attached image inputs. "
    "Do not claim to have visually inspected pixels unless the request includes actual image inputs or explicit visual descriptions. "
    "Preserve factual accuracy and avoid inventing players, scores, or events not present in the input. "
    "For each asset, classify it using only the allowed tag dictionary, assign exactly one usage recommendation, "
    "and keep scene_description and humor_point short compatibility fields derived from the tags."
)


MULTIMODAL_USER_PROMPT_TEMPLATE = """Analyze the available KBO issue assets and return JSON with:
- overall_summary
- assets[{{asset_reference,asset_type,scene_description,humor_point,usage_recommendation,subject_tags,event_tags,emotion_tags,composition_tags,risk_tags,tag_summary,caution_note,confidence}}]

Allowed tag dictionary JSON:
{allowed_tag_dictionary_json}

Hard rules:
- Rely only on provided asset metadata, text hints, and attached image inputs.
- Use only tags from the allowed tag dictionary. Do not create any new tag.
- If OCR or caption is weak, reflect that in risk_tags and caution_note instead of fabricating certainty.
- Keep scene_description grounded and concise. It must stay consistent with tag_summary.
- humor_point should be safe for sports-card-news tone and must not introduce facts outside the selected tags.
- usage_recommendation should map to likely card-news slots such as cover, detail_a, detail_b, reaction, data_context, quick_info, or summary_cta.
- tag_summary must be a short Korean sentence derived from the selected tags.
- overall_summary must be at most 2 short Korean sentences and should summarize the whole asset set only once without repetition.
- If event_tags is empty, do not write a concrete event like 홈런, 결승타, 인터뷰, 세리머니 in tag_summary or scene_description.
- If subject_tags contains only team or role tags, do not invent a real player name in tag_summary, scene_description, humor_point, or caution_note.
- Prefer cover, detail_a, detail_b, reaction for photo assets. Use data_context or quick_info only when the asset is clearly more suitable for information/support context than for a main visual.
- Prefer fewer tags over speculative tags when evidence is weak.
- Write overall_summary, scene_description, humor_point, tag_summary, and caution_note in Korean.
- If there is no meaningful caution_note, return an empty string instead of generic filler like "No specific cautions noted."
- In this call, you must return exactly one asset entry for each of these asset_reference values: {expected_asset_references}.
- Do not omit any listed asset_reference.
- Do not return any extra asset_reference outside the listed set.
{memory_instruction}

Input JSON:
{input_json}
"""


def build_multimodal_user_prompt(
    input_json: str,
    *,
    expected_asset_references: str,
    has_memory_context: bool = False,
) -> str:
    memory_instruction = ""
    if has_memory_context:
        memory_instruction = (
            "\nOptional memory guidance:\n"
            "- The input may include memory_context_summary or memory_context_by_asset from similar past human edits.\n"
            "- Treat them as soft editorial hints only.\n"
            "- Current asset metadata, OCR, captions, and attached image evidence always take priority.\n"
            "- Do not copy old wording verbatim unless it naturally fits the current asset evidence.\n"
            "- If memory guidance conflicts with the current input, ignore the memory guidance.\n"
        )
    return MULTIMODAL_USER_PROMPT_TEMPLATE.format(
        input_json=input_json,
        expected_asset_references=expected_asset_references,
        memory_instruction=memory_instruction,
        allowed_tag_dictionary_json=json.dumps(MULTIMODAL_TAG_DICTIONARY, ensure_ascii=False, indent=2),
    )
