# 구형 맥북 서버 핸드오프 2026-05-09

이 문서는 다음 세션에서 바로 이어서 작업하기 위한 현재 상태 요약이다.

## 현재 결론

구형 맥북에서 MVP 전체 흐름을 1회 성공했다.

```text
프로젝트 복사
-> Python 3.11 세팅
-> .env 작성
-> automation DB 초기화
-> 실제 뉴스 수집
-> 후보 job 생성
-> Discord 알림 발송
-> approved 처리
-> editor build
-> Cloudflare quick tunnel 접속
-> editor에서 PNG 저장
-> job 상태 render_ready 기록
```

Instagram 자동 업로드와 launchd 상시 실행은 아직 진행하지 않았다.

## 구형 맥북 경로

```text
/Users/wonjaechoi/kbo/title_news_code
```

구형 맥북 계정:

```text
wonjaechoi
```

Python:

```text
python3.11
```

기본 실행 prefix:

```bash
cd ~/kbo/title_news_code
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python3.11 -m kbo_card_news.automation.cli <command>
```

## 설치/환경에서 확인된 것

### Python

기본 `python3`는 3.9.6이라 사용하지 않는다.

```text
Python 3.9.6 = 사용하지 않음
Python 3.11 = 사용
```

Homebrew로 `python@3.11` 설치했다.

### pip

초기에는 `pip`가 없어서 `ensurepip`와 site-packages 경로 정리 후 사용 가능해졌다.

### 필요한 Python 패키지

구형 맥북에서 실제로 필요했던 패키지:

```text
discord.py
Pillow
```

확인/설치 예시:

```bash
python3.11 -m pip install -r requirements-automation.txt
```

`pytest`는 테스트 실행용이다.

```bash
python3.11 -m pip install pytest
```

### 브라우저 렌더

PNG 렌더는 Playwright가 아니라 `src/kbo_card_news/imaging/browser_export.py`의 Chrome headless 실행을 사용한다.

필수 브라우저 후보:

```text
/Applications/Google Chrome.app
/Applications/Chromium.app
/Applications/Microsoft Edge.app
/Applications/Brave Browser.app
```

구형 맥북에는 Google Chrome 앱 설치가 필요했다.

```bash
brew install --cask google-chrome
```

확인:

```bash
ls -d "/Applications/Google Chrome.app"
```

### Cloudflare

구형 맥북에서 `cloudflared` 설치 및 quick tunnel 성공.

```bash
cloudflared --version
cloudflared tunnel --url http://localhost:8787
```

quick tunnel URL은 실행할 때마다 바뀐다.

## 패키지 누락 수정

처음 만든 이전 패키지에 `design.py`가 빠져 있었다.

`tests/manual_checks/manual_check_title_html_editor_no_multimodal.py`가 루트의 `design.py`를 import하므로 필수 이전 대상에 포함해야 한다.

현재 문서에는 반영 완료:

```text
docs/macbook_transfer_manifest.md
docs/macbook_server_deployment_roadmap.md
```

현재 로컬 이전 패키지에도 `design.py`를 추가했다.

```text
macbook_transfer_package_20260508/design.py
```

## 실제 검증에 사용한 job

```text
job_id: kbo-news-20260509_030725-1
topic: 김재환, 친정팀 두산 상대 첫 방문 경기서 활약
```

최종 상태:

```text
status: render_ready
```

렌더 결과:

```text
/Users/wonjaechoi/kbo/title_news_code/outputs/approval_run_20260509_030724/title_render_pngs/01_260508_김재환멀티히트_01.png
/Users/wonjaechoi/kbo/title_news_code/outputs/approval_run_20260509_030724/title_render_pngs/title_render_social_copy.md
```

확인 명령:

```bash
cd ~/kbo/title_news_code
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python3.11 -m kbo_card_news.automation.cli show kbo-news-20260509_030725-1
```

## 성공한 명령 흐름

### 기본 확인

```bash
cd ~/kbo/title_news_code
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python3.11 -m kbo_card_news.automation.cli --help
PYTHONDONTWRITEBYTECODE=1 python3.11 -m compileall -q src/kbo_card_news/automation
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python3.11 -m kbo_card_news.automation.cli init
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python3.11 -m kbo_card_news.automation.cli health
```

### Discord dry-run 및 실제 발송

```bash
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python3.11 -m kbo_card_news.automation.cli notify-pending --dry-run
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python3.11 -m kbo_card_news.automation.cli notify-pending
```

초기 실제 발송에서 SSL 인증서 오류가 있었다.
Homebrew `ca-certificates` 설치와 인증서 경로 설정 후 해결했다.

### 실제 watcher 1개 후보 생성

```bash
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python3.11 -m kbo_card_news.automation.cli watch-cycle \
  --candidate-count 1 \
  --max-candidates 1 \
  --json
```

성공 결과:

```text
created_count=1
topic=김재환, 친정팀 두산 상대 첫 방문 경기서 활약
job_id=kbo-news-20260509_030725-1
```

### 승인 및 editor build

```bash
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python3.11 -m kbo_card_news.automation.cli approve kbo-news-20260509_030725-1

PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python3.11 -m kbo_card_news.automation.cli build-approved-editor kbo-news-20260509_030725-1 \
  --host 127.0.0.1 \
  --port 8787
```

성공 후:

```text
status=editor_ready
editor_url=http://127.0.0.1:8787/topic/1?token=...
```

### editor server + Cloudflare

터미널 1:

```bash
cd ~/kbo/title_news_code
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python3.11 -m kbo_card_news.automation.cli serve-job-editor kbo-news-20260509_030725-1 \
  --host 127.0.0.1 \
  --port 8787 \
  --notify-render
```

터미널 2:

```bash
cloudflared tunnel --url http://localhost:8787
```

Cloudflare quick tunnel URL 뒤에 `/topic/1?token=<editor_token>`을 붙여 접속했다.

### PNG 저장 확인

editor에서 `PNG 저장`을 눌렀고 서버 로그에 아래가 찍혔다.

```text
POST /render?... HTTP/1.1" 200
```

job은 `render_ready`가 됐다.

## 알려진 문제

### Discord 버튼 interaction 응답

버튼 클릭 시 DB 처리는 된 것으로 보이나 Discord 쪽에 아래 오류가 발생했다.

```text
discord.errors.NotFound: 404 Not Found (error code: 10062): Unknown interaction
```

원인 추정:

```text
interaction 3초 응답 제한을 넘긴 뒤 send_message를 호출함
```

수정 방향:

```text
on_interaction
-> interaction.response.defer(ephemeral=True)
-> DB 처리/build 처리
-> interaction.followup.send(...)
```

현재 코드 패치 완료. 구형 맥북에서 실제 Discord 버튼 클릭으로 재검증이 필요하다.

### failure_message 잔존

초기 실패 때문에 job의 `failure_message`에 아래 값이 남아 있었다.

```text
No module named 'design'
```

이후 `design.py`를 추가해 build/render는 성공했지만 `failure_message`는 DB에 남아 있다.
운영상 치명적이지는 않지만, 성공 상태 전환 시 `failure_message`를 clear하는 개선이 있으면 좋다.

현재 코드 패치 및 테스트 추가 완료. `approved`, `pipeline_running`, `editor_ready`, `render_ready`, `publish_approved`, `published` 전환 시 기존 `failure_message`를 비운다.

### 폰 editor UX

폰에서 editor는 열리지만 조작성이 완벽하지 않을 수 있다.
MVP 검증에는 문제 없었다.

## 다음 세션 권장 시작점

다음 세션에서는 아래 순서로 진행한다.

1. Discord 버튼 실제 클릭으로 defer/followup 패치 검증
2. 구형 맥북에서 `failure_message` clear 동작 확인
3. `requirements-automation.txt`로 의존성 재설치 확인
4. watcher launchd plist load/unload 검증
5. discord-button-worker launchd plist load/unload 검증
6. 재부팅 후 watcher/worker 자동 기동 검증

## 2026-05-09 추가 확인

launchd worker와 watcher가 알림 후 자동 시작한 worker가 동시에 떠서 Discord 버튼 interaction을 중복 처리하는 문제가 있었다.

수정 기준:

```text
watcher launchd plist에는 --no-start-button-worker를 넣는다.
discord-button-worker는 직접 실행될 때도 outputs/automation/discord_button_worker.pid를 기록한다.
```

구형 맥북은 Homebrew 인증서 경로가 아래였다.

```text
/usr/local/etc/ca-certificates/cert.pem
```

launchd plist의 `EnvironmentVariables`에 `SSL_CERT_FILE`, `REQUESTS_CA_BUNDLE`로 위 경로를 넣는다.

watcher launchd가 manual check의 입력 프롬프트에서 멈추는 문제도 확인했다.
수정 기준:

```text
tests/manual_checks/manual_check_batch_topic_selection.py
  --non-interactive
  --window-start-kst
  --window-end-kst
  --candidate-count

src/kbo_card_news/automation/pipeline_runner.py
  generate_topic_candidates()가 stdin 대신 위 인자로 manual check를 실행한다.
  window가 명시되지 않으면 실행 시점 기준 최근 24시간 KST window를 자동 계산한다.
```

따라서 launchd watcher는 고정 window 값을 plist에 넣지 않아도 된다.

다음 세션에서 먼저 열 문서:

```text
docs/macbook_server_handoff_20260509.md
docs/macbook_server_deployment_roadmap.md
docs/automation_operations.md
docs/macbook_transfer_manifest.md
```
