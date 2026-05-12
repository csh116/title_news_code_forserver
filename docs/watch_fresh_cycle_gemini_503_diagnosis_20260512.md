# watch-fresh-cycle Gemini 503 Diagnosis - 2026-05-12

이 문서는 2026-05-12 21:11 KST 기준 Mac 서버의 `watch-fresh-cycle` 실행 상태와 최근 `All models failed` 오류 원인을 정리한 코드 진단 기록이다.

## 1. 결론

`watch-fresh-cycle`의 10분 주기 실행 자체는 정상이다.

실제 설치된 LaunchAgent는 다음 명령을 600초마다 실행한다.

```bash
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python3.11 -m kbo_card_news.automation.cli \
  watch-fresh-cycle \
  --notify \
  --json
```

확인 시점의 launchd 상태는 다음과 같았다.

```text
state = not running
runs = 23
last exit code = 0
run interval = 600 seconds
```

`state = not running`은 비정상이 아니다. 이 watcher는 장기 실행 프로세스가 아니라 매 cycle마다 실행 후 종료되는 배치 작업이다.

오류의 핵심은 새 기사가 들어온 cycle에서 Gemini decision gate가 `gemini-3.1-flash-lite-preview` 호출에 실패한 것이다. health check 결과 해당 모델이 다음 응답을 반환했다.

```text
HTTP 503
status: UNAVAILABLE
message: This model is currently experiencing high demand. Spikes in demand are usually temporary. Please try again later.
```

따라서 최근 `model_error: "All models failed"`는 수집기나 launchd 실패가 아니라 Gemini API의 일시적 모델 과부하/가용성 문제다.

## 2. 관측된 실행 상태

최근 fresh run 디렉토리는 계속 생성되고 있었다.

```text
20260512_202500
20260512_203700
20260512_204800
20260512_205900
```

각 cycle 간격은 대체로 10-12분이었다. launchd `StartInterval=600`이더라도 실제 시작 시각은 이전 cycle 실행 시간, 시스템 스케줄링, 네트워크 대기 시간 때문에 정확히 10분 정각으로 고정되지 않는다.

최신 확인 시점에 `outputs/automation/logs/watcher.launchd.out.log`는 `2026-05-12 20:59:32 KST`에 갱신되어 있었다. `watcher.launchd.err.log`는 2026-05-09 이후 새 에러가 없었다.

## 3. 실패한 cycle 패턴

최근 실패 예시는 다음 cycle들에서 확인됐다.

```text
20260512_194000
20260512_200200
20260512_201400
20260512_202500
```

공통 패턴:

- `collected_count = 1`
- `inserted_count = 1`
- `target_article_count = 1`
- `model_call_status = no_decision`
- `model_error = All models failed`
- `decision_count = 0`
- `created_count = 0`

반대로 새 타겟 기사가 없던 cycle은 정상 스킵됐다.

```text
model_call_status = skipped_empty_target
model_error = ""
```

즉 오류는 "새 기사 수집" 이후의 Gemini 판단 단계에서만 발생한다.

## 4. 코드 흐름

관련 진입점:

- `src/kbo_card_news/automation/cli.py`
- `src/kbo_card_news/automation/fresh_window_decision.py`
- `src/kbo_card_news/runtime/model_fallback.py`

핵심 흐름:

1. `watch-fresh-cycle`이 최근 10분 기사 수집을 수행한다.
2. 새 target article이 있으면 `GeminiFreshWindowDecisionEngine.decide()`가 호출된다.
3. `call_with_fallback()`이 `call_gemini()`을 호출한다.
4. Gemini HTTP 503 또는 기타 예외가 발생하면 retry한다.
5. 모든 시도가 실패하면 `RuntimeError("All models failed")`를 발생시킨다.
6. `watch_fresh_window_once()`가 예외를 잡고 response/report 파일에 `model_call_status = no_decision`으로 저장한다.

현재 `fresh_window_decision.py`의 `GeminiFreshWindowDecisionEngine`은 Gemini 모델만 fallback policy에 남긴다.

```python
self.model_policy = [
    candidate
    for candidate in build_model_fallback_policy(model_name)
    if candidate.startswith("gemini")
]
```

기본 모델이 `gemini-3.1-flash-lite-preview`일 때 `build_model_fallback_policy()`에 등록된 추가 fallback이 없으면 사실상 같은 모델만 재시도한다. 그래서 메시지는 `All models failed`지만 운영상 의미는 "`gemini-3.1-flash-lite-preview` 호출이 모든 retry에서 실패했다"에 가깝다.

## 5. 재현 및 확인 명령

LaunchAgent 설정 확인:

```bash
plutil -p /Users/wonjaechoi/Library/LaunchAgents/com.kbo.title-news-automation.watcher.plist
launchctl print gui/501/com.kbo.title-news-automation.watcher
```

최근 실행 간격 확인:

```bash
ls -lt outputs/automation/fresh_watch_runs | head -n 30
```

최신 watcher 로그 확인:

```bash
tail -n 220 outputs/automation/logs/watcher.launchd.out.log
tail -n 80 outputs/automation/logs/watcher.launchd.err.log
```

Gemini 모델별 health check 예시:

```bash
PYTHONPATH=src python3.11 - <<'PY'
from kbo_card_news.config.env import load_default_env
from kbo_card_news.runtime.model_fallback import call_model
from kbo_card_news.scoring.engine import UrllibHttpTransport
import os

load_default_env()
schema = {
    "type": "object",
    "properties": {"ok": {"type": "string"}},
    "required": ["ok"],
}

for model in [
    "gemini-2.5-flash-lite",
    "gemini-2.5-flash",
    "gemini-3.1-flash-lite-preview",
]:
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

2026-05-12 21:xx KST 확인 결과:

```text
gemini-2.5-flash-lite OK {"ok":"yes"}
gemini-2.5-flash RetryableModelError Gemini API request failed: HTTP 503 {
gemini-3.1-flash-lite-preview RetryableModelError Gemini API request failed: HTTP 503 {
```

## 6. 권장 조치

우선순위 높은 조치:

1. fresh decision 기본 모델을 `gemini-2.5-flash-lite`로 바꾼다.
2. 또는 `gemini-3.1-flash-lite-preview` 실패 시 `gemini-2.5-flash-lite`로 fallback하도록 policy를 보강한다.
3. `All models failed` 저장 시 root cause를 함께 남기도록 response/report 저장 로직을 개선한다.

권장 코드 개선:

- `call_with_fallback()`에서 마지막 예외 타입과 메시지를 `RuntimeError`의 `__cause__`로만 남기지 말고 구조화해서 반환하거나 로그에 출력한다.
- `watch_fresh_window_once()`의 except 블록에서 `exc.__cause__`까지 포함해 `fresh_window_decision_response.json`에 저장한다.
- Gemini fallback policy에 현재 운영 가능한 모델을 명시한다.

예상 효과:

- Gemini preview 모델의 일시적 과부하가 있어도 stable lite 모델로 decision gate를 계속 통과할 수 있다.
- 다음 장애 때 `All models failed`만 보고 재조사하지 않아도 된다.

## 7. 주의사항

진단 중 `.env` 조회 명령으로 API 키 값이 터미널 출력에 노출된 이력이 있다. 이 로그가 외부에 공유될 가능성이 있으면 Gemini/OpenAI 키를 회전하는 것이 안전하다.

문서 저장소의 예시 plist와 실제 설치된 LaunchAgent가 다를 수 있다. 이번 진단은 실제 설치 파일인 아래 경로 기준이다.

```text
/Users/wonjaechoi/Library/LaunchAgents/com.kbo.title-news-automation.watcher.plist
```
