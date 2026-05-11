from __future__ import annotations

from datetime import datetime, timezone
from html import escape
from typing import Protocol

from kbo_card_news.models.issue import (
    CardDesignAutomationInput,
    CardDesignBundle,
    CardDesignConsistencyPageResult,
    CardDesignConsistencyReport,
    CardDesignPage,
    CardImagePlanPage,
    CardNewsPageDraft,
)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


class CardDesignAutomationEngine(Protocol):
    def build_bundle(self, automation_input: CardDesignAutomationInput) -> CardDesignBundle:
        ...


class HeuristicCardDesignAutomationEngine:
    def __init__(
        self,
        *,
        width: int = 1080,
        height: int = 1080,
        template_version: str = "standard_5p_v1",
    ) -> None:
        self.width = width
        self.height = height
        self.template_version = template_version

    def build_bundle(self, automation_input: CardDesignAutomationInput) -> CardDesignBundle:
        draft = automation_input.draft
        image_plan = automation_input.image_plan
        _validate_alignment(draft.pages, image_plan.pages)

        team_code = str(draft.metadata.get("team_code", "KBO")).strip() or "KBO"
        theme = _resolve_theme(team_code)
        pages = [
            self._build_page(draft_page, plan_page, draft.template_name, theme)
            for draft_page, plan_page in zip(draft.pages, image_plan.pages)
        ]
        html_document = _render_preview_document(
            issue_id=draft.issue_id,
            team_code=team_code,
            theme=theme,
            template_name=draft.template_name,
            overall_art_direction=image_plan.overall_art_direction,
            pages=pages,
        )
        return CardDesignBundle(
            issue_id=draft.issue_id,
            template_name=draft.template_name,
            team_code=team_code,
            width=self.width,
            height=self.height,
            overall_art_direction=image_plan.overall_art_direction,
            pages=pages,
            html_document=html_document,
            metadata={
                "template_version": _resolve_template_version(
                    template_name=draft.template_name,
                    default_version=self.template_version,
                ),
                "theme": theme,
                "page_count": len(pages),
            },
            created_at=_utc_now(),
        )

    def _build_page(
        self,
        draft_page: CardNewsPageDraft,
        plan_page: CardImagePlanPage,
        template_name: str,
        theme: dict[str, str],
    ) -> CardDesignPage:
        layout_variant = _resolve_layout_variant(template_name, draft_page.page_role)
        component_order = _resolve_component_order(draft_page.page_role)
        background_style = _resolve_background_style(plan_page.render_strategy, theme)
        html_fragment = _render_page_fragment(
            draft_page=draft_page,
            plan_page=plan_page,
            layout_variant=layout_variant,
            component_order=component_order,
            theme=theme,
            background_style=background_style,
        )
        return CardDesignPage(
            page_number=draft_page.page_number,
            page_role=draft_page.page_role,
            layout_variant=layout_variant,
            headline=draft_page.headline,
            body=draft_page.body,
            primary_asset_reference=plan_page.primary_asset_reference,
            render_strategy=plan_page.render_strategy,
            accent_color=theme["accent_primary"],
            background_style=background_style,
            component_order=component_order,
            html_fragment=html_fragment,
            metadata={
                "overlay_style": plan_page.overlay_style,
                "text_layout_hint": plan_page.text_layout_hint,
                "crop_focus": plan_page.crop_focus,
                "generation_prompt": plan_page.generation_prompt,
                "edit_instructions": plan_page.edit_instructions,
                "caution_note": plan_page.caution_note,
            },
        )


class CardDesignAutomationService:
    def __init__(self, engine: CardDesignAutomationEngine | None = None) -> None:
        self.engine = engine or HeuristicCardDesignAutomationEngine()

    def build_bundle(self, automation_input: CardDesignAutomationInput) -> CardDesignBundle:
        return self.engine.build_bundle(automation_input)


class CardDesignConsistencyEngine(Protocol):
    def review_bundle(self, bundle: CardDesignBundle) -> CardDesignConsistencyReport:
        ...


class HeuristicCardDesignConsistencyEngine:
    def review_bundle(self, bundle: CardDesignBundle) -> CardDesignConsistencyReport:
        theme = _resolve_theme(bundle.team_code)
        normalized_pages: list[CardDesignPage] = []
        page_results: list[CardDesignConsistencyPageResult] = []
        global_warnings = _collect_global_warnings(bundle)

        for page in bundle.pages:
            normalized_page, result = self._normalize_page(page, theme)
            normalized_pages.append(normalized_page)
            page_results.append(result)

        consistency_score = round(
            sum(result.quality_score for result in page_results) / max(len(page_results), 1), 1
        )
        passed = consistency_score >= 85 and not any(
            "구조" in warning or "초과" in warning
            for result in page_results
            for warning in result.warnings
        )

        consistent_bundle = CardDesignBundle(
            issue_id=bundle.issue_id,
            template_name=bundle.template_name,
            team_code=bundle.team_code,
            width=bundle.width,
            height=bundle.height,
            overall_art_direction=bundle.overall_art_direction,
            pages=normalized_pages,
            html_document=_render_preview_document(
                issue_id=bundle.issue_id,
                team_code=bundle.team_code,
                theme=theme,
                template_name=bundle.template_name,
                overall_art_direction=bundle.overall_art_direction,
                pages=normalized_pages,
                preview_label="Phase 4.3 Design Consistency Preview",
                hero_title_suffix="카드 디자인 일관성 점검 미리보기",
                consistency_summary={
                    "score": consistency_score,
                    "passed": passed,
                    "warning_count": sum(len(result.warnings) for result in page_results)
                    + len(global_warnings),
                },
            ),
            metadata={
                **bundle.metadata,
                "consistency_score": consistency_score,
                "consistency_passed": passed,
                "global_warnings": global_warnings,
            },
            created_at=_utc_now(),
        )
        return CardDesignConsistencyReport(
            issue_id=bundle.issue_id,
            team_code=bundle.team_code,
            passed=passed,
            consistency_score=consistency_score,
            bundle=consistent_bundle,
            pages=page_results,
            global_warnings=global_warnings,
            created_at=_utc_now(),
        )

    def _normalize_page(
        self,
        page: CardDesignPage,
        theme: dict[str, str],
    ) -> tuple[CardDesignPage, CardDesignConsistencyPageResult]:
        warnings: list[str] = []
        adjustments: list[str] = []
        headline_budget, body_budget = _resolve_copy_budget(page.page_role)
        headline_length = len(page.headline.strip())
        body_length = len(page.body.strip())
        text_density = _resolve_text_density(
            headline_length=headline_length,
            body_length=body_length,
            headline_budget=headline_budget,
            body_budget=body_budget,
        )

        normalized_overlay_style = _resolve_consistent_overlay_style(
            page_role=page.page_role,
            render_strategy=page.render_strategy,
        )
        if page.metadata.get("overlay_style") != normalized_overlay_style:
            adjustments.append("overlay_style_normalized")

        normalized_hint = _resolve_consistent_text_layout_hint(
            page_role=page.page_role,
            text_density=text_density,
        )
        if page.metadata.get("text_layout_hint") != normalized_hint:
            adjustments.append("text_layout_hint_normalized")

        normalized_components = _normalize_component_order(page.component_order)
        if normalized_components != page.component_order:
            warnings.append("구조 컴포넌트 순서를 표준 순서로 보정함")
            adjustments.append("component_order_normalized")

        normalized_background = _resolve_background_style(page.render_strategy, theme)
        if page.background_style != normalized_background:
            adjustments.append("background_style_normalized")

        if page.accent_color != theme["accent_primary"]:
            warnings.append("강조 색상이 팀 테마와 달라 표준 색상으로 정규화함")
            adjustments.append("accent_color_normalized")

        if headline_length > headline_budget:
            warnings.append(f"헤드라인 길이가 권장치({headline_budget})를 초과함")
        if body_length > body_budget:
            warnings.append(f"본문 길이가 권장치({body_budget})를 초과함")

        quality_score = _calculate_quality_score(
            warnings=warnings,
            adjustments=adjustments,
            text_density=text_density,
        )
        normalized_page = CardDesignPage(
            page_number=page.page_number,
            page_role=page.page_role,
            layout_variant=page.layout_variant,
            headline=page.headline,
            body=page.body,
            primary_asset_reference=page.primary_asset_reference,
            render_strategy=page.render_strategy,
            accent_color=theme["accent_primary"],
            background_style=normalized_background,
            component_order=normalized_components,
            html_fragment="",
            metadata={
                **page.metadata,
                "overlay_style": normalized_overlay_style,
                "text_layout_hint": normalized_hint,
                "text_density": text_density,
                "quality_score": quality_score,
                "quality_warnings": warnings,
                "quality_adjustments": adjustments,
            },
        )
        normalized_page.html_fragment = _render_page_fragment_from_design_page(normalized_page)
        return normalized_page, CardDesignConsistencyPageResult(
            page_number=page.page_number,
            page_role=page.page_role,
            quality_score=quality_score,
            text_density=text_density,
            warnings=warnings,
            adjustments=adjustments,
            metadata={
                "headline_length": headline_length,
                "body_length": body_length,
                "overlay_style": normalized_overlay_style,
                "text_layout_hint": normalized_hint,
            },
        )


class CardDesignConsistencyService:
    def __init__(self, engine: CardDesignConsistencyEngine | None = None) -> None:
        self.engine = engine or HeuristicCardDesignConsistencyEngine()

    def review_bundle(self, bundle: CardDesignBundle) -> CardDesignConsistencyReport:
        return self.engine.review_bundle(bundle)


def _validate_alignment(
    draft_pages: list[CardNewsPageDraft],
    plan_pages: list[CardImagePlanPage],
) -> None:
    if len(draft_pages) != len(plan_pages):
        raise RuntimeError(
            "design automation page alignment failed: "
            f"draft has {len(draft_pages)} pages but image plan has {len(plan_pages)} pages"
        )
    for draft_page, plan_page in zip(draft_pages, plan_pages):
        if draft_page.page_number != plan_page.page_number:
            raise RuntimeError(
                "design automation page-number alignment failed: "
                f"draft page {draft_page.page_number}, plan page {plan_page.page_number}"
            )
        if draft_page.page_role != plan_page.page_role:
            raise RuntimeError(
                "design automation page-role alignment failed: "
                f"draft role {draft_page.page_role}, plan role {plan_page.page_role}"
            )


def _resolve_theme(team_code: str) -> dict[str, str]:
    themes = {
        "LG": {
            "accent_primary": "#C30452",
            "accent_secondary": "#2F2A85",
            "bg_primary": "#FFF8FA",
            "bg_secondary": "#F7F1FF",
            "text_primary": "#171717",
            "text_inverse": "#FFFFFF",
            "line": "rgba(23, 23, 23, 0.12)",
        },
        "KIA": {
            "accent_primary": "#EA0029",
            "accent_secondary": "#06141F",
            "bg_primary": "#FFF8F8",
            "bg_secondary": "#F4F5F7",
            "text_primary": "#171717",
            "text_inverse": "#FFFFFF",
            "line": "rgba(23, 23, 23, 0.12)",
        },
        "DOOSAN": {
            "accent_primary": "#131230",
            "accent_secondary": "#C60C30",
            "bg_primary": "#F7F8FC",
            "bg_secondary": "#FFF6F8",
            "text_primary": "#171717",
            "text_inverse": "#FFFFFF",
            "line": "rgba(23, 23, 23, 0.12)",
        },
        "KBO": {
            "accent_primary": "#0B5FFF",
            "accent_secondary": "#0B1B3B",
            "bg_primary": "#F5F8FF",
            "bg_secondary": "#F4F7FB",
            "text_primary": "#171717",
            "text_inverse": "#FFFFFF",
            "line": "rgba(23, 23, 23, 0.12)",
        },
    }
    return themes.get(team_code.upper(), themes["KBO"])


def _resolve_template_version(*, template_name: str, default_version: str) -> str:
    versions = {
        "standard_5p": "standard_5p_v1",
        "compact_2p": "compact_2p_v1",
    }
    return versions.get(template_name, default_version)


def _resolve_layout_variant(template_name: str, page_role: str) -> str:
    layout_map = {
        ("standard_5p", "cover"): "hero_cover",
        ("standard_5p", "detail_a"): "story_split",
        ("standard_5p", "detail_b"): "reaction_split",
        ("standard_5p", "data_context"): "stat_focus",
        ("standard_5p", "summary_cta"): "closing_cta",
        ("compact_2p", "quick_info"): "compact_flash",
        ("compact_2p", "summary_cta"): "compact_cta",
    }
    return layout_map.get((template_name, page_role), "default_editorial")


def _resolve_component_order(page_role: str) -> list[str]:
    orders = {
        "cover": ["TeamHeader", "LogoBadge", "HeadlineBlock", "BodyBlock", "FooterPager"],
        "detail_a": ["TeamHeader", "HeadlineBlock", "BodyBlock", "QuoteBlock", "FooterPager"],
        "detail_b": ["TeamHeader", "HeadlineBlock", "BodyBlock", "QuoteBlock", "FooterPager"],
        "data_context": ["TeamHeader", "HeadlineBlock", "StatCard", "BodyBlock", "FooterPager"],
        "summary_cta": ["TeamHeader", "HeadlineBlock", "CtaBlock", "FooterPager"],
        "quick_info": ["TeamHeader", "HeadlineBlock", "StatCard", "BodyBlock", "FooterPager"],
    }
    return orders.get(page_role, ["TeamHeader", "HeadlineBlock", "BodyBlock", "FooterPager"])


def _resolve_background_style(render_strategy: str, theme: dict[str, str]) -> str:
    if render_strategy == "generated_background":
        return (
            f"linear-gradient(145deg, {theme['accent_secondary']} 0%, "
            f"{theme['accent_primary']} 100%)"
        )
    if render_strategy == "hybrid_overlay":
        return (
            "linear-gradient(180deg, rgba(12, 18, 28, 0.12) 0%, rgba(12, 18, 28, 0.72) 100%), "
            "radial-gradient(circle at top right, rgba(255, 255, 255, 0.18), transparent 30%)"
        )
    return (
        f"linear-gradient(180deg, {theme['bg_primary']} 0%, {theme['bg_secondary']} 100%)"
    )


def _render_page_fragment(
    *,
    draft_page: CardNewsPageDraft,
    plan_page: CardImagePlanPage,
    layout_variant: str,
    component_order: list[str],
    theme: dict[str, str],
    background_style: str,
) -> str:
    asset_reference = plan_page.primary_asset_reference or "생성형 또는 무자산 배경"
    caution_note = plan_page.caution_note or "없음"
    component_badges = "".join(
        f'<span class="component-pill">{escape(component)}</span>' for component in component_order
    )
    return (
        f'<article class="card-page layout-{escape(layout_variant)} strategy-{escape(plan_page.render_strategy)}" '
        f'style="--page-accent:{escape(theme["accent_primary"])};--page-bg:{escape(background_style)};">'
        f'<header class="card-page__top"><div><p class="card-page__eyebrow">Page {draft_page.page_number}</p>'
        f'<h2>{escape(draft_page.headline)}</h2></div>'
        f'<span class="card-page__role">{escape(draft_page.page_role)}</span></header>'
        f'<section class="card-page__hero"><div class="card-page__asset">'
        f'<p class="label">대표 자산</p><p>{escape(asset_reference)}</p></div>'
        f'<div class="card-page__copy"><p>{escape(draft_page.body)}</p>'
        f'<p class="hint">{escape(plan_page.text_layout_hint)}</p></div></section>'
        f'<section class="card-page__meta"><div><span class="label">레이아웃</span>'
        f'<strong>{escape(layout_variant)}</strong></div><div><span class="label">오버레이</span>'
        f'<strong>{escape(plan_page.overlay_style)}</strong></div><div><span class="label">크롭</span>'
        f'<strong>{escape(plan_page.crop_focus)}</strong></div></section>'
        f'<section class="card-page__components">{component_badges}</section>'
        f'<footer class="card-page__footer"><p>{escape(plan_page.edit_instructions)}</p>'
        f'<p class="caution">주의: {escape(caution_note)}</p></footer></article>'
    )


def _render_page_fragment_from_design_page(page: CardDesignPage) -> str:
    asset_reference = page.primary_asset_reference or "생성형 또는 무자산 배경"
    caution_note = str(page.metadata.get("caution_note") or "없음")
    overlay_style = str(page.metadata.get("overlay_style") or "기본 오버레이")
    crop_focus = str(page.metadata.get("crop_focus") or "중앙 정렬")
    text_layout_hint = str(page.metadata.get("text_layout_hint") or "기본 본문 블록")
    edit_instructions = str(page.metadata.get("edit_instructions") or "후속 편집 지시 없음")
    component_badges = "".join(
        f'<span class="component-pill">{escape(component)}</span>'
        for component in page.component_order
    )
    return (
        f'<article class="card-page layout-{escape(page.layout_variant)} strategy-{escape(page.render_strategy)}" '
        f'style="--page-accent:{escape(page.accent_color)};--page-bg:{escape(page.background_style)};">'
        f'<header class="card-page__top"><div><p class="card-page__eyebrow">Page {page.page_number}</p>'
        f'<h2>{escape(page.headline)}</h2></div>'
        f'<span class="card-page__role">{escape(page.page_role)}</span></header>'
        f'<section class="card-page__hero"><div class="card-page__asset">'
        f'<p class="label">대표 자산</p><p>{escape(asset_reference)}</p></div>'
        f'<div class="card-page__copy"><p>{escape(page.body)}</p>'
        f'<p class="hint">{escape(text_layout_hint)}</p></div></section>'
        f'<section class="card-page__meta"><div><span class="label">레이아웃</span>'
        f'<strong>{escape(page.layout_variant)}</strong></div><div><span class="label">오버레이</span>'
        f'<strong>{escape(overlay_style)}</strong></div><div><span class="label">크롭</span>'
        f'<strong>{escape(crop_focus)}</strong></div></section>'
        f'<section class="card-page__components">{component_badges}</section>'
        f'<footer class="card-page__footer"><p>{escape(edit_instructions)}</p>'
        f'<p class="caution">주의: {escape(caution_note)}</p></footer></article>'
    )


def _render_preview_document(
    *,
    issue_id: str,
    team_code: str,
    theme: dict[str, str],
    template_name: str,
    overall_art_direction: str,
    pages: list[CardDesignPage],
    preview_label: str = "Phase 4.2 Design Automation Preview",
    hero_title_suffix: str = "카드 템플릿 자동 합성 미리보기",
    consistency_summary: dict[str, object] | None = None,
) -> str:
    fragments = "\n".join(page.html_fragment for page in pages)
    consistency_box = ""
    if consistency_summary:
        consistency_box = (
            '<div class="summary-box"><dt>Consistency</dt>'
            f'<dd>{escape(str(consistency_summary["score"]))} / '
            f'{"PASS" if consistency_summary["passed"] else "CHECK"}</dd></div>'
            '<div class="summary-box"><dt>Warnings</dt>'
            f'<dd>{escape(str(consistency_summary["warning_count"]))}</dd></div>'
        )
    return f"""<!DOCTYPE html>
<html lang="ko">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Phase 4.2 Design Preview</title>
  <style>
    :root {{
      --bg-primary: {theme["bg_primary"]};
      --bg-secondary: {theme["bg_secondary"]};
      --ink: {theme["text_primary"]};
      --ink-inverse: {theme["text_inverse"]};
      --accent-primary: {theme["accent_primary"]};
      --accent-secondary: {theme["accent_secondary"]};
      --line: {theme["line"]};
      --shadow: 0 24px 60px rgba(23, 23, 23, 0.12);
      --radius: 28px;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      color: var(--ink);
      font-family: "Pretendard", "Apple SD Gothic Neo", "Noto Sans KR", sans-serif;
      background:
        radial-gradient(circle at top left, rgba(195, 4, 82, 0.10), transparent 28%),
        radial-gradient(circle at top right, rgba(47, 42, 133, 0.12), transparent 26%),
        linear-gradient(180deg, var(--bg-primary) 0%, var(--bg-secondary) 100%);
    }}
    .wrap {{
      width: min(1280px, calc(100% - 32px));
      margin: 0 auto;
      padding: 40px 0 72px;
    }}
    .hero {{
      padding: 32px;
      border-radius: 32px;
      background: rgba(255, 255, 255, 0.72);
      border: 1px solid var(--line);
      box-shadow: var(--shadow);
      margin-bottom: 28px;
    }}
    .hero h1 {{
      margin: 8px 0 12px;
      font-size: clamp(28px, 4vw, 44px);
    }}
    .summary-grid {{
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
      gap: 12px;
      margin-top: 18px;
    }}
    .summary-box {{
      padding: 16px 18px;
      background: rgba(255, 255, 255, 0.8);
      border: 1px solid var(--line);
      border-radius: 18px;
    }}
    .summary-box dt {{
      font-size: 12px;
      text-transform: uppercase;
      letter-spacing: 0.08em;
      color: rgba(23, 23, 23, 0.6);
      margin-bottom: 8px;
    }}
    .summary-box dd {{
      margin: 0;
      font-weight: 700;
    }}
    .page-grid {{
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(320px, 1fr));
      gap: 18px;
    }}
    .card-page {{
      min-height: 560px;
      padding: 24px;
      border-radius: var(--radius);
      border: 1px solid var(--line);
      box-shadow: var(--shadow);
      background: var(--page-bg);
      color: var(--ink);
      position: relative;
      overflow: visible;
    }}
    .strategy-generated_background {{
      color: var(--ink-inverse);
    }}
    .card-page__top, .card-page__hero, .card-page__meta, .card-page__footer {{
      position: relative;
      z-index: 1;
    }}
    .card-page__top {{
      display: flex;
      justify-content: space-between;
      gap: 12px;
      align-items: flex-start;
    }}
    .card-page__eyebrow, .label {{
      margin: 0 0 8px;
      font-size: 12px;
      font-weight: 700;
      letter-spacing: 0.08em;
      text-transform: uppercase;
      opacity: 0.72;
    }}
    .card-page__top h2 {{
      margin: 0;
      font-size: 28px;
      line-height: 1.15;
    }}
    .card-page__role {{
      padding: 8px 12px;
      border-radius: 999px;
      background: rgba(255, 255, 255, 0.18);
      border: 1px solid rgba(255, 255, 255, 0.22);
      font-size: 12px;
      font-weight: 700;
    }}
    .card-page__hero {{
      margin-top: 22px;
      display: grid;
      gap: 14px;
    }}
    .card-page__asset, .card-page__copy, .card-page__meta, .card-page__footer {{
      padding: 16px;
      border-radius: 18px;
      background: rgba(255, 255, 255, 0.78);
      border: 1px solid rgba(255, 255, 255, 0.18);
      backdrop-filter: blur(10px);
    }}
    .strategy-generated_background .card-page__asset,
    .strategy-generated_background .card-page__copy,
    .strategy-generated_background .card-page__meta,
    .strategy-generated_background .card-page__footer {{
      background: rgba(12, 18, 28, 0.28);
      border-color: rgba(255, 255, 255, 0.12);
    }}
    .card-page__copy p {{
      margin: 0;
      line-height: 1.55;
    }}
    .hint {{
      margin-top: 10px !important;
      font-size: 13px;
      opacity: 0.82;
    }}
    .card-page__meta {{
      display: grid;
      grid-template-columns: repeat(3, minmax(0, 1fr));
      gap: 10px;
      margin-top: 14px;
    }}
    .card-page__meta strong {{
      display: block;
      line-height: 1.4;
    }}
    .card-page__components {{
      display: flex;
      flex-wrap: wrap;
      gap: 8px;
      margin-top: 14px;
    }}
    .component-pill {{
      display: inline-flex;
      padding: 8px 12px;
      border-radius: 999px;
      background: rgba(255, 255, 255, 0.72);
      border: 1px solid var(--line);
      font-size: 12px;
      font-weight: 700;
    }}
    .card-page__footer {{
      margin-top: 14px;
    }}
    .card-page__footer p {{
      margin: 0;
      line-height: 1.5;
    }}
    .caution {{
      margin-top: 10px !important;
      opacity: 0.8;
    }}
    @media (max-width: 720px) {{
      .wrap {{
        width: min(100%, calc(100% - 20px));
        padding-top: 20px;
      }}
      .hero, .card-page {{
        padding: 20px;
      }}
      .card-page__meta {{
        grid-template-columns: 1fr;
      }}
    }}
  </style>
</head>
<body>
  <main class="wrap">
    <section class="hero">
      <p class="card-page__eyebrow">{escape(preview_label)}</p>
      <h1>{escape(team_code)} {escape(hero_title_suffix)}</h1>
      <p>{escape(overall_art_direction)}</p>
      <dl class="summary-grid">
        <div class="summary-box"><dt>Issue</dt><dd>{escape(issue_id)}</dd></div>
        <div class="summary-box"><dt>Template</dt><dd>{escape(template_name)}</dd></div>
        <div class="summary-box"><dt>Pages</dt><dd>{len(pages)}</dd></div>
        <div class="summary-box"><dt>Accent</dt><dd>{escape(theme["accent_primary"])}</dd></div>
        {consistency_box}
      </dl>
    </section>
    <section class="page-grid">
      {fragments}
    </section>
  </main>
</body>
</html>
"""


def _resolve_copy_budget(page_role: str) -> tuple[int, int]:
    budgets = {
        "cover": (24, 70),
        "detail_a": (26, 95),
        "detail_b": (26, 95),
        "data_context": (22, 60),
        "summary_cta": (22, 55),
        "quick_info": (24, 65),
    }
    return budgets.get(page_role, (24, 80))


def _resolve_text_density(
    *,
    headline_length: int,
    body_length: int,
    headline_budget: int,
    body_budget: int,
) -> str:
    if headline_length > headline_budget or body_length > body_budget:
        return "dense"
    if headline_length > int(headline_budget * 0.75) or body_length > int(body_budget * 0.75):
        return "balanced"
    return "airy"


def _resolve_consistent_overlay_style(*, page_role: str, render_strategy: str) -> str:
    if render_strategy == "generated_background":
        return "구단 컬러 중심 풀프레임 그라데이션"
    overlay_by_role = {
        "cover": "상단 타이틀 보호용 딥 그라데이션",
        "detail_a": "본문 가독성 우선 반투명 패널",
        "detail_b": "반응 문구 강조용 소프트 패널",
        "summary_cta": "CTA 대비 확보용 하단 패널",
        "quick_info": "속보형 정보 박스 오버레이",
    }
    return overlay_by_role.get(page_role, "기본 반투명 오버레이")


def _resolve_consistent_text_layout_hint(*, page_role: str, text_density: str) -> str:
    hint_map = {
        ("cover", "airy"): "상단 타이틀 2줄 + 짧은 서브카피",
        ("cover", "balanced"): "상단 타이틀 2줄 + 본문 2줄",
        ("cover", "dense"): "상단 타이틀 3줄 제한 + 본문 축약 필요",
        ("detail_a", "airy"): "본문 3줄 중심 설명 블록",
        ("detail_a", "balanced"): "본문 4줄 중심 설명 블록",
        ("detail_a", "dense"): "본문 4줄 제한 + 핵심 문장 우선",
        ("detail_b", "airy"): "반응 요약 2줄 + 짧은 보조카피",
        ("detail_b", "balanced"): "반응 요약 3줄 + 보조카피 1줄",
        ("detail_b", "dense"): "반응 요약 3줄 제한 + 문장 압축 필요",
        ("data_context", "airy"): "숫자 카드 중심 + 보조설명 1줄",
        ("data_context", "balanced"): "숫자 카드 중심 + 보조설명 2줄",
        ("data_context", "dense"): "숫자 카드 유지, 본문 최소화",
        ("summary_cta", "airy"): "마무리 한 줄 + CTA 한 줄",
        ("summary_cta", "balanced"): "마무리 2줄 + CTA 1줄",
        ("summary_cta", "dense"): "CTA 우선 배치 + 문장 축약 필요",
        ("quick_info", "airy"): "속보 헤드라인 2줄 + 정보 1줄",
        ("quick_info", "balanced"): "속보 헤드라인 2줄 + 정보 2줄",
        ("quick_info", "dense"): "속보 헤드라인 3줄 제한 + 정보 압축",
    }
    return hint_map.get((page_role, text_density), "본문 우선 배치")


def _normalize_component_order(component_order: list[str]) -> list[str]:
    normalized = [component for component in component_order if component not in {"TeamHeader", "FooterPager"}]
    return ["TeamHeader", *normalized, "FooterPager"]


def _calculate_quality_score(
    *,
    warnings: list[str],
    adjustments: list[str],
    text_density: str,
) -> float:
    score = 100.0
    score -= len(warnings) * 8.0
    score -= len(adjustments) * 2.5
    if text_density == "dense":
        score -= 6.0
    elif text_density == "balanced":
        score -= 1.5
    return max(round(score, 1), 0.0)


def _collect_global_warnings(bundle: CardDesignBundle) -> list[str]:
    warnings: list[str] = []
    accent_colors = {page.accent_color for page in bundle.pages}
    if len(accent_colors) > 1:
        warnings.append("페이지별 accent color가 서로 달라 팀 톤 일관성이 약함")
    if not bundle.pages:
        warnings.append("디자인 번들에 페이지가 없어 일관성 점검이 불가능함")
    return warnings
