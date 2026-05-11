from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from html.parser import HTMLParser
from urllib.parse import urljoin
from urllib.request import Request, urlopen

from kbo_card_news.collectors.base import BaseCollector
from kbo_card_news.models.collector import CollectedItem, MediaAsset


@dataclass(slots=True)
class DCInsideCollectorConfig:
    gallery_list_url: str
    user_agent: str = "Mozilla/5.0"
    timeout_seconds: int = 10
    max_items: int = 20


class _DCInsideListParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.items: list[dict[str, str]] = []
        self._current_href: str | None = None
        self._capture_title = False
        self._current_title = ""

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attrs_map = dict(attrs)
        if tag == "a":
            href = attrs_map.get("href")
            class_name = attrs_map.get("class", "")
            if href and ("view" in href or "title" in class_name):
                self._current_href = href
                self._capture_title = True
                self._current_title = ""

    def handle_data(self, data: str) -> None:
        if self._capture_title:
            self._current_title += data.strip()

    def handle_endtag(self, tag: str) -> None:
        if tag == "a" and self._current_href:
            title = self._current_title.strip()
            if title:
                self.items.append({"href": self._current_href, "title": title})
            self._current_href = None
            self._capture_title = False
            self._current_title = ""


class _DCInsideDetailParser(HTMLParser):
    def __init__(self, article_url: str) -> None:
        super().__init__()
        self.article_url = article_url
        self.title: str | None = None
        self.author_name: str | None = None
        self.published_at: str | None = None
        self.body_parts: list[str] = []
        self.image_urls: list[str] = []
        self._capture_title = False
        self._capture_author = False
        self._capture_body = False
        self._body_depth = 0
        self._ignore_data_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attrs_map = dict(attrs)
        class_name = " ".join(
            value for key, value in attrs if key in {"class", "id"} and value
        ).lower()
        if tag in {"script", "style"}:
            self._ignore_data_depth += 1
            return

        if tag == "span" and "title_subject" in class_name:
            self._capture_title = True
        elif tag == "span" and "nickname" in class_name:
            self._capture_author = True
        elif tag == "span" and "gall_date" in class_name:
            self.published_at = attrs_map.get("title") or attrs_map.get("data-time")
        elif tag == "div" and "write_div" in class_name:
            self._capture_body = True
            self._body_depth = 1
            return
        elif self._capture_body and tag in {
            "div",
            "p",
            "span",
            "strong",
            "em",
            "blockquote",
            "ul",
            "ol",
            "li",
            "br",
        }:
            self._body_depth += 1

        if tag == "img" and self._capture_body:
            src = attrs_map.get("src") or attrs_map.get("data-src")
            if src:
                resolved = urljoin(self.article_url, src)
                if resolved not in self.image_urls:
                    self.image_urls.append(resolved)

    def handle_data(self, data: str) -> None:
        if self._ignore_data_depth > 0:
            return
        clean = " ".join(data.split())
        if not clean:
            return
        if self._capture_title and not self.title:
            self.title = clean
        elif self._capture_author and not self.author_name:
            self.author_name = clean
        elif self._capture_body:
            self.body_parts.append(clean)

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style"} and self._ignore_data_depth > 0:
            self._ignore_data_depth -= 1
            return
        if tag == "span" and self._capture_title:
            self._capture_title = False
        elif tag == "span" and self._capture_author:
            self._capture_author = False
        elif self._capture_body and tag in {
            "div",
            "p",
            "span",
            "strong",
            "em",
            "blockquote",
            "ul",
            "ol",
            "li",
            "br",
        }:
            self._body_depth -= 1
            if self._body_depth <= 0:
                self._capture_body = False
                self._body_depth = 0


class DCInsideCollector(BaseCollector):
    source_name = "dcinside"

    def __init__(self, config: DCInsideCollectorConfig) -> None:
        self.config = config

    def collect(self) -> list[CollectedItem]:
        html = self._fetch(self.config.gallery_list_url)
        parser = _DCInsideListParser()
        parser.feed(html)

        items: list[CollectedItem] = []
        now = datetime.utcnow()
        for raw in parser.items[: self.config.max_items]:
            href = urljoin(self.config.gallery_list_url, raw["href"])
            title = raw["title"]
            body_text = None
            author_name = None
            published_at = None
            assets = []
            try:
                detail_html = self._fetch(href)
                detail_parser = _DCInsideDetailParser(href)
                detail_parser.feed(detail_html)
                title = detail_parser.title or title
                body_text = self._clean_body_text("\n".join(detail_parser.body_parts))
                author_name = detail_parser.author_name
                published_at = self._parse_datetime(detail_parser.published_at)
                assets = [
                    MediaAsset(asset_type="image", origin_url=image_url, sort_order=index)
                    for index, image_url in enumerate(detail_parser.image_urls)
                ]
            except Exception:  # noqa: BLE001
                pass
            items.append(
                CollectedItem(
                    source_type=self.source_name,
                    source_item_type="post",
                    source_url=href,
                    source_external_id=None,
                    title=title,
                    body_text=body_text,
                    author_name=author_name,
                    published_at=published_at,
                    collected_at=now,
                    assets=assets,
                    raw_payload=raw,
                )
            )
        return items

    @staticmethod
    def _parse_datetime(value: str | None) -> datetime | None:
        if not value:
            return None
        normalized = value.replace(".", "-")
        try:
            return datetime.fromisoformat(normalized)
        except ValueError:
            return None

    @staticmethod
    def _clean_body_text(value: str) -> str | None:
        if not value:
            return None
        stop_keywords = (
            "\n추천검색",
            "\n추천 비추천",
            "\n개념 추천",
            "\n실베추",
            "\n공유",
            "\n스크랩",
            "\n신고",
            "\n원본 첨부파일",
            "\n본문 이미지 다운로드",
            "\n- dc official App",
        )
        cleaned = value
        for keyword in stop_keywords:
            index = cleaned.find(keyword)
            if index != -1:
                cleaned = cleaned[:index]
                break
        cleaned = "\n".join(line.strip() for line in cleaned.splitlines() if line.strip())
        return cleaned or None

    def _fetch(self, url: str) -> str:
        request = Request(url, headers={"User-Agent": self.config.user_agent})
        with urlopen(request, timeout=self.config.timeout_seconds) as response:
            return response.read().decode("utf-8", errors="ignore")
