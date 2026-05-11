# 구형 맥북 서버 배포 및 상시실행 로드맵

## 목표

현재 개발 환경에서 검증된 뉴스 제작 흐름을 구형 맥북으로 옮겨 상시 실행한다.

최종 목표 흐름은 다음과 같다.

```text
맥북 부팅
-> launchd가 watcher와 Discord worker 자동 시작
-> watcher가 새 뉴스 후보 감지
-> 중복 제거 후 Discord 알림
-> Discord 제작 버튼 승인
-> editor 생성 및 URL 발송
-> 사용자가 브라우저/폰에서 편집 후 렌더
-> PNG와 인스타 본문을 Discord로 수신
```

아직 이 단계에서는 Instagram 자동 업로드는 포함하지 않는다.

## 운영 단위

상시 실행 대상은 서버 하나가 아니라 세 역할로 나눈다.

```text
watcher
  주기적으로 뉴스 수집, 후보 생성, 중복 제거, Discord 알림을 수행한다.

discord-button-worker
  Discord 제작/폐기 버튼을 상시 수신한다.

editor server
  제작 승인된 job이 있을 때만 켜진다.
  편집/렌더 완료 후 자동 종료한다.
```

launchd로 상시 유지할 대상은 watcher와 Discord worker다. editor server는 승인된 작업 단위로 실행한다.

## Phase 1. 현재 코드 정리

### 1. stable topic fingerprint 중복 제거

진행 상태: 완료.

`watch_once()`는 이제 `topic_id`만 보지 않고 최근 24시간 내 안정 키를 함께 비교한다.

문제는 후보 생성 시 `topic_id`가 다음처럼 실행 시각과 순번을 포함한다는 점이다.

```text
kbo-news-20260507_170738:2
kbo-news-20260507_172400:2
```

같은 기사와 같은 이슈라도 후보 생성 시간이 달라지면 새 job으로 들어올 수 있다.

구현 방향:

- 대표 기사 URL 기반 fingerprint 생성
- 후보에 묶인 article URL set 기반 fingerprint 생성
- 정규화한 `topic_name` 기반 보조 key 생성
- 최근 24시간 내 fingerprint 또는 기사 URL overlap이 있는 job은 duplicate 처리
- job metadata에 아래 값을 저장

```text
topic_fingerprint
representative_article_url
article_url_fingerprint
normalized_topic_key
article_urls
duplicate_lookback_hours
```

중복으로 판정된 후보는 새 job을 만들지 않고 기존 job에 `duplicate_candidate_seen` event를 남긴다.
event metadata에는 `duplicate_match_reason`을 남긴다.

### 2. watcher 실행 비용 정리

현재 `watch-cycle`은 가벼운 poller가 아니라 기존 후보 생성 스크립트인 `tests/manual_checks/manual_check_batch_topic_selection.py`를 실행한다.

따라서 너무 짧은 주기로 반복하면 후보 선정 비용과 API 비용이 커질 수 있다.

진행 상태:

- `watch-cycle` 운영 기본 후보 생성 수를 10개로 제한했다.
- `watch-cycle` 운영 기본 job 저장 수도 10개로 제한했다.
- launchd watcher 예시의 `StartInterval`을 1800초, 30분으로 변경했다.
- 운영 문서의 10분 주기 예시를 30분 기준으로 정리했다.

초기 운영 기준:

- 기본 주기: 30분
- 경기 직후/뉴스가 많은 시간대: 15분
- 기본 후보 생성 수: 10개
- 기본 job 저장 수: 10개
- 안정화 후 필요하면 새 기사 감지와 후보 생성을 분리

장기 개선 방향:

```text
light collector
-> 새 기사 URL만 빠르게 저장
-> 새 기사 수/키워드/시간 조건 만족 시 topic selection 실행
```

### 3. URL 모드 정리

editor URL 생성 방식을 명시적으로 선택할 수 있게 한다.

초기 운영 기본값:

```text
Cloudflare quick tunnel
```

Cloudflare quick tunnel은 이미 `discord-button-worker`의 자동 제작 흐름에 구현되어 있다.
Discord 제작 버튼을 누르면 editor server를 로컬에서 띄우고, `cloudflared tunnel --url http://localhost:<port>`로 공개 URL을 만든 뒤 Discord에 editor URL을 보낸다.

운영 전제:

- 구형 맥북에 `cloudflared`가 설치되어 있어야 한다.
- editor URL에는 job별 token을 붙인다.
- editor server는 렌더 완료 후 자동 종료한다.
- idle timeout을 설정해 편집이 중단된 editor server도 닫히게 한다.
- quick tunnel URL은 작업 단위 임시 URL로 보고 장기 보관하지 않는다.

Tailscale 또는 LAN URL은 quick tunnel이 불안정하거나 외부 공개 URL을 피하고 싶을 때의 대안으로 둔다.

구현 후보:

```text
--editor-url-mode cloudflare
--editor-url-mode tailscale
--editor-url-mode lan
```

초기 구현에서는 별도 `--editor-url-mode`를 새로 만들기보다, 현재 구현된 quick tunnel 경로를 운영 표준으로 삼고 `cloudflared` 설치/실행 검증을 먼저 한다.

진행 상태: 현재 개발 머신에서 `cloudflared` 설치 확인 완료.

```text
/opt/homebrew/bin/cloudflared
cloudflared version 2026.3.0
```

### 4. 비밀값 노출 정리

Discord bot token을 subprocess 인자로 넘기는 부분을 줄인다.

구현 방향:

- `.env` 로드 기반으로 통일
- `--bot-token`은 테스트/수동 override 용도로만 유지
- worker 시작 로그와 process command에 token이 남지 않게 확인

진행 상태: `ensure_discord_button_worker()`가 child process command에 `--bot-token`을 넣지 않도록 정리했다.
worker subprocess에는 `DISCORD_BOT_TOKEN`, `DISCORD_CHANNEL_ID`를 env로 전달한다.

### 5. build + serve 흐름 정리

운영에서는 editor manifest 생성만으로는 부족하다. 실제 사용자는 URL을 열어야 하므로 build 이후 server 실행까지 이어져야 한다.

정리할 흐름:

```text
Discord 제작 버튼
-> approved 처리
-> build-approved-editor
-> serve-job-editor
-> editor URL Discord 전송
-> 렌더 후 render_ready 기록
```

진행 상태: Discord 제작 버튼의 자동 build + serve 경로를 정리했다.
`build-approved-editor`가 `--lock-path`를 지원하므로 버튼 worker가 넘기는 build lock 인자가 CLI에서 깨지지 않는다.

## Phase 2. 구형 맥북으로 옮길 대상 확정

진행 상태: 완료.

상세 이전 manifest는 `docs/macbook_transfer_manifest.md`에 확정했다.
기본 전략은 git clone 또는 rsync로 repo를 옮기되, `.env`와 `outputs/automation/` 런타임 상태는 기본 제외하는 것이다.

### 필수 코드

```text
src/kbo_card_news/
design.py
tests/manual_checks/manual_check_batch_topic_selection.py
tests/manual_checks/manual_check_title_html_editor_no_multimodal.py
tests/automation/test_news_watcher_duplicate.py
tests/automation/test_job_state_status.py
docs/automation_operations.md
docs/macbook_server_deployment_roadmap.md
docs/macbook_transfer_manifest.md
docs/com.kbo.title-news-automation.watcher.plist.example
docs/com.kbo.title-news-automation.watcher.macbook.plist
docs/com.kbo.title-news-automation.discord-worker.macbook.plist
docs/selection_policy.md
.env.example
requirements-automation.txt
```

### 필수 데이터 후보

```text
outputs/completed_topic_registry.json
outputs/source_collection.db
feedback_memory.db
```

### 상황에 따라 옮길 것

```text
outputs/approval_run_*/
backups/feedback_memory/
```

과거 결과물 참고나 복구가 필요하면 옮긴다. 새 맥북에서 깨끗하게 시작할 경우 필수는 아니다.

### 기본적으로 옮기지 않을 것

```text
outputs/automation/automation_state.db
outputs/automation/logs/
outputs/automation/locks/
```

`automation_state.db`는 구형 맥북에서 새로 시작하는 편이 깔끔하다. 기존 job 상태까지 이어가야 할 때만 복사한다.
기존 job 상태를 이어갈 경우 해당 job이 참조하는 `outputs/approval_run_*`도 같이 복사한다.
lock, log, pid 파일은 복사하지 않는다.

`.env`는 직접 복사할 수 있지만 토큰과 API key가 들어 있으므로 별도 관리한다.

## Phase 3. 구형 맥북 환경 준비

진행 상태: 완료.

구형 맥북 경로:

```text
/Users/wonjaechoi/kbo/title_news_code
```

검증 기준 Python:

```text
python3.11
```

기본 `python3`는 3.9.6이라 사용하지 않는다.

필수 준비:

- 전원 상시 연결: 진행
- 잠자기 방지: 테스트 중 수동 관리
- repo 복사 또는 git clone: `macbook_transfer_package_20260508` 기반 복사 완료
- Python 버전 확인: `python3.11` 설치 완료
- Python 의존성 설치: `discord.py`, `Pillow` 설치 확인
- 브라우저 렌더 환경 확인: Google Chrome 앱 설치 후 PNG 렌더 성공
- `.env` 작성: 완료
- Discord bot token, channel id 확인: 실제 Discord 발송 성공
- Cloudflare quick tunnel용 `cloudflared` 설치: 완료
- 필요 시 Tailscale 또는 같은 Wi-Fi 접근 방식도 예비로 준비
- 디스크 여유 공간 확인: `health` 기준 OK

`.env` 필수 값:

```text
OPENAI_API_KEY
GEMINI_API_KEY
DISCORD_BOT_TOKEN
DISCORD_CHANNEL_ID
DISCORD_USERNAME
```

Instagram 관련 값은 이 단계에서 필수로 두지 않는다.

구형 맥북에서 추가로 확인된 필수 항목:

```text
design.py
discord.py
Pillow
Google Chrome.app
cloudflared
ca-certificates
```

주의: PNG 렌더는 Playwright가 아니라 실제 Chrome 앱의 headless screenshot 경로를 사용한다.
따라서 `/Applications/Google Chrome.app` 또는 호환 브라우저가 필요하다.

## Phase 4. 배포 전 검증

진행 상태: 완료.

구형 맥북에서 실제 MVP e2e를 1회 성공했다.

검증된 흐름:

```text
watch-cycle --candidate-count 1 --max-candidates 1
-> 후보 job 생성
-> Discord 실제 알림
-> CLI approve
-> build-approved-editor
-> serve-job-editor --notify-render
-> cloudflared quick tunnel
-> 폰/브라우저에서 editor URL 접속
-> PNG 저장
-> render_ready 기록
```

검증 job:

```text
job_id: kbo-news-20260509_030725-1
topic: 김재환, 친정팀 두산 상대 첫 방문 경기서 활약
status: render_ready
```

렌더 산출물:

```text
outputs/approval_run_20260509_030724/title_render_pngs/01_260508_김재환멀티히트_01.png
outputs/approval_run_20260509_030724/title_render_pngs/title_render_social_copy.md
```

구형 맥북에서 실제 Discord 발송 전 dry-run으로 확인한다.

기본 import/CLI 확인:

```bash
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python3.11 -m kbo_card_news.automation.cli --help
PYTHONDONTWRITEBYTECODE=1 python3.11 -m compileall -q src/kbo_card_news/automation
```

상태 DB 초기화/점검:

```bash
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python3.11 -m kbo_card_news.automation.cli init
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python3.11 -m kbo_card_news.automation.cli health
```

watcher dry-run:

```bash
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python3.11 -m kbo_card_news.automation.cli watch-cycle \
  --candidate-count 10 \
  --max-candidates 10 \
  --notify \
  --dry-run-notify
```

Discord payload dry-run:

```bash
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python3.11 -m kbo_card_news.automation.cli notify-pending --dry-run
```

## Phase 5. launchd 상시 실행 설계

launchd는 watcher와 Discord worker를 분리한다.

```text
com.kbo.title-news-automation.watcher.plist
com.kbo.title-news-automation.discord-worker.plist
```

### watcher

역할:

- 주기 실행
- 후보 생성
- stable fingerprint 기반 중복 제거
- Discord 알림 발송

초기 주기:

```text
1800초, 30분
```

뉴스가 많은 시간대에만 900초, 15분으로 줄이는 방식을 검토한다.

### discord-button-worker

역할:

- Discord interaction 수신
- 제작/폐기 버튼 처리
- 제작 승인 시 editor 생성
- editor server 실행
- editor URL 알림

상시 실행하며 실패 시 재시작되도록 구성한다.

### editor server

역할:

- 승인된 job의 HTML editor 제공
- token 검증
- 렌더 요청 처리
- 렌더 완료 시 job 상태를 `render_ready`로 기록
- Discord에 PNG와 social copy 알림

운영 기준:

- 평소에는 실행하지 않는다.
- 제작 승인 후에만 실행한다.
- Cloudflare quick tunnel을 통해 임시 공개 URL을 생성한다.
- 렌더 완료 후 자동 종료한다.
- idle timeout을 설정한다.

## Phase 6. 실제 운영 전 체크리스트

- 같은 주제가 반복 알림되지 않는지
- Discord 제작 버튼이 job 상태를 `approved`로 바꾸는지
- 승인 후 editor 생성이 시작되는지
- Cloudflare quick tunnel URL이 생성되는지
- editor URL이 폰/브라우저에서 열리는지
- token 없는 URL 요청이 403으로 막히는지
- PNG 저장 후 job 상태가 `render_ready`가 되는지
- Discord에 PNG와 social copy가 오는지
- 맥북 재부팅 후 watcher와 worker가 다시 살아나는지
- 실패 로그가 `outputs/automation/logs/`에 남는지
- lock이 중복 실행을 막는지
- 디스크 여유 공간이 충분한지

## 구현 우선순위

1. Discord interaction 응답 지연 문제 수정: 코드 패치 완료, 구형 맥북 버튼 재검증 필요
2. 성공 상태 전환 시 오래된 `failure_message` clear 개선: 코드 패치 및 테스트 추가 완료
3. 구형 맥북 의존성 목록 문서화 또는 requirements 파일 추가: `requirements-automation.txt` 추가 완료
4. launchd plist를 watcher와 Discord worker로 분리: plist 2개 추가 완료
5. 구형 맥북 실제 경로 기준 plist 작성: `/Users/wonjaechoi/kbo/title_news_code` 기준 작성 완료
6. launchd load/unload 및 재부팅 후 자동 기동 검증
7. 버튼 제작 승인 -> editor build -> quick tunnel 자동 경로 재검증
8. 운영 주기 30분으로 watcher 실사용 시작
