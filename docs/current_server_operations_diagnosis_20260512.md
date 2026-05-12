# Current Server Operations Diagnosis - 2026-05-12

이 문서는 2026-05-12 21:27 KST 기준으로 이 Mac 서버에서 실제 로드된 LaunchAgent, 실행 중인 프로세스, watcher 로그, 상태 DB, Tailscale Funnel 상태를 다시 확인해 갱신한 운영 진단이다.

`docs/current_server_operations_diagnosis_20260511.md`는 구형 `watch-cycle` 중심 진단이고, 이 문서는 현재 설치된 `watch-fresh-cycle` 운영 기준이다. 이전 2026-05-12 문서에 있던 5분 주기, `--no-gemini-review`, `--min-issue-score`, `--max-jobs` 설명은 현재 실제 설치 상태와 다르다.

## 1. 요약

현재 서버는 다음 작업을 중심으로 운영 중이다.

1. `com.kbo.title-news-automation.watcher`
   - 실제 명령은 `watch-fresh-cycle --notify --json`이다.
   - `StartInterval=600`이므로 10분마다 한 번 실행된다.
   - 장기 실행 프로세스가 아니라 실행 후 종료되는 배치라서 `state = not running`이 정상이다.
   - 확인 시점 기준 `runs = 25`, `last exit code = 0`, `run interval = 600 seconds`다.
   - fresh window decision gate는 Gemini 모델 `gemini-3.1-flash-lite-preview`를 사용한다.

2. `com.kbo.title-news-automation.discord-worker`
   - Discord 버튼 interaction을 상시 대기하는 장기 실행 worker다.
   - 확인 시점 기준 `state = running`, PID `51725`, `runs = 2`다.
   - `제작` 버튼 이후 editor 생성, editor server 기동, Tailscale Funnel 공개 URL 안내, render 기록까지 담당한다.

3. `com.kbo.tailscale-userspace`
   - Homebrew `tailscaled`를 userspace mode로 띄운 공개 터널용 daemon이다.
   - 확인 시점 기준 `state = running`, PID `35763`, `runs = 1`이다.
   - Funnel은 live 상태이며 `https://kbo-editor-macbook.tailfb7825.ts.net`을 `127.0.0.1:8787`로 프록시한다.

4. `com.wonjaechoi.bb-future`
   - 이 저장소 밖의 `/Users/wonjaechoi/SSH/BB_FUTURE.py`를 실행 중이다.
   - 확인 시점 기준 PID `38489`이며 CPU/RAM을 공유하는 별도 상주 Python 작업이다.

상태 DB 기준 자동화 job은 총 64개다. 상태 분포는 `notified=42`, `editor_ready=13`, `render_ready=5`, `skipped=2`, `approved=1`, `failed=1`이다. 오래 멈춘 `pipeline_running`은 없지만 오래된 승인 대기 job은 39개로 누적되어 있다.

현재 가장 중요한 운영 리스크는 watcher 스케줄이 아니라 Gemini decision 단계다. 최근 새 기사가 들어온 cycle에서 `gemini-3.1-flash-lite-preview`가 HTTP 503을 반환해 `model_error = "All models failed"`가 기록됐다. 별도 상세 진단은 `docs/watch_fresh_cycle_gemini_503_diagnosis_20260512.md`에 정리했다.

## 2. 실제 백그라운드 서비스 상태

### 2.1 Fresh watcher

LaunchAgent:

```text
/Users/wonjaechoi/Library/LaunchAgents/com.kbo.title-news-automation.watcher.plist
```

실제 설치 명령:

```bash
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python3.11 -m kbo_card_news.automation.cli \
  watch-fresh-cycle \
  --notify \
  --json
```

실제 plist 주요값:

```text
StartInterval = 600
RunAtLoad = true
WorkingDirectory = /Users/wonjaechoi/kbo/title_news_code
stdout = outputs/automation/logs/watcher.launchd.out.log
stderr = outputs/automation/logs/watcher.launchd.err.log
```

launchd 확인 결과:

```text
state = not running
runs = 25
last exit code = 0
run interval = 600 seconds
```

운영 특성:

- 10분마다 최근 10분 window를 수집한다.
- 새 target article이 없으면 Gemini를 호출하지 않고 `skipped_empty_target`으로 정상 종료한다.
- 새 target article이 있으면 `fresh-window-decision.v1` 프롬프트로 Gemini decision gate를 호출한다.
- 기본 fresh decision 모델은 `gemini-3.1-flash-lite-preview`다.
- `--no-gemini-review`는 현재 지원되지 않는다. 코드상 `watch-fresh-cycle requires Gemini fresh window decision gate; --no-gemini-review is no longer supported.`로 종료된다.
- `--min-issue-score`, `--max-jobs` 인자는 남아 있지만 현재 CLI help에서는 숨겨져 있고, 현재 설치 plist에는 사용되지 않는다.

코드 진입점:

- `src/kbo_card_news/automation/cli.py`의 `watch-fresh-cycle`
- `src/kbo_card_news/automation/fresh_window_decision.py`
- `src/kbo_card_news/runtime/model_fallback.py`
- `src/kbo_card_news/automation/news_collection.py`
- `src/kbo_card_news/automation/job_deduplication.py`

### 2.2 Discord button worker

LaunchAgent:

```text
/Users/wonjaechoi/Library/LaunchAgents/com.kbo.title-news-automation.discord-worker.plist
```

실제 설치 명령:

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

launchd 확인 결과:

```text
state = running
pid = 51725
runs = 2
last terminating signal = Terminated: 15
```

운영 특성:

- `KeepAlive=true`, `RunAtLoad=true`라 로그인 세션에서 계속 살아 있어야 한다.
- Discord interaction을 받아 job 승인 및 editor build를 처리한다.
- editor server는 `0.0.0.0:8787`로 뜨고, Funnel public URL은 Tailscale 경유로 안내된다.
- build lock은 `outputs/automation/locks/editor_build.lock`을 사용한다.

코드 진입점:

- `src/kbo_card_news/automation/cli.py`의 `discord-button-worker`
- `src/kbo_card_news/automation/discord_bot_runner.py`

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

launchd 확인 결과:

```text
state = running
pid = 35763
runs = 1
last exit code = never exited
```

Funnel status:

```text
# Funnel on:
#     - https://kbo-editor-macbook.tailfb7825.ts.net

https://kbo-editor-macbook.tailfb7825.ts.net (Funnel on)
|-- / proxy http://127.0.0.1:8787
```

주의할 점:

- automation은 macOS Tailscale 앱의 기본 daemon이 아니라 `/usr/local/opt/tailscale/bin/tailscale`과 `TAILSCALE_SOCKET` 기반 userspace daemon을 기준으로 작동한다.
- Funnel이 live여도 editor server가 떠 있지 않으면 public URL 접속은 실패할 수 있다. 이 경우는 Tailscale 장애가 아니라 `127.0.0.1:8787` 대상 프로세스가 없는 상태다.

### 2.4 BB_FUTURE 별도 프로세스

확인된 프로세스:

```text
PID 38489
/usr/local/Cellar/python@3.14/3.14.4_1/.../Python /Users/wonjaechoi/SSH/BB_FUTURE.py
```

KBO 자동화 코드 경로와 직접 연결되지는 않지만, 같은 서버 리소스를 쓰는 장기 실행 Python 프로세스다.

## 3. 현재 자동화 데이터 흐름

현재 watcher는 구형 24시간 후보 재선정 방식이 아니라 fresh window decision 방식이다.

```text
launchd watcher
  -> watch-fresh-cycle
  -> 최근 10분 기사 수집
  -> 새로 insert된 기사만 target article로 사용
  -> 최근 24시간 context article 조회
  -> feedback/completed topic context 구성
  -> Gemini fresh-window-decision.v1 호출
  -> publish/hold/reject decision 파싱
  -> publish decision에 대해 automation job 생성
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

- fresh decision report: `outputs/automation/fresh_watch_runs/YYYYMMDD_HHMMSS/fresh_window_decision_report.json`
- fresh decision response: `outputs/automation/fresh_watch_runs/YYYYMMDD_HHMMSS/fresh_window_decision_response.json`
- fresh decision prompt: `outputs/automation/fresh_watch_runs/YYYYMMDD_HHMMSS/fresh_window_decision_prompt.json`
- choice file: `outputs/automation/fresh_watch_runs/YYYYMMDD_HHMMSS/topic_selection_choice.json`
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
total_jobs = 64
approved = 1
editor_ready = 13
failed = 1
notified = 42
render_ready = 5
skipped = 2
stale_pipeline_running = []
stale_pending_approval = 39
disk_free_gb = 76.28
disk_ok = true
```

진단:

- `pipeline_running`에 오래 멈춘 job은 없다.
- `render_ready=5`라 승인 후 editor/render 흐름은 실제로 작동한 이력이 있다.
- `notified=42`와 오래된 승인 대기 job 39개는 누적 관리가 필요하다.
- `failed=1`이 있으므로 실패 job의 failure message는 별도 확인이 필요하다.
- 디스크 여유 공간은 충분하다.

오래된 승인 대기 job 정리 권장 명령:

```bash
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python3.11 -m kbo_card_news.automation.cli expire-pending --stale-hours 12
```

## 5. Watcher 최근 동작

최근 fresh run 디렉토리:

```text
20260512_212000
20260512_210900
20260512_205900
20260512_204800
20260512_203700
20260512_202500
20260512_201400
20260512_200200
```

최근 실행은 10-12분 간격으로 생성되고 있다. `StartInterval=600`이어도 cycle 실행 시간, 네트워크 대기, launchd 스케줄링 때문에 디렉토리 timestamp가 정확히 10분 정각으로 고정되지는 않는다.

최신 확인 파일 상태:

```text
watcher.launchd.out.log    May 12 21:20:35 2026
watcher.launchd.err.log    May  9 19:46:33 2026
```

최신 `20260512_212000` report:

```json
{
  "collection_window_start": "2026-05-12T21:10:00+09:00",
  "collection_window_end": "2026-05-12T21:20:00+09:00",
  "collected_count": 0,
  "inserted_count": 0,
  "target_article_count": 0,
  "context_article_count": 46,
  "model_name": "gemini-3.1-flash-lite-preview",
  "model_call_status": "skipped_empty_target",
  "model_error": "",
  "created_jobs": []
}
```

진단:

- scheduler/launchd는 정상적으로 10분 주기로 실행 중이다.
- 최근 20:37 이후 새 target article이 없던 cycle들은 `skipped_empty_target`으로 정상 종료했다.
- 새 target article이 있던 19:40, 20:02, 20:14, 20:25 cycle에서는 Gemini decision gate가 실패해 `model_error = "All models failed"`가 기록됐다.

## 6. Gemini 503 이슈

현재 watcher는 새 기사가 있을 때 `gemini-3.1-flash-lite-preview`를 호출한다. 최근 해당 모델 health check에서 다음 응답이 확인됐다.

```text
HTTP 503
status: UNAVAILABLE
message: This model is currently experiencing high demand. Spikes in demand are usually temporary. Please try again later.
```

같은 시점의 모델별 health check:

```text
gemini-2.5-flash-lite OK {"ok":"yes"}
gemini-2.5-flash RetryableModelError Gemini API request failed: HTTP 503 {
gemini-3.1-flash-lite-preview RetryableModelError Gemini API request failed: HTTP 503 {
```

코드상 `call_with_fallback()`은 마지막 예외를 cause로 둔 채 `RuntimeError("All models failed")`를 올리고, `watch_fresh_window_once()`는 `str(exc)`만 저장한다. 그래서 report에는 실제 503 원인이 아니라 `All models failed`만 남는다.

권장:

- fresh decision 기본 모델을 현재 응답 가능한 `gemini-2.5-flash-lite`로 바꾸거나, `gemini-3.1-flash-lite-preview -> gemini-2.5-flash-lite` fallback을 추가한다.
- `fresh_window_decision_response.json`에 `exc.__cause__` 메시지까지 저장해 다음 장애 때 root cause를 바로 볼 수 있게 한다.
- 상세 진단 문서: `docs/watch_fresh_cycle_gemini_503_diagnosis_20260512.md`

## 7. Discord worker와 editor 경로

확인 시점의 Discord worker:

```text
state = running
pid = 51725
stdout log = outputs/automation/logs/discord-worker.launchd.out.log
stderr log = outputs/automation/logs/discord-worker.launchd.err.log
```

로그 파일 timestamp:

```text
discord-worker.launchd.out.log  May 12 17:55:40 2026
discord-worker.launchd.err.log  May 12 17:53:59 2026
```

진단:

- worker 프로세스는 살아 있다.
- 최근 로그 timestamp는 17:55/17:53으로, 확인 시점 이후 새 버튼 interaction은 없었던 것으로 보인다.
- editor 공개는 `127.0.0.1:8787` 단일 포트와 Tailscale Funnel 하나에 의존한다.
- 동시 제작 또는 이전 editor server 잔존 시 `Address already in use`가 발생할 수 있으므로 `editor_build.lock` 기반의 한 번에 하나 제작 운영을 유지한다.

## 8. 현재 운영 리스크

### 8.1 Gemini preview 모델 가용성

현재 watcher의 주요 리스크다. `gemini-3.1-flash-lite-preview`가 503이면 새 기사 decision cycle이 `no_decision`으로 끝나고 job이 생성되지 않는다.

권장 우선순위:

1. 기본 모델을 `gemini-2.5-flash-lite`로 변경한다.
2. fallback policy에 `gemini-2.5-flash-lite`를 추가한다.
3. failure report에 root cause를 저장한다.

### 8.2 오래된 notified job 누적

현재 `notified=42`, 오래된 승인 대기 job 39개다. 승인되지 않은 후보가 계속 쌓이면 운영 화면과 중복 판정이 지저분해질 수 있다.

권장:

```bash
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python3.11 -m kbo_card_news.automation.cli expire-pending --stale-hours 12
```

### 8.3 문서/예시 plist drift

실제 설치된 watcher는 `watch-fresh-cycle --notify --json`, `StartInterval=600`이다. 반면 일부 docs 예시 plist는 여전히 구형 `watch-cycle` 또는 30분 주기 기준일 수 있다.

권장:

- `docs/com.kbo.title-news-automation.watcher.macbook.plist`와 운영 문서를 실제 설치값에 맞춘다.
- 구형 `watch-cycle` 문서는 legacy/수동 진단용으로 분리한다.

### 8.4 8787 단일 포트 운영

Funnel은 항상 `127.0.0.1:8787`로 프록시한다. editor server도 이 포트를 사용한다.

권장:

- 한 번에 하나의 제작 흐름만 운영한다.
- `outputs/automation/locks/editor_build.lock`을 유지한다.
- 포트 충돌 시 discord worker 로그에서 기존 editor server 종료/재기동 기록을 확인한다.

### 8.5 API 키 노출 이력

진단 중 `.env` 조회 명령으로 API 키 값이 터미널 출력에 노출된 이력이 있다. 이 세션 로그가 외부에 공유될 가능성이 있으면 Gemini/OpenAI 키를 회전하는 것이 안전하다.

## 9. 운영 점검 명령

서비스 상태:

```bash
launchctl print gui/501/com.kbo.title-news-automation.watcher
launchctl print gui/501/com.kbo.title-news-automation.discord-worker
launchctl print gui/501/com.kbo.tailscale-userspace
launchctl print gui/501/com.wonjaechoi.bb-future
```

실제 plist:

```bash
plutil -p /Users/wonjaechoi/Library/LaunchAgents/com.kbo.title-news-automation.watcher.plist
plutil -p /Users/wonjaechoi/Library/LaunchAgents/com.kbo.title-news-automation.discord-worker.plist
plutil -p /Users/wonjaechoi/Library/LaunchAgents/com.kbo.tailscale-userspace.plist
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
/usr/local/opt/tailscale/bin/tailscale \
  --socket=/Users/wonjaechoi/.local/share/kbo-tailscale/tailscaled.sock \
  funnel status
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
find outputs/automation/fresh_watch_runs -maxdepth 2 -name fresh_window_decision_report.json | sort | tail
```

Gemini health check:

```bash
PYTHONPATH=src python3.11 - <<'PY'
from kbo_card_news.config.env import load_default_env
from kbo_card_news.runtime.model_fallback import call_model
from kbo_card_news.scoring.engine import UrllibHttpTransport
import os

load_default_env()
schema = {"type": "object", "properties": {"ok": {"type": "string"}}, "required": ["ok"]}

for model in ["gemini-2.5-flash-lite", "gemini-2.5-flash", "gemini-3.1-flash-lite-preview"]:
    try:
        text = call_model(
            model,
            'Return JSON {"ok":"yes"} only.',
            schema_name="health_check",
            json_schema=schema,
            transport=UrllibHttpTransport(),
            gemini_api_key=os.getenv("GEMINI_API_KEY"),
        )
        print(model, "OK", text[:120])
    except Exception as exc:
        print(model, type(exc).__name__, str(exc).splitlines()[0][:400])
PY
```

## 10. 결론

현재 서버의 백그라운드 서비스 자체는 동작 중이다.

- watcher는 `watch-fresh-cycle --notify --json`으로 10분마다 실행된다.
- launchd 기준 watcher의 마지막 종료 코드는 0이다.
- Discord worker는 PID `51725`로 실행 중이다.
- Tailscale userspace daemon은 PID `35763`으로 실행 중이고 Funnel도 live다.
- 상태 DB에는 오래 멈춘 `pipeline_running` job이 없다.
- 디스크 여유 공간도 충분하다.

다만 새 기사 decision 단계는 Gemini 모델 가용성에 영향을 받고 있다. 현재 운영 개선 우선순위는 `gemini-3.1-flash-lite-preview` 503 대응, 오래된 `notified` job 정리, 예시 plist와 실제 설치값의 drift 해소다.
