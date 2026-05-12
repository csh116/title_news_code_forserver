# Current Server Operations Diagnosis - 2026-05-12

이 문서는 2026-05-12 23:47 KST 기준으로 `/Users/wonjaechoi/kbo/title_news_code`에서 실제 백그라운드로 돌고 있는 작업을 다시 확인한 운영 진단이다.

이전 문서의 구형 `watch-cycle`, 30분 주기, `--no-gemini-review` 기준 설명은 현재 설치 상태와 다르다. 현재 운영 기준은 `watch-fresh-cycle --notify --json`, 10분 주기다.

## 1. 현재 결론

현재 백그라운드 운영 자체는 살아 있다.

- `watch-fresh-cycle` watcher는 launchd에 로드되어 있고 `StartInterval=600`으로 10분마다 실행된다.
- watcher는 장기 상주 프로세스가 아니라 배치 실행 후 종료되는 구조라 `state = not running`이 정상이다.
- 확인 시점 기준 watcher는 `runs = 39`, `last exit code = 0`이다.
- 재시작 후 최신 run `20260512_234300`은 정상 종료됐다. 다만 그 회차는 새 target article이 0건이라 모델 호출 없이 `skipped_empty_target`으로 끝났다.
- Discord button worker는 PID `51725`로 실행 중이다.
- Tailscale userspace daemon은 PID `35763`으로 실행 중이고 Funnel도 live다.
- 상태 DB 기준 오래 멈춘 `pipeline_running` job은 없다.
- 디스크 여유 공간은 약 `76.25 GB`로 충분하다.

현재 주의할 점은 두 가지다.

1. Gemini/GPT fallback 및 validator 보정 코드는 반영됐고 수동 호출 테스트는 통과했지만, 재시작 후 실제 새 target article이 있는 live cycle은 아직 관측되지 않았다.
2. 오래된 승인 대기 job이 많이 쌓여 있다. `notified=42`, stale pending approval 42건이다.

## 2. 백그라운드 서비스 현황

### 2.1 Fresh watcher

LaunchAgent:

```text
/Users/wonjaechoi/Library/LaunchAgents/com.kbo.title-news-automation.watcher.plist
```

실제 실행 명령:

```bash
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python3.11 -m kbo_card_news.automation.cli \
  watch-fresh-cycle \
  --notify \
  --json
```

launchd 확인 결과:

```text
state = not running
runs = 39
last exit code = 0
run interval = 600 seconds
working directory = /Users/wonjaechoi/kbo/title_news_code
stdout = outputs/automation/logs/watcher.launchd.out.log
stderr = outputs/automation/logs/watcher.launchd.err.log
```

진단:

- `not running`은 장애가 아니다. 10분마다 한 번 실행되고 끝나는 배치 작업이기 때문이다.
- `last exit code = 0`이라 마지막 launchd 실행은 정상 종료로 기록됐다.
- 최신 stdout 로그 timestamp는 `May 12 23:44:12 2026`이다.
- stderr 로그 timestamp는 `May 9 19:46:33 2026`으로, 최근 watcher stderr 갱신은 없다.

현재 모델 fallback 기준:

```text
gemini-3.1-flash-lite-preview
-> gemini-2.5-flash-lite
-> gpt-4o-mini
```

관련 코드:

- `src/kbo_card_news/automation/cli.py`
- `src/kbo_card_news/automation/fresh_window_decision.py`
- `src/kbo_card_news/runtime/model_fallback.py`

### 2.2 Discord button worker

LaunchAgent:

```text
/Users/wonjaechoi/Library/LaunchAgents/com.kbo.title-news-automation.discord-worker.plist
```

실제 실행 명령:

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

진단:

- worker는 현재 살아 있다.
- Discord 후보 메시지의 `제작` 버튼을 받아 editor build, editor server 기동, render 기록, Discord 알림을 처리한다.
- 최신 stdout 로그 timestamp는 `May 12 17:55:40 2026`이다.
- 최신 stderr 로그 timestamp는 `May 12 22:01:38 2026`이다.
- editor build lock은 `outputs/automation/locks/editor_build.lock`을 사용한다.

관련 코드:

- `src/kbo_card_news/automation/cli.py`
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

Funnel 상태:

```text
https://kbo-editor-macbook.tailfb7825.ts.net (Funnel on)
|-- / proxy http://127.0.0.1:8787
```

진단:

- Tailscale daemon과 Funnel은 정상으로 보인다.
- Funnel이 live여도 `127.0.0.1:8787`에 editor server가 떠 있지 않으면 접속은 실패할 수 있다. 이 경우는 Funnel 장애가 아니라 backend 대상 프로세스 부재다.

### 2.4 BB_FUTURE 별도 프로세스

현재 같은 Mac에서 별도 Python 프로세스도 실행 중이다.

```text
PID 38489
/usr/local/Cellar/python@3.14/3.14.4_1/.../Python /Users/wonjaechoi/SSH/BB_FUTURE.py
```

이 프로세스는 이 저장소의 KBO 자동화 경로와 직접 연결되지는 않지만, 같은 장비의 CPU/RAM을 공유한다.

## 3. 최신 fresh watcher 실행 결과

최신 fresh run:

```text
outputs/automation/fresh_watch_runs/20260512_234300
```

해당 report 요약:

```json
{
  "collection_window_start": "2026-05-12T23:33:00+09:00",
  "collection_window_end": "2026-05-12T23:43:00+09:00",
  "collected_count": 0,
  "inserted_count": 0,
  "target_article_count": 0,
  "context_article_count": 64,
  "decision_count": 0,
  "publish_count": 0,
  "created_count": 0,
  "model_name": "gemini-3.1-flash-lite-preview",
  "model_call_status": "skipped_empty_target",
  "model_error": ""
}
```

진단:

- 재시작 후 코드가 실행된 것은 확인됐다.
- 이 회차는 새로 수집된 target article이 없어서 Gemini/GPT를 호출하지 않았다.
- 따라서 “백그라운드 스케줄/프로세스는 정상”이라고 볼 수 있지만, “실제 새 기사에 대해 모델 fallback이 live cycle에서 성공했다”까지는 아직 말할 수 없다.

최근 run 디렉토리는 계속 생성되고 있다.

```text
20260512_234300
20260512_233900
20260512_232800
20260512_231800
20260512_230200
20260512_225800
20260512_224700
20260512_223600
```

## 4. 모델/validator 관련 현재 상태

이전에 새 기사 cycle에서 실패한 원인은 크게 두 갈래였다.

- `gemini-3.1-flash-lite-preview`가 503 high demand로 실패할 수 있음.
- fallback 모델이 응답하더라도 fresh decision validator가 ID 필드 배치를 너무 엄격하게 해석해 실패할 수 있음.
- OpenAI fallback은 strict JSON schema 요구사항에 맞지 않아 실패할 수 있었음.

현재 코드 기준 반영된 대응:

- Gemini fallback에 `gemini-2.5-flash-lite`가 포함되어 있다.
- 최종 OpenAI fallback 모델은 `gpt-4o-mini`다.
- OpenAI strict schema용으로 `additionalProperties=false`, `required` 보정이 적용된다.
- fresh decision validator는 context ID가 `target_article_ids`에 섞인 경우 related 쪽으로 옮기고, target ID가 related/representative에 들어간 경우 target 쪽으로 회수한다.
- unknown/invented ID는 계속 실패시킨다. 이건 실제 존재하지 않는 기사 ID를 막기 위한 기준이다.
- `publish`인데 보정 후 target article ID가 하나도 남지 않으면 전체 실패 대신 해당 decision을 `reject`로 낮춘다.

수동 테스트 결과:

```text
gemini-2.5-flash-lite fresh prompt call + validation: OK
gpt-4o-mini fresh schema call: OK
unit tests: tests.automation.test_model_fallback + tests.automation.test_fresh_window_decision OK
```

남은 확인:

- 새 target article이 있는 실제 10분 watcher 회차에서 `publish/hold/reject` decision까지 정상 기록되는지 확인해야 한다.

## 5. 상태 DB 진단

확인 명령:

```bash
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python3.11 -m kbo_card_news.automation.cli health --json
```

확인 결과:

```text
total_jobs = 64
approved = 1
editor_ready = 13
failed = 1
notified = 42
render_ready = 5
skipped = 2
stale_pipeline_running = []
stale_pending_approval = 42
disk_free_gb = 76.25
disk_ok = true
```

진단:

- 오래 멈춘 `pipeline_running`은 없다.
- `render_ready=5`라 승인 후 editor/render 흐름은 실제 성공 이력이 있다.
- `notified=42`와 stale pending approval 42건은 운영상 정리 대상이다.
- `failed=1`은 별도 failure message 확인 대상이다.
- 디스크는 정상이다.

정리 권장 명령:

```bash
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python3.11 -m kbo_card_news.automation.cli expire-pending --stale-hours 12
```

## 6. 현재 운영 리스크

### 6.1 live target 기사 회차 미관측

재시작 후 watcher 실행은 확인됐지만 최신 회차가 `skipped_empty_target`이므로 모델 fallback이 실제 새 기사 회차에서 작동한 증거는 아직 없다.

확인 기준:

- 최신 report에서 `target_article_count > 0`
- `model_call_status = success`
- `model_error = ""`
- `decision_count`가 0 이상으로 정상 기록
- 필요한 경우 `created_jobs` 생성

### 6.2 Gemini 3.1 preview 가용성

첫 모델 `gemini-3.1-flash-lite-preview`는 high demand로 503을 낼 수 있다. 현재는 `gemini-2.5-flash-lite`, `gpt-4o-mini` fallback이 있으므로 이전보다 운영 리스크는 낮아졌다.

### 6.3 오래된 pending 누적

승인 대기 job이 많이 쌓이면 Discord/운영 판단과 중복 관리가 지저분해질 수 있다. `expire-pending --stale-hours 12`로 정리하는 것이 좋다.

### 6.4 8787 단일 포트

Funnel은 항상 `127.0.0.1:8787`로 프록시한다. editor server도 이 포트를 사용하므로 한 번에 하나의 제작 흐름을 유지하는 것이 안전하다.

### 6.5 BB_FUTURE 리소스 공유

`BB_FUTURE.py`가 별도로 CPU/RAM을 쓰고 있다. KBO 자동화 장애는 아니지만 모델 호출/렌더링 시 장비 부하가 높아지면 간접 영향은 가능하다.

## 7. 운영 점검 명령

launchd 상태:

```bash
launchctl print gui/501/com.kbo.title-news-automation.watcher
launchctl print gui/501/com.kbo.title-news-automation.discord-worker
launchctl print gui/501/com.kbo.tailscale-userspace
```

프로세스:

```bash
ps aux | grep -E 'discord-button-worker|tailscaled|watch-fresh-cycle|BB_FUTURE|kbo_card_news' | grep -v grep
```

자동화 health:

```bash
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python3.11 -m kbo_card_news.automation.cli health --json
```

최근 fresh run:

```bash
ls -lt outputs/automation/fresh_watch_runs | head
cat outputs/automation/fresh_watch_runs/<latest>/fresh_window_decision_report.json
```

로그 timestamp:

```bash
stat -f '%Sm %N' \
  outputs/automation/logs/watcher.launchd.out.log \
  outputs/automation/logs/watcher.launchd.err.log \
  outputs/automation/logs/discord-worker.launchd.out.log \
  outputs/automation/logs/discord-worker.launchd.err.log \
  outputs/automation/logs/tailscale-userspace.err.log
```

Tailscale Funnel:

```bash
/usr/local/opt/tailscale/bin/tailscale \
  --socket=/Users/wonjaechoi/.local/share/kbo-tailscale/tailscaled.sock \
  funnel status
```

## 8. 다음 확인 포인트

다음 새 기사 유입 회차에서 아래를 확인하면 된다.

```text
target_article_count > 0
model_call_status = success
model_error = ""
created_jobs / decisions 내용 정상
watcher last exit code = 0
```

현재 기준으로는 백그라운드 스케줄러, Discord worker, Tailscale Funnel은 모두 운영 가능한 상태다. 남은 검증은 “새 target article이 들어온 실제 fresh cycle에서 모델 decision이 성공하는지”다.
