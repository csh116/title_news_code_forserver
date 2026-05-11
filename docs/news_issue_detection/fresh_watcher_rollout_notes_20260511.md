# Fresh Watcher Rollout Notes - 2026-05-11

이 문서는 `docs/news_issue_detection` 설계안을 코드에 반영하고 서버에서 1차 smoke test한 내용을 요약한 기록이다.

## 반영한 코드 변경

### 1. Gemini 후보 선정 기본 off

기존 `watch-cycle`은 `.env`에 `GEMINI_API_KEY`가 있으면 후보 선정 단계에서 Gemini를 자동 사용했다.

수정 후:

```text
candidates --selection-engine heuristic|gemini
watch-once --selection-engine heuristic|gemini
watch-cycle --selection-engine heuristic|gemini
```

기본값은 모두 `heuristic`이다.

즉, 서버 `.env`에 `GEMINI_API_KEY`가 있어도 기존 watcher는 명시적으로 `--selection-engine gemini`를 주지 않는 한 Gemini quota를 쓰지 않는다.

수정 파일:

```text
tests/manual_checks/manual_check_batch_topic_selection.py
src/kbo_card_news/automation/pipeline_runner.py
src/kbo_card_news/automation/news_watcher.py
src/kbo_card_news/automation/cli.py
```

### 2. fresh watcher 추가

신규 CLI:

```bash
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python3.11 -m kbo_card_news.automation.cli watch-fresh-cycle
```

주요 옵션:

```text
--source-db-path
--collection-window-minutes
--context-window-hours
--duplicate-lookback-hours
--min-issue-score
--max-jobs
--gemini-review / --no-gemini-review
--notify
--dry-run-notify
--json
```

신규 파일:

```text
src/kbo_card_news/automation/fresh_issue_detector.py
src/kbo_card_news/automation/issue_keywords.py
src/kbo_card_news/automation/job_deduplication.py
src/kbo_card_news/automation/news_collection.py
```

fresh watcher 흐름:

```text
최근 10분 기사 수집
-> 새로 insert된 기사만 fresh article로 사용
-> 최근 24시간 source DB context 조회
-> 팀/키워드/상태 변화 기반 issue_score 계산
-> threshold 이상이면 automation job 생성
-> 기존 topic_selection_choice.json 호환 파일 생성
-> notify 옵션이면 Discord 알림 또는 dry-run payload 생성
```

승인 이후 editor build 호환성을 위해 fresh watcher도 아래 파일을 만든다.

```text
outputs/automation/fresh_watch_runs/YYYYMMDD_HHMMSS/topic_selection_choice.json
outputs/automation/fresh_watch_runs/YYYYMMDD_HHMMSS/fresh_watch_report.json
```

job metadata에는 `choice_json_path`를 넣어서 기존 `build-approved-editor`가 그대로 쓸 수 있게 했다.

### 3. source DB 시간 범위 조회

`SQLiteSourceItemRepository`에 추가:

```python
list_items_published_between(window_start, window_end, limit=500)
```

조회 기준:

```text
COALESCE(published_at, collected_at)
```

timezone offset 문자열 비교 문제를 피하기 위해 SQLite `julianday()` 기준으로 비교한다.

수정 파일:

```text
src/kbo_card_news/pipeline/storage.py
```

### 4. Discord fresh issue 메시지

`job.metadata["source"] == "watch_fresh_once"`이면 fresh issue용 메시지를 사용한다.

표시 내용:

```text
[강한 이슈] 또는 [확인 후보]
점수
Gemini 판단
신규 기사 수
24시간 관련 기사 수
매체 수
키워드
세부 근거
리스크
기사 URL
```

수정 파일:

```text
src/kbo_card_news/automation/discord_bot.py
```

### 5. fresh watcher 새벽 휴지/catch-up

`watch-fresh-cycle`은 기본적으로 KST 기준 00:00 이상 07:00 미만에는 실행을 건너뛴다.

07시대 첫 실행은 일반 10분 window 대신 00:00-07:00 발행 기사를 한 번에 수집/선정한다. 성공 후 아래 마커를 남겨 같은 날 07시대 후속 실행은 원래 10분 watcher처럼 동작한다.

```text
outputs/automation/fresh_watch_runs/morning_catchup_YYYYMMDD.done
```

추가 옵션:

```text
--quiet-start-hour
--quiet-end-hour
```

수정 파일:

```text
src/kbo_card_news/automation/cli.py
src/kbo_card_news/automation/fresh_issue_detector.py
```

## 로컬 테스트

작업용 노트북에서 검증:

```bash
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python3 -m compileall -q src tests/automation tests/manual_checks/manual_check_batch_topic_selection.py
```

통과.

기본 환경에 `pytest`가 없어 `/private/tmp/kbo_pytest_deps`에 임시 설치 후 실행:

```bash
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=/private/tmp/kbo_pytest_deps:src python3 -m pytest tests/automation -q
```

결과:

```text
14 passed, 1 warning
```

추가 테스트 파일:

```text
tests/automation/test_source_item_repository_time_queries.py
tests/automation/test_fresh_issue_detector.py
tests/automation/test_discord_fresh_issue_message.py
tests/automation/test_fresh_cycle_schedule.py
```

추가 확인:

```bash
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python3 -m unittest discover -s tests/automation
```

결과:

```text
10 tests OK
```

## 서버 smoke test 기록

서버 경로:

```text
/Users/wonjaechoi/kbo/title_news_code
```

확인한 명령:

```bash
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python3.11 -m kbo_card_news.automation.cli watch-cycle --help
```

`--selection-engine {heuristic,gemini}` 표시 확인.

fresh watcher smoke test:

```bash
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python3.11 -m kbo_card_news.automation.cli watch-fresh-cycle \
  --no-gemini-review \
  --json
```

초기에는 SSL 오류 발생:

```text
SSL: CERTIFICATE_VERIFY_FAILED
unable to get local issuer certificate
self-signed certificate in certificate chain
```

원인:

```text
/usr/local/etc/openssl@3/cert.pem 없음
python3.11 _ssl이 /usr/local/opt/openssl@3/lib/libssl.3.dylib을 못 찾던 상태
```

서버에서 복구 후 확인:

```bash
ls -l /usr/local/opt/openssl@3/lib/libssl.3.dylib
ls -l /usr/local/opt/openssl@3/lib/libcrypto.3.dylib
python3.11 -c "import ssl; print(ssl.OPENSSL_VERSION)"
```

정상 출력:

```text
OpenSSL 3.6.2 7 Apr 2026
```

certifi 설치 및 OpenSSL CA 경로 연결:

```bash
python3.11 -m pip install --upgrade certifi
ln -sf "$(python3.11 -c "import certifi; print(certifi.where())")" /usr/local/etc/openssl@3/cert.pem
```

확인:

```bash
ls -l /usr/local/etc/openssl@3/cert.pem
python3.11 -c "import ssl; print(ssl.get_default_verify_paths())"
```

정상 상태:

```text
/usr/local/etc/openssl@3/cert.pem -> /usr/local/lib/python3.11/site-packages/certifi/cacert.pem
DefaultVerifyPaths(cafile='/usr/local/lib/python3.11/site-packages/certifi/cacert.pem', ...)
```

재실행 결과:

```json
{
  "collected_count": 0,
  "inserted_count": 0,
  "fresh_article_count": 0,
  "context_article_count": 71,
  "candidate_count": 0,
  "created_count": 0,
  "collector_errors": []
}
```

해석:

```text
fresh watcher 실행 정상
SSL/수집기 오류 없음
해당 10분 window에 새 기사 없음
context DB 조회 정상
job 생성 없음은 정상
```

Discord dry-run:

```bash
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python3.11 -m kbo_card_news.automation.cli watch-fresh-cycle \
  --no-gemini-review \
  --notify \
  --dry-run-notify \
  --json
```

결과:

```text
collector_errors=[]
created_count=0
notification_payloads=[]
```

해당 window에 새 이슈가 없어 payload가 없는 정상 상태.

## launchd 전환 방침

기존 Discord worker는 건드리지 않는다.

교체 대상:

```text
~/Library/LaunchAgents/com.kbo.title-news-automation.watcher.plist
```

새 명령:

```bash
cd /Users/wonjaechoi/kbo/title_news_code
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python3.11 -m kbo_card_news.automation.cli watch-fresh-cycle --no-gemini-review --notify --min-issue-score 70 --max-jobs 3 --json
```

권장 interval:

```text
StartInterval = 300
```

초기 운영값:

```text
--no-gemini-review
--min-issue-score 70
--max-jobs 3
00:00-07:00 자동 skip
07시대 첫 실행 00:00-07:00 catch-up
```

새 기사 감지 품질을 확인한 뒤 Gemini 2차 판단을 켤 경우 `--no-gemini-review`를 제거한다.

## 서버 Codex 작업 지시문

서버 Codex에게 줄 수 있는 지시:

```text
현재 repo는 /Users/wonjaechoi/kbo/title_news_code 입니다.

목표:
기존 launchd watcher를 기존 30분 watch-cycle에서 5분 fresh watcher로 교체해주세요.
Discord worker는 건드리지 마세요.
수정 전 watcher plist 백업을 남기고, 적용 후 launchctl 상태와 로그까지 확인해주세요.

대상 plist:
~/Library/LaunchAgents/com.kbo.title-news-automation.watcher.plist

새 watcher 명령:
cd /Users/wonjaechoi/kbo/title_news_code
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python3.11 -m kbo_card_news.automation.cli watch-fresh-cycle --no-gemini-review --notify --min-issue-score 70 --max-jobs 3 --json

StartInterval은 300초로 바꿔주세요.
이 명령은 코드 기본값으로 00:00-07:00에는 skipped_quiet_hours를 출력하고 종료합니다.
07시대 첫 실행은 00:00-07:00 발행 기사 catch-up으로 한 번 실행됩니다.

주의:
- .env, outputs DB, Discord worker plist는 수정하지 마세요.
- com.kbo.title-news-automation.discord-worker는 unload/load 하지 마세요.
- watcher plist만 백업 후 수정하세요.
- 기존 StandardOutPath/StandardErrorPath, WorkingDirectory, EnvironmentVariables가 있으면 유지하세요.
- SSL 인증서는 이미 /usr/local/etc/openssl@3/cert.pem이 certifi로 연결되어 있으니 건드리지 마세요.

작업 후 검증:
1. plist ProgramArguments가 watch-fresh-cycle로 바뀌었는지 확인
2. StartInterval이 300인지 확인
3. watcher만 launchctl unload/load 또는 bootstrap/bootout으로 재적용
4. launchctl kickstart -k gui/$(id -u)/com.kbo.title-news-automation.watcher 로 즉시 1회 실행
5. 아래 로그 확인
   tail -n 120 /Users/wonjaechoi/kbo/title_news_code/outputs/automation/logs/watcher.launchd.out.log
   tail -n 120 /Users/wonjaechoi/kbo/title_news_code/outputs/automation/logs/watcher.launchd.err.log
6. collector_errors가 없거나, 실행 결과가 정상 JSON인지 확인
7. 최종 응답에 백업 파일 경로, 적용된 ProgramArguments, launchctl 상태, 로그 요약을 알려주세요.
```

## 현재 남은 확인 사항

아직 새 기사 발생 window에서 `created_count > 0` 케이스는 확인하지 못했다.

따라서 launchd 전환은 가능하지만, 첫 운영 중 확인해야 할 것:

```text
fresh 기사 발생 시 candidate_count가 올라가는지
created_count가 과도하게 많지 않은지
Discord 메시지 내용이 괜찮은지
중복 job이 과하게 생기지 않는지
```

문제 발생 시 즉시 이전 watcher plist 백업으로 되돌리면 된다.
