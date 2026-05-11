# Tailscale Funnel Handoff 2026-05-11

이 문서는 다른 작업용 노트북이나 다음 Codex 세션에서 바로 이어서 작업할 수 있도록 현재 상태를 정리한 handoff다.

## 현재 결론

Tailscale Standalone macOS 앱은 `Failed to load preferences` 상태를 반복했고, `tailscale serve status` / `tailscale funnel status`가 `No serve config`만 반환했다.

해결 경로는 Homebrew `tailscale` formula의 open-source `tailscaled`를 userspace daemon으로 별도 띄우는 방식이었다.

현재 실제로 동작하는 공개 URL은 아래다.

```text
https://kbo-editor-macbook.tailfb7825.ts.net/
```

이 URL은 `127.0.0.1:8787`으로 프록시된다.

## 현재 살아 있는 구성

### Userspace Tailscale daemon

LaunchAgent:

```text
/Users/wonjaechoi/Library/LaunchAgents/com.kbo.tailscale-userspace.plist
```

실행 바이너리:

```text
/usr/local/opt/tailscale/bin/tailscaled
```

상태 파일:

```text
/Users/wonjaechoi/.local/share/kbo-tailscale/tailscaled.state
```

소켓:

```text
/Users/wonjaechoi/.local/share/kbo-tailscale/tailscaled.sock
```

확인 명령:

```bash
/usr/local/opt/tailscale/bin/tailscale --socket=/Users/wonjaechoi/.local/share/kbo-tailscale/tailscaled.sock status
/usr/local/opt/tailscale/bin/tailscale --socket=/Users/wonjaechoi/.local/share/kbo-tailscale/tailscaled.sock funnel status
```

### Discord worker

LaunchAgent:

```text
/Users/wonjaechoi/Library/LaunchAgents/com.kbo.title-news-automation.discord-worker.plist
```

worker는 다시 로드했다.

```bash
launchctl unload /Users/wonjaechoi/Library/LaunchAgents/com.kbo.title-news-automation.discord-worker.plist
launchctl load /Users/wonjaechoi/Library/LaunchAgents/com.kbo.title-news-automation.discord-worker.plist
```

## 코드 변경

### `src/kbo_card_news/automation/discord_bot_runner.py`

변경 내용:

1. `TAILSCALE_CLI_PATH`를 지원하도록 추가.
2. `TAILSCALE_SOCKET`를 지원하도록 추가.
3. `tailscale` 호출 시 socket을 붙여 별도 userspace daemon을 쓸 수 있게 변경.
4. Funnel base URL 보정 시 socket 기반 `status --json`의 `Self.DNSName`을 우선 반영하도록 추가.
5. 기존 `tailscale funnel` 호출 경로를 현재 userspace daemon에 맞게 정리.

핵심은 자동화가 이제 `/usr/local/opt/tailscale/bin/tailscale`과 `TAILSCALE_SOCKET`을 사용한다는 점이다.

### `.env`

현재 추가된 값:

```text
TAILSCALE_FUNNEL_BASE_URL=https://kbo-editor-macbook.tailfb7825.ts.net
TAILSCALE_CLI_PATH=/usr/local/opt/tailscale/bin/tailscale
TAILSCALE_SOCKET=/Users/wonjaechoi/.local/share/kbo-tailscale/tailscaled.sock
```

주의: 기존 Standalone 앱 기준 `tailscale` 경로는 `/usr/local/bin/tailscale`이었지만, 이제 automation은 Homebrew formula 경로를 쓴다.

### `tests/automation/test_tailscale_funnel_url.py`

새 테스트를 추가했다.

1. socket 기반 DNSName 우선 보정
2. `TAILSCALE_SOCKET`가 command에 붙는지 확인

현재 이 테스트 파일은 통과한다.

## 검증 결과

### Tailscale 상태

현재 노드:

```text
kbo-editor-macbook.tailfb7825.ts.net
```

`tailscale status --json`에서 `CapMap.funnel`과 `https://tailscale.com/cap/funnel-ports?ports=443,8443,10000`이 보였다.

### Funnel 상태

이 명령이 정상이다.

```bash
/usr/local/opt/tailscale/bin/tailscale --socket=/Users/wonjaechoi/.local/share/kbo-tailscale/tailscaled.sock funnel status
```

출력 요약:

```text
https://kbo-editor-macbook.tailfb7825.ts.net (Funnel on)
|-- / proxy http://127.0.0.1:8787
```

### 실제 public URL 검증

임시로 `python3.11 -m http.server 8787 --bind 127.0.0.1`를 띄운 뒤 아래 URL이 HTTP 200을 반환하는 것을 확인했다.

```text
https://kbo-editor-macbook.tailfb7825.ts.net/
```

즉, Funnel은 현재 live다.

## 주의사항

1. `tailscale down`은 쓰지 마라. Tailscale SSH 경로가 끊길 수 있다.
2. Standalone macOS 앱 쪽 `tailscale`은 계속 문제를 냈다. automation은 이제 Homebrew formula + userspace daemon을 기준으로 봐야 한다.
3. 재부팅 후에는 아래 두 개를 먼저 확인하면 된다.

```bash
launchctl list | grep -E 'com.kbo.tailscale-userspace|com.kbo.title-news-automation.discord-worker'
/usr/local/opt/tailscale/bin/tailscale --socket=/Users/wonjaechoi/.local/share/kbo-tailscale/tailscaled.sock funnel status
```

4. Discord에서 제작 버튼을 다시 누를 때는 별도 조치가 필요 없다. 지금 worker가 새 설정을 읽고 있다.

## 다음 세션 시작점

다음 Codex는 아래부터 보면 된다.

```text
docs/tailscale_funnel_handoff_20260511.md
src/kbo_card_news/automation/discord_bot_runner.py
tests/automation/test_tailscale_funnel_url.py
```

제작 버튼을 다시 눌러야 하는 상황이면, 먼저 `kbo-editor-macbook.tailfb7825.ts.net`가 열리는지만 확인하고 진행하면 된다.
