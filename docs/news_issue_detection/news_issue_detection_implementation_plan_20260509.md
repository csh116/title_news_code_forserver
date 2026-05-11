# 뉴스 이슈 감지 구조 구현 설계안 2026-05-09

이 문서는 `docs/news_issue_detection_redesign_notes_20260509.md`의 방향을 실제 코드에 반영하기 위한 구현 설계안이다.

목표는 기존 자동화 MVP를 깨지 않고, watcher를 "최근 24시간 후보 추천" 중심에서 "방금 나온 이슈 감지" 중심으로 점진 전환하는 것이다.

## 핵심 목표

```text
기존:
30분마다 최근 24시간 기사를 모아 카드뉴스 후보를 추천

신규:
5분마다 새로 발행된 기사만 수집하고,
최근 24시간 맥락과 시간대별 확산 흐름을 참고해 후보를 압축한 뒤,
Gemini 2차 판단을 통과한 경우에만 Discord 알림
```

운영 원칙:

```text
watcher 단계에서 모든 후보 선정에 Gemini를 쓰지 않는다.
Gemini는 로컬 rule을 통과한 소수 fresh 후보의 2차 적합성 판단에만 사용한다.
사용자가 제작 승인한 이후에는 카드뉴스 품질 개선 단계에서도 Gemini를 사용한다.
기존 watch-cycle은 당장 제거하지 않고 신규 watch-fresh-cycle과 병행 검증한다.
```

## 현재 코드 기준 진단

현재 watcher 진입점:

```text
src/kbo_card_news/automation/cli.py
  -> _handle_watch_cycle()
  -> _run_watch_cycle_under_lock()
  -> news_watcher.watch_once()
```

현재 후보 생성 흐름:

```text
src/kbo_card_news/automation/news_watcher.py
  -> watch_once()
  -> pipeline_runner.generate_topic_candidates()

src/kbo_card_news/automation/pipeline_runner.py
  -> generate_topic_candidates()
  -> tests/manual_checks/manual_check_batch_topic_selection.py 실행

tests/manual_checks/manual_check_batch_topic_selection.py
  -> choose_selection_engine()
  -> GEMINI_API_KEY가 있으면 GeminiBatchIssueSelectionEngine 사용
```

즉, `.env`에 `GEMINI_API_KEY`가 있으면 30분마다 실행되는 watcher 후보 선정에서도 Gemini API를 쓸 수 있다.

현재 수집 DB:

```text
outputs/source_collection.db
```

관련 repository:

```text
src/kbo_card_news/pipeline/storage.py
  -> SQLiteSourceItemRepository
  -> source_items
  -> source_collection_windows
```

현재 automation job DB:

```text
outputs/automation/automation_state.db
```

관련 repository:

```text
src/kbo_card_news/automation/job_state.py
  -> AutomationJobRepository
  -> automation_jobs
  -> automation_job_articles
  -> automation_job_events
```

현재 Discord 알림:

```text
src/kbo_card_news/automation/discord_bot.py
  -> build_job_message()
  -> build_job_notification_payload()
```

## 구현 방향 요약

1차 구현은 기존 `watch-cycle`을 직접 바꾸지 않는다.

신규 명령을 추가한다.

```text
watch-fresh-cycle
```

신규 흐름:

```text
watch-fresh-cycle
-> 직전 5~10분 collection window 계산
-> 뉴스 사이트 collector 실행
-> source_collection.db에 신규 기사 ingest
-> 이번 실행에서 새로 insert된 기사만 fresh articles로 사용
-> 최근 24시간 source_items를 context articles로 조회
-> fresh articles를 issue group으로 묶음
-> 로컬 rule 기반 issue_score 계산
-> threshold 이상 후보만 Gemini 2차 판단
-> Gemini approve 후보만 automation job 생성
-> notify 옵션이 있으면 Discord 알림
```

## 1단계: Gemini 후보 선정 기본 off

### 수정 대상

```text
tests/manual_checks/manual_check_batch_topic_selection.py
```

### 현재 문제 함수

```python
def choose_selection_engine():
    load_default_env(ROOT_DIR)
    if os.getenv("GEMINI_API_KEY"):
        return GeminiBatchIssueSelectionEngine()
    return HeuristicBatchIssueSelectionEngine()
```

### 변경 설계

CLI 옵션을 추가한다.

```text
--selection-engine heuristic
--selection-engine gemini
```

기본값:

```text
heuristic
```

변경 후 정책:

```text
기본 실행: HeuristicBatchIssueSelectionEngine
명시적으로 --selection-engine gemini를 준 경우만 GeminiBatchIssueSelectionEngine
gemini 선택인데 GEMINI_API_KEY가 없으면 명확한 오류 발생
```

### pipeline_runner 연동

수정 대상:

```text
src/kbo_card_news/automation/pipeline_runner.py
```

`generate_topic_candidates()`에 선택 옵션을 추가한다.

```python
def generate_topic_candidates(
    *,
    approval_run_dir: str | Path | None = None,
    window_start_kst: str | None = None,
    window_end_kst: str | None = None,
    candidate_count: int | None = None,
    selection_engine: str = "heuristic",
) -> CandidateGenerationResult:
```

manual script 실행 args에 추가:

```text
--selection-engine heuristic
```

### cli 연동

수정 대상:

```text
src/kbo_card_news/automation/cli.py
```

기존 명령에 옵션 추가:

```text
candidates --selection-engine heuristic|gemini
watch-once --selection-engine heuristic|gemini
watch-cycle --selection-engine heuristic|gemini
```

기본값은 모두 `heuristic`이다.

### 완료 기준

```text
GEMINI_API_KEY가 있어도 watch-cycle 기본 실행은 Gemini를 호출하지 않는다.
Gemini를 쓰려면 명령에 --selection-engine gemini가 명시되어야 한다.
```

## 2단계: source DB 시간 범위 조회 추가

fresh watcher는 전체 `list_items()`를 가져오면 불필요하게 무겁다.

### 수정 대상

```text
src/kbo_card_news/pipeline/storage.py
```

### 추가 메서드

```python
def list_items_published_between(
    self,
    *,
    window_start: datetime,
    window_end: datetime,
    limit: int = 500,
) -> list[PersistedSourceItem]:
```

조회 기준:

```text
published_at이 있으면 published_at 기준
published_at이 없으면 collected_at 기준 fallback
```

SQL 개념:

```sql
SELECT *
FROM source_items
WHERE COALESCE(published_at, collected_at) >= ?
  AND COALESCE(published_at, collected_at) < ?
ORDER BY COALESCE(published_at, collected_at) ASC, id ASC
LIMIT ?
```

### 권장 인덱스

`ensure_schema()`에 추가:

```sql
CREATE INDEX IF NOT EXISTS idx_source_items_published_collected
ON source_items(published_at, collected_at);
```

SQLite에서 `COALESCE` 조건 최적화가 제한될 수 있으므로, 데이터가 커지면 generated column 또는 별도 `effective_published_at` 컬럼을 검토한다. MVP 단계에서는 위 인덱스와 limit로 충분하다.

### 완료 기준

```text
최근 5~10분 신규 기사 조회 가능
최근 24시간 context 기사 조회 가능
최근 30분/1시간/3시간/6시간/24시간 시간대별 feature 계산 가능
기존 list_items() 동작 유지
```

## 3단계: fresh issue detector 모듈 추가

### 신규 파일

```text
src/kbo_card_news/automation/fresh_issue_detector.py
```

### 주요 dataclass

```python
@dataclass(slots=True)
class FreshIssueDetectorConfig:
    collection_window_minutes: int = 10
    context_window_hours: int = 24
    duplicate_lookback_hours: int = 72
    min_issue_score: float = 65.0
    max_jobs: int = 5
    gemini_review_enabled: bool = True
```

```python
@dataclass(slots=True)
class FreshIssueCandidate:
    issue_id: str
    topic_name: str
    representative_article_id: str
    fresh_articles: list[PersistedSourceItem]
    context_articles: list[PersistedSourceItem]
    matched_teams: list[str]
    matched_keywords: list[str]
    issue_score: float
    gemini_decision: str | None
    gemini_confidence: float | None
    notification_level: str
    reasons: list[str]
    risk_flags: list[str]
    metadata: dict[str, Any]
```

```python
@dataclass(slots=True)
class FreshWatchResult:
    collection_window_start: datetime
    collection_window_end: datetime
    context_window_start: datetime
    context_window_end: datetime
    collected_count: int
    inserted_count: int
    duplicate_count: int
    candidate_count: int
    created_jobs: list[AutomationJob]
    duplicate_jobs: list[AutomationJob]
    skipped_count: int
    collector_errors: list[str]
```

### 공개 함수

```python
def watch_fresh_once(
    *,
    job_repository: AutomationJobRepository,
    source_db_path: str | Path,
    config: FreshIssueDetectorConfig | None = None,
    now: datetime | None = None,
) -> FreshWatchResult:
```

### 내부 처리 순서

```text
1. now를 KST 기준으로 분 단위 정규화
2. collection window = now - collection_window_minutes ~ now
3. context window = now - context_window_hours ~ now
4. CollectorService(build_news_collectors()).collect_all(collection window)
5. SourceItemIngestionService.ingest()
6. inserted 항목만 fresh articles로 사용
7. fresh articles가 없으면 즉시 종료
8. source repository에서 context window articles 조회
9. fresh articles를 issue group으로 묶음
10. 각 group에 issue_score 계산
11. threshold 미만 제외
12. threshold 이상 후보를 Gemini 2차 판단에 전달
13. Gemini reject/hold 후보 제외
14. automation job 중복 검사
15. job 생성
```

## 4단계: issue grouping 설계

fresh articles는 단순 URL 단위가 아니라 같은 이슈 단위로 묶어야 한다.

### 1차 grouping key

다음 값들을 조합한다.

```text
matched_team
strong_keyword_group
player_candidate
normalized_title_tokens
```

예:

```text
LG + 말소 + 오스틴
한화 + 복귀 + 류현진
KIA + 부상 + 김도영
롯데 + 끝내기 + 전준우
```

### 팀 추출

기존 `topic_ranker.py`의 팀 목록을 재사용하거나 공통 모듈로 분리한다.

현재 위치:

```text
src/kbo_card_news/automation/topic_ranker.py
  -> PRIMARY_TEAMS
  -> SECONDARY_TEAMS
  -> OTHER_TEAMS
  -> ALL_TEAMS
```

권장:

```text
src/kbo_card_news/automation/issue_keywords.py
```

로 팀/키워드 상수를 분리하고, `topic_ranker.py`와 `fresh_issue_detector.py`가 같이 사용한다.

### 키워드 그룹

초기 그룹:

```text
injury:
  부상, 인대, 수술, 시즌 아웃, 시즌아웃, 말소, 이탈, 병원, MRI, 검진, 통증

controversy:
  징계, 논란, 사과, 욕설, 물의, 도박, 불법, 사건, 발언

drama:
  끝내기, 역전, 대승, 완승, 스윕, 연승, 연패 탈출, 혈투, 위닝

record:
  기록, 신기록, 최다, 통산, 홈런, 세이브, MVP, 호투

roster:
  복귀, 영입, 방출, 교체, 대체, 외국인, 콜업, 엔트리, 거취

low_priority:
  이벤트, 상품, 협업, 관중, 중계, 후보, 행사, 프리뷰, 선발 예고
```

### 선수명 추출

MVP에서는 완벽한 NER를 하지 않는다.

간단 규칙:

```text
제목에서 2~4글자 한글 토큰 추출
팀명, 일반 야구 용어, 조사성 토큰 제외
강한 키워드 주변 토큰 우선
```

정확도가 낮으면 group key에서 선수명은 보조 신호로만 쓴다.

## 5단계: issue_score 계산

### 점수 범위

```text
0~100
```

### 초기 공식

```text
issue_score =
  fresh_article_score
+ source_diversity_score
+ context_growth_score
+ keyword_score
+ team_fit_score
+ recency_score
- low_priority_penalty
- single_source_penalty
- stale_or_duplicate_penalty
```

### 세부 점수

fresh_article_score:

```text
fresh 기사 1건: +10
fresh 기사 2건: +18
fresh 기사 3건 이상: +25
```

source_diversity_score:

```text
1개 매체: +0
2개 매체: +12
3개 이상: +20
```

context_growth_score:

```text
최근 24시간 관련 기사를 조회한 뒤 시간대별로 계산한다.
최근 1~3시간 증가는 빠른 확산 신호로 보고,
최근 6~24시간 분포는 오래된 이슈/후속 이슈/반복 이슈 판별에 사용한다.

최근 1~3시간 관련 기사 2건: +8
최근 1~3시간 관련 기사 3~4건: +15
최근 1~3시간 관련 기사 5건 이상: +22
```

keyword_score:

```text
injury: +28
controversy: +25
roster: +20
record: +18
drama: +16
```

team_fit_score:

```text
KIA, 한화, LG, 롯데: +12
두산, 삼성: +8
KT, NC, SSG, 키움: +5
팀 불명: -8
```

recency_score:

```text
대표 기사 10분 이내: +10
대표 기사 30분 이내: +6
대표 기사 60분 이내: +3
```

감점:

```text
low_priority 키워드 포함: -18
단일 기사 + 단일 매체 + 강한 키워드 없음: -20
퓨처스/2군 중심: -15
경기결과 종합/순위표/프리뷰/선발 예고: -15
최근 72시간 유사 job 존재: -100
```

### notification level

```text
immediate:
  issue_score >= 75
  또는 injury/controversy 강한 키워드가 있고 issue_score >= 65

watch:
  issue_score >= 60

digest:
  issue_score < 60
```

fresh watcher에서는 기본적으로 `digest`는 job으로 만들지 않는다.

### threshold

초기 운영값:

```text
min_issue_score = 65
```

알림 과다 발생 시:

```text
min_issue_score = 70 또는 75
```

## 5-1단계: Gemini 2차 이슈 판단

로컬 `issue_score`가 threshold를 넘은 후보만 Gemini에 전달한다.
Gemini는 점수를 다시 매기는 모델이 아니라 Discord 알림 전 최종 게이트다.

### 입력 요약

```text
후보 이슈명
fresh articles
최근 24시간 관련 기사 중 관련도 높은 기사
최근 30분/1시간/3시간/6시간/24시간 기사 수와 매체 수
로컬 issue_score와 score_reasons
risk_flags
최근 72시간 유사 알림 이력
가능하면 과거 승인/거절/성과 데이터
```

### 출력 JSON

```json
{
  "decision": "approve",
  "confidence": 0.86,
  "notification_level": "immediate",
  "final_score": 82,
  "issue_type": "roster",
  "is_new_issue": true,
  "is_card_news_worthy": true,
  "is_time_sensitive": true,
  "main_reason": "외국인 주전 말소 이슈로 팬 반응 가능성과 즉시성이 높음",
  "supporting_reasons": ["상태 변화가 명확함", "최근 짧은 시간에 복수 매체 보도"],
  "risk_flags": ["구단 공식 발표 여부 확인 필요"],
  "suggested_topic_name": "LG 오스틴 말소",
  "suggested_angle": "말소 배경과 LG 타선 영향",
  "learning_notes": ["외국인 주전 말소는 단일 기사여도 승인 가능성이 높음"]
}
```

`decision`은 `approve`, `reject`, `hold` 중 하나다.
`approve`만 job 생성과 Discord 알림 대상으로 본다.
`hold`는 report에는 남기되 알림은 보내지 않는다.

## 6단계: job 생성 설계

fresh issue가 threshold를 넘고 Gemini 2차 판단에서 approve를 받으면 `AutomationJob`으로 저장한다.
threshold는 최종 알림 기준이 아니라 Gemini 검토 대상을 줄이는 1차 필터다.

### topic_id

안정적인 fingerprint를 사용한다.

```text
fresh-{yyyyMMddHHmm}-{hash}
```

hash basis:

```text
normalized_team
keyword_group
player_candidate
representative_article_url
fresh article urls
```

### job_id

기존 정책과 충돌하지 않도록 topic_id 기반 또는 timestamp 기반으로 생성한다.

권장:

```text
topic_id를 job_id로 직접 사용
```

단, 길이는 80자 이하로 제한한다.

### AutomationJob 필드 매핑

```text
topic_id:
  fresh issue fingerprint

topic_name:
  사람이 볼 수 있는 짧은 이슈명
  예: LG 오스틴 말소

status:
  detected

notification_level:
  immediate 또는 watch

virality_potential_score:
  issue_score

account_fit_score:
  team_fit + keyword/card suitability 보정값

recommendation_summary:
  Discord에 보여줄 짧은 판단 문장
```

### metadata

필수 metadata:

```json
{
  "source": "watch_fresh_once",
  "issue_score": 82.0,
  "gemini_decision": "approve",
  "gemini_confidence": 0.86,
  "gemini_main_reason": "외국인 주전 말소 이슈로 팬 반응 가능성과 즉시성이 높음",
  "score_reasons": ["최근 10분 신규 기사 2건", "2개 매체 동시 보도", "말소 키워드"],
  "risk_flags": ["단일 발표성 기사 여부 확인 필요"],
  "matched_teams": ["LG"],
  "matched_keywords": ["말소"],
  "keyword_groups": ["injury", "roster"],
  "fresh_article_count": 2,
  "context_article_count": 4,
  "time_bucket_counts": {"30m": 2, "1h": 2, "3h": 4, "6h": 5, "24h": 8},
  "source_diversity": 2,
  "collection_window_start": "2026-05-09T11:10:00+09:00",
  "collection_window_end": "2026-05-09T11:20:00+09:00",
  "context_window_start": "2026-05-09T08:20:00+09:00",
  "context_window_end": "2026-05-09T11:20:00+09:00",
  "topic_fingerprint": "fresh:...",
  "article_url_fingerprint": "...",
  "duplicate_lookback_hours": 72
}
```

### articles

`AutomationJobArticle`에는 fresh article 우선, context article 일부를 뒤에 붙인다.

정렬:

```text
fresh articles 최신순
context articles 최신순
최대 5개
```

Discord 메시지는 현재 3개까지만 보여주므로 저장은 5개까지 허용한다.

## 7단계: 중복 제거 설계

중복 제거는 2단계로 한다.

### 기사 URL 중복

수집 DB 단계:

```text
source_items.source_url UNIQUE
SourceItemIngestionService.find_duplicate()
```

이미 존재한다.

### 이슈 job 중복

automation job 단계:

```text
AutomationJobRepository.list_recent_jobs(hours=72)
```

비교 기준:

```text
same topic_fingerprint
same article_url_fingerprint
representative_article_url overlap
article URL overlap
same normalized issue key
```

기존 `news_watcher._find_duplicate_job()`과 유사하다.

권장 구현:

```text
news_watcher.py의 fingerprint/duplicate helper를 공통 모듈로 분리
```

신규 파일 후보:

```text
src/kbo_card_news/automation/job_deduplication.py
```

공유 함수:

```python
def normalize_url(value: str | None) -> str:
def hash_parts(parts: list[str]) -> str:
def find_duplicate_job_by_fingerprint(...) -> tuple[AutomationJob | None, str]:
```

## 8단계: Discord 메시지 확장

### 수정 대상

```text
src/kbo_card_news/automation/discord_bot.py
```

### 현재 메시지

```text
[즉시 확인]
주제명

판단: 화제성 높음 · 계정핏 높음

기사
1. ...
```

### fresh issue 메시지 추가 정보

`job.metadata["source"] == "watch_fresh_once"`이면 fresh용 정보를 표시한다.

예:

```text
[강한 이슈]
LG 오스틴 말소

점수: 82
Gemini 판단: approve
근거: 최근 10분 신규 2건 / 최근 3시간 관련 4건 / 24시간 관련 8건 / 2개 매체 / 말소 키워드
리스크: 단일 발표성 기사 여부 확인 필요

기사
1. ...
2. ...
```

### 구현 방식

`build_job_message()` 내부에서 source 분기:

```python
if job.metadata.get("source") == "watch_fresh_once":
    return build_fresh_issue_job_message(job)
```

새 helper:

```python
def build_fresh_issue_job_message(job: AutomationJob) -> str:
```

기존 job 메시지는 그대로 유지한다.

## 9단계: CLI 추가

### 수정 대상

```text
src/kbo_card_news/automation/cli.py
```

### 신규 명령

```text
watch-fresh-cycle
```

### 옵션

```text
--source-db-path
  기본값: outputs/source_collection.db

--collection-window-minutes
  기본값: 10

--context-window-hours
  기본값: 24

--duplicate-lookback-hours
  기본값: 72

--min-issue-score
  기본값: 65

--gemini-review
  기본값: true

--no-gemini-review
  로컬 rule과 report만 검증할 때 사용

--max-jobs
  기본값: 5

--notify
--channel-id
--bot-token
--dry-run-notify
--no-start-button-worker
--button-worker-host
--button-worker-public-host
--button-worker-port
--lock-path
--json
```

### handler 구조

```python
def _handle_watch_fresh_cycle(repository: AutomationJobRepository, args: argparse.Namespace) -> None:
    try:
        payload = run_with_lock(
            args.lock_path,
            lambda: _run_watch_fresh_cycle_under_lock(repository, args),
        )
    except AutomationLockBusy as exc:
        raise SystemExit(str(exc)) from exc
    except Exception as exc:
        log_path = write_failure_log(
            operation="watch_fresh_cycle",
            exc=exc,
            metadata={"db_path": str(repository.db_path)},
        )
        raise SystemExit(f"watch-fresh-cycle failed; failure_log={log_path}") from exc
```

```python
def _run_watch_fresh_cycle_under_lock(repository: AutomationJobRepository, args: argparse.Namespace) -> dict[str, Any]:
    result = watch_fresh_once(...)
    if args.notify:
        send_job_notification(...) for created jobs
    return payload
```

기존 `_run_watch_cycle_under_lock()`과 거의 같은 알림/버튼 worker 처리를 재사용한다.

## 10단계: collector 재사용

현재 manual script에만 있는 함수:

```text
tests/manual_checks/manual_check_batch_topic_selection.py
  -> build_news_collectors()
```

운영 코드에서 tests/manual_checks를 import하지 않는 것이 맞다.

### 권장 이동

신규 파일:

```text
src/kbo_card_news/automation/news_collection.py
```

포함 함수:

```python
def build_news_collectors() -> list[NewsSiteCollector]:
```

manual script와 fresh watcher가 둘 다 이 함수를 사용한다.

### 현재 수집 언론사

```text
sports_chosun: 스포츠조선
sports_hankook: 스포츠한국
isplus: 일간스포츠
starnews: 스타뉴스
news1_sports: 뉴스1 스포츠
```

언론사 확대는 fresh watcher 안정화 후 별도 단계에서 진행한다.

## 11단계: 산출 report 파일

fresh watcher는 디버깅을 위해 실행 report를 남긴다.

### 저장 위치

```text
outputs/automation/fresh_watch_runs/YYYYMMDD_HHMMSS/fresh_watch_report.json
```

### 포함 내용

```json
{
  "collection_window_start": "...",
  "collection_window_end": "...",
  "context_window_start": "...",
  "context_window_end": "...",
  "collected_count": 12,
  "inserted_count": 3,
  "duplicate_count": 9,
  "collector_errors": [],
  "fresh_article_count": 3,
  "context_article_count": 18,
  "candidate_count": 2,
  "created_count": 1,
  "duplicate_job_count": 1,
  "skipped_count": 0,
  "candidates": [],
  "created_jobs": []
}
```

CLI `--json` 출력에는 핵심 요약과 report path를 포함한다.

## 12단계: 테스트 계획

### 단위 테스트

신규 테스트:

```text
tests/test_fresh_issue_detector.py
```

검증:

```text
강한 키워드 + 인기 구단이면 issue_score 상승
단일 기사 + 약한 키워드는 threshold 미만
source diversity가 점수에 반영됨
low priority 키워드는 감점됨
같은 URL/같은 fingerprint는 중복 job으로 처리됨
```

DB 조회 테스트:

```text
tests/test_source_item_repository_time_queries.py
```

검증:

```text
published_at 기준 범위 조회
published_at 없는 항목은 collected_at fallback
window end는 exclusive 처리
```

Discord 메시지 테스트:

```text
tests/test_discord_fresh_issue_message.py
```

검증:

```text
fresh metadata가 있으면 점수/근거/리스크 표시
기존 job 메시지 포맷은 유지
2000자 제한 준수
```

### 수동 검증

1. Gemini 기본 off 확인:

```bash
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python3.11 -m kbo_card_news.automation.cli watch-cycle --candidate-count 1 --max-candidates 1 --json
```

기대:

```text
selection_model_name이 heuristic 계열
기존 watch-cycle 후보 선정 단계에서 Gemini 호출 없음
```

2. fresh dry-run:

```bash
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python3.11 -m kbo_card_news.automation.cli watch-fresh-cycle \
  --collection-window-minutes 10 \
  --context-window-hours 3 \
  --min-issue-score 65 \
  --json
```

3. Discord dry-run:

```bash
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python3.11 -m kbo_card_news.automation.cli watch-fresh-cycle \
  --notify \
  --dry-run-notify \
  --json
```

4. 실제 Discord 알림:

```bash
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python3.11 -m kbo_card_news.automation.cli watch-fresh-cycle \
  --notify \
  --json
```

## 13단계: 배포 순서

### Phase 1

```text
Gemini 후보 선정 기본 off
기존 watch-cycle 유지
```

이 단계만 먼저 배포해도 quota 소모 문제는 크게 줄어든다.

### Phase 2

```text
source DB 시간 범위 조회 추가
fresh_issue_detector.py 추가
watch-fresh-cycle 추가
테스트 추가
```

이 단계에서는 launchd 변경 없이 CLI 수동 실행으로 검증한다.

### Phase 3

```text
Discord dry-run 검증
실제 Discord 알림 소량 검증
threshold 조정
```

초기에는:

```text
collection_window_minutes = 10
context_window_hours = 24
min_issue_score = 70
max_jobs = 3
```

알림이 너무 적으면:

```text
min_issue_score = 65
```

알림이 너무 많으면:

```text
min_issue_score = 75
```

### Phase 4

```text
launchd interval 5분 적용
기존 watch-cycle launchd 비활성화
fresh watcher 운영 전환
```

launchd 변경은 마지막에 한다.

## launchd 운영 명령 예시

구형 맥북 기준 기본 명령:

```bash
cd ~/kbo/title_news_code
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python3.11 -m kbo_card_news.automation.cli watch-fresh-cycle --notify --json
```

권장 interval:

```text
300초
```

초기 안정화 기간에는 10분도 가능하다.

```text
600초
```

## 구현 순서 체크리스트

1. `manual_check_batch_topic_selection.py`에 `--selection-engine` 추가
2. `pipeline_runner.generate_topic_candidates()`에 `selection_engine` 전달 추가
3. `cli.py`의 기존 candidates/watch-once/watch-cycle에 `--selection-engine` 추가
4. `SQLiteSourceItemRepository`에 시간 범위 조회 메서드 추가
5. `automation/news_collection.py`로 collector builder 이동
6. `automation/issue_keywords.py`로 팀/키워드 상수 분리
7. `automation/job_deduplication.py`로 중복 helper 분리
8. `automation/fresh_issue_detector.py` 추가
9. `cli.py`에 `watch-fresh-cycle` 추가
10. `discord_bot.py`에 fresh issue 메시지 formatter 추가
11. 단위 테스트 추가
12. dry-run 검증
13. 실제 Discord 소량 검증
14. launchd interval 전환

## 리스크와 대응

### 리스크: 발행 시간이 늦게 찍힌 기사 누락

대응:

```text
collection_window_minutes 기본을 10분으로 둔다.
source_collection_windows로 이미 수집한 window는 중복 수집을 줄인다.
URL unique 제약으로 기사 중복은 DB에서 처리한다.
```

### 리스크: 단일 기사에 과민 반응

대응:

```text
단일 기사 + 단일 매체 + 강한 키워드 없음이면 -20 감점
min_issue_score를 초기 70으로 운영
```

### 리스크: context 조회가 너무 넓어짐

대응:

```text
context_window_hours 기본 24
조회 limit 기본 500
필요 시 source_type, keyword, 관련도 필터로 Gemini 입력 기사 수를 제한
```

### 리스크: 기존 editor build 흐름과 호환성

현재 `build-approved-editor`는 job metadata의 `choice_json_path`를 필요로 한다.

fresh watcher job은 기존 `topic_selection_choice.json`이 없으면 editor build가 바로 깨질 수 있다.

따라서 fresh watcher 구현에서 반드시 둘 중 하나를 선택해야 한다.

선택 A:

```text
fresh issue candidate로 기존 topic_selection_choice.json 호환 파일을 생성한다.
job.metadata["choice_json_path"]에 그 경로를 저장한다.
```

선택 B:

```text
build-approved-editor가 fresh job metadata를 직접 읽어 confirmed topic payload를 만들 수 있게 확장한다.
```

권장:

```text
선택 A
```

이유:

```text
기존 승인 -> editor build -> render 흐름을 거의 수정하지 않아도 된다.
fresh watcher는 감지 방식만 바꾸고, 제작 pipeline 입력 형식은 기존 topic_selection_choice.json을 유지한다.
```

### fresh choice_json 생성 규격

fresh watcher는 job 생성 시 아래 파일을 만든다.

```text
outputs/automation/fresh_watch_runs/YYYYMMDD_HHMMSS/topic_selection_choice.json
```

형식은 기존 `build_topic_selection_template()` 출력과 호환되게 맞춘다.

candidate 최소 필드:

```json
{
  "topic_id": "fresh-...",
  "topic_name": "LG 오스틴 말소",
  "topic_score": 82.0,
  "importance_rank": 1,
  "reason_summary": "최근 10분 신규 2건 / 2개 매체 / 말소 키워드",
  "representative_article_id": "...",
  "article_ids": ["..."],
  "selected": false,
  "metadata": {
    "article_publication_summary": {
      "articles": []
    }
  }
}
```

이렇게 하면 기존 `build-approved-editor`가 `confirm_topic_candidates()`를 그대로 사용할 수 있다.

## 최종 판단

가장 안전한 구현 경로는 아래 순서다.

```text
1. Gemini 기본 off로 quota 누수 차단
2. 기존 watch-cycle은 유지
3. watch-fresh-cycle을 새로 추가
4. fresh watcher가 기존 topic_selection_choice.json 호환 파일을 생성
5. Discord 알림과 승인 이후 제작 pipeline은 기존 구조 재사용
6. 1~2일 병행 검증 후 launchd를 5분 fresh watcher로 전환
```

이 방식은 새 이슈 감지 구조로 바꾸면서도, 이미 구형 맥북에서 검증된 승인/에디터/렌더 흐름을 최대한 보존한다.
