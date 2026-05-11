from __future__ import annotations

import argparse
from datetime import datetime
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional, Tuple
from PIL import Image, ImageDraw, ImageFont, ImageFilter


# =========================
# 설정
# =========================

CANVAS_WIDTH = 1080
CANVAS_HEIGHT = 1350  # 인스타 4:5

FRAME_MARGIN_X = 20
TOP_BAR_HEIGHT = 26
FRAME_MARGIN_BOTTOM = 26
PHOTO_PANEL_RADIUS = 138

CONTENT_PADDING_X = 44
CONTENT_BOTTOM_PADDING = 38
TITLE_TO_SUB_GAP = 8
TITLE_TOP_RATIO = 0.755
SUBTITLE_TOP_RATIO = 0.878

HEADLINE_FONT_SIZE = 120
SUBHEADLINE_FONT_SIZE = 30
DATE_FONT_SIZE = 20
INSTAGRAM_FONT_SIZE = 30

# 이 템플릿에서 팀별로 달라지는 값은 대표 색(primary) 하나뿐이다.
# 공식 채널이 "red / blue / burgundy"처럼 색 이름만 공개한 경우가 많아서,
# 아래 hex는 현재 로고/유니폼의 대표 톤을 기준으로 실무용으로 고정한 값이다.
TEAM_COLORS = {
    "LG": "#C30452",
    "KIA": "#EA0029",
    "두산": "#131230",
    "삼성": "#0066B3",
    "롯데": "#041E42",
    "SSG": "#CE0E2D",
    "한화": "#FC4E00",
    "KT": "#231F20",
    "NC": "#315288",
    "키움": "#7A003C",
}

DEFAULT_NEWS_GRADIENT = "#111111"
DEFAULT_INSTAGRAM_HANDLE = "@news_kbo"
MAX_HEADLINE_CHARS = 7
MAX_SUBHEADLINE_LINES = 2

FONT_CANDIDATES = {
    "bold": [
        Path("/Users/s.h.choi/Library/Fonts/esamanru OTF Bold.otf"),
        Path("/Users/s.h.choi/Library/Fonts/이사만루OTF Bold.otf"),
        Path("/Users/s.h.choi/Library/Fonts/이사만루OTF B.otf"),
        Path("/Users/s.h.choi/Library/Fonts/IsamanruOTF Bold.otf"),
        Path("/Users/s.h.choi/Library/Fonts/IsamanruOTF B.otf"),
        Path("/Library/Fonts/이사만루OTF Bold.otf"),
        Path("/Library/Fonts/이사만루OTF B.otf"),
    ],
    "medium": [
        Path("/Users/s.h.choi/Library/Fonts/esamanru OTF Medium.otf"),
        Path("/Users/s.h.choi/Library/Fonts/esamanru OTF Bold.otf"),
        Path("/Users/s.h.choi/Library/Fonts/이사만루OTF Medium.otf"),
        Path("/Users/s.h.choi/Library/Fonts/이사만루OTF Regular.otf"),
    ],
    "light": [
        Path("/Users/s.h.choi/Library/Fonts/esamanru OTF Light.otf"),
        Path("/Users/s.h.choi/Library/Fonts/esamanru OTF Medium.otf"),
        Path("/Users/s.h.choi/Library/Fonts/esamanru OTF Bold.otf"),
        Path("/Users/s.h.choi/Library/Fonts/이사만루OTF Light.otf"),
        Path("/Users/s.h.choi/Library/Fonts/이사만루OTF Regular.otf"),
    ],
}
FALLBACK_FONT = "/Users/s.h.choi/Library/Fonts/Pretendard-Bold.otf"


# =========================
# 데이터 구조
# =========================

@dataclass
class CardNewsInput:
    image_path: str
    output_path: str
    headline_label: str
    subheadline: str
    title: str
    team_name: Optional[str]
    date_text: str
    instagram_handle: str
    is_team_news: bool = True
    team_color_override: Optional[str] = None


# =========================
# 유틸
# =========================

def hex_to_rgb(hex_color: str) -> Tuple[int, int, int]:
    hex_color = hex_color.strip().lstrip("#")
    return tuple(int(hex_color[i:i+2], 16) for i in (0, 2, 4))


def get_gradient_color(
    team_name: Optional[str],
    is_team_news: bool,
    team_color_override: Optional[str] = None,
) -> Tuple[int, int, int]:
    if team_color_override:
        return hex_to_rgb(team_color_override)
    if is_team_news and team_name and team_name in TEAM_COLORS:
        return hex_to_rgb(TEAM_COLORS[team_name])
    return hex_to_rgb(DEFAULT_NEWS_GRADIENT)


def get_template_colors(
    team_name: Optional[str],
    is_team_news: bool,
    team_color_override: Optional[str] = None,
) -> dict[str, Tuple[int, int, int]]:
    primary = get_gradient_color(team_name, is_team_news, team_color_override)
    return {
        "primary": primary,
        "gradient_top": blend_color(primary, (255, 255, 255), 0.18),
        "gradient_bottom": blend_color(primary, (0, 0, 0), 0.15),
        "fog": primary,
        "bottom_panel": primary,
    }


def blend_color(color: Tuple[int, int, int], target: Tuple[int, int, int], amount: float) -> Tuple[int, int, int]:
    return tuple(
        int(color[i] + ((target[i] - color[i]) * amount))
        for i in range(3)
    )


def resolve_font_path(weight: str = "bold") -> Optional[str]:
    for candidate in FONT_CANDIDATES.get(weight, []):
        if candidate.exists():
            return str(candidate)
    fallback = Path(FALLBACK_FONT)
    if fallback.exists():
        return str(fallback)
    return None


def limit_headline_chars(text: str, max_chars: int = MAX_HEADLINE_CHARS) -> str:
    compact = text.replace(" ", "")
    if len(compact) <= max_chars:
        return text

    kept: list[str] = []
    count = 0
    for ch in text:
        if ch == " ":
            continue
        kept.append(ch)
        count += 1
        if count >= max_chars:
            break
    return "".join(kept)


def trim_text_to_lines(
    draw: ImageDraw.ImageDraw,
    text: str,
    font: ImageFont.FreeTypeFont | ImageFont.ImageFont,
    max_width: int,
    max_lines: int,
) -> str:
    wrapped = wrap_korean_text(draw, text, font, max_width)
    lines = wrapped.splitlines()
    if len(lines) <= max_lines:
        return wrapped

    kept = lines[:max_lines]
    while kept and draw.textbbox((0, 0), kept[-1] + "...", font=font)[2] > max_width:
        kept[-1] = kept[-1][:-1].rstrip()
    kept[-1] = kept[-1].rstrip() + "..."
    return "\n".join(kept)


def fit_cover_image(img: Image.Image, target_w: int, target_h: int) -> Image.Image:
    src_w, src_h = img.size
    src_ratio = src_w / src_h
    target_ratio = target_w / target_h

    if src_ratio > target_ratio:
        # 이미지가 더 넓음 -> 좌우 잘라냄
        new_h = target_h
        new_w = int(new_h * src_ratio)
    else:
        # 이미지가 더 좁음 -> 상하 잘라냄
        new_w = target_w
        new_h = int(new_w / src_ratio)

    resized = img.resize((new_w, new_h), Image.LANCZOS)

    left = (new_w - target_w) // 2
    top = (new_h - target_h) // 2
    return resized.crop((left, top, left + target_w, top + target_h))


def draw_vertical_gradient(
    base: Image.Image,
    top_color: Tuple[int, int, int],
    bottom_color: Tuple[int, int, int],
    start_y_ratio: float = 0.54,
    max_opacity: int = 230,
) -> None:
    overlay = Image.new("RGBA", base.size, (0, 0, 0, 0))
    px = overlay.load()
    start_y = int(base.height * start_y_ratio)

    for y in range(base.height):
        if y < start_y:
            alpha = 0
            color = top_color
        else:
            progress = (y - start_y) / max(1, (base.height - start_y))
            eased = progress ** 1.2
            alpha = int(eased * max_opacity)
            color = blend_color(top_color, bottom_color, min(1.0, eased))
        for x in range(base.width):
            px[x, y] = (color[0], color[1], color[2], alpha)

    base.alpha_composite(overlay)


def draw_soft_top_dim(base: Image.Image, opacity: int = 56) -> None:
    overlay = Image.new("RGBA", base.size, (0, 0, 0, 0))
    px = overlay.load()
    end_y = int(base.height * 0.28)

    for y in range(base.height):
        if y > end_y:
            alpha = 0
        else:
            progress = 1 - (y / max(1, end_y))
            alpha = int((progress ** 1.8) * opacity)
        for x in range(base.width):
            px[x, y] = (0, 0, 0, alpha)

    base.alpha_composite(overlay)


def create_round_mask(size: Tuple[int, int], radius: int) -> Image.Image:
    mask = Image.new("L", size, 0)
    mask_draw = ImageDraw.Draw(mask)
    mask_draw.rounded_rectangle((0, 0, size[0], size[1]), radius=radius, fill=255)
    return mask


def add_bottom_fog(base: Image.Image, color: Tuple[int, int, int]) -> None:
    overlay = Image.new("RGBA", base.size, (0, 0, 0, 0))
    px = overlay.load()
    start_y = int(base.height * 0.6)
    haze_color = blend_color(color, (255, 255, 255), 0.45)

    for y in range(base.height):
        if y < start_y:
            alpha = 0
        else:
            progress = (y - start_y) / max(1, (base.height - start_y))
            alpha = int((progress ** 1.4) * 115)
        for x in range(base.width):
            px[x, y] = (haze_color[0], haze_color[1], haze_color[2], alpha)

    softened = overlay.filter(ImageFilter.GaussianBlur(radius=16))
    base.alpha_composite(softened)


def add_bottom_team_panel(base: Image.Image, color: Tuple[int, int, int]) -> None:
    overlay = Image.new("RGBA", base.size, (0, 0, 0, 0))
    px = overlay.load()
    start_y = int(base.height * 0.68)
    solid_y = int(base.height * 0.86)
    light_color = blend_color(color, (255, 255, 255), 0.22)
    deep_color = color

    for y in range(base.height):
        if y < start_y:
            alpha = 0
            row_color = light_color
        elif y >= solid_y:
            alpha = 255
            row_color = deep_color
        else:
            progress = (y - start_y) / max(1, (solid_y - start_y))
            eased = progress ** 0.82
            alpha = int(92 + (eased * 155))
            row_color = blend_color(light_color, deep_color, min(1.0, eased))
        for x in range(base.width):
            px[x, y] = (row_color[0], row_color[1], row_color[2], alpha)

    softened = overlay.filter(ImageFilter.GaussianBlur(radius=18))
    hard_panel = Image.new("RGBA", base.size, (0, 0, 0, 0))
    hard_draw = ImageDraw.Draw(hard_panel)
    hard_draw.rectangle((0, solid_y, base.width, base.height), fill=deep_color + (255,))
    base.alpha_composite(softened)
    base.alpha_composite(hard_panel)


def build_masked_bottom_panel(
    size: Tuple[int, int],
    radius: int,
    start_y: int,
    color: Tuple[int, int, int],
) -> Image.Image:
    panel = Image.new("RGBA", size, (0, 0, 0, 0))
    panel_draw = ImageDraw.Draw(panel)
    panel_draw.rectangle((0, start_y, size[0], size[1]), fill=color + (255,))

    rounded_mask = create_round_mask(size, radius)
    transparent = Image.new("RGBA", size, (0, 0, 0, 0))
    return Image.composite(panel, transparent, rounded_mask)


def wrap_text_by_width(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.FreeTypeFont, max_width: int) -> str:
    words = text.split()
    if not words:
        return text

    lines = []
    current = words[0]

    for word in words[1:]:
        test = current + " " + word
        bbox = draw.textbbox((0, 0), test, font=font)
        if bbox[2] - bbox[0] <= max_width:
            current = test
        else:
            lines.append(current)
            current = word
    lines.append(current)

    return "\n".join(lines)


def wrap_korean_text(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.FreeTypeFont, max_width: int) -> str:
    """
    한국어는 어절 단위 줄바꿈을 우선하고,
    매우 긴 어절만 예외적으로 문자 단위로 분해
    """
    words = text.split()
    if not words:
        return text

    lines: list[str] = []
    current = ""

    for word in words:
        candidate = word if not current else f"{current} {word}"
        bbox = draw.textbbox((0, 0), candidate, font=font)
        if bbox[2] - bbox[0] <= max_width:
            current = candidate
        else:
            if current:
                lines.append(current)
                current = ""

            word_bbox = draw.textbbox((0, 0), word, font=font)
            if word_bbox[2] - word_bbox[0] <= max_width:
                current = word
            else:
                chunk = ""
                for ch in word:
                    test = chunk + ch
                    test_bbox = draw.textbbox((0, 0), test, font=font)
                    if test_bbox[2] - test_bbox[0] <= max_width:
                        chunk = test
                    else:
                        if chunk:
                            lines.append(chunk)
                        chunk = ch
                current = chunk

    if current:
        lines.append(current)

    return "\n".join(lines)


def add_text_shadow(
    draw: ImageDraw.ImageDraw,
    xy: Tuple[int, int],
    text: str,
    font: ImageFont.FreeTypeFont,
    fill: Tuple[int, int, int],
    shadow_offset: Tuple[int, int] = (4, 6),
    shadow_fill: Tuple[int, int, int, int] = (0, 0, 0, 140),
    spacing: int = 0,
) -> None:
    x, y = xy
    sx, sy = shadow_offset
    draw.multiline_text((x + sx, y + sy), text, font=font, fill=shadow_fill, spacing=spacing)
    draw.multiline_text((x, y), text, font=font, fill=fill, spacing=spacing)


def load_font(font_path: str, size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    if not font_path:
        return ImageFont.load_default()
    try:
        return ImageFont.truetype(font_path, size)
    except OSError:
        return ImageFont.load_default()


def fit_wrapped_text(
    draw: ImageDraw.ImageDraw,
    text: str,
    font_path: str,
    start_size: int,
    max_width: int,
    max_lines: int,
    min_size: int = 22,
    spacing: int = 0,
) -> tuple[str, ImageFont.FreeTypeFont | ImageFont.ImageFont]:
    size = start_size
    while size >= min_size:
        font = load_font(font_path, size)
        wrapped = wrap_korean_text(draw, text, font, max_width)
        if wrapped.count("\n") + 1 <= max_lines:
            bbox = draw.multiline_textbbox((0, 0), wrapped, font=font, spacing=spacing)
            if bbox[2] - bbox[0] <= max_width:
                return wrapped, font
        size -= 4
    font = load_font(font_path, min_size)
    return wrap_korean_text(draw, text, font, max_width), font


def open_source_image(image_path: str) -> Image.Image:
    candidates = [
        Path(image_path),
        Path(f"{image_path}.png"),
    ]
    original = Path(image_path)
    if original.suffix.lower() in {".avif", ".jpg", ".jpeg", ".webp"}:
        candidates.append(original.with_suffix(".png"))

    seen: set[Path] = set()
    for candidate in candidates:
        if candidate in seen or not candidate.exists():
            continue
        seen.add(candidate)
        try:
            return Image.open(candidate).convert("RGB")
        except Exception:
            continue

    raise FileNotFoundError(f"Unable to open source image from: {image_path}")


def load_replay_defaults(
    report_path: str,
    page_number: int,
) -> dict[str, Any]:
    report = json.loads(Path(report_path).read_text(encoding="utf-8"))
    drafts = report.get("drafts") or []
    if not drafts:
        raise ValueError(f"No drafts found in replay report: {report_path}")

    draft = drafts[0]
    pages = draft.get("pages") or []
    page = next((item for item in pages if item.get("page_number") == page_number), None)
    if page is None:
        raise ValueError(f"Page {page_number} not found in replay report: {report_path}")

    metadata = ((draft.get("metadata") or {}).get("request") or {})
    candidate = (((metadata.get("contents") or [{}])[0].get("parts") or [{}])[0].get("text"))

    team_name = None
    if isinstance(candidate, str):
        if "\"team_code\": \"LG\"" in candidate:
            team_name = "LG"
        elif "\"team_code\": \"KIA\"" in candidate:
            team_name = "KIA"
        elif "\"team_code\": \"SSG\"" in candidate:
            team_name = "SSG"

    return {
        "headline_label": page.get("headline", "헤드라인"),
        "subheadline": page.get("body", ""),
        "title": draft.get("title", page.get("headline", "제목")),
        "team_name": team_name,
        "date_text": datetime.now().strftime("%Y.%m.%d"),
        "instagram_handle": DEFAULT_INSTAGRAM_HANDLE,
        "is_team_news": True,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Render a single KBO-style card news image.")
    parser.add_argument("--image-path", help="Background image path to render on top of.")
    parser.add_argument("--output-path", required=True, help="Output image path.")
    parser.add_argument("--headline-label", help="Optional legacy field. If title is empty, this is used as headline.")
    parser.add_argument("--subheadline", help="Bottom description text.")
    parser.add_argument("--title", help="Main headline text.")
    parser.add_argument("--team-name", help="Team name used for gradient color.")
    parser.add_argument("--date-text", help="Top date text. Example: 2025.04.11")
    parser.add_argument("--instagram-handle", help="Top Instagram handle. Example: @news_kbo")
    parser.add_argument(
        "--is-team-news",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Whether to use team-color gradient.",
    )
    parser.add_argument("--replay-report", help="Replay report JSON path for default copy.")
    parser.add_argument(
        "--page-number",
        type=int,
        default=1,
        help="Page number to load from replay report when used.",
    )
    return parser


def build_input_from_args(args: argparse.Namespace) -> CardNewsInput:
    defaults: dict[str, Any] = {}
    if args.replay_report:
        defaults = load_replay_defaults(args.replay_report, args.page_number)

    image_path = args.image_path
    if not image_path:
        raise ValueError("--image-path is required. The replay folder you shared does not contain local source images.")

    headline_label = args.headline_label if args.headline_label is not None else defaults.get("headline_label", "헤드라인")
    subheadline = args.subheadline if args.subheadline is not None else defaults.get("subheadline", "")
    default_title = defaults.get("title") or headline_label or "제목 제목"
    title = args.title if args.title is not None else default_title
    team_name = args.team_name if args.team_name is not None else defaults.get("team_name")
    date_text = args.date_text if args.date_text is not None else defaults.get("date_text", datetime.now().strftime("%Y.%m.%d"))
    instagram_handle = (
        args.instagram_handle
        if args.instagram_handle is not None
        else defaults.get("instagram_handle", DEFAULT_INSTAGRAM_HANDLE)
    )
    is_team_news = args.is_team_news if args.is_team_news is not None else defaults.get("is_team_news", True)

    return CardNewsInput(
        image_path=image_path,
        output_path=args.output_path,
        headline_label=headline_label,
        subheadline=subheadline,
        title=title,
        team_name=team_name,
        date_text=date_text,
        instagram_handle=instagram_handle,
        is_team_news=is_team_news,
    )


# =========================
# 메인 렌더
# =========================

def render_card_news(data: CardNewsInput) -> None:
    colors = get_template_colors(data.team_name, data.is_team_news, data.team_color_override)
    team_color = colors["primary"]
    canvas = Image.new("RGBA", (CANVAS_WIDTH, CANVAS_HEIGHT), team_color + (255,))

    photo_w = CANVAS_WIDTH - (FRAME_MARGIN_X * 2)
    photo_h = CANVAS_HEIGHT - TOP_BAR_HEIGHT - FRAME_MARGIN_BOTTOM
    photo_x = FRAME_MARGIN_X
    photo_y = TOP_BAR_HEIGHT

    bg = open_source_image(data.image_path)
    bg = fit_cover_image(bg, photo_w, photo_h).convert("RGBA")
    bg = bg.filter(ImageFilter.GaussianBlur(radius=0.3))
    draw_soft_top_dim(bg, opacity=18)
    add_bottom_fog(bg, colors["fog"])
    add_bottom_team_panel(bg, colors["bottom_panel"])
    draw_vertical_gradient(bg, colors["gradient_top"], colors["gradient_bottom"], start_y_ratio=0.66, max_opacity=180)

    rounded_mask = create_round_mask((photo_w, photo_h), PHOTO_PANEL_RADIUS)
    bg.putalpha(rounded_mask)
    canvas.alpha_composite(bg, (photo_x, photo_y))

    draw = ImageDraw.Draw(canvas)
    font_bold_path = resolve_font_path("bold")
    font_medium_path = resolve_font_path("medium")
    font_light_path = resolve_font_path("light")

    font_date = load_font(font_light_path, DATE_FONT_SIZE)
    font_handle = load_font(font_light_path, INSTAGRAM_FONT_SIZE)
    headline_source = limit_headline_chars(data.title)
    title_text, font_title = fit_wrapped_text(
        draw,
        headline_source,
        font_bold_path,
        HEADLINE_FONT_SIZE,
        CANVAS_WIDTH - (CONTENT_PADDING_X * 2),
        max_lines=1,
        min_size=84,
        spacing=-8,
    )
    sub_text = ""
    font_sub = load_font(font_medium_path, SUBHEADLINE_FONT_SIZE)
    if data.subheadline:
        fitted_sub_text, font_sub = fit_wrapped_text(
            draw,
            data.subheadline,
            font_medium_path,
            SUBHEADLINE_FONT_SIZE,
            CANVAS_WIDTH - (CONTENT_PADDING_X * 2),
            max_lines=MAX_SUBHEADLINE_LINES,
            min_size=24,
            spacing=0,
        )
        sub_text = trim_text_to_lines(
            draw,
            fitted_sub_text,
            font_sub,
            CANVAS_WIDTH - (CONTENT_PADDING_X * 2),
            MAX_SUBHEADLINE_LINES,
        )

    title_bbox = draw.multiline_textbbox((0, 0), title_text, font=font_title, spacing=-8)
    title_h = title_bbox[3] - title_bbox[1]
    sub_bbox = draw.multiline_textbbox((0, 0), sub_text, font=font_sub, spacing=0) if sub_text else (0, 0, 0, 0)
    sub_h = sub_bbox[3] - sub_bbox[1] if sub_text else 0

    title_y = int(CANVAS_HEIGHT * TITLE_TOP_RATIO) - title_bbox[1]
    if sub_text:
        sub_y = max(
            title_y + title_h + TITLE_TO_SUB_GAP,
            int(CANVAS_HEIGHT * SUBTITLE_TOP_RATIO) - sub_bbox[1],
        )
    else:
        sub_y = CANVAS_HEIGHT - CONTENT_BOTTOM_PADDING - sub_h
    title_x = CONTENT_PADDING_X - title_bbox[0]
    sub_x = CONTENT_PADDING_X - sub_bbox[0] if sub_text else CONTENT_PADDING_X

    opaque_panel_start = max(0, sub_y - photo_y + 54)
    bottom_panel = build_masked_bottom_panel(
        (photo_w, photo_h),
        PHOTO_PANEL_RADIUS,
        opaque_panel_start,
        colors["bottom_panel"],
    )
    canvas.alpha_composite(bottom_panel, (photo_x, photo_y))

    date_bbox = draw.textbbox((0, 0), data.date_text, font=font_date)
    handle_bbox = draw.textbbox((0, 0), data.instagram_handle, font=font_handle)
    date_x = (CANVAS_WIDTH - (date_bbox[2] - date_bbox[0])) / 2 - date_bbox[0]
    handle_x = (CANVAS_WIDTH - (handle_bbox[2] - handle_bbox[0])) / 2 - handle_bbox[0]

    draw.text((date_x, 4 - date_bbox[1]), data.date_text, font=font_date, fill=(255, 255, 255))
    draw.text((handle_x, photo_y + 4 - handle_bbox[1]), data.instagram_handle, font=font_handle, fill=(255, 255, 255))
    add_text_shadow(
        draw,
        (title_x, title_y),
        title_text,
        font=font_title,
        fill=(255, 255, 255),
        shadow_offset=(0, 3),
        shadow_fill=(0, 0, 0, 50),
        spacing=-8,
    )
    if sub_text:
        add_text_shadow(
            draw,
            (sub_x, sub_y),
            sub_text,
            font=font_sub,
            fill=(255, 255, 255),
            shadow_offset=(0, 1),
            shadow_fill=(0, 0, 0, 35),
            spacing=0,
        )

    # 저장
    out_path = Path(data.output_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    canvas.convert("RGB").save(out_path, quality=95)
    print(f"Saved: {out_path}")


# =========================
# 사용 예시
# =========================

if __name__ == "__main__":
    parser = build_parser()
    args = parser.parse_args()
    render_card_news(build_input_from_args(args))
