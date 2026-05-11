from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from kbo_card_news.models.collector import CollectedItem


@dataclass(frozen=True, slots=True)
class StadiumDefinition:
    team_code: str
    team_name: str
    stadium_name: str
    latitude: float
    longitude: float
    aliases: tuple[str, ...]


@dataclass(slots=True)
class StadiumWeatherCollectorConfig:
    user_agent: str = "Mozilla/5.0"
    timeout_seconds: int = 10
    weather_api_base_url: str = "https://api.open-meteo.com/v1/forecast"


class StadiumWeatherCollector:
    source_name = "kma_weather"

    def __init__(self, config: StadiumWeatherCollectorConfig | None = None) -> None:
        self.config = config or StadiumWeatherCollectorConfig()

    def collect_for_stadium(self, user_input: str) -> CollectedItem:
        stadiums = self._find_matching_stadiums(user_input)
        if not stadiums:
            raise ValueError(f"알 수 없는 야구장 또는 구단 이름입니다: {user_input}")

        summaries: list[str] = []
        payloads: list[dict] = []
        for stadium in stadiums:
            payload = self._fetch_weather_payload(stadium)
            current = payload.get("current", {})
            current_units = payload.get("current_units", {})
            summaries.append(self._build_summary(stadium, current, current_units))
            payloads.append(payload)

        title = (
            f"{stadiums[0].stadium_name} 현재 날씨"
            if len(stadiums) == 1
            else f"{stadiums[0].stadium_name} 현재 날씨 (공동 홈구장)"
        )
        now = datetime.utcnow()
        return CollectedItem(
            source_type=self.source_name,
            source_item_type="weather",
            source_url=self._build_api_url(stadiums[0]),
            source_external_id=",".join(stadium.team_code for stadium in stadiums),
            title=title,
            body_text="\n\n".join(summaries),
            author_name=None,
            published_at=None,
            collected_at=now,
            raw_payload={"results": payloads},
            metadata={
                "team_code": stadiums[0].team_code if len(stadiums) == 1 else None,
                "team_name": stadiums[0].team_name if len(stadiums) == 1 else None,
                "stadium_name": stadiums[0].stadium_name,
                "provider": "open-meteo",
                "latitude": stadiums[0].latitude,
                "longitude": stadiums[0].longitude,
                "match_count": len(stadiums),
                "matched_teams": [stadium.team_name for stadium in stadiums],
            },
        )

    def resolve_stadium(self, user_input: str) -> StadiumDefinition:
        stadiums = self._find_matching_stadiums(user_input)
        if not stadiums:
            raise ValueError(f"알 수 없는 야구장 또는 구단 이름입니다: {user_input}")
        return stadiums[0]

    def _find_matching_stadiums(self, user_input: str) -> list[StadiumDefinition]:
        normalized = self._normalize_text(user_input)
        if not normalized:
            raise ValueError("야구장 또는 구단 이름을 입력해야 합니다.")

        matched: list[StadiumDefinition] = []
        for stadium in STADIUM_DEFINITIONS:
            alias_set = {self._normalize_text(alias) for alias in stadium.aliases}
            alias_set.add(self._normalize_text(stadium.team_name))
            alias_set.add(self._normalize_text(stadium.team_code))
            alias_set.add(self._normalize_text(stadium.stadium_name))
            if normalized in alias_set:
                matched.append(stadium)
        return matched

    def _fetch_weather_payload(self, stadium: StadiumDefinition) -> dict:
        request = Request(
            self._build_api_url(stadium),
            headers={"User-Agent": self.config.user_agent},
        )
        with urlopen(request, timeout=self.config.timeout_seconds) as response:
            return json.loads(response.read().decode("utf-8"))

    def _build_api_url(self, stadium: StadiumDefinition) -> str:
        query = urlencode(
            {
                "latitude": stadium.latitude,
                "longitude": stadium.longitude,
                "current": ",".join(
                    [
                        "temperature_2m",
                        "relative_humidity_2m",
                        "apparent_temperature",
                        "precipitation",
                        "weather_code",
                        "wind_speed_10m",
                    ]
                ),
                "timezone": "Asia/Seoul",
                "forecast_days": 1,
            }
        )
        return f"{self.config.weather_api_base_url}?{query}"

    @staticmethod
    def _build_summary(
        stadium: StadiumDefinition,
        current: dict,
        current_units: dict,
    ) -> str:
        return (
            f"{stadium.stadium_name} / {stadium.team_name}\n"
            f"측정 시각: {current.get('time')}\n"
            f"기온: {current.get('temperature_2m')} {current_units.get('temperature_2m', '')}\n"
            f"체감온도: {current.get('apparent_temperature')} {current_units.get('apparent_temperature', '')}\n"
            f"습도: {current.get('relative_humidity_2m')} {current_units.get('relative_humidity_2m', '')}\n"
            f"강수량: {current.get('precipitation')} {current_units.get('precipitation', '')}\n"
            f"풍속: {current.get('wind_speed_10m')} {current_units.get('wind_speed_10m', '')}\n"
            f"날씨코드: {current.get('weather_code')}"
        )

    @staticmethod
    def _normalize_text(value: str) -> str:
        return "".join(value.lower().split())


STADIUM_DEFINITIONS: tuple[StadiumDefinition, ...] = (
    StadiumDefinition("KIA", "KIA 타이거즈", "광주-KIA 챔피언스 필드", 35.1681, 126.8891, ("기아", "kia", "광주", "챔필", "광주기아챔피언스필드")),
    StadiumDefinition("SS", "삼성 라이온즈", "대구 삼성 라이온즈 파크", 35.8419, 128.6811, ("삼성", "대구", "라팍", "삼성라이온즈파크")),
    StadiumDefinition("LG", "LG 트윈스", "잠실야구장", 37.5121, 127.0718, ("lg", "엘지", "잠실", "잠실야구장")),
    StadiumDefinition("OB", "두산 베어스", "잠실야구장", 37.5121, 127.0718, ("두산", "잠실", "잠실야구장")),
    StadiumDefinition("KT", "KT 위즈", "수원 KT 위즈 파크", 37.2998, 127.0097, ("kt", "케이티", "수원", "위즈파크", "수원kt위즈파크")),
    StadiumDefinition("SSG", "SSG 랜더스", "인천 SSG 랜더스필드", 37.4369, 126.6934, ("ssg", "인천", "랜더스필드", "ssg랜더스필드")),
    StadiumDefinition("LT", "롯데 자이언츠", "사직야구장", 35.1940, 129.0615, ("롯데", "사직", "사직야구장")),
    StadiumDefinition("HH", "한화 이글스", "대전 한화생명 볼파크", 36.3171, 127.4304, ("한화", "대전", "볼파크", "한화생명볼파크")),
    StadiumDefinition("NC", "NC 다이노스", "창원 NC 파크", 35.2226, 128.5829, ("nc", "엔씨", "창원", "nc파크", "창원nc파크")),
    StadiumDefinition("WO", "키움 히어로즈", "고척스카이돔", 37.4982, 126.8671, ("키움", "고척", "고척돔", "고척스카이돔")),
)
