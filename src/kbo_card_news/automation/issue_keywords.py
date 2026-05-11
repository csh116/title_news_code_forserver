from __future__ import annotations

PRIMARY_TEAMS = ("KIA", "한화", "LG", "롯데")
SECONDARY_TEAMS = ("두산", "삼성")
OTHER_TEAMS = ("KT", "NC", "SSG", "키움")
ALL_TEAMS = PRIMARY_TEAMS + SECONDARY_TEAMS + OTHER_TEAMS

TEAM_ALIASES: dict[str, tuple[str, ...]] = {
    "KIA": ("KIA", "기아", "타이거즈"),
    "한화": ("한화", "이글스"),
    "LG": ("LG", "엘지", "트윈스"),
    "롯데": ("롯데", "자이언츠"),
    "두산": ("두산", "베어스"),
    "삼성": ("삼성", "라이온즈"),
    "KT": ("KT", "케이티", "위즈"),
    "NC": ("NC", "엔씨", "다이노스"),
    "SSG": ("SSG", "랜더스"),
    "키움": ("키움", "히어로즈"),
}

INJURY_KEYWORDS = (
    "부상",
    "인대",
    "수술",
    "시즌 아웃",
    "시즌아웃",
    "말소",
    "이탈",
    "병원",
    "MRI",
    "검진",
    "구급차",
    "앰뷸런스",
    "통증",
)
CONTROVERSY_KEYWORDS = (
    "징계",
    "논란",
    "사과",
    "욕설",
    "물의",
    "도박",
    "불법",
    "사건",
    "폭행",
    "계약 해지",
    "감독 경질",
)
DRAMA_KEYWORDS = (
    "끝내기",
    "역전",
    "대승",
    "완승",
    "스윕",
    "연승",
    "연패 탈출",
    "혈투",
    "위닝",
)
RECORD_KEYWORDS = (
    "기록",
    "신기록",
    "최다",
    "통산",
    "홈런",
    "세이브",
    "MVP",
    "호투",
)
ROSTER_KEYWORDS = (
    "복귀",
    "콜업",
    "엔트리",
    "등록",
    "영입",
    "방출",
    "트레이드",
    "교체",
    "대체",
    "외국인",
    "거취",
    "보직 변경",
)
LOW_PRIORITY_KEYWORDS = (
    "이벤트",
    "상품",
    "협업",
    "굿즈",
    "팬 사인회",
    "시구",
    "중계",
    "티켓",
    "프로모션",
    "행사",
    "프리뷰",
    "선발 예고",
    "관전 포인트",
    "순위표",
    "종합",
)
FUTURES_KEYWORDS = ("퓨처스", "2군", "육성선수", "교육리그", "재활군")

KEYWORD_GROUPS: dict[str, tuple[str, ...]] = {
    "injury": INJURY_KEYWORDS,
    "controversy": CONTROVERSY_KEYWORDS,
    "drama": DRAMA_KEYWORDS,
    "record": RECORD_KEYWORDS,
    "roster": ROSTER_KEYWORDS,
    "low_priority": LOW_PRIORITY_KEYWORDS,
}
