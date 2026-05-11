# Current Server Operations Diagnosis - 2026-05-12

이 문서는 2026-05-12 02:35 KST에 이 Mac 서버에서 실제 실행 중인 launchd 서비스, 로그, 상태 DB, Tailscale Funnel 상태를 기준으로 다시 작성한 운영 진단이다.

`docs/current_server_operations_diagnosis_20260511.md`는 기존 `watch-cycle` 중심 진단이므로 지금 운영 기준과 다르다. 현재 watcher는 `watch-fresh-cycle` 기반의 5분 주기 fresh issue 감지기로 교체되어 있다.

## 1. 요약

현재 이 서버는 KBO 카드뉴스 자동화를 다음 구조로 운영하고 있다.

1. `com.kbo.title-news-automation.watcher`
   - 현재 명령은 구형 `watch-cycle`이 아니라 `watch-fresh-cycle`이다.
   - 5분마다 최근 기사 삽입분을 보고 fresh issue를 감지한다.
   - KST 00:00-07:00에는 quiet hours 정책으로 실행을 건너뛴다.
   - 확인 시점 기준 `not running`이 정상이며, `runs = 33`, `last exit code = 0`, `StartInterval = 300`이다.

2. `com.kbo.title-news-automation.discord-worker`
   - Discord 버튼 interaction을 상시 대기하는 장기 실행 worker다.
   - 확인 시점 기준 `running`, PID `41032`.
   - `제작` 버튼을 누르면 job 승인, editor 생성, editor server 기동, Tailscale Funnel 공개 URL 생성, 완료 알림까지 이어진다.

3. `com.kbo.tailscale-userspace`
   - Homebrew `tailscaled`를 userspace mode로 띄운 공개 터널용 daemon이다.
   - 확인 시점 기준 `running`, PID `35763`.
   - Funnel은 live 상태이며 `https://kbo-editor-macbook.tailfb7825.ts.net`을 `127.0.0.1:8787`로 프록시한다.

4. `com.wonjaechoi.bb-future`
   - 이 저장소와 별도 경로인 `/Users/wonjaechoi/SSH/BB_FUTURE.py`를 실행 중이다.
   - 확인 시점 기준 `running`, PID `38489`.
   - KBO 자동화와 직접 연결된 코드는 아니지만 같은 서버 리소스를 쓰는 상시 Python 프로세스다.

상태 DB 기준 자동화 job은 총 53개이며, 상태 분포는 `notified=35`, `editor_ready=13`, `render_ready=4`, `skipped=1`이다. 오래 멈춘 `pipeline_running`은 없다. 오래된 승인 대기 job은 22개다.

## 2. 실제 백그라운드 서비스 상태

### 2.1 Fresh watcher

LaunchAgent:

```text
/Users/wonjaechoi/Library/LaunchAgents/com.kbo.title-news-automation.watcher.plist
```

실행 명령:

```bash
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python3.11 -m kbo_card_news.automation.cli \
  watch-fresh-cycle \
  --no-gemini-review \
  --notify \
  --min-issue-score 70 \
  --max-jobs 3 \
  --json
```

운영 특성:

- `StartInterval=300`이므로 5분마다 한 번 실행된다.
- 장기 실행 프로세스가 아니라 매 cycle마다 실행 후 종료된다.
- 확인 시점 기준 `state = not running`, `runs = 33`, `last exit code = 0`이다.
- `--no-gemini-review`가 적용되어 fresh watcher는 Gemini 2차 판단 없이 rule 기반 점수로 알림 대상을 고른다.
- `--min-issue-score 70`, `--max-jobs 3` 기준으로 한 cycle에서 최대 3개 job만 만든다.
- 표준 출력/에러 로그:
  - `outputs/automation/logs/watcher.launchd.out.log`
  - `outputs/automation/logs/watcher.launchd.err.log`

코드 진입점:

- `src/kbo_card_news/automation/cli.py`의 `watch-fresh-cycle`
- `src/kbo_card_news/automation/fresh_issue_detector.py`
- `src/kbo_card_news/automation/news_collection.py`
- `src/kbo_card_news/automation/job_deduplication.py`

### 2.2 Discord button worker

LaunchAgent:

```text
/Users/wonjaechoi/Library/LaunchAgents/com.kbo.title-news-automation.discord-worker.plist
```

실행 명령:

```bash
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python3.11 -m kbo_card_news.automation.cli \
  discord-button-worker \
  --auto-build \
  --notify-build \
  --host 0.0.0.0 \
  --public-host 100.73.65.42 \
  --port 8787 \
  --build-lock-path /Users/wonjaechoi/kbo/title_news_code/outputs/automation/locks/editor_build.lock
```

운영 특성:

- `KeepAlive=true`, `RunAtLoad=true`라서 로그인 세션에서 계속 살아 있어야 한다.
- 확인 시점 기준 `state = running`, `runs = 2`, PID `41032`.
- `last terminating signal = Terminated: 15` 기록이 있으므로 한 번 재시작된 이력이 있다. 현재는 정상 running 상태다.
- 표준 출력/에러 로그:
  - `outputs/automation/logs/discord-worker.launchd.out.log`
  - `outputs/automation/logs/discord-worker.launchd.err.log`
- PID 파일:
  - `outputs/automation/discord_button_worker.pid`

코드 진입점:

- `src/kbo_card_news/automation/cli.py`의 `discord-button-worker`
- `src/kbo_card_news/automation/discord_bot_runner.py`의 `run_discord_button_worker`

### 2.3 Tailscale userspace daemon

LaunchAgent:

```text
/Users/wonjaechoi/Library/LaunchAgents/com.kbo.tailscale-userspace.plist
```

실행 명령:

```bash
/usr/local/opt/tailscale/bin/tailscaled \
  --tun=userspace-networking \
  --socket=/Users/wonjaechoi/.local/share/kbo-tailscale/tailscaled.sock \
  --state=/Users/wonjaechoi/.local/share/kbo-tailscale/tailscaled.state \
  --statedir=/Users/wonjaechoi/.local/share/kbo-tailscale
```

확인 시점 상태:

```text
state = running
pid = 35763
Funnel URL = https://kbo-editor-macbook.tailfb7825.ts.net
Funnel proxy = http://127.0.0.1:8787
```

주의할 점:

- automation은 macOS Tailscale 앱의 기본 daemon이 아니라 `/usr/local/opt/tailscale/bin/tailscale`과 `TAILSCALE_SOCKET` 기반 userspace daemon을 기준으로 작동한다.
- sandbox 안에서 `tailscale --socket ... funnel status`를 실행하면 socket 접근이 `operation not permitted`로 막힐 수 있다. 실제 운영 점검은 일반 터미널이나 권한 허용된 실행에서 확인해야 한다.
- Funnel은 항상 `127.0.0.1:8787`로 프록시한다. editor server가 떠 있지 않은 시간에는 public URL 접속 시 `connection refused`가 정상적으로 발생할 수 있다.

### 2.4 BB_FUTURE 별도 프로세스

LaunchAgent:

```text
/Users/wonjaechoi/Library/LaunchAgents/com.wonjaechoi.bb-future.plist
```

실행 명령:

```bash
/usr/local/bin/python3 /Users/wonjaechoi/SSH/BB_FUTURE.py
```

확인 시점 기준 `running`, PID `38489`다. 이 저장소의 KBO 자동화와 직접 같은 코드 경로를 쓰지는 않지만, CPU/RAM과 Python runtime을 공유하는 별도 상주 작업이다.

## 3. 현재 자동화 데이터 흐름

현재 운영 흐름은 구형 24시간 후보 재선정 방식이 아니라 fresh issue 감지 방식이다.

```text
launchd watcher
  -> watch-fresh-cycle
  -> 최근 10분 기사 수집
  -> 새로 insert된 기사만 fresh article로 사용
  -> 최근 24시간 context 조회
  -> 팀/키워드/상태 변화 기반 issue_score 계산
  -> min_issue_score 이상 후보만 job 생성
  -> Discord 후보 알림 전송

Discord 사용자
  -> 제작 버튼 클릭

discord-button-worker
  -> Discord interaction defer/응답
  -> job 승인 처리
  -> build-approved-editor 실행
  -> title HTML editor 산출물 생성
  -> serve-job-editor subprocess 실행
  -> Tailscale Funnel 확인/시작
  -> public editor URL Discord 알림

Editor 사용자
  -> 브라우저에서 편집
  -> PNG 저장
  -> /render 호출
  -> record-render
  -> job 상태 render_ready
  -> Discord 렌더 완료 알림
```

주요 산출물 경로:

- fresh watcher report: `outputs/automation/fresh_watch_runs/YYYYMMDD_HHMMSS/fresh_watch_report.json`
- fresh watcher choice: `outputs/automation/fresh_watch_runs/YYYYMMDD_HHMMSS/topic_selection_choice.json`
- editor manifest/report: `outputs/approval_run_*/03_title_html_editor_no_multimodal/`
- PNG/state/social copy: `outputs/approval_run_*/title_render_pngs/`
- 상태 DB: `outputs/automation/automation_state.db`
- 수집 DB: `outputs/source_collection.db`

## 4. 상태 DB 진단

확인 명령:

```bash
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python3.11 -m kbo_card_news.automation.cli health --json
```

확인 시점 결과:

```text
total_jobs = 53
editor_ready = 13
notified = 35
render_ready = 4
skipped = 1
stale_pipeline_running = []
stale_pending_approval = 22
disk_free_gb = 76.33
disk_ok = true
```

진단:

- `pipeline_running`에 오래 멈춘 job이 없다. build 도중 중단된 job 복구는 현재 필요 없어 보인다.
- `render_ready`가 4개로 늘어 실제 제작/렌더 완료 흐름은 작동하고 있다.
- `notified=35`, 오래된 승인 대기 job 22개는 계속 누적 중이다.
- 디스크 여유 공간은 충분하다.

오래된 승인 대기 job 정리 권장 명령:

```bash
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python3.11 -m kbo_card_news.automation.cli expire-pending --stale-hours 12
```

## 5. 로그에서 확인된 운영 상태

### 5.1 Watcher 로그

확인 시점은 KST 02시대라 `watch-fresh-cycle`이 quiet hours 정책에 따라 정상적으로 skip되고 있다.

최근 로그 패턴:

```json
{
  "status": "skipped_quiet_hours",
  "reason": "quiet_hours_00_to_07",
  "quiet_start_hour": 0,
  "quiet_end_hour": 7,
  "now": "2026-05-12T02:36:00+09:00"
}
```

진단:

- 5분 주기로 launchd가 실행하고 있다.
- 00:00-07:00 사이 skip은 장애가 아니라 의도된 운영 정책이다.
- 07시대 첫 실행은 00:00-07:00 발행 기사를 catch-up 처리하는 설계다.

quiet hours 직전 fresh watcher report 예시:

```text
collection_window = 2026-05-11 23:49-23:59 KST
collected_count = 0
inserted_count = 0
fresh_article_count = 0
context_article_count = 71
candidate_count = 0
created_count = 0
collector_errors = []
```

초기 fresh watcher 실행에는 SSL 인증서 오류가 있었지만, 최신 report에서는 `collector_errors = []`로 보인다.

### 5.2 Discord worker 로그

Discord gateway 연결은 유지되고 있으며 session resume 로그가 반복된다.

확인된 성공 흐름:

```text
tailscale_funnel_started url=https://kbo-editor-macbook.tailfb7825.ts.net
public_editor_url_ready url=https://kbo-editor-macbook.tailfb7825.ts.net/topic/1?token=...
POST /render?... 200
```

진단:

- Tailscale Funnel 기반 공개 editor URL 생성은 동작한다.
- 최근 editor server는 `/render` POST 200까지 기록되어 렌더 완료 흐름이 확인된다.
- 과거 `Address already in use`가 한 번 있었고 worker가 기존 8787 editor server PID를 종료해 복구한 기록이 있다.
- `BrokenPipeError`는 asset 전송 중 클라이언트가 연결을 끊은 흔적으로 보이며, 단독으로는 운영 장애로 판단하지 않는다.
- `/font/light`, `/font/medium`, `/font/bold` 404가 과거 일부 editor 세션에서 있었지만 이후 세션에서는 200으로 확인된다.

### 5.3 Tailscale Funnel

현재 Funnel status:

```text
# Funnel on:
#     - https://kbo-editor-macbook.tailfb7825.ts.net

https://kbo-editor-macbook.tailfb7825.ts.net (Funnel on)
|-- / proxy http://127.0.0.1:8787
```

진단:

- Funnel 자체는 live다.
- public base URL이 살아 있어도 editor server는 작업 단위로만 떠 있다.
- editor server가 내려간 상태에서 base URL만 열면 `connection refused`가 날 수 있다. 이것은 Tailscale 장애가 아니라 프록시 대상이 없는 상태다.

## 6. 현재 운영 리스크

### 6.1 Watcher 운영 문서/예시 plist가 구형이다

실제 설치된 watcher plist는 `watch-fresh-cycle`과 `StartInterval=300`을 사용한다. 반면 `docs/com.kbo.title-news-automation.watcher.macbook.plist`는 아직 `watch-cycle`, `StartInterval=1800` 기준이다.

권장:

- 배포/복구용 문서와 plist 예시를 현재 운영값으로 맞춘다.
- 구형 `watch-cycle` 문서는 "수동 진단용" 또는 "legacy"로 분리한다.

### 6.2 오래된 notified job 누적

현재 `notified=35`이고, health 기준 오래된 승인 대기 job이 22개다. 후보 알림 후 승인되지 않은 job이 계속 쌓이면 DB와 Discord 운영이 지저분해지고 중복 판정에도 영향을 줄 수 있다.

권장:

```bash
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python3.11 -m kbo_card_news.automation.cli expire-pending --stale-hours 12
```

### 6.3 5분 주기 fresh watcher와 quiet hours

5분 주기는 기존 30분 `watch-cycle`보다 훨씬 촘촘하다. 현재는 `watch-fresh-cycle`이 새로 insert된 기사만 보고, 00:00-07:00 quiet hours를 적용하므로 비용과 알림 피로도를 줄이는 방향이다.

주의:

- `--no-gemini-review`이므로 알림 품질은 rule score에 직접 의존한다.
- 기준이 너무 느슨하면 불필요한 `notified`가 쌓이고, 너무 빡빡하면 중요한 이슈를 놓칠 수 있다.
- 현재 기준은 `--min-issue-score 70`, `--max-jobs 3`이다.

### 6.4 editor server 포트 8787 단일화

모든 editor 공개가 `127.0.0.1:8787`과 Tailscale Funnel 하나에 의존한다. 동시 제작 또는 이전 editor server 잔존 시 `Address already in use`가 발생한다.

현재 worker에는 포트 점유 회복 로직이 있고 실제 복구 기록도 있다. 그래도 동시에 여러 job을 제작하는 운영에는 맞지 않는다.

권장:

- 현재처럼 "한 번에 하나의 제작" 운영을 유지한다.
- `outputs/automation/locks/editor_build.lock`을 계속 사용한다.

### 6.5 Funnel live와 editor live는 다르다

`https://kbo-editor-macbook.tailfb7825.ts.net`이 Funnel on이어도, editor server가 없으면 접속은 실패할 수 있다.

구분법:

```bash
/usr/local/opt/tailscale/bin/tailscale --socket=/Users/wonjaechoi/.local/share/kbo-tailscale/tailscaled.sock funnel status
launchctl print gui/501/com.kbo.title-news-automation.discord-worker
tail -n 120 outputs/automation/logs/discord-worker.launchd.err.log
```

## 7. 운영 점검 명령

서비스 상태:

```bash
launchctl print gui/501/com.kbo.title-news-automation.discord-worker
launchctl print gui/501/com.kbo.title-news-automation.watcher
launchctl print gui/501/com.kbo.tailscale-userspace
launchctl print gui/501/com.wonjaechoi.bb-future
```

자동화 health:

```bash
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python3.11 -m kbo_card_news.automation.cli health --json
```

최근 job:

```bash
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python3.11 -m kbo_card_news.automation.cli list --limit 30
```

Tailscale Funnel:

```bash
/usr/local/opt/tailscale/bin/tailscale --socket=/Users/wonjaechoi/.local/share/kbo-tailscale/tailscaled.sock funnel status
```

로그:

```bash
tail -n 120 outputs/automation/logs/watcher.launchd.out.log
tail -n 120 outputs/automation/logs/watcher.launchd.err.log
tail -n 120 outputs/automation/logs/discord-worker.launchd.out.log
tail -n 120 outputs/automation/logs/discord-worker.launchd.err.log
tail -n 120 outputs/automation/logs/tailscale-userspace.err.log
```

Fresh watcher 최신 report:

```bash
find outputs/automation/fresh_watch_runs -maxdepth 2 -name fresh_watch_report.json | sort | tail
```

## 8. 결론

현재 서버 운영은 대체로 정상이다.

- watcher는 현재 `watch-fresh-cycle`로 교체되어 5분마다 실행된다.
- 확인 시점이 quiet hours라 watcher가 skip되는 것은 정상이다.
- Discord worker는 상시 실행 중이며 Discord gateway 연결을 유지한다.
- Tailscale userspace daemon과 Funnel은 live 상태다.
- 상태 DB에는 멈춘 `pipeline_running` job이 없다.
- 실제 렌더 완료 job이 4개 있어 승인 후 editor/render 흐름도 작동 중이다.
- 디스크 여유 공간도 충분하다.

당장 장애로 보이는 항목은 없다. 다만 현재 기준으로는 문서/예시 plist의 구형 watcher 설정 정리, 오래된 `notified` job 만료, 8787 단일 포트 운영 한계 관리가 우선 개선 대상이다.
