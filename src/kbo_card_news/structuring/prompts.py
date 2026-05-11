from __future__ import annotations


STRUCTURING_SYSTEM_PROMPT = (
    "You are structuring KBO card news into either a standard_5p or compact_2p draft. "
    "Return only JSON. Keep Korean copy concise and readable for Instagram cards. "
    "Use collected assets only as references, and avoid overclaiming uncertain facts. "
    "You must preserve factual accuracy from the input JSON. "
    "Do not invent scores, score gaps, records, rankings, innings, players, or statistics that are not explicitly present in the input. "
    "If a number or fact is uncertain, omit it instead of guessing. "
    "For baseball score expressions, only use score formats that are explicitly present in the input text. "
    "Template rules are strict: weather or breaking issues must use compact_2p with exactly 2 pages. "
    "All other issues must use standard_5p with exactly 5 pages unless the input explicitly says otherwise."
)


STRUCTURING_USER_PROMPT_TEMPLATE = """Build a card-news draft with fields:
- template_name
- title
- subtitle
- planning_notes
- pages[{{page_number,page_role,headline,body,image_prompt,asset_reference}}]

Hard rules:
- Use only facts that appear in the input JSON.
- Never invent score gaps such as '0.5점 차' or any unsupported baseball expression.
- Never rewrite explicit numbers into new numbers.
- If a fact is not in the input, leave it vague instead of fabricating it.

Template selection rules:
- If source_type is kma_weather or issue_category is weather or breaking, template_name must be compact_2p.
- compact_2p must return exactly 2 pages.
- standard_5p must return exactly 5 pages.
- For this input, the required template_name is {expected_template_name}.
- For this input, the required page roles in order are: {expected_page_roles}.
- Do not return any other template or page count.

Input JSON:
{input_json}
"""


def build_structuring_user_prompt(
    input_json: str,
    *,
    expected_template_name: str,
    expected_page_roles: str,
) -> str:
    return STRUCTURING_USER_PROMPT_TEMPLATE.format(
        input_json=input_json,
        expected_template_name=expected_template_name,
        expected_page_roles=expected_page_roles,
    )
