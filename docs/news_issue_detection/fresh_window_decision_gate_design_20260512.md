# Fresh Window Decision Gate Design - 2026-05-12

## 목표

기존 `watch-fresh-cycle`은 최근 수집 기사들을 휴리스틱으로 묶고 점수화한 뒤 기준점 이상이면 Discord 알림/job을 만들었다. 이 방식은 아래 문제가 있었다.

- 화제성이 낮은 기사도 키워드/팀/최신성 점수만으로 통과한다.
- 같은 팀이라는 이유만으로 24시간 context 기사가 섞여, 하나의 주제처럼 보이지만 URL은 서로 무관한 경우가 생긴다.
- 장기간 운영하면서 쌓이는 "발행한 기사/폐기한 기사/알림만 보낸 기사" 피드백을 판단에 거의 쓰지 못한다.

새 구조의 목표는 다음과 같다.

```text
최근 10분 안에 발행된 모든 기사를 하나의 batch로 Gemini가 보고,
전체 기사 DB와 피드백 이력을 판단 근거로 참고해,
지금 알림/job으로 만들 만한 주제 묶음이 있는지 결정한다.
```

중요한 점은 판단 단위가 "개별 기사 하나씩"도 아니고 "24시간 top topic"도 아니라 **최근 10분 발행 기사 batch**라는 것이다. 기존 manual check 초반의 24시간 기사 후보 선정 흐름을 10분 기사 batch로 줄이고, 대신 이전 기사 DB와 피드백 이력을 Gemini 입력에 붙여 판단 품질을 높인다.

## 비목표

- 휴리스틱 점수기를 fallback으로 남기지 않는다.
- Gemini quota가 없거나 API 호출이 실패했을 때 임의 휴리스틱으로 알림을 보내지 않는다.
- 후보 수를 항상 채우지 않는다. 보낼 게 없으면 `0`건이 정상이다.
- 같은 팀 기사들을 느슨하게 묶어 context URL로 첨부하지 않는다.

## 현재 재사용할 수 있는 구성

### 기사 수집/저장

- `src/kbo_card_news/automation/news_collection.py`
- `src/kbo_card_news/pipeline/storage.py`
- `SQLiteSourceItemRepository`
- `SourceItemIngestionService`
- `list_items_published_between(window_start, window_end, limit=...)`

기존 source DB는 그대로 쓴다.

```text
outputs/source_collection.db
```

### 기사 batch 구성

- `src/kbo_card_news/pipeline/issue_feed.py`
- `StoredArticleBatchBuilder`

이 빌더는 source DB의 `PersistedSourceItem`을 Gemini 입력에 적합한 `BatchArticleCandidate`로 정리한다. `article_kind`, `league_tier`, KBO 기사 필터링을 이미 처리하므로 재사용한다. 기존 manual check에서는 24시간 window 전체를 batch로 만들었지만, 새 watcher에서는 판단 대상 batch를 10분 window로 만든다.

단, 기존 `GeminiBatchIssueSelectionEngine`은 그대로 쓰지 않는다. 해당 엔진은 "top_k 후보를 항상 채우는" 목적이므로 fresh watcher의 "보낼 게 없으면 보내지 않음" 정책과 맞지 않는다.

### 자동화 job/status

- `src/kbo_card_news/automation/job_state.py`
- `AutomationJobRepository`
- `automation_jobs`
- `automation_job_articles`
- `automation_job_events`

Gemini가 `publish`로 판단한 10분 window 안의 주제 묶음만 기존 `AutomationJob`으로 변환한다.

### 완료/피드백 이력

- `outputs/completed_topic_registry.json`
- `automation_jobs.status`
- `automation_job_events`

초기 버전에서는 이 세 가지를 피드백 이력으로 사용한다. 이후 별도 피드백 테이블이 쌓이면 retrieval 품질을 높인다.

## 새 파이프라인

### 전체 흐름

```text
watch-fresh-cycle
  -> 최근 10분 window 수집
  -> source DB ingest
  -> source DB에서 최근 10분 발행 target articles 조회
  -> source DB에서 이전 기사 context 조회
  -> automation DB / completed registry / decision log에서 과거 피드백 조회
  -> FreshWindowDecisionEngine(Gemini) 호출
  -> 10분 target articles 안에서 publish 결정된 주제 묶음만 AutomationJob 생성
  -> Discord 알림
  -> Gemini의 window 판단 결과와 topic/article 판단 근거를 decision log에 저장
```

### 핵심 데이터 구분

```text
target_articles
  이번 cycle의 판단 대상 batch.
  기본 기준은 "발행시각 published_at이 최근 10분 window 안에 있음".
  Gemini는 이 기사들을 한꺼번에 보고, 서로 묶일 수 있는 기사와 버릴 기사를 동시에 판단한다.

historical_context_articles
  판단 근거.
  기본 기준은 "target window 이전에 발행된 기사".
  최근 24~72시간 기사는 현재 보도 흐름 확인에 우선 사용하고, 더 오래된 기사는 피드백/유사 사례로 사용한다.

historical_feedback
  판단 근거.
  과거에 publish/approved/published/failed/discard/skipped 된 기사와 주제.

decision_log
  이번 판단 결과 저장소.
  Gemini가 10분 window 안에서 어떤 topic group을 publish/hold/reject 했는지 누적한다.
```

## 새 모듈 설계

### 1. `fresh_window_decision.py`

신규 파일:

```text
src/kbo_card_news/automation/fresh_window_decision.py
```

주요 dataclass:

```python
@dataclass(slots=True)
class FreshWindowDecisionConfig:
    collection_window_minutes: int = 10
    context_window_hours: int = 24
    feedback_lookback_days: int = 90
    duplicate_lookback_hours: int = 72
    max_target_articles_per_call: int = 40
    max_context_articles: int = 180
    max_feedback_examples: int = 40
    model_name: str = "gemini-3.1-flash-lite"
```

```python
@dataclass(slots=True)
class FreshWindowTopicDecision:
    decision: str  # publish | hold | reject
    issue_score: float
    notification_level: str  # immediate | watch
    topic_name: str
    group_key: str
    dedupe_key: str
    representative_article_id: str
    target_article_ids: list[str]
    related_article_ids: list[str]
    reason_summary: str
    risk_flags: list[str]
    matched_positive_examples: list[str]
    matched_negative_examples: list[str]
    metadata: dict[str, Any]
```

```python
@dataclass(slots=True)
class FreshWindowDecisionResult:
    collection_window_start: datetime
    collection_window_end: datetime
    context_window_start: datetime
    context_window_end: datetime
    target_article_count: int
    context_article_count: int
    decision_count: int
    created_jobs: list[AutomationJob]
    duplicate_jobs: list[AutomationJob]
    skipped_count: int
    report_path: Path
    decisions: list[FreshWindowTopicDecision]
```

공개 함수:

```python
def watch_fresh_window_once(
    *,
    job_repository: AutomationJobRepository,
    source_db_path: str | Path = SOURCE_DB_PATH,
    config: FreshWindowDecisionConfig | None = None,
    now: datetime | None = None,
    collection_window_start: datetime | None = None,
    collection_window_end: datetime | None = None,
) -> FreshWindowDecisionResult:
    ...
```

기존 `watch_fresh_once()`를 바로 지우기보다 새 함수로 만든 뒤 CLI가 새 함수를 호출하게 바꾼다. 기존 함수는 테스트/마이그레이션이 끝나면 제거한다.

### 2. `FreshWindowDecisionEngine`

같은 파일 또는 별도 파일:

```text
src/kbo_card_news/automation/fresh_window_decision_engine.py
```

인터페이스:

```python
class FreshWindowDecisionEngine(Protocol):
    def decide(self, request: FreshWindowDecisionRequest) -> FreshWindowDecisionModelResult:
        ...
```

구현체:

```python
class GeminiFreshWindowDecisionEngine:
    ...
```

휴리스틱 구현체는 만들지 않는다.

Gemini API key가 없거나 호출 실패 시:

```text
이번 cycle은 no_decision 상태로 종료한다.
job/Discord 알림은 만들지 않는다.
fresh_window_decision_report.json에는 실패 사유를 남긴다.
```

## Gemini 입력 설계

### 입력 payload

Gemini에는 다음 정보를 한 번에 보낸다.

```json
{
  "task": "Analyze the target 10-minute KBO article batch with historical context and decide whether any publishable topic groups exist now.",
  "policy": {
    "decision_unit": "target_window_batch",
    "allowed_decisions": ["publish", "hold", "reject"],
    "publish_requires_target_article": true,
    "allow_empty_publish": true
  },
  "collection_window": {
    "start": "...",
    "end": "..."
  },
  "target_articles": [
    {
      "article_id": "...",
      "title": "...",
      "source_type": "...",
      "source_url": "...",
      "published_at": "...",
      "excerpt_text": "...",
      "metadata": {
        "article_kind": "...",
        "league_tier": "..."
      }
    }
  ],
  "historical_context_articles": [
    {
      "article_id": "...",
      "title": "...",
      "source_type": "...",
      "source_url": "...",
      "published_at": "...",
      "excerpt_text": "...",
      "metadata": {
        "article_kind": "...",
        "league_tier": "..."
      }
    }
  ],
  "feedback_examples": {
    "positive": [
      {
        "job_id": "...",
        "topic_name": "...",
        "status": "published",
        "article_titles": ["..."],
        "reason": "..."
      }
    ],
    "negative": [
      {
        "job_id": "...",
        "topic_name": "...",
        "status": "skipped",
        "article_titles": ["..."],
        "reason": "..."
      }
    ]
  },
  "completed_topics": [
    {
      "topic_name": "...",
      "article_ids": ["..."],
      "completed_at": "..."
    }
  ]
}
```

### context 제한

Gemini 입력이 과도하게 커지지 않도록 제한한다.

- `target_articles`: 기본 최대 40건
- `historical_context_articles`: 기본 최대 180건
- `feedback_examples.positive`: 기본 최대 20건
- `feedback_examples.negative`: 기본 최대 20건
- `excerpt_text`: 기사당 300~500자

처음 구현에서는 lexical retrieval을 단순하게 시작한다.

```text
target article batch의 제목/본문에서 팀명, 선수명 후보, 사건 키워드 추출
-> 같은 팀/이름/핵심 단어가 있는 context와 feedback을 우선 선택
-> 부족하면 최근성 순으로 보강
```

단, 이 retrieval은 Gemini 입력을 줄이기 위한 전처리일 뿐이다. 10분 batch 안에서 무엇을 묶고 publish/hold/reject할지는 Gemini만 판단한다.

## Gemini 출력 schema

Gemini는 10분 target article batch를 보고 publish/hold/reject topic group을 반환한다. 보낼 만한 주제가 없으면 빈 배열을 반환한다.

```json
{
  "topic_decisions": [
    {
      "decision": "publish",
      "issue_score": 86,
      "notification_level": "immediate",
      "topic_name": "한화 김서현 부상 말소",
      "group_key": "hanwha:kimseohyun:injury-roster-change",
      "dedupe_key": "hanwha:kimseohyun:injury",
      "representative_article_id": "source item id",
      "target_article_ids": ["source item id"],
      "related_article_ids": ["source item id"],
      "reason_summary": "10분 batch 안에서 1군 전력 이탈 가능성이 있는 같은 사건 보도가 확인됐고, 과거 발행 이력상 부상/말소 유형은 계정 적합도가 높다.",
      "risk_flags": []
    },
    {
      "decision": "reject",
      "issue_score": 24,
      "notification_level": "watch",
      "topic_name": "선발 예고 묶음",
      "group_key": "probable-starters-low-priority",
      "dedupe_key": "probable-starters",
      "representative_article_id": "source item id",
      "target_article_ids": ["source item id"],
      "related_article_ids": [],
      "reason_summary": "선발 예고성 기사이며 독립 카드뉴스 이슈로 보기 어렵다.",
      "risk_flags": ["probable_starter", "low_virality"]
    }
  ]
}
```

검증 규칙:

- `topic_decisions`는 배열이어야 한다. 보낼 만한 주제가 없으면 빈 배열을 허용한다.
- `decision`은 `publish`, `hold`, `reject` 중 하나여야 한다.
- `publish`는 `topic_name`, `group_key`, `dedupe_key`, `representative_article_id`, `target_article_ids`가 비어 있으면 안 된다.
- `publish.target_article_ids`에는 반드시 최근 10분 target article이 1개 이상 있어야 한다.
- `related_article_ids`는 입력 article id에 존재하는 값만 허용한다.
- historical context article만으로 publish topic을 만들면 invalid다.
- invalid 응답은 재시도한다. 재시도 실패 시 cycle은 no_decision으로 끝낸다.

## 프롬프트 정책

프롬프트에는 아래 기준을 명시한다.

### publish 기준

`publish`는 최근 10분 target batch 안에 지금 알림/job으로 만들 가치가 있는 주제 묶음이 있을 때만 사용한다.

강한 publish 후보:

- 부상, 검진, 수술, 장기 이탈, 시즌 아웃, 1군 말소
- 징계, 논란, 사과, 사건, 폭행, 도박, 계약 해지
- 트레이드, 방출, 외국인 교체, 감독 경질, 보직 변경
- 순위/포스트시즌 판도를 바꿀 수 있는 직접 이슈
- 복수 매체가 같은 핵심 사실을 보도하는 확산 이슈

### hold 기준

`hold`는 지금 바로 보내기에는 약하지만, 추가 보도나 반응이 있으면 커질 수 있는 경우다.

- 10분 batch 안에는 단일 매체 보도만 있지만 사실관계가 중요할 수 있음
- 같은 사건인지 context가 부족함
- 과거 피드백상 애매한 유형
- 경기 결과/기록이지만 특정 팀 팬덤 반응 가능성은 있음

`hold`는 job을 만들지 않는다. decision log에만 남긴다.

### reject 기준

아래는 기본 reject다.

- 프리뷰
- 선발 예고
- 관전 포인트
- 순위표 단순 정리
- 경기 종합 기사
- 2군/퓨처스 중심 기사
- 이미 다룬 주제의 반복
- 같은 팀이라는 이유 외에는 관련성이 없는 기사 묶음

### grouping 기준

10분 target batch 안의 여러 기사가 같은 사건이면 같은 `group_key`를 부여하고 하나의 topic decision으로 묶는다.

같은 주제로 인정하려면 다음 중 다수가 일치해야 한다.

- 같은 팀
- 같은 선수/감독/외국인/구단 관계자
- 같은 사건 유형
- 같은 핵심 사실
- 비슷한 발행 시간대

같은 팀만 같으면 같은 주제로 묶지 않는다.

## Decision Log 저장

새 테이블을 automation DB에 추가한다.

```sql
CREATE TABLE IF NOT EXISTS fresh_window_decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    decision TEXT NOT NULL,
    issue_score REAL NOT NULL DEFAULT 0,
    notification_level TEXT NOT NULL DEFAULT 'watch',
    topic_name TEXT NOT NULL DEFAULT '',
    group_key TEXT NOT NULL DEFAULT '',
    dedupe_key TEXT NOT NULL DEFAULT '',
    representative_article_id TEXT NOT NULL DEFAULT '',
    target_article_ids_json TEXT NOT NULL DEFAULT '[]',
    related_article_ids_json TEXT NOT NULL DEFAULT '[]',
    reason_summary TEXT NOT NULL DEFAULT '',
    risk_flags_json TEXT NOT NULL DEFAULT '[]',
    model_name TEXT NOT NULL DEFAULT '',
    prompt_version TEXT NOT NULL DEFAULT '',
    raw_decision_json TEXT NOT NULL DEFAULT '{}',
    created_job_id TEXT,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_fresh_window_decisions_decision_created
ON fresh_window_decisions(decision, created_at);

CREATE INDEX IF NOT EXISTS idx_fresh_window_decisions_group_key
ON fresh_window_decisions(group_key, created_at);

CREATE INDEX IF NOT EXISTS idx_fresh_window_decisions_dedupe_key
ON fresh_window_decisions(dedupe_key, created_at);
```

저장 정책:

- Gemini가 반환한 `publish`, `hold`, `reject` topic decision을 모두 저장한다.
- 판단 대상이 된 10분 target article id 목록은 run report와 각 decision의 `target_article_ids_json`에 남긴다.
- Gemini 호출 실패로 판단이 없으면 run report에만 남긴다.
- job 생성 후에는 `created_job_id`를 업데이트한다.
- 향후 retrieval은 이 테이블을 우선 사용한다.

## Feedback Retrieval

초기 retrieval은 세 층으로 구성한다.

### Positive examples

아래 상태의 job/article을 positive로 본다.

- `approved`
- `pipeline_running`
- `editor_ready`
- `render_ready`
- `publish_approved`
- `published`

단, `failed`는 positive로 보지 않는다. 제작 의도는 있었지만 기술 실패일 수 있으므로 별도 neutral bucket으로 둘 수 있다.

### Negative examples

아래 상태나 이벤트를 negative로 본다.

- `skipped`
- `expired`
- 사용자가 Discord에서 `폐기`한 job
- decision log에서 반복적으로 `reject`된 유형

### Completed topics

`completed_topic_registry.json`은 이미 완성된 카드뉴스 중복 방지에 사용한다.

Gemini 입력에는 "이미 다룬 주제"로 제공하고, 같은 사건 반복이면 reject하도록 한다.

## Job 생성 정책

Gemini topic decision 중 `publish`만 job으로 만든다.

### 같은 group_key 병합

Gemini가 반환한 publish topic decision 하나가 job 하나가 된다. 여러 target article이 같은 사건이면 Gemini가 하나의 `topic_decision` 안에 `target_article_ids`로 묶어 반환한다.

대표 기사:

```text
representative_article_id가 있으면 우선 사용
없으면 target_article_ids 중 최신 발행 기사
그래도 없으면 최신 발행 기사
```

job articles:

```text
1. publish topic의 target_article_ids
2. Gemini related_article_ids 중 같은 사건이라고 판단된 context
3. 최대 5건
```

context article은 Gemini가 `related_article_ids`로 명시한 경우에만 붙인다. 24시간 context 전체를 자동 첨부하지 않는다.

### Duplicate check

기존 `job_deduplication.py`를 재사용하되 fingerprint 기준을 fresh watcher에 맞춘다.

```text
topic_fingerprint = "fresh-decision:" + dedupe_key
representative_article_url = representative article source_url
article_url_fingerprint = publish group article urls hash
normalized_topic_key = topic_name + dedupe_key
```

중복이면 새 job을 만들지 않고 `duplicate_fresh_window_decision_seen` event만 남긴다.

## CLI 변경

기존 명령은 유지한다.

```bash
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python3.11 -m kbo_card_news.automation.cli watch-fresh-cycle
```

내부 구현만 새 decision gate를 호출한다.

옵션 정리:

```text
--collection-window-minutes 기본 10
--context-window-hours 기본 24
--duplicate-lookback-hours 기본 72
--feedback-lookback-days 기본 90
--fresh-decision-model 기본 gemini-3.1-flash-lite
--max-target-articles-per-call 기본 40
--max-context-articles 기본 180
--max-feedback-examples 기본 40
--notify
--dry-run-notify
--json
```

제거 또는 deprecated:

```text
--min-issue-score
--gemini-review
--no-gemini-review
```

`--no-gemini-review`는 더 이상 허용하지 않는 편이 맞다. 호환성을 위해 남긴다면 실행 시 명확히 실패시킨다.

```text
watch-fresh-cycle requires Gemini fresh window decision gate; --no-gemini-review is no longer supported.
```

## Report 파일

기존 fresh run directory는 유지한다.

```text
outputs/automation/fresh_watch_runs/YYYYMMDD_HHMMSS/
```

생성 파일:

```text
fresh_window_decision_report.json
fresh_window_decision_prompt.json
fresh_window_decision_response.json
topic_selection_choice.json
```

`topic_selection_choice.json`은 기존 editor build 호환을 위해 publish된 job 후보만 담는다. publish가 없으면 빈 template을 쓴다.

report 주요 필드:

```json
{
  "collection_window_start": "...",
  "collection_window_end": "...",
  "context_window_start": "...",
  "context_window_end": "...",
  "collected_count": 0,
  "inserted_count": 0,
  "target_article_count": 0,
  "context_article_count": 0,
  "decision_count": 0,
  "publish_count": 0,
  "hold_count": 0,
  "reject_count": 0,
  "created_count": 0,
  "duplicate_job_count": 0,
  "model_name": "gemini-3.1-flash-lite",
  "model_call_status": "ok",
  "model_error": "",
  "decisions": []
}
```

## Discord 메시지 변경

fresh window decision job 메시지는 아래 중심으로 보여준다.

```text
[강한 이슈] 또는 [확인 후보]
topic_name

점수: 86
판단: Gemini fresh window gate
근거: reason_summary
리스크: risk_flags
target 기사: N건
관련 기사: M건

기사
1. target article title
url
2. related article title
url
```

중요한 차이:

- "24시간 관련 N건"을 자동 과시하지 않는다.
- 실제 첨부 URL은 Gemini가 같은 사건이라고 지정한 것만 쓴다.
- rejected/hold topic decision은 Discord로 보내지 않는다.

## 테스트 계획

### Unit tests

신규 테스트:

```text
tests/automation/test_fresh_window_decision.py
tests/automation/test_fresh_window_decision_storage.py
tests/automation/test_fresh_window_decision_prompt.py
```

필수 케이스:

1. Gemini가 publish/hold/reject topic decision을 반환하면 publish만 job 생성.
2. 모든 topic decision이 decision log에 저장.
3. Gemini 응답이 target_article_ids 없는 publish를 반환하면 재시도/실패.
4. target article 없이 historical context만 publish하면 invalid 처리.
5. 10분 batch 안 같은 사건 기사들은 하나의 publish topic으로 묶임.
6. 같은 팀이지만 다른 사건인 related_article_ids는 프롬프트/검증 정책상 자동 첨부되지 않음.
7. duplicate dedupe_key는 새 job을 만들지 않음.
8. API key가 없으면 job 생성 없이 실패 report.

### Manual check

추가 수동 점검 스크립트:

```text
tests/manual_checks/manual_check_fresh_window_decision_gate.py
```

기능:

- 원하는 KST window 입력
- source DB에서 10분 target batch와 이전 context 기사 조회
- Gemini window decision gate dry run
- publish/hold/reject를 표 형태로 출력
- prompt/response/report 저장
- job 생성은 하지 않음

## 마이그레이션 단계

### Phase 1: 설계 기반 신규 엔진 추가

- `fresh_window_decision.py` 추가
- Gemini request/response schema 구현
- decision log 테이블 추가
- dry-run manual check 작성
- 기존 fresh watcher는 그대로 둠

### Phase 2: CLI 연결

- `watch-fresh-cycle`이 새 `watch_fresh_window_once()`를 호출하도록 변경
- `--no-gemini-review` 비활성화
- `--min-issue-score` deprecated
- report/choice json 호환 유지

### Phase 3: Discord/job 정리

- fresh window decision metadata 기반 메시지 수정
- job article 첨부 정책 변경
- duplicate fingerprint를 `dedupe_key` 중심으로 변경

### Phase 4: 기존 휴리스틱 제거

- `fresh_issue_detector.py`의 휴리스틱 경로 제거 또는 legacy 파일로 이동
- 관련 테스트 제거/대체
- rollout note 업데이트

## 운영 정책

### 호출량

10분 주기면 하루 최대 호출 수는 다음과 같다.

```text
24시간 풀가동: 144회/일
00:00-07:00 휴지 + 07시 catch-up: 약 103회/일
```

하루 200회 quota 기준으로는 1 cycle 1 Gemini call 구조가 적합하다.

재시도는 응답 invalid/일시 오류에만 사용한다. invalid가 반복되면 알림 없이 실패 report를 남긴다.

### 실패 정책

Gemini 호출 실패:

```text
알림 없음
job 생성 없음
report에 model_error 기록
다음 cycle에서 다시 판단
```

이 정책은 의도적이다. 휴리스틱 fallback을 두면 다시 잡음 알림 문제가 생긴다.

### 데이터 축적

장기적으로 decision log는 가장 중요한 피드백 DB가 된다.

- reject가 반복되는 유형은 Gemini 입력의 negative examples로 들어간다.
- publish 후 실제 `published`까지 간 유형은 positive examples로 들어간다.
- publish됐지만 사용자가 폐기한 유형은 negative examples로 들어간다.
- 같은 dedupe_key가 반복되면 중복/후속 보도 판정에 쓰인다.

## 구현 시 주의점

- 발행 기준은 `published_at`이다. `collected_at`은 fallback으로만 사용한다.
- source DB에서 target batch를 고를 때 "이번 cycle insert된 기사"만 보지 않는다. 이미 이전 cycle에 수집됐더라도 `published_at`이 이번 10분 window에 있으면 판단 대상이 될 수 있다.
- 같은 10분 window가 반복 판단되지 않도록 `run_id`, collection window, `target_article_ids_json`을 확인한다.
- context는 판단 근거로만 사용하고, 자동으로 job articles에 붙이지 않는다.
- Gemini가 `publish`라고 해도 duplicate check는 반드시 통과해야 한다.
- Gemini output의 `issue_score`는 ranking/표시용이다. 휴리스틱 threshold 대체물이 아니다.

## 최종 형태

새 watcher의 의미는 다음 한 줄로 정리된다.

```text
10분마다 해당 window의 발행 기사 batch 전체를 Gemini가 보고, 이전 기사 DB와 축적된 피드백 이력을 근거로 publish/hold/reject topic group을 결정한다.
```

이 구조에서는 기사 DB가 오래 쌓일수록 판단 근거가 좋아지고, 사용자 선택/폐기/발행 결과가 다음 판단에 반영된다. 기존 휴리스틱 watcher처럼 단순 키워드 점수로 많이 보내는 방식은 제거한다.
