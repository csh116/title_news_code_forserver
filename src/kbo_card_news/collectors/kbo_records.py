from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from html.parser import HTMLParser
from urllib.parse import quote, urljoin
from urllib.request import Request, urlopen

from kbo_card_news.models.collector import CollectedItem


@dataclass(frozen=True, slots=True)
class TeamDefinition:
    canonical_code: str
    team_name: str
    aliases: tuple[str, ...]
    info_layer_id: str
    info_page_team_label: str


class _SimpleTableParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.tables: list[dict[str, list[list[str]]]] = []
        self._in_table = False
        self._in_row = False
        self._cell_tag: str | None = None
        self._current_headers: list[str] = []
        self._current_rows: list[list[str]] = []
        self._current_row: list[str] = []
        self._cell_parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "table":
            self._in_table = True
            self._current_headers = []
            self._current_rows = []
        elif self._in_table and tag == "tr":
            self._in_row = True
            self._current_row = []
        elif self._in_row and tag in {"th", "td"}:
            self._cell_tag = tag
            self._cell_parts = []
        elif self._cell_tag == "br":
            self._cell_parts.append(" ")

    def handle_data(self, data: str) -> None:
        if self._cell_tag:
            clean = " ".join(data.split())
            if clean:
                self._cell_parts.append(clean)

    def handle_endtag(self, tag: str) -> None:
        if tag in {"th", "td"} and self._cell_tag == tag:
            value = " ".join(self._cell_parts).strip()
            if self._in_row:
                self._current_row.append(value)
            self._cell_tag = None
            self._cell_parts = []
        elif self._in_table and tag == "tr":
            if self._current_row:
                if not self._current_headers and self._row_looks_like_header(self._current_row):
                    self._current_headers = self._current_row
                else:
                    self._current_rows.append(self._current_row)
            self._current_row = []
            self._in_row = False
        elif tag == "table" and self._in_table:
            self.tables.append({"headers": self._current_headers, "rows": self._current_rows})
            self._in_table = False

    @staticmethod
    def _row_looks_like_header(row: list[str]) -> bool:
        header_keywords = {"팀명", "선수명", "순위", "AVG", "ERA", "일자"}
        if any(cell in header_keywords for cell in row):
            return True
        return all(not re.search(r"\d", cell) for cell in row)


class KBORecordsCollector:
    source_name = "kbo_stats"
    base_url = "https://www.koreabaseball.com"
    team_rank_url = "https://www.koreabaseball.com/Record/TeamRank/TeamRank.aspx"
    team_hitter_url = "https://www.koreabaseball.com/Record/Team/Hitter/Basic1.aspx"
    team_pitcher_url = "https://www.koreabaseball.com/Record/Team/Pitcher/Basic1.aspx"
    team_info_url = "https://www.koreabaseball.com/Kbo/League/TeamInfo.aspx"
    player_search_url = "https://www.koreabaseball.com/Player/Search.aspx?searchWord={search_word}"

    def __init__(self, user_agent: str = "Mozilla/5.0", timeout_seconds: int = 15) -> None:
        self.user_agent = user_agent
        self.timeout_seconds = timeout_seconds

    def collect_team(self, user_input: str) -> CollectedItem:
        team = self.resolve_team(user_input)
        rank_row = self._extract_team_row(self._fetch(self.team_rank_url), team)
        hitter_row = self._extract_team_row(self._fetch(self.team_hitter_url), team)
        pitcher_row = self._extract_team_row(self._fetch(self.team_pitcher_url), team)
        info_map = self._extract_team_info(self._fetch(self.team_info_url), team)

        body_text = self._build_team_summary(team, rank_row, hitter_row, pitcher_row, info_map)
        return CollectedItem(
            source_type=self.source_name,
            source_item_type="team_record",
            source_url=self.team_rank_url,
            source_external_id=team.canonical_code,
            title=f"{team.team_name} 현재 팀 정보",
            body_text=body_text,
            author_name=None,
            published_at=None,
            collected_at=datetime.utcnow(),
            raw_payload={
                "team_rank": rank_row,
                "team_hitter": hitter_row,
                "team_pitcher": pitcher_row,
                "team_info": info_map,
            },
            metadata={
                "team_code": team.canonical_code,
                "team_name": team.team_name,
            },
        )

    def collect_player(self, player_name: str) -> CollectedItem:
        team_hint, normalized_name = self._split_player_query(player_name)
        results = self._search_players(normalized_name)
        selected_results = self._select_player_results(results, team_hint)

        payload_results = []
        summaries = []
        for result in selected_results:
            try:
                detail_html = self._fetch(result["detail_url"])
                current_stats = self._extract_player_current_season_stats(detail_html)
            except ValueError:
                continue
            payload_results.append({"search_result": result, "current_stats": current_stats})
            summaries.append(self._build_player_summary(result, current_stats))

        if not payload_results:
            raise ValueError(f"{normalized_name} 선수의 현재 시즌 성적을 추출하지 못했습니다.")

        if len(payload_results) == 1:
            selected = payload_results[0]["search_result"]
            title = f"{selected['name']} 현재 시즌 성적"
            source_url = selected["detail_url"]
            source_external_id = selected["detail_url"].split("playerId=")[-1]
            metadata = {
                "player_name": selected["name"],
                "team_name": selected["team_name"],
                "position": selected["position"],
                "player_type": selected["player_type"],
                "match_count": 1,
            }
            body_text = summaries[0]
        else:
            title = f"{normalized_name} 동명이인 현재 시즌 성적"
            source_url = self.player_search_url.format(search_word=quote(normalized_name))
            source_external_id = normalized_name
            metadata = {
                "player_name": normalized_name,
                "team_name": None,
                "position": None,
                "player_type": "multiple",
                "match_count": len(payload_results),
            }
            body_text = "\n\n".join(summaries)

        return CollectedItem(
            source_type=self.source_name,
            source_item_type="player_record",
            source_url=source_url,
            source_external_id=source_external_id,
            title=title,
            body_text=body_text,
            author_name=None,
            published_at=None,
            collected_at=datetime.utcnow(),
            raw_payload={"results": payload_results},
            metadata=metadata,
        )

    def resolve_team(self, user_input: str) -> TeamDefinition:
        normalized = self._normalize_text(user_input)
        for team in TEAM_DEFINITIONS:
            candidates = {self._normalize_text(team.team_name), self._normalize_text(team.canonical_code)}
            candidates.update(self._normalize_text(alias) for alias in team.aliases)
            if normalized in candidates:
                return team
        raise ValueError(f"알 수 없는 팀명입니다: {user_input}")

    def _fetch(self, url: str) -> str:
        request = Request(url, headers={"User-Agent": self.user_agent})
        with urlopen(request, timeout=self.timeout_seconds) as response:
            return response.read().decode("utf-8", errors="ignore")

    def _extract_team_row(self, html: str, team: TeamDefinition) -> dict[str, str]:
        parser = _SimpleTableParser()
        parser.feed(html)
        for table in parser.tables:
            headers = table["headers"]
            for row in table["rows"]:
                if len(row) != len(headers):
                    continue
                row_map = dict(zip(headers, row))
                team_name = row_map.get("팀명", "")
                if self._matches_team(team, team_name):
                    return row_map
        raise ValueError(f"{team.team_name}의 팀 기록을 찾지 못했습니다.")

    def _extract_team_info(self, html: str, team: TeamDefinition) -> dict[str, str]:
        pattern = rf'<div id="{re.escape(team.info_layer_id)}" class="layerPop".*?<tbody>(.*?)</tbody>'
        match = re.search(pattern, html, re.S | re.I)
        if not match:
            raise ValueError(f"{team.team_name}의 구단 소개 블록을 찾지 못했습니다.")
        body = match.group(1)
        info: dict[str, str] = {}
        for key, value in re.findall(r"<th[^>]*>(.*?)</th>\s*<td[^>]*>(.*?)</td>", body, re.S | re.I):
            clean_key = self._clean_html_text(key)
            clean_value = self._clean_html_text(value)
            info[clean_key] = clean_value
        return info

    def _search_players(self, player_name: str) -> list[dict[str, str]]:
        url = self.player_search_url.format(search_word=quote(player_name))
        html = self._fetch(url)
        results = []
        for match in re.findall(
            r"<tr>\s*<td>(.*?)</td>\s*<td><a href=['\"]([^'\"]+)['\"]>(.*?)</a></td>\s*<td>(.*?)</td>\s*<td>(.*?)</td>",
            html,
            re.S | re.I,
        ):
            number, href, name, team_name, position = match
            clean_name = self._clean_html_text(name)
            clean_team = self._clean_html_text(team_name)
            clean_position = self._clean_html_text(position)
            if clean_name != player_name:
                continue
            results.append(
                {
                    "number": self._clean_html_text(number),
                    "name": clean_name,
                    "team_name": clean_team,
                    "position": clean_position,
                    "detail_url": urljoin(self.base_url, href),
                    "player_type": "pitcher" if "PitcherDetail" in href else "hitter",
                    "active": "/Record/Player/" in href,
                }
            )
        if not results:
            raise ValueError(f"{player_name} 선수 검색 결과가 없습니다.")
        return results

    def _select_player_results(
        self,
        results: list[dict[str, str]],
        team_hint: str | None,
    ) -> list[dict[str, str]]:
        active_results = [result for result in results if result["active"]]
        candidate_results = active_results or results

        if team_hint:
            filtered = [
                result
                for result in candidate_results
                if self._normalize_text(result["team_name"]) == self._normalize_text(team_hint)
            ]
            if filtered:
                return filtered

        exact_name_results = candidate_results
        if len(exact_name_results) == 1:
            return exact_name_results
        return exact_name_results

    def _extract_player_current_season_stats(self, html: str) -> dict[str, str]:
        section_start = html.find("성적</h6>")
        if section_start == -1:
            raise ValueError("선수 현재 시즌 성적 섹션을 찾지 못했습니다.")
        section_end = html.find("최근 10경기", section_start)
        snippet = html[section_start:section_end if section_end != -1 else len(html)]
        parser = _SimpleTableParser()
        parser.feed(snippet)
        stats: dict[str, str] = {}
        for table in parser.tables[:2]:
            headers = table["headers"]
            if not table["rows"]:
                continue
            row = table["rows"][0]
            if len(headers) != len(row):
                continue
            stats.update(dict(zip(headers, row)))
        if not stats:
            raise ValueError("선수 현재 시즌 성적을 추출하지 못했습니다.")
        return stats

    def _build_team_summary(
        self,
        team: TeamDefinition,
        rank_row: dict[str, str],
        hitter_row: dict[str, str],
        pitcher_row: dict[str, str],
        info_map: dict[str, str],
    ) -> str:
        return (
            f"{team.team_name}\n"
            f"현재 순위: {rank_row.get('순위')}위\n"
            f"경기: {rank_row.get('경기')} / 승-패-무: {rank_row.get('승')}-{rank_row.get('패')}-{rank_row.get('무')}\n"
            f"승률: {rank_row.get('승률')} / 게임차: {rank_row.get('게임차')}\n"
            f"최근10경기: {rank_row.get('최근10경기')} / 연속: {rank_row.get('연속')}\n"
            f"홈: {rank_row.get('홈')} / 방문: {rank_row.get('방문')}\n"
            f"감독: {info_map.get('감독', '-')}\n"
            f"단장: {info_map.get('단장', '-')}\n"
            f"창단년도: {info_map.get('창단년도', '-')}\n"
            f"연고지역: {info_map.get('연고지역', '-')}\n"
            f"사무실: {self._pick_team_office(info_map)}\n"
            f"홈페이지: {info_map.get('홈페이지', '-')}\n"
            f"팀 타율: {hitter_row.get('AVG')} / 득점: {hitter_row.get('R')} / 안타: {hitter_row.get('H')} / 홈런: {hitter_row.get('HR')} / 타점: {hitter_row.get('RBI')}\n"
            f"팀 방어율: {pitcher_row.get('ERA')} / 실점: {pitcher_row.get('R')} / 자책: {pitcher_row.get('ER')} / 탈삼진: {pitcher_row.get('SO')} / WHIP: {pitcher_row.get('WHIP')}"
        )

    def _build_player_summary(self, result: dict[str, str], current_stats: dict[str, str]) -> str:
        base = (
            f"{result['name']} / {result['team_name']} / {result['position']}\n"
            f"선수 유형: {result['player_type']}\n"
            f"등번호: {result['number']}\n"
        )
        lines = [base.rstrip()]
        for key, value in current_stats.items():
            lines.append(f"{key}: {value}")
        return "\n".join(lines)

    def _matches_team(self, team: TeamDefinition, row_team_name: str) -> bool:
        normalized_row = self._normalize_text(row_team_name)
        return normalized_row in {
            self._normalize_text(team.canonical_code),
            self._normalize_text(team.team_name),
            *(self._normalize_text(alias) for alias in team.aliases),
            self._normalize_text(team.info_page_team_label),
        }

    @staticmethod
    def _normalize_text(value: str) -> str:
        return "".join(value.lower().split())

    @staticmethod
    def _clean_html_text(value: str) -> str:
        text = re.sub(r"<br\s*/?>", " ", value, flags=re.I)
        text = re.sub(r"<[^>]+>", "", text)
        replacements = {
            "&nbsp;": " ",
            "&amp;": "&",
            "&#39;": "'",
            "&quot;": '"',
            "&hellip;": "...",
        }
        for old, new in replacements.items():
            text = text.replace(old, new)
        return " ".join(text.split())

    def _split_player_query(self, value: str) -> tuple[str | None, str]:
        cleaned = " ".join(value.split())
        tokens = cleaned.split()
        if len(tokens) < 2:
            return None, cleaned

        first_team = self._resolve_team_hint(tokens[0])
        last_team = self._resolve_team_hint(tokens[-1])
        if first_team:
            return first_team, " ".join(tokens[1:])
        if last_team:
            return last_team, " ".join(tokens[:-1])
        return None, cleaned

    def _resolve_team_hint(self, token: str) -> str | None:
        normalized = self._normalize_text(token)
        for team in TEAM_DEFINITIONS:
            candidates = {
                self._normalize_text(team.team_name),
                self._normalize_text(team.info_page_team_label),
                self._normalize_text(team.canonical_code),
                *(self._normalize_text(alias) for alias in team.aliases),
            }
            if normalized in candidates:
                return team.info_page_team_label
        return None

    @staticmethod
    def _pick_team_office(info_map: dict[str, str]) -> str:
        for key in ("구단사무실", "광주 사무실", "창원 사무실", "분당 사무실"):
            if key in info_map:
                return info_map[key]
        return "-"


TEAM_DEFINITIONS: tuple[TeamDefinition, ...] = (
    TeamDefinition("KIA", "KIA 타이거즈", ("기아", "타이거즈", "HT"), "layerPopHT", "KIA"),
    TeamDefinition("SS", "삼성 라이온즈", ("삼성", "라이온즈"), "layerPopSS", "삼성"),
    TeamDefinition("LG", "LG 트윈스", ("엘지", "트윈스"), "layerPopLG", "LG"),
    TeamDefinition("OB", "두산 베어스", ("두산", "베어스"), "layerPopOB", "두산"),
    TeamDefinition("KT", "KT 위즈", ("케이티", "위즈"), "layerPopKT", "KT"),
    TeamDefinition("SSG", "SSG 랜더스", ("랜더스", "SK"), "layerPopSK", "SSG"),
    TeamDefinition("LT", "롯데 자이언츠", ("롯데", "자이언츠"), "layerPopLT", "롯데"),
    TeamDefinition("HH", "한화 이글스", ("한화", "이글스"), "layerPopHH", "한화"),
    TeamDefinition("NC", "NC 다이노스", ("엔씨", "다이노스"), "layerPopNC", "NC"),
    TeamDefinition("WO", "키움 히어로즈", ("키움", "히어로즈"), "layerPopWO", "키움"),
)
