from __future__ import annotations

import hashlib
import io
import urllib.request
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont

from kbo_card_news.imaging.rendering import _resolve_theme
from kbo_card_news.models.issue import CardDesignBundle, CardDesignPage, IssueAssetContext


class FinalCardRenderer:
    def __init__(self, *, cache_dir: Path | None = None) -> None:
        self.cache_dir = cache_dir

    def render_bundle(
        self,
        bundle: CardDesignBundle,
        assets: list[IssueAssetContext],
        *,
        output_dir: Path,
        prefix: str,
    ) -> list[Path]:
        output_dir.mkdir(parents=True, exist_ok=True)
        asset_lookup = _build_asset_lookup(assets)
        theme = _resolve_theme(bundle.team_code)
        paths: list[Path] = []
        for page in bundle.pages:
            image = self._render_page(bundle, page, theme, asset_lookup)
            output_path = output_dir / f"{prefix}_page_{page.page_number}.png"
            image.save(output_path, format="PNG")
            paths.append(output_path)
        return paths

    def _render_page(
        self,
        bundle: CardDesignBundle,
        page: CardDesignPage,
        theme: dict[str, str],
        asset_lookup: dict[str, IssueAssetContext],
    ) -> Image.Image:
        canvas = Image.new("RGB", (bundle.width, bundle.height), color="#F3F4F6")
        draw = ImageDraw.Draw(canvas)
        draw.rounded_rectangle((20, 20, bundle.width - 20, bundle.height - 20), radius=42, fill="#10192D")

        header_h = 108
        draw.rounded_rectangle((52, 52, bundle.width - 52, 52 + header_h), radius=28, fill=theme["accent_primary"])
        header_font = _load_font(20, bold=False)
        pager_font = _load_font(20, bold=True)
        draw.text((82, 86), f"{bundle.team_code} CARD NEWS", font=header_font, fill="#FFFFFF")
        draw.text((bundle.width - 160, 86), f"{page.page_number}/{len(bundle.pages)}", font=pager_font, fill="#FFFFFF")

        hero_box = (52, 178, bundle.width - 52, 582)
        hero_image = self._build_hero_image(page, hero_box[2] - hero_box[0], hero_box[3] - hero_box[1], theme, asset_lookup)
        canvas.paste(hero_image, hero_box[:2])

        content_box = (52, 624, bundle.width - 52, bundle.height - 92)
        draw.rounded_rectangle(content_box, radius=32, fill="#1A2438")

        label_font = _load_font(16, bold=True)
        title_font = _load_font(58, bold=True)
        body_font = _load_font(30, bold=False)
        footer_font = _load_font(16, bold=False)

        x0, y0, x1, y1 = content_box
        draw.text((x0 + 32, y0 + 34), page.page_role, font=label_font, fill=theme["accent_primary"])
        title_lines = _wrap_text(draw, page.headline, title_font, (x1 - x0) - 64, max_lines=2)
        title_y = y0 + 72
        for line in title_lines:
            draw.text((x0 + 32, title_y), line, font=title_font, fill="#F8FAFC")
            title_y += 68

        body_lines = _wrap_text(draw, page.body, body_font, (x1 - x0) - 64, max_lines=5)
        body_y = max(title_y + 20, y0 + 182)
        for line in body_lines:
            draw.text((x0 + 32, body_y), line, font=body_font, fill="#E5E7EB")
            body_y += 44

        components = " · ".join(page.component_order)
        draw.text((x0 + 32, y1 - 56), components, font=footer_font, fill="#9CA3AF")
        return canvas

    def _build_hero_image(
        self,
        page: CardDesignPage,
        width: int,
        height: int,
        theme: dict[str, str],
        asset_lookup: dict[str, IssueAssetContext],
    ) -> Image.Image:
        base = self._resolve_page_asset(page, asset_lookup, width, height)
        if base is None:
            base = _build_gradient_background(width, height, theme["accent_secondary"], theme["accent_primary"])
        else:
            base = _fit_cover(base, width, height)

        if page.render_strategy == "source_photo":
            overlay = Image.new("RGBA", (width, height), (12, 18, 28, 40))
            return Image.alpha_composite(base.convert("RGBA"), overlay).convert("RGB")

        if page.render_strategy == "hybrid_overlay":
            overlay = _build_bottom_overlay(width, height, alpha_top=10, alpha_bottom=155)
            return Image.alpha_composite(base.convert("RGBA"), overlay).convert("RGB")

        texture = _build_gradient_background(width, height, theme["accent_secondary"], theme["accent_primary"])
        texture = texture.filter(ImageFilter.GaussianBlur(radius=1.5))
        return texture

    def _resolve_page_asset(
        self,
        page: CardDesignPage,
        asset_lookup: dict[str, IssueAssetContext],
        width: int,
        height: int,
    ) -> Image.Image | None:
        reference = page.primary_asset_reference
        if not reference:
            return None
        asset = asset_lookup.get(reference)
        url = asset.origin_url if asset else reference
        if not isinstance(url, str) or not url.startswith(("http://", "https://")):
            return None
        return self._download_image(url, width=width, height=height)

    def _download_image(self, url: str, *, width: int, height: int) -> Image.Image | None:
        cache_path = None
        if self.cache_dir is not None:
            self.cache_dir.mkdir(parents=True, exist_ok=True)
            digest = hashlib.sha256(url.encode("utf-8")).hexdigest()[:24]
            cache_path = self.cache_dir / f"{digest}.img"
            if cache_path.exists():
                try:
                    return Image.open(cache_path).convert("RGB")
                except OSError:
                    cache_path.unlink(missing_ok=True)
        try:
            request = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(request, timeout=20) as response:
                payload = response.read()
        except Exception:
            return None
        try:
            image = Image.open(io.BytesIO(payload)).convert("RGB")
        except OSError:
            return None
        if cache_path is not None:
            image.save(cache_path, format="PNG")
        return image


def _build_asset_lookup(assets: list[IssueAssetContext]) -> dict[str, IssueAssetContext]:
    lookup: dict[str, IssueAssetContext] = {}
    for asset in assets:
        if asset.asset_id:
            lookup[asset.asset_id] = asset
        lookup[asset.origin_url] = asset
    return lookup


def _build_gradient_background(width: int, height: int, start_hex: str, end_hex: str) -> Image.Image:
    start = _hex_to_rgb(start_hex)
    end = _hex_to_rgb(end_hex)
    image = Image.new("RGB", (width, height))
    draw = ImageDraw.Draw(image)
    for y in range(height):
        ratio = y / max(height - 1, 1)
        color = tuple(int(start[i] + (end[i] - start[i]) * ratio) for i in range(3))
        draw.line((0, y, width, y), fill=color)
    return image


def _build_bottom_overlay(width: int, height: int, *, alpha_top: int, alpha_bottom: int) -> Image.Image:
    overlay = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    pixels = overlay.load()
    for y in range(height):
        ratio = y / max(height - 1, 1)
        alpha = int(alpha_top + (alpha_bottom - alpha_top) * ratio)
        for x in range(width):
            pixels[x, y] = (9, 15, 25, alpha)
    return overlay


def _fit_cover(image: Image.Image, width: int, height: int) -> Image.Image:
    source_ratio = image.width / max(image.height, 1)
    target_ratio = width / max(height, 1)
    if source_ratio > target_ratio:
        resized = image.resize((int(height * source_ratio), height))
        left = max((resized.width - width) // 2, 0)
        return resized.crop((left, 0, left + width, height))
    resized = image.resize((width, int(width / max(source_ratio, 1e-6))))
    top = max((resized.height - height) // 2, 0)
    return resized.crop((0, top, width, top + height))


def _wrap_text(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.ImageFont, max_width: int, *, max_lines: int) -> list[str]:
    compact = " ".join(text.split()).strip()
    if not compact:
        return [""]
    units = compact.split()
    if len(units) == 1:
        units = list(compact)
    lines: list[str] = []
    current = units[0]
    for unit in units[1:]:
        trial = f"{current} {unit}" if len(compact.split()) > 1 else f"{current}{unit}"
        if draw.textlength(trial, font=font) <= max_width:
            current = trial
            continue
        lines.append(current)
        current = unit
    remaining = current
    lines.append(remaining)
    return lines


def _hex_to_rgb(value: str) -> tuple[int, int, int]:
    value = value.lstrip("#")
    return tuple(int(value[index:index + 2], 16) for index in (0, 2, 4))


def _load_font(size: int, *, bold: bool) -> ImageFont.ImageFont:
    candidates = [
        ("/System/Library/Fonts/AppleSDGothicNeo.ttc", 1 if bold else 0),
        ("/System/Library/Fonts/Supplemental/AppleGothic.ttf", 0),
    ]
    for path, index in candidates:
        try:
            return ImageFont.truetype(path, size=size, index=index)
        except OSError:
            continue
    return ImageFont.load_default()
