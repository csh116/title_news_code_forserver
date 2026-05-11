# Current Server Operations Diagnosis - 2026-05-11

이 문서는 2026-05-11 21시대 KST에 이 Mac 서버에서 실제 실행 중인 백그라운드 프로세스, launchd 등록 상태, 로그, SQLite 상태 DB, 관련 코드 경로를 기준으로 정리한 운영 진단이다.

## 1. 요약

현재 이 서버는 KBO 카드뉴스 자동화를 다음 구조로 운영하고 있다.

1. `com.kbo.title-news-automation.watcher`
   - 30분마다 뉴스 후보를 수집/선정하고 Discord에 후보 알림을 보내는 주기 작업.
   - launchd 기준 현재 순간에는 `not running`이 정상이다. `StartInterval=1800`으로 짧게 실행되고 종료되는 배치 작업이다.
   - 확인 시점 기준 `runs = 19`, `last exit code = 0`.

2. `com.kbo.title-news-automation.discord-worker`
   - Discord 버튼 interaction을 상시 대기하는 장기 실행 worker.
   - 확인 시점 기준 `running`, PID `35809`.
   - Discord에서 `제작` 버튼을 누르면 job 승인, editor 생성, editor server 기동, Tailscale Funnel 공개 URL 생성, 완료 알림까지 이어진다.

3. `com.kbo.tailscale-userspace`
   - Tailscale macOS 앱과 별도로 Homebrew `tailscaled`를 userspace mode로 띄운 공개 터널용 daemon.
   - 확인 시점 기준 `running`, PID `35763`.
   - Funnel은 live 상태이며 `https://kbo-editor-macbook.tailfb7825.ts.net`을 `127.0.0.1:8787`로 프록시한다.

4. `com.wonjaechoi.bb-future`
   - 이 저장소와 별도 경로인 `/Users/wonjaechoi/SSH/BB_FUTURE.py`를 실행 중이다.
   - 확인 시점 기준 `running`, PID `38489`.
   - KBO 자동화와 직접 연결된 코드는 아니지만 같은 서버 리소스를 쓰는 상시 Python 프로세스다.

상태 DB 기준 자동화 job은 총 52개이며, 상태 분포는 `notified=35`, `editor_ready=13`, `render_ready=3`, `skipped=1`이다. 오래 걸린 `pipeline_running`은 없다.

## 2. 실제 백그라운드 서비스 상태

### 2.1 Discord button worker

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
- `launchctl print` 기준 `state = running`, `runs = 1`, `last exit code = never exited`.
- 표준 출력/에러 로그:
  - `outputs/automation/logs/discord-worker.launchd.out.log`
  - `outputs/automation/logs/discord-worker.launchd.err.log`
- PID 파일:
  - `outputs/automation/discord_button_worker.pid`

코드 진입점:

- `src/kbo_card_news/automation/cli.py`의 `discord-button-worker` subcommand
- `src/kbo_card_news/automation/discord_bot_runner.py`의 `run_discord_button_worker`

### 2.2 Watcher

LaunchAgent:

```text
/Users/wonjaechoi/Library/LaunchAgents/com.kbo.title-news-automation.watcher.plist
```

실행 명령:

```bash
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python3.11 -m kbo_card_news.automation.cli \
  watch-cycle \
  --candidate-count 10 \
  --max-candidates 10 \
  --notify \
  --no-start-button-worker
```

운영 특성:

- `StartInterval=1800`이므로 30분마다 한 번 실행된다.
- 장기 실행 프로세스가 아니라 매 cycle마다 실행 후 종료된다.
- 확인 시점 기준 `state = not running`, `runs = 19`, `last exit code = 0`이다. 즉, 서비스가 죽은 것이 아니라 마지막 cycle을 정상 종료한 상태로 해석하는 것이 맞다.
- 표준 출력/에러 로그:
  - `outputs/automation/logs/watcher.launchd.out.log`
  - `outputs/automation/logs/watcher.launchd.err.log`
- lock:
  - `outputs/automation/locks/watcher.lock`

코드 진입점:

- `src/kbo_card_news/automation/cli.py`의 `watch-cycle`
- `src/kbo_card_news/automation/news_watcher.py`의 `watch_once`
- `src/kbo_card_news/automation/pipeline_runner.py`의 `generate_topic_candidates`

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

Tailscale status에는 다음 노드가 보였다.

```text
100.105.233.85  kbo-editor-macbook
100.83.82.12    macbookpro-for-af-group-3
100.78.208.52   macbookpro
```

주의할 점:

- automation은 macOS Tailscale 앱의 기본 daemon이 아니라 `/usr/local/opt/tailscale/bin/tailscale`과 `TAILSCALE_SOCKET` 기반 userspace daemon을 기준으로 작동한다.
- Funnel은 항상 `127.0.0.1:8787`로 프록시한다. editor server가 떠 있지 않은 시간에는 public URL 접속 시 `connection refused`가 로그에 남을 수 있다.

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

## 3. 자동화 데이터 흐름

현재 KBO 자동화의 운영 흐름은 다음과 같다.

```text
launchd watcher
  -> watch-cycle
  -> 최근 24시간 뉴스 후보 수집
  -> Gemini 기반 후보 선정
  -> topic 후보 10개 생성
  -> 중복 검사
  -> automation_state.db에 job 생성
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

- 후보/선정 산출물: `outputs/approval_run_*/01_topic_candidates/`
- 확정 topic: `outputs/approval_run_*/02_topic_selection/`
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
total_jobs = 52
editor_ready = 13
notified = 35
render_ready = 3
skipped = 1
stale_pipeline_running = []
disk_free_gb = 76.35
disk_ok = true
```

진단:

- `pipeline_running`에 오래 멈춘 job이 없다. build 도중 중단된 job 복구는 현재 필요 없어 보인다.
- `notified`가 35개로 많다. 후보 알림 후 승인되지 않은 job이 누적되는 구조다.
- `health` 기준 오래된 승인 대기 job은 18개다. 운영상 오래된 후보를 정리하려면 `expire-pending --stale-hours 12`를 주기적으로 실행하는 것이 좋다.
- 디스크 여유 공간은 충분하다.

## 5. 로그에서 확인된 운영 상태

### 5.1 Watcher 로그

최근 watcher 로그는 30분 주기로 정상 실행되는 패턴이다.

관찰된 최근 cycle:

- `2026-05-11 20:14 KST` window 기준 실행
- `2026-05-11 20:45 KST` window 기준 실행
- `2026-05-11 21:16 KST` window 기준 실행

각 cycle은 다음 정보를 출력한다.

- 최근 24시간 window
- `candidate_count=10`
- `gemini-2.5-flash-lite` 모델 시도
- 후보 topic 10개
- 생성/중복/알림 수

최근 예시:

```text
approval_run_20260511_211634
created_count=1
duplicate_count=9
notified_count=1
notification_failed_count=0
```

진단:

- watcher 자체는 정상적으로 실행되고 있다.
- 최근 뉴스 수집은 새 기사 삽입이 0개인 cycle도 있었지만, 기존 수집 DB와 후보 생성은 동작했다.
- 중복 제거가 강하게 작동해 10개 후보 중 9개가 중복으로 처리되는 cycle이 자주 보인다. 이는 버그라기보다 같은 24시간 window를 30분마다 재평가하는 설계의 결과다.

### 5.2 Discord worker 로그

Discord gateway 연결은 유지되고 있으며 session resume 로그가 반복된다.

확인된 성공 흐름:

```text
tailscale_funnel_starting
tailscale_funnel_started url=https://kbo-editor-macbook.tailfb7825.ts.net
public_editor_url_ready url=https://kbo-editor-macbook.tailfb7825.ts.net/topic/1?token=...
```

진단:

- 2026-05-11 13:56 이후 userspace Tailscale socket 기반 Funnel 생성은 성공했다.
- `Address already in use`가 한 번 발생했으나 worker가 기존 8787 editor server PID를 종료하고 회복한 기록이 있다.
- 과거 로그에는 Discord interaction 3초 제한 문제로 `Unknown interaction` / `Interaction has already been acknowledged`가 있었다. 현재 코드에는 defer 처리와 followup 응답 로직이 들어가 있어 이전보다 개선된 상태로 보인다.

### 5.3 Tailscale 로그

Funnel 자체는 live다. 다만 editor server가 떠 있지 않은 시간에 public URL 접근이 들어오면 다음 로그가 남는다.

```text
http: proxy error: dial tcp 127.0.0.1:8787: connect: connection refused
```

진단:

- 이것은 Tailscale daemon 장애가 아니라, Funnel이 프록시할 로컬 editor server가 현재 떠 있지 않다는 의미다.
- editor server는 제작 버튼 처리 후 필요할 때 뜨고, `idle_timeout_seconds=1800` 또는 렌더 후 종료 정책으로 내려간다.
- 따라서 이 로그는 상시 발생 가능하며, 특정 job 제작 직후에도 계속 발생하면 editor server 기동 실패를 의심해야 한다.

## 6. 현재 운영 리스크

### 6.1 오래된 notified job 누적

현재 `notified=35`이고, health 기준 오래된 승인 대기 job이 18개다. 후보 알림이 계속 쌓이면 Discord/DB 목록이 지저분해지고 중복 판정 기준에도 영향을 줄 수 있다.

권장:

```bash
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python3.11 -m kbo_card_news.automation.cli expire-pending --stale-hours 12
```

### 6.2 editor server 포트 8787 단일화

모든 editor 공개가 `127.0.0.1:8787`과 Tailscale Funnel 하나에 의존한다. 동시 제작 또는 이전 editor server 잔존 시 `Address already in use`가 발생한다.

현재 worker에는 포트 점유 회복 로직이 있어 한 번 복구된 기록이 있다. 그래도 동시에 여러 job을 제작하는 운영에는 맞지 않는다.

권장:

- 현재처럼 “한 번에 하나의 제작” 운영을 유지한다.
- `outputs/automation/locks/editor_build.lock`을 계속 사용한다.

### 6.3 Funnel URL은 살아 있지만 editor는 상시 서비스가 아니다

`https://kbo-editor-macbook.tailfb7825.ts.net`은 계속 열려 있어도, editor server가 없으면 `connection refused`가 난다. 운영자 입장에서는 Funnel 장애와 editor 미기동을 구분해야 한다.

구분법:

```bash
/usr/local/opt/tailscale/bin/tailscale --socket=/Users/wonjaechoi/.local/share/kbo-tailscale/tailscaled.sock funnel status
```

Funnel이 on인데 접속이 안 되면 다음을 본다.

```bash
launchctl print gui/501/com.kbo.title-news-automation.discord-worker
tail -n 120 outputs/automation/logs/discord-worker.launchd.err.log
```

### 6.4 BB_FUTURE 프로세스는 별도 운영물

`/Users/wonjaechoi/SSH/BB_FUTURE.py`가 같은 서버에서 상시 실행 중이다. KBO 자동화 장애 원인은 아니지만 리소스 경합, Python 버전 차이, 포트 사용 여부를 진단할 때 함께 확인해야 한다.

## 7. 운영 점검 명령

서비스 상태:

```bash
launchctl print gui/501/com.kbo.title-news-automation.discord-worker
launchctl print gui/501/com.kbo.title-news-automation.watcher
launchctl print gui/501/com.kbo.tailscale-userspace
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

## 8. 결론

현재 서버 운영은 대체로 정상이다.

- watcher는 30분마다 정상 실행되고 종료된다.
- Discord worker는 상시 실행 중이며 Discord gateway 연결을 유지한다.
- Tailscale userspace daemon과 Funnel은 live 상태다.
- 상태 DB에는 멈춘 `pipeline_running` job이 없다.
- 디스크 여유 공간도 충분하다.

다만 운영 품질 측면에서는 오래된 `notified` job 정리, 8787 단일 포트 운영의 한계, Funnel live와 editor server live를 구분하는 모니터링이 필요하다. 당장 장애로 보이는 항목은 없지만, 승인 대기 job 정리는 권장한다.
