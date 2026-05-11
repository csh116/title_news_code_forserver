# 구형 맥북 이전 대상 Manifest

이 문서는 구형 맥북으로 뉴스 자동화 운영 환경을 옮길 때 포함/제외할 대상을 확정한다.

기본 원칙:

- 가능하면 git clone으로 코드 전체를 옮긴다.
- 토큰이 들어 있는 `.env`는 그대로 공유하지 않고 구형 맥북에서 새로 작성한다.
- `outputs/automation/`의 상태 DB, lock, log는 새 맥북에서 새로 만든다.
- 과거 approval run은 복구나 참고 목적이 있을 때만 옮긴다.

## 필수 코드

repo clone 기준으로는 전체 repo를 옮긴다. rsync로 최소 이전할 때도 아래는 반드시 포함한다.

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

이유:

- `src/kbo_card_news/automation/`은 watcher, Discord bot, editor serve, render record의 운영 코드다.
- `design.py`는 title editor/manual pipeline이 import하는 루트 디자인 유틸이다.
- 두 manual check 파일은 자동화 runner가 실제 후보 생성과 editor build에 호출한다.
- `tests/automation/test_news_watcher_duplicate.py`는 이전 후 stable duplicate 동작을 빠르게 확인하는 최소 테스트다.
- 운영 문서는 launchd, watcher, editor, render 기록 절차의 기준 문서다.
- `requirements-automation.txt`는 구형 맥북 상시 실행에 필요한 최소 Python 패키지 목록이다.

## 필수 데이터

새 맥북에서 기존 선정 이력과 수집 DB를 이어가려면 아래를 옮긴다.

```text
outputs/completed_topic_registry.json
outputs/source_collection.db
feedback_memory.db
```

현재 개발 머신 기준 존재 확인:

```text
outputs/completed_topic_registry.json
outputs/source_collection.db
feedback_memory.db
```

대략 크기:

```text
outputs/completed_topic_registry.json  48K
outputs/source_collection.db           7.0M
feedback_memory.db                     196K
```

## 선택 이전 대상

과거 결과물을 새 맥북에서 참고하거나, 기존 editor/render 산출물을 열어볼 필요가 있을 때만 옮긴다.

```text
outputs/approval_run_*/
backups/feedback_memory/
```

현재 개발 머신에는 `outputs/approval_run_*` 디렉터리 15개와 `backups/feedback_memory/` 백업 DB가 있다.

## 기본 제외 대상

아래는 운영 중 생성되는 로컬 상태라 새 맥북에서 새로 시작하는 편이 낫다.

```text
outputs/automation/automation_state.db
outputs/automation/logs/
outputs/automation/locks/
outputs/automation/discord_button_worker.pid
__pycache__/
.pytest_cache/
.DS_Store
```

예외:

- 진행 중인 job 상태까지 그대로 이어가야 하면 `outputs/automation/automation_state.db`를 복사한다.
- 이 경우 기존 `outputs/approval_run_*` 중 해당 job이 참조하는 run directory도 같이 복사해야 한다.
- `outputs/automation/locks/`와 `discord_button_worker.pid`는 복사하지 않는다.

## 비밀값

`.env`는 자동 복사 대상에서 제외한다.

구형 맥북에서는 `.env.example`을 기준으로 아래 값만 새로 작성한다.

```text
OPENAI_API_KEY
GEMINI_API_KEY
DISCORD_BOT_TOKEN
DISCORD_CHANNEL_ID
DISCORD_USERNAME
```

Instagram 값은 이번 배포 단계에서 필수가 아니다.

## rsync 예시

같은 네트워크에서 rsync로 옮길 때의 예시다. 실제 사용자명, 호스트명, 대상 경로는 구형 맥북에 맞춘다.

```bash
rsync -av \
  --exclude '.env' \
  --exclude '.DS_Store' \
  --exclude '__pycache__/' \
  --exclude '.pytest_cache/' \
  --exclude 'outputs/automation/' \
  "/Users/s.h.choi/Desktop/kbo/title_automation/title news code/" \
  "<old-mac-user>@<old-mac-host>:/Users/<old-mac-user>/kbo/title-news-code/"
```

깨끗하게 시작하려면 위 명령 그대로 사용한다.

기존 수집/선정 데이터만 추가로 확실히 포함하려면 아래 파일이 대상 경로에 있는지 확인한다.

```bash
ls -lh outputs/completed_topic_registry.json outputs/source_collection.db feedback_memory.db
```

과거 approval run까지 옮겨야 하면 `outputs/approval_run_*/` 제외 규칙을 추가하지 않은 상태로 복사하면 된다.

## 이전 후 최소 확인

구형 맥북에서 `.env` 작성과 Python 의존성 설치 후 아래를 확인한다.

```bash
python3.11 -m pip install -r requirements-automation.txt
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python3.11 -m kbo_card_news.automation.cli --help
PYTHONDONTWRITEBYTECODE=1 python3.11 -m compileall -q src/kbo_card_news/automation
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python3.11 -m pytest -q tests/automation/test_news_watcher_duplicate.py tests/automation/test_job_state_status.py
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python3.11 -m kbo_card_news.automation.cli init
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python3.11 -m kbo_card_news.automation.cli health
```
