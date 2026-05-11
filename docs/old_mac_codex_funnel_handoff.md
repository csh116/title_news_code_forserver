# 구형 맥북 Codex 작업 지시서: Tailscale Funnel 전환

작성 시각: 2026-05-11 02:28 KST

이 문서는 구형 맥북에서 새로 실행할 Codex가 바로 이어서 작업하기 위한 현재 상태와 주의사항이다.

## 절대 주의

원격 SSH로 접속한 상태에서 아래 명령을 실행하지 말 것.

```bash
tailscale down
```

이 명령은 Tailscale 네트워크를 내려서 SSH 연결을 끊는다. 꼭 필요하면 구형 맥북 로컬 화면/키보드에서만 실행한다.

운영 중인 `discord-button-worker`, `serve-job-editor`, `watcher` 프로세스는 임의로 죽이지 말고 먼저 상태를 확인한다. 이미 떠 있는 editor 서버가 있을 수 있다.

## 구형 맥북 기본 정보

프로젝트 경로:

```text
/Users/wonjaechoi/kbo/title_news_code
```

계정:

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

수동 watcher 실행 시 SSL 인증서 환경변수가 필요할 수 있다.

```bash
SSL_CERT_FILE=/usr/local/etc/ca-certificates/cert.pem \
REQUESTS_CA_BUNDLE=/usr/local/etc/ca-certificates/cert.pem \
PYTHONDONTWRITEBYTECODE=1 \
PYTHONPATH=src \
python3.11 -m kbo_card_news.automation.cli <command>
```

## 현재 목표

Cloudflare Quick Tunnel을 폐기하고 Tailscale Funnel 기반 공개 HTTPS editor URL을 Discord에 보내는 것이 목표다.

원하는 최종 URL 형태:

```text
https://macbookpro-for-af-group-1.tailfb7825.ts.net/topic/1?token=...
```

폰에서 Tailscale 앱을 켜지 않아도 LTE/5G 또는 외부 네트워크에서 열려야 한다.

## 현재 코드 상태

구형 맥북에는 Tailscale Funnel 전환 코드가 이미 반영된 것을 확인했다.

확인 명령:

```bash
cd ~/kbo/title_news_code
grep -n "tailscale_funnel" src/kbo_card_news/automation/discord_bot_runner.py
grep -n "tailscale-funnel-base-url" src/kbo_card_news/automation/cli.py
```

확인된 주요 파일:

```text
src/kbo_card_news/automation/discord_bot_runner.py
src/kbo_card_news/automation/cli.py
```

구현된 동작:

```text
1. discord-button-worker가 제작 버튼을 받음
2. build-approved-editor 실행
3. serve-job-editor 실행
4. TAILSCALE_FUNNEL_BASE_URL 또는 --tailscale-funnel-base-url로 Funnel base URL 생성
5. editor path/token을 붙여 Discord에 editor URL 전송
6. 실패 시 --public-host 기반 URL로 fallback
```

환경변수도 구형 맥북 `.env`에 추가 완료:

```text
TAILSCALE_FUNNEL_BASE_URL=https://macbookpro-for-af-group-1.tailfb7825.ts.net
```

## 현재 서비스 상태

discord worker는 launchd로 떠 있었다.

확인된 상태:

```text
launchctl list | grep com.kbo.title-news-automation
2451    0    com.kbo.title-news-automation.discord-worker
```

프로세스:

```text
discord-button-worker --auto-build --notify-build --host 0.0.0.0 --public-host 100.123.62.78 --port 8787
```

주의: command line에 `--tailscale-funnel-base-url`이 없어도 괜찮다. 코드가 `.env`의 `TAILSCALE_FUNNEL_BASE_URL`을 읽는다.

## 오늘 테스트한 실제 job

```text
job_id: kbo-news-20260511_015048-1
topic: 키움 안치홍, 5연패 탈출 이끈 끝내기 만루포
token: JVQCj1Jn1loSEkIZhnbxoDBHUFZgxuge
```

Discord 결과:

```text
[Tailscale Funnel URL 실패 - LAN URL로 대체]
Editor: http://100.123.62.78:8787/topic/1?token=JVQCj1Jn1loSEkIZhnbxoDBHUFZgxuge

실패한 Funnel URL:
https://macbookpro-for-af-group-1.tailfb7825.ts.net/topic/1?token=JVQCj1Jn1loSEkIZhnbxoDBHUFZgxuge

실패 원인:
public editor URL was not reachable: <urlopen error [Errno 61] Connection refused>
```

editor 서버 자체는 살아 있었다.

```bash
lsof -nP -iTCP:8787 -sTCP:LISTEN
```

확인된 출력:

```text
Python  2482 wonjaechoi  TCP *:8787 (LISTEN)
```

로컬 HTTP는 정상이라고 사용자가 확인했다.

## Tailscale Funnel 현재 문제

Funnel 명령은 성공처럼 보이지만 실제 공개 접속이 안 된다.

실행했던 명령:

```bash
tailscale funnel --yes --https=443 http://127.0.0.1:8787
```

출력:

```text
Available on the internet:

https://macbookpro-for-af-group-1.tailfb7825.ts.net/
|-- proxy http://127.0.0.1:8787

Press Ctrl+C to exit.
```

하지만 다른 터미널에서:

```bash
curl -v 'https://macbookpro-for-af-group-1.tailfb7825.ts.net/' 2>&1 | head -n 40
```

결과:

```text
Host macbookpro-for-af-group-1.tailfb7825.ts.net:443 was resolved.
IPv4: 100.123.62.78
Trying 100.123.62.78:443...
Connection refused
```

`text:hello` Funnel 테스트도 폰에서 열리지 않았다.

```bash
tailscale funnel --yes --https=10000 text:hello
```

폰에서 아래 URL을 열어도 아무것도 뜨지 않았다.

```text
https://macbookpro-for-af-group-1.tailfb7825.ts.net:10000/
```

따라서 현재 문제는 Python/editor 코드가 아니라 Tailscale Funnel 자체가 외부 공개 리스너를 제대로 열지 못하는 상태로 판단한다.

## Tailscale 설정 확인 내용

tailnet DNS name:

```text
tailfb7825.ts.net
```

MagicDNS:

```text
켜져 있음
```

ACL에는 이미 Funnel 권한이 들어가 있다.

```json
"nodeAttrs": [
  {
    "target": ["autogroup:member"],
    "attr": ["funnel"]
  }
]
```

Tailscale CLI:

```bash
which tailscale
```

출력:

```text
/usr/local/bin/tailscale
```

버전:

```text
1.96.5
```

상태:

```bash
tailscale status
```

출력 요약:

```text
100.123.62.78  macbookpro-for-af-group-1  auraji1106@  macOS
```

문제 증상:

```bash
tailscale funnel status --json
```

출력:

```json
{}
```

또는:

```bash
tailscale funnel status
tailscale serve status
```

출력:

```text
No serve config
```

즉, CLI가 `Available on the internet`이라고 출력해도 daemon에 serve/funnel config가 저장되지 않거나 status에 반영되지 않는다.

## 다음 Codex가 해야 할 일

### 1. SSH 경로 확인

현재 SSH가 Tailscale 경유라면 `tailscale down` 금지.

먼저 현재 접속 경로를 확인한다.

```bash
who
echo "$SSH_CONNECTION"
```

### 2. Tailscale Funnel 단독 진단

editor를 빼고 `text:hello`부터 살린다.

```bash
tailscale serve reset
tailscale funnel reset
tailscale serve --bg --yes --https=443 text:hello
tailscale serve status
tailscale funnel --bg --yes 443
tailscale funnel status
```

기대 상태:

```text
tailscale serve status
  https://macbookpro-for-af-group-1.tailfb7825.ts.net/
  |-- text "hello"

tailscale funnel status
  https://macbookpro-for-af-group-1.tailfb7825.ts.net/
  |-- text "hello"
```

만약 `status`가 계속 `{}` 또는 `No serve config`이면 코드 수정이 아니라 Tailscale 설치/daemon/권한 문제다.

### 3. Tailscale daemon/app 상태 확인

```bash
ps aux | grep -i tailscale | grep -v grep
tailscale debug prefs
tailscale status --json
```

확인할 것:

```text
Use Tailscale DNS
MagicDNS
ServeConfig/Funnel 관련 필드
Self DNSName
```

### 4. Tailscale 앱 설치 방식 확인

구형 맥북은 `/usr/local/bin/tailscale`을 사용 중이다. macOS 앱과 CLI/daemon이 엇갈려 있을 가능성이 있다.

확인:

```bash
ls -l /Applications | grep -i tailscale
ls -l /usr/local/bin/tailscale
```

필요하면 Tailscale 앱을 열어 로그인 상태와 Settings를 직접 확인한다.

### 5. 임시 운영은 fallback URL 사용

Funnel이 살아나기 전까지는 폰에서 Tailscale 앱을 켜고 아래 형태를 사용한다.

```text
http://100.123.62.78:8787/topic/1?token=...
```

현재 job의 fallback URL:

```text
http://100.123.62.78:8787/topic/1?token=JVQCj1Jn1loSEkIZhnbxoDBHUFZgxuge
```

## 코드 쪽 후속 수정 후보

Funnel이 정상화된 뒤에도 아래 개선이 필요하다.

1. 자동 Funnel 시작 target을 `localhost:<port>`가 아니라 `http://127.0.0.1:<port>`로 바꾼다.
2. `TAILSCALE_FUNNEL_BASE_URL`을 쓰는 경우 구형 맥북 내부 health check가 false negative를 만들 수 있다. 내부 DNS가 `100.123.62.78`로 해석되어 `:443 connection refused`가 날 수 있기 때문이다.
3. 따라서 Tailscale Funnel URL은 내부 health check 실패만으로 LAN fallback으로 바꾸지 말고, Discord에는 Funnel URL을 우선 보내되 warning event만 기록하는 방식이 더 낫다.
4. 별도 URL 모드를 추가하는 것도 좋다.

예상 옵션:

```text
--editor-public-url-mode tailscale-funnel
--editor-public-url-mode tailscale-ip
--editor-public-url-mode lan
```

현재 운영에서는 Funnel이 안 되면 `tailscale-ip` 모드로 바로 보내는 것이 사용자 경험상 낫다.

## 로그 확인 명령

```bash
cd ~/kbo/title_news_code
tail -n 120 outputs/automation/logs/discord-worker.launchd.err.log
tail -n 120 outputs/automation/logs/discord-worker.launchd.out.log
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python3.11 -m kbo_card_news.automation.cli show kbo-news-20260511_015048-1
```

## 설치 중인 Codex에게 요청할 첫 프롬프트 예시

구형 맥북에서 Codex를 열면 이렇게 시작하면 된다.

```text
docs/old_mac_codex_funnel_handoff.md를 읽고, 현재 Tailscale Funnel이 외부 공개로 안 열리는 원인을 로컬에서 진단해줘. 원격 SSH 중이면 tailscale down은 절대 실행하지 마. 먼저 text:hello Funnel이 serve/funnel status에 저장되는지 확인하고, 안 되면 Tailscale daemon/app 설치 상태를 점검해줘.
```

