# 뉴스 자동화 운영 메모

이 문서는 구형 맥북에서 자동화 watcher를 주기 실행할 때 필요한 최소 운영 절차를 정리한다.

## 주요 경로

- 상태 DB: `outputs/automation/automation_state.db`
- 중복 실행 lock: `outputs/automation/locks/watcher.lock`
- 실패 로그: `outputs/automation/logs/`

## 현재 운영 결정

- Discord는 webhook이 아니라 bot으로 운영한다.
- `.env`에는 `DISCORD_BOT_TOKEN`, `DISCORD_CHANNEL_ID`, `DISCORD_USERNAME`을 둔다.
- 현재 코드의 Discord 전송부는 bot REST API 기준이다. Discord 명령 처리는 아직 남아 있다.
- Instagram 업로드는 Meta Graph API 방향으로 가되, 계정/권한/공개 이미지 URL 생성 방식은 아직 확정 전이다.
- 폰에서 HTML editor를 여는 방식은 Tailscale Funnel을 우선 사용한다.

## 구형 맥북 검증 상태

2026-05-09 기준 구형 맥북에서 MVP e2e를 1회 성공했다.

```text
watch-cycle
-> Discord 알림
-> approve
-> build-approved-editor
-> serve-job-editor --notify-render
-> Tailscale Funnel URL
-> editor 접속
-> PNG 저장
-> render_ready
```

검증 job:

```text
kbo-news-20260509_030725-1
```

구형 맥북 프로젝트 경로:

```text
/Users/wonjaechoi/kbo/title_news_code
```

구형 맥북에서는 `python3`가 3.9.6이므로 운영 명령은 `python3.11`을 사용한다.

```bash
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python3.11 -m kbo_card_news.automation.cli health
```

## 구형 맥북 필수 의존성

구형 맥북에서 실제 검증 중 필요했던 항목:

```text
python@3.11
discord.py
Pillow
Google Chrome.app
tailscale
ca-certificates
```

설치 예시:

```bash
brew install python@3.11 ca-certificates
python3.11 -m pip install -r requirements-automation.txt
python3.11 -m pip install pytest
brew install --cask google-chrome
```

PNG 렌더는 Playwright가 아니라 실제 브라우저 앱을 headless로 실행한다.
아래 중 하나가 `/Applications`에 있어야 한다.

```text
Google Chrome.app
Chromium.app
Microsoft Edge.app
Brave Browser.app
```

## 남은 결정 사항

- Discord 버튼 interaction 응답 지연 수정. 현재 DB 처리는 되지만 3초 제한으로 `Unknown interaction`이 날 수 있다.
- 폰 editor UX 개선. 현재는 모바일에서 열리지만 편집 조작은 불편하다.
- Instagram용 공개 이미지 URL을 어디서 만들지. 후보는 Cloudflare R2, S3, NAS/홈서버 HTTPS, 임시 호스팅이다.
- Instagram access token을 장기 토큰으로 운용할지, 만료 점검 CLI를 추가할지.

## 기본 점검

```bash
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python3.11 -m kbo_card_news.automation.cli health
```

JSON으로 확인하려면:

```bash
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python3.11 -m kbo_card_news.automation.cli health --json
```

점검 항목:

- 상태별 job 수
- 오래 멈춘 `pipeline_running`
- 오래된 승인 대기 job
- 디스크 여유 공간
- lock/log 경로

## Watcher 주기 실행

`watch-cycle`은 `watch-once`에 lock과 실패 로그를 붙인 운영용 명령이다. 이미 실행 중이면 두 번째 실행은 바로 실패한다.

주의: `--notify`는 `.env`의 `DISCORD_BOT_TOKEN`, `DISCORD_CHANNEL_ID`를 사용해 Discord bot으로 전송한다.

운영 기본값:

- `watch-cycle` 기본 후보 생성 수는 10개다.
- DB에 job으로 저장하는 후보도 기본 10개까지만 둔다.
- launchd 기본 실행 주기는 1800초, 30분이다.
- 경기 직후나 뉴스가 많은 시간대에만 수동으로 900초, 15분 주기를 검토한다.

```bash
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python3.11 -m kbo_card_news.automation.cli watch-cycle \
  --candidate-count 10 \
  --max-candidates 10 \
  --notify
```

Discord 전송 없이 payload만 보려면:

```bash
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python3.11 -m kbo_card_news.automation.cli watch-cycle \
  --candidate-count 10 \
  --max-candidates 10 \
  --notify \
  --dry-run-notify
```

## 승인 후 Editor 생성

후보를 Discord에서 확인한 뒤 CLI로 승인한다.

```bash
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python3.11 -m kbo_card_news.automation.cli approve <job_id>
```

같은 Wi-Fi의 폰에서 editor를 열려면 맥북 LAN IP를 확인하고 `--public-host`에 넣는다. 서버는 `0.0.0.0`에 바인딩하지만 Discord에는 LAN IP와 job token이 포함된 URL을 보낸다.

```bash
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python3.11 -m kbo_card_news.automation.cli build-approved-editor <job_id> \
  --host 0.0.0.0 \
  --public-host <맥북_LAN_IP> \
  --port 8787 \
  --notify
```

이미 만들어진 editor를 다시 열 때:

```bash
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python3.11 -m kbo_card_news.automation.cli serve-job-editor <job_id> \
  --host 127.0.0.1 \
  --port 8787 \
  --notify-render
```

Tailscale Funnel:

```bash
tailscale funnel --bg --yes --https=443 localhost:8787
tailscale funnel status
```

주의:

- editor URL에는 `?token=...`이 붙는다.
- token 없는 `/topic`, `/payload`, `/asset`, `/render` 요청은 403으로 막힌다.
- `serve-job-editor`는 `.env`를 로드해야 렌더 후 social copy 생성에서 `OPENAI_API_KEY`를 사용할 수 있다.
- Funnel URL은 `https://<machine>.<tailnet>.ts.net` 형태다. URL 뒤에 `/topic/1?token=<editor_token>`을 붙여 접속한다.
- `.env`에 `TAILSCALE_FUNNEL_BASE_URL=https://<machine>.<tailnet>.ts.net`을 넣으면 worker가 이 값을 우선 사용한다.

## Discord 버튼 자동 제작

Discord 후보 메시지의 `제작` 버튼을 누르면 button worker가 다음 순서로 처리한다.

```text
approved 처리
-> build-approved-editor
-> serve-job-editor
-> Tailscale Funnel URL 생성/확인
-> 공개 editor URL Discord 전송
-> 렌더 완료 시 render_ready 기록 및 Discord 알림
```

운영 기준:

- worker subprocess command에는 Discord bot token을 넣지 않는다.
- token은 `.env` 또는 child process env로 전달한다.
- 자동 build는 lock을 사용해 동시에 하나만 진행한다.
- Tailscale Funnel은 공개 HTTPS URL이므로 editor token을 유지하고, editor server idle timeout 정책을 유지한다.

## 렌더 완료 기록

editor에서 `PNG 저장`을 누르면 `outputs/<approval_run>/title_render_pngs/` 아래에 `.png`와 `.state.json`이 생긴다. 최신 state 파일을 job에 기록하고 Discord로 렌더 완료 알림을 보낸다.

```bash
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python3.11 -m kbo_card_news.automation.cli record-render <job_id> \
  --state-path <path/to/render.state.json> \
  --notify
```

확인할 산출물:

- `render_png_path`
- `social_copy_md_path`
- job 상태가 `render_ready`인지
- Discord 렌더 완료 메시지가 왔는지

## 복구

재시작 후 `pipeline_running`에 오래 남은 작업은 다시 승인 상태로 되돌려 재실행 가능하게 한다. 이미 editor manifest가 있으면 `editor_ready`로 복구된다.

```bash
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python3.11 -m kbo_card_news.automation.cli recover-running --stale-hours 2
```

보수적으로 실패 처리하고 싶으면:

```bash
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python3.11 -m kbo_card_news.automation.cli recover-running --stale-hours 2 --target-status failed
```

## 오래된 승인 대기 만료

```bash
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python3.11 -m kbo_card_news.automation.cli expire-pending --stale-hours 12
```

특정 상태만 대상으로 할 수도 있다.

```bash
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python3.11 -m kbo_card_news.automation.cli expire-pending --status notified --stale-hours 12
```

## Digest

```bash
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python3.11 -m kbo_card_news.automation.cli digest --since-hours 24
```

## launchd

구형 맥북 실제 경로 기준 launchd plist는 아래 두 파일이다.

```text
docs/com.kbo.title-news-automation.watcher.macbook.plist
docs/com.kbo.title-news-automation.discord-worker.macbook.plist
```

설치 전 로그 디렉터리를 만든다.

```bash
mkdir -p /Users/wonjaechoi/kbo/title_news_code/outputs/automation/logs
mkdir -p /Users/wonjaechoi/kbo/title_news_code/outputs/automation/locks
```

설치:

```bash
cp docs/com.kbo.title-news-automation.watcher.macbook.plist ~/Library/LaunchAgents/com.kbo.title-news-automation.watcher.plist
cp docs/com.kbo.title-news-automation.discord-worker.macbook.plist ~/Library/LaunchAgents/com.kbo.title-news-automation.discord-worker.plist
launchctl load ~/Library/LaunchAgents/com.kbo.title-news-automation.watcher.plist
launchctl load ~/Library/LaunchAgents/com.kbo.title-news-automation.discord-worker.plist
```

상태 확인:

```bash
launchctl list | grep com.kbo.title-news-automation
tail -n 100 outputs/automation/logs/watcher.launchd.err.log
tail -n 100 outputs/automation/logs/discord-worker.launchd.err.log
```

수정 후 다시 로드:

```bash
launchctl unload ~/Library/LaunchAgents/com.kbo.title-news-automation.watcher.plist
launchctl unload ~/Library/LaunchAgents/com.kbo.title-news-automation.discord-worker.plist
launchctl load ~/Library/LaunchAgents/com.kbo.title-news-automation.watcher.plist
launchctl load ~/Library/LaunchAgents/com.kbo.title-news-automation.discord-worker.plist
```

watcher plist는 30분마다 watcher cycle을 실행한다. 현재 `watch-cycle`은 가벼운 URL poller가 아니라 기존 후보 생성 스크립트를 실행하므로, 기본 운영에서는 30분보다 짧게 두지 않는다.
watcher와 Discord worker는 launchd에서 분리해 관리하므로, watcher plist에는 `--no-start-button-worker`를 넣는다. 이 옵션이 없으면 watcher가 알림 발송 후 별도 worker를 추가로 띄워 Discord 버튼 interaction을 중복 처리할 수 있다.

아래는 개발 머신용 예시다. 실제 운영은 위의 `.macbook.plist` 파일을 사용한다.

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>com.kbo.title-news-automation.watcher</string>

  <key>WorkingDirectory</key>
  <string>/Users/s.h.choi/Desktop/kbo/title_automation/title news code</string>

  <key>ProgramArguments</key>
  <array>
    <string>/usr/bin/env</string>
    <string>PYTHONDONTWRITEBYTECODE=1</string>
    <string>PYTHONPATH=src</string>
    <string>python</string>
    <string>-m</string>
    <string>kbo_card_news.automation.cli</string>
    <string>watch-cycle</string>
    <string>--candidate-count</string>
    <string>10</string>
    <string>--max-candidates</string>
    <string>10</string>
    <string>--notify</string>
  </array>

  <key>StartInterval</key>
  <integer>1800</integer>

  <key>StandardOutPath</key>
  <string>/Users/s.h.choi/Desktop/kbo/title_automation/title news code/outputs/automation/logs/launchd.out.log</string>

  <key>StandardErrorPath</key>
  <string>/Users/s.h.choi/Desktop/kbo/title_automation/title news code/outputs/automation/logs/launchd.err.log</string>

  <key>RunAtLoad</key>
  <true/>
</dict>
</plist>
```

설치 예시:

```bash
cp docs/com.kbo.title-news-automation.watcher.plist.example ~/Library/LaunchAgents/com.kbo.title-news-automation.watcher.plist
launchctl load ~/Library/LaunchAgents/com.kbo.title-news-automation.watcher.plist
```

실제 plist 파일은 맥북 세팅 단계에서 사용자명, Python 경로, 실행 주기를 확정한 뒤 만든다.
