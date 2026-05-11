from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from html.parser import HTMLParser
from urllib.parse import parse_qs, unquote, urlencode, urljoin, urlparse, urlunparse
from urllib.request import Request, urlopen

from kbo_card_news.collectors.base import BaseCollector
from kbo_card_news.models.collector import CollectedItem, MediaAsset


@dataclass(slots=True)
class NewsSiteDefinition:
    source_name: str
    site_name: str
    list_url: str
    article_link_keywords: tuple[str, ...]
    page_query_param: str = "page"
    body_class_keywords: tuple[str, ...] = ("article", "content", "news", "view", "body")
    image_url_keywords: tuple[str, ...] = ()
    include_text_keywords: tuple[str, ...] = ()
    exclude_text_keywords: tuple[str, ...] = ()
    min_title_length: int = 8
    prefer_meta_title_when_long: bool = False
    long_title_threshold: int = 120


@dataclass(slots=True)
class NewsSiteCollectorConfig:
    definition: NewsSiteDefinition
    user_agent: str = "Mozilla/5.0"
    timeout_seconds: int = 10
    default_page_limit: int = 1
    window_page_limit_min: int = 5
    window_page_limit_per_day: int = 5
    window_page_limit_max: int = 30


class _AnchorListParser(HTMLParser):
    def __init__(self, keywords: tuple[str, ...], min_title_length: int) -> None:
        super().__init__()
        self.keywords = keywords
        self.min_title_length = min_title_length
        self.items: list[dict[str, str]] = []
        self._href: str | None = None
        self._title_parts: list[str] = []
        self._in_anchor = False
        self._seen: set[str] = set()

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag != "a":
            return
        attrs_map = dict(attrs)
        href = attrs_map.get("href")
        if not href:
            return
        if self.keywords and not any(keyword in href for keyword in self.keywords):
            return
        if href in self._seen:
            return
        self._href = href
        self._title_parts = []
        self._in_anchor = True

    def handle_data(self, data: str) -> None:
        if self._in_anchor:
            clean = " ".join(data.split())
            if clean:
                self._title_parts.append(clean)

    def handle_endtag(self, tag: str) -> None:
        if tag != "a" or not self._href:
            return
        title = " ".join(self._title_parts).strip()
        if title and len(title) >= self.min_title_length:
            self.items.append({"href": self._href, "title": title})
            self._seen.add(self._href)
        self._href = None
        self._title_parts = []
        self._in_anchor = False


class _ArticleParser(HTMLParser):
    def __init__(
        self,
        body_class_keywords: tuple[str, ...],
        image_url_keywords: tuple[str, ...],
        article_url: str,
    ) -> None:
        super().__init__()
        self.body_class_keywords = body_class_keywords
        self.image_url_keywords = image_url_keywords
        self.article_url = article_url
        self.title: str | None = None
        self.author_name: str | None = None
        self.published_at: str | None = None
        self.og_image_url: str | None = None
        self.body_parts: list[str] = []
        self.image_urls: list[str] = []
        self._body_container_started = False
        self._capture_text_depth = 0
        self._ignore_data_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attrs_map = dict(attrs)
        if tag == "meta":
            self._handle_meta(attrs_map)
            return
        if tag in {"script", "style"}:
            self._ignore_data_depth += 1
            return
        class_name = " ".join(
            value for key, value in attrs if key in {"class", "id"} and value
        ).lower()
        if not self._body_container_started and tag in {"article", "section", "div"}:
            if self._is_body_container(tag, attrs_map, class_name):
                self._body_container_started = True
                self._capture_text_depth = 1
                return
        elif self._capture_text_depth > 0 and tag in {
            "article",
            "section",
            "div",
            "p",
            "span",
            "figure",
            "figcaption",
            "strong",
            "em",
            "blockquote",
            "ul",
            "ol",
            "li",
            "br",
        }:
            self._capture_text_depth += 1

        if tag == "img" and self._capture_text_depth > 0:
            src = attrs_map.get("src") or attrs_map.get("data-src")
            if not src:
                return
            src = self._normalize_image_url(urljoin(self.article_url, src))
            if self.image_url_keywords and not any(
                keyword in src for keyword in self.image_url_keywords
            ):
                return
            if self._looks_like_article_image(src) and src not in self.image_urls:
                self.image_urls.append(src)

    def handle_data(self, data: str) -> None:
        if self._ignore_data_depth > 0:
            return
        if self._capture_text_depth > 0:
            clean = " ".join(data.split())
            if clean and not self._is_noise_text(clean):
                self.body_parts.append(clean)

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style"} and self._ignore_data_depth > 0:
            self._ignore_data_depth -= 1
            return
        if tag in {
            "article",
            "section",
            "div",
            "p",
            "span",
            "figure",
            "figcaption",
            "strong",
            "em",
            "blockquote",
            "ul",
            "ol",
            "li",
            "br",
        } and self._capture_text_depth > 0:
            self._capture_text_depth -= 1

    def _handle_meta(self, attrs_map: dict[str, str | None]) -> None:
        property_name = attrs_map.get("property") or attrs_map.get("name")
        content = attrs_map.get("content")
        if not property_name or not content:
            return
        property_name = property_name.lower()
        content = content.strip()
        if property_name in {"og:title", "twitter:title"} and not self.title:
            self.title = content
        elif property_name in {"author", "article:author"} and not self.author_name:
            self.author_name = content
        elif property_name in {"article:published_time", "og:article:published_time"}:
            self.published_at = content
        elif property_name in {"og:image", "twitter:image"} and not self.og_image_url:
            resolved = urljoin(self.article_url, content)
            resolved = self._normalize_image_url(resolved)
            if self._looks_like_article_image(resolved):
                self.og_image_url = resolved

    def _is_body_container(
        self,
        tag: str,
        attrs_map: dict[str, str | None],
        class_name: str,
    ) -> bool:
        item_prop = (attrs_map.get("itemprop") or "").lower()
        if item_prop == "articlebody":
            return True

        normalized = class_name.replace("-", "").replace("_", "").replace(" ", "")
        strong_keywords = {
            "articlebody",
            "articlebodycontent",
            "articlebox",
            "articletext",
            "articletxt",
            "newsbody",
            "newscontent",
            "viewtxt",
            "writediv",
        }
        if any(keyword in normalized for keyword in strong_keywords):
            return True

        normalized_keywords = tuple(
            keyword.lower().replace("-", "").replace("_", "").replace(" ", "")
            for keyword in self.body_class_keywords
        )
        blocked_generic_keywords = {"article", "content", "news", "view", "body"}
        filtered_keywords = tuple(
            keyword for keyword in normalized_keywords if keyword and keyword not in blocked_generic_keywords
        )
        return any(keyword in normalized for keyword in filtered_keywords)

    @staticmethod
    def _looks_like_article_image(src: str) -> bool:
        lowered = src.lower()
        blocked_keywords = ("logo", "icon", "sprite", "banner", "adserver", "blank")
        blocked_suffixes = (".svg", ".ico")
        if any(keyword in lowered for keyword in blocked_keywords):
            return False
        if lowered.endswith(blocked_suffixes):
            return False
        return True

    @staticmethod
    def _normalize_image_url(src: str) -> str:
        parsed = urlparse(src)
        if parsed.path.endswith("/_next/image"):
            query = parse_qs(parsed.query)
            nested = query.get("url")
            if nested and nested[0]:
                return unquote(nested[0])
        return src

    @staticmethod
    def _is_noise_text(value: str) -> bool:
        blocked_exact = {
            "english",
            "크립토허브",
            "노동신문",
            "닫기",
            "본문 이미지",
            "|",
            "Advertisement",
        }
        blocked_contains = (
            "display:none",
            "onclick=",
            "{{if",
            "{{each",
            "imgnum",
            "dcimg_num_tip",
            "마우스 커서를 올리면",
        )
        stripped = value.strip()
        if stripped in blocked_exact:
            return True
        return any(keyword in stripped for keyword in blocked_contains)


class NewsSiteCollector(BaseCollector):
    def __init__(self, config: NewsSiteCollectorConfig) -> None:
        self.config = config
        self.source_name = config.definition.source_name

    def collect(
        self,
        *,
        window_start: datetime | None = None,
        window_end: datetime | None = None,
    ) -> list[CollectedItem]:
        now = datetime.utcnow()
        normalized_start = _normalize_datetime(window_start)
        normalized_end = _normalize_datetime(window_end)
        items: list[CollectedItem] = []
        seen_article_urls: set[str] = set()
        exhausted_window = False

        for list_url in self._build_list_page_urls(window_start=window_start, window_end=window_end):
            list_html = self._fetch(list_url)
            list_parser = _AnchorListParser(
                self.config.definition.article_link_keywords,
                self.config.definition.min_title_length,
            )
            list_parser.feed(list_html)
            if not list_parser.items:
                break

            page_found_new_link = False
            page_newest_published: datetime | None = None

            for raw in list_parser.items:
                article_url = urljoin(list_url, raw["href"])
                if article_url in seen_article_urls:
                    continue
                seen_article_urls.add(article_url)
                page_found_new_link = True

                article_html = self._fetch(article_url)
                article_parser = _ArticleParser(
                    body_class_keywords=self.config.definition.body_class_keywords,
                    image_url_keywords=self.config.definition.image_url_keywords,
                    article_url=article_url,
                )
                article_parser.feed(article_html)
                published_at = self._parse_datetime(article_parser.published_at)
                normalized_published = _normalize_datetime(published_at)
                if normalized_published and (
                    page_newest_published is None or normalized_published > page_newest_published
                ):
                    page_newest_published = normalized_published

                if normalized_end and normalized_published and normalized_published > normalized_end:
                    continue

                resolved_title = raw.get("title") or article_parser.title
                if (
                    self.config.definition.prefer_meta_title_when_long
                    and raw.get("title")
                    and len(raw["title"]) >= self.config.definition.long_title_threshold
                    and article_parser.title
                ):
                    resolved_title = article_parser.title
                item = CollectedItem(
                    source_type=self.source_name,
                    source_item_type="article",
                    source_url=article_url,
                    source_external_id=None,
                    title=resolved_title,
                    body_text="\n".join(article_parser.body_parts) or None,
                    author_name=article_parser.author_name,
                    published_at=published_at,
                    collected_at=now,
                    assets=[
                        MediaAsset(asset_type="image", origin_url=image_url, sort_order=index)
                        for index, image_url in enumerate(
                            article_parser.image_urls
                            or ([article_parser.og_image_url] if article_parser.og_image_url else [])
                        )
                    ],
                    raw_payload=raw,
                    metadata={"site_name": self.config.definition.site_name},
                )
                if not self._matches_content_policy(item):
                    continue
                if normalized_start and normalized_published and normalized_published < normalized_start:
                    continue
                items.append(item)

            if not page_found_new_link:
                break
            if normalized_start and page_newest_published and page_newest_published < normalized_start:
                exhausted_window = True
            if exhausted_window:
                break
        return items

    def _build_list_page_urls(
        self,
        *,
        window_start: datetime | None,
        window_end: datetime | None,
    ) -> list[str]:
        page_limit = self._resolve_page_limit(window_start=window_start, window_end=window_end)
        return [
            self._build_paginated_url(self.config.definition.list_url, page_number)
            for page_number in range(1, page_limit + 1)
        ]

    def _resolve_page_limit(
        self,
        *,
        window_start: datetime | None,
        window_end: datetime | None,
    ) -> int:
        if window_start is None and window_end is None:
            return self.config.default_page_limit

        normalized_start = _normalize_datetime(window_start)
        normalized_end = _normalize_datetime(window_end)
        if normalized_start and normalized_end and normalized_end >= normalized_start:
            delta_days = max(1, (normalized_end - normalized_start).days + 1)
        else:
            delta_days = 1
        return min(
            self.config.window_page_limit_max,
            max(
                self.config.window_page_limit_min,
                delta_days * self.config.window_page_limit_per_day,
            ),
        )

    def _build_paginated_url(self, base_url: str, page_number: int) -> str:
        if page_number <= 1:
            return base_url
        parsed = urlparse(base_url)
        query = parse_qs(parsed.query, keep_blank_values=True)
        query[self.config.definition.page_query_param] = [str(page_number)]
        encoded_query = urlencode(query, doseq=True)
        return urlunparse(parsed._replace(query=encoded_query))

    def _fetch(self, url: str) -> str:
        request = Request(url, headers={"User-Agent": self.config.user_agent})
        with urlopen(request, timeout=self.config.timeout_seconds) as response:
            return response.read().decode("utf-8", errors="ignore")

    @staticmethod
    def _parse_datetime(value: str | None) -> datetime | None:
        if not value:
            return None
        normalized = value.replace("Z", "+00:00")
        try:
            return datetime.fromisoformat(normalized)
        except ValueError:
            return None

    def _matches_content_policy(self, item: CollectedItem) -> bool:
        haystack = " ".join(
            value for value in [item.title or "", item.body_text or ""] if value
        ).lower()
        include_keywords = tuple(keyword.lower() for keyword in self.config.definition.include_text_keywords)
        exclude_keywords = tuple(keyword.lower() for keyword in self.config.definition.exclude_text_keywords)

        if include_keywords and not any(keyword in haystack for keyword in include_keywords):
            return False
        if exclude_keywords and any(keyword in haystack for keyword in exclude_keywords):
            return False
        return True


def _normalize_datetime(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


NEWS_SITE_DEFINITIONS: dict[str, NewsSiteDefinition] = {
    "sports_chosun": NewsSiteDefinition(
        source_name="sports_chosun",
        site_name="스포츠조선",
        list_url="https://www.sportschosun.com/baseball/?action=baseball",
        article_link_keywords=("/baseball/20",),
        include_text_keywords=("야구", "KBO", "삼성", "LG", "두산", "한화", "KIA", "SSG", "롯데", "키움", "NC", "KT"),
    ),
    "sports_hankook": NewsSiteDefinition(
        source_name="sports_hankook",
        site_name="스포츠한국",
        list_url="https://sports.hankooki.com/news/articleList.html?sc_sub_section_code=S2N1&view_type=sm",
        article_link_keywords=("/news/articleView.html", "/news/articleView"),
        include_text_keywords=("야구", "KBO", "삼성", "LG", "두산", "한화", "KIA", "SSG", "롯데", "키움", "NC", "KT"),
    ),
    "isplus": NewsSiteDefinition(
        source_name="isplus",
        site_name="일간스포츠",
        list_url="https://isplus.com/article/list/isp_SC002002000",
        article_link_keywords=("/article/view/",),
        body_class_keywords=("article_body", "view_txt"),
        include_text_keywords=("야구", "KBO", "프로야구", "삼성", "LG", "두산", "한화", "KIA", "SSG", "롯데", "키움", "NC", "KT"),
        exclude_text_keywords=("연예", "영화", "방송", "OTT", "가수", "배우"),
        prefer_meta_title_when_long=True,
    ),
    "starnews": NewsSiteDefinition(
        source_name="starnews",
        site_name="스타뉴스",
        list_url="https://www.starnewskorea.com/sports/baseball",
        article_link_keywords=("/sports/20",),
        include_text_keywords=("야구", "KBO", "삼성", "LG", "두산", "한화", "KIA", "SSG", "롯데", "키움", "NC", "KT"),
    ),
    "news1_sports": NewsSiteDefinition(
        source_name="news1_sports",
        site_name="뉴스1 스포츠",
        list_url="https://www.news1.kr/sports/baseball",
        article_link_keywords=("/sports/baseball/", "/articles/"),
        include_text_keywords=("야구", "KBO", "삼성", "LG", "두산", "한화", "KIA", "SSG", "롯데", "키움", "NC", "KT"),
    ),
}
