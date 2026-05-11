from __future__ import annotations

import asyncio
import ipaddress
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import discord

from kbo_card_news.automation.discord_actions import handle_component_action
from kbo_card_news.automation.discord_bot import (
    resolve_discord_bot_config,
    send_channel_message,
    send_editor_ready_notification,
)
from kbo_card_news.automation.job_state import AUTOMATION_OUTPUT_DIR, AUTOMATION_STATE_DB_PATH, AutomationJobRepository


_ACTIVE_EDITOR_SERVERS: list[asyncio.subprocess.Process] = []
DISCORD_WORKER_PID_PATH = AUTOMATION_OUTPUT_DIR / "discord_button_worker.pid"
DISCORD_WORKER_LOG_PATH = AUTOMATION_OUTPUT_DIR / "logs" / "discord_button_worker.log"


@dataclass(slots=True)
class DiscordButtonWorkerStartResult:
    started: bool
    pid: int | None
    message: str
    pid_path: Path
    log_path: Path


def ensure_discord_button_worker(
    *,
    db_path: str | Path = AUTOMATION_STATE_DB_PATH,
    bot_token: str | None = None,
    channel_id: str | None = None,
    auto_build: bool = True,
    build_host: str = "127.0.0.1",
    build_public_host: str | None = None,
    build_port: int = 8787,
    tailscale_funnel_base_url: str | None = None,
    notify_build: bool = True,
    build_lock_path: str | None = None,
) -> DiscordButtonWorkerStartResult:
    existing_pid = _read_pid(DISCORD_WORKER_PID_PATH)
    if existing_pid and _pid_is_running(existing_pid):
        return DiscordButtonWorkerStartResult(
            started=False,
            pid=existing_pid,
            message="already_running",
            pid_path=DISCORD_WORKER_PID_PATH,
            log_path=DISCORD_WORKER_LOG_PATH,
        )
    config = resolve_discord_bot_config(bot_token=bot_token, channel_id=channel_id)
    DISCORD_WORKER_PID_PATH.parent.mkdir(parents=True, exist_ok=True)
    DISCORD_WORKER_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    command = [
        sys.executable,
        "-m",
        "kbo_card_news.automation.cli",
        "--db",
        str(db_path),
        "discord-button-worker",
        "--channel-id",
        config.channel_id,
        "--host",
        build_host,
        "--port",
        str(build_port),
    ]
    if auto_build:
        command.append("--auto-build")
    if notify_build:
        command.append("--notify-build")
    if build_public_host:
        command.extend(["--public-host", build_public_host])
    if tailscale_funnel_base_url:
        command.extend(["--tailscale-funnel-base-url", tailscale_funnel_base_url])
    if build_lock_path:
        command.extend(["--build-lock-path", build_lock_path])
    log_handle = DISCORD_WORKER_LOG_PATH.open("a", encoding="utf-8")
    log_handle.write(json.dumps({"event": "start", "command": _redact_command(command)}, ensure_ascii=False) + "\n")
    log_handle.flush()
    root_dir = Path(__file__).resolve().parents[3]
    env = os.environ.copy()
    src_dir = root_dir / "src"
    existing_pythonpath = env.get("PYTHONPATH", "").strip()
    env["PYTHONPATH"] = str(src_dir) if not existing_pythonpath else os.pathsep.join([str(src_dir), existing_pythonpath])
    env["DISCORD_BOT_TOKEN"] = config.bot_token
    env["DISCORD_CHANNEL_ID"] = config.channel_id
    process = subprocess.Popen(
        command,
        stdout=log_handle,
        stderr=subprocess.STDOUT,
        cwd=str(root_dir),
        env=env,
        start_new_session=True,
    )
    DISCORD_WORKER_PID_PATH.write_text(str(process.pid), encoding="utf-8")
    return DiscordButtonWorkerStartResult(
        started=True,
        pid=process.pid,
        message="started",
        pid_path=DISCORD_WORKER_PID_PATH,
        log_path=DISCORD_WORKER_LOG_PATH,
    )


def run_discord_button_worker(
    *,
    db_path: str | Path = AUTOMATION_STATE_DB_PATH,
    bot_token: str | None = None,
    channel_id: str | None = None,
    auto_build: bool = False,
    build_host: str = "127.0.0.1",
    build_public_host: str | None = None,
    build_port: int = 8787,
    tailscale_funnel_base_url: str | None = None,
    notify_build: bool = False,
    build_lock_path: str | None = None,
) -> None:
    config = resolve_discord_bot_config(bot_token=bot_token, channel_id=channel_id)
    DISCORD_WORKER_PID_PATH.parent.mkdir(parents=True, exist_ok=True)
    DISCORD_WORKER_PID_PATH.write_text(str(os.getpid()), encoding="utf-8")
    intents = discord.Intents.default()
    client = discord.Client(intents=intents)

    @client.event
    async def on_ready() -> None:
        user = client.user
        print(f"discord_button_worker_ready user={user} channel_id={config.channel_id}")

    @client.event
    async def on_interaction(interaction: discord.Interaction) -> None:
        print(f"discord_interaction_event type={interaction.type} data={interaction.data}")
        if interaction.type is not discord.InteractionType.component:
            return
        custom_id = str((interaction.data or {}).get("custom_id") or "")
        if not custom_id.startswith("kbo:"):
            return
        deferred = await _defer_interaction(interaction, ephemeral=True)
        actor = _interaction_actor(interaction)
        with AutomationJobRepository(db_path) as repository:
            try:
                result = handle_component_action(repository, custom_id, actor=actor)
            except Exception as exc:
                await _respond(interaction, f"처리 실패: {exc}", ephemeral=True, prefer_followup=deferred)
                return
        response_message = result.message
        should_build = (
            auto_build
            and result.ok
            and result.job is not None
            and str(custom_id).startswith("kbo:produce:")
            and result.job.status in {"approved", "editor_ready"}
        )
        if should_build:
            if result.job.status == "editor_ready":
                response_message = f"{result.message}\n공개 링크 생성을 다시 시도합니다."
            else:
                response_message = f"{result.message}\n에디터 생성을 시작합니다."
        await _respond(interaction, response_message, ephemeral=True, prefer_followup=deferred)
        if should_build:
            asyncio.create_task(
                _run_build_and_serve_editor(
                    interaction,
                    job_id=result.job.job_id,
                    db_path=db_path,
                    host=build_host,
                    public_host=build_public_host,
                    port=build_port,
                    tailscale_funnel_base_url=tailscale_funnel_base_url,
                    notify=notify_build,
                    lock_path=build_lock_path,
                )
            )
        print(f"discord_button_action ok={result.ok} actor={actor} custom_id={custom_id} message={result.message}")

    @client.event
    async def on_socket_response(payload: dict[str, Any]) -> None:
        if payload.get("t") != "INTERACTION_CREATE":
            return
        data = payload.get("d") or {}
        interaction_data = data.get("data") or {}
        print(
            "discord_raw_interaction "
            f"type={data.get('type')} "
            f"custom_id={interaction_data.get('custom_id')} "
            f"component_type={interaction_data.get('component_type')}"
        )

    try:
        client.run(config.bot_token)
    finally:
        if _read_pid(DISCORD_WORKER_PID_PATH) == os.getpid():
            try:
                DISCORD_WORKER_PID_PATH.unlink()
            except FileNotFoundError:
                pass


def _read_pid(path: Path) -> int | None:
    try:
        text = path.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return None
    if not text:
        return None
    try:
        return int(text)
    except ValueError:
        return None


def _pid_is_running(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _redact_command(command: list[str]) -> list[str]:
    redacted: list[str] = []
    skip_next = False
    for value in command:
        if skip_next:
            redacted.append("<redacted>")
            skip_next = False
            continue
        redacted.append(value)
        if value == "--bot-token":
            skip_next = True
    return redacted


async def _defer_interaction(interaction: discord.Interaction, *, ephemeral: bool = True) -> bool:
    if interaction.response.is_done():
        return True
    try:
        await interaction.response.defer(ephemeral=ephemeral)
        return True
    except discord.NotFound as exc:
        print(f"discord_interaction_defer_failed error={exc}")
        return False
    except discord.HTTPException as exc:
        if getattr(exc, "code", None) != 40060:
            raise
        print(f"discord_interaction_already_acknowledged error={exc}")
        return True


async def _respond(
    interaction: discord.Interaction,
    message: str,
    *,
    ephemeral: bool = True,
    prefer_followup: bool = False,
) -> None:
    try:
        if prefer_followup or interaction.response.is_done():
            await interaction.followup.send(message, ephemeral=ephemeral)
            return
        await interaction.response.send_message(message, ephemeral=ephemeral)
    except discord.NotFound as exc:
        print(f"discord_interaction_response_failed error={exc}")
    except discord.HTTPException as exc:
        if getattr(exc, "code", None) != 40060:
            raise
        await interaction.followup.send(message, ephemeral=ephemeral)


def _interaction_actor(interaction: discord.Interaction) -> str:
    user = interaction.user
    if user is None:
        return ""
    return f"{user} ({user.id})"


async def _run_build_and_serve_editor(
    interaction: discord.Interaction,
    *,
    job_id: str,
    db_path: str | Path,
    host: str,
    public_host: str | None,
    port: int,
    tailscale_funnel_base_url: str | None,
    notify: bool,
    lock_path: str | None,
) -> None:
    build_returncode, stdout, stderr = await _build_editor_if_needed(
        db_path=db_path,
        job_id=job_id,
        host=host,
        public_host=public_host,
        port=port,
        lock_path=lock_path,
    )
    if build_returncode == 0:
        server: asyncio.subprocess.Process | None = None
        try:
            server = await _start_editor_server(
                db_path=db_path,
                job_id=job_id,
                host=host,
                port=port,
                notify_render=notify,
            )
            tailscale_base_url = await _resolve_tailscale_funnel_base_url(
                configured_base_url=tailscale_funnel_base_url,
                port=port,
            )
        except Exception as exc:
            error_message = _format_exception(exc)
            print(f"discord_auto_public_link_failed job_id={job_id} error={error_message}", file=sys.stderr)
            fallback_url = ""
            if server is not None and server.returncode is None:
                _ACTIVE_EDITOR_SERVERS.append(server)
                fallback_url = _build_lan_editor_url(
                    _load_job_editor_url(db_path, job_id),
                    host=public_host,
                    port=port,
                )
                with AutomationJobRepository(db_path) as repository:
                    updated = repository.update_job_paths(
                        job_id,
                        editor_url=fallback_url,
                        message="LAN fallback editor URL recorded after public link failure",
                    )
                    repository.record_event(
                        job_id,
                        "editor_ready_public_link_failed",
                        message=error_message,
                        metadata={"fallback_editor_url": fallback_url},
                    )
                    if notify:
                        notification = send_editor_ready_notification(updated)
                        repository.record_event(
                            job_id,
                            "editor_ready_notification_sent" if notification.ok else "editor_ready_notification_failed",
                            message="Discord editor-ready notification sent via LAN fallback"
                            if notification.ok
                            else notification.message,
                            metadata={"discord_status_code": notification.status_code},
                        )
                if notify:
                    _send_channel_message_safely(
                        "[Tailscale Funnel 실패 - LAN URL로 대체]\n"
                        f"job_id: {job_id}\n"
                        f"Editor: {fallback_url}\n\n"
                        f"실패 원인: {error_message}"
                    )
            else:
                _notify_public_link_failure(
                    db_path=db_path,
                    job_id=job_id,
                    message=f"에디터 생성 완료. 공개 링크 생성 실패: {error_message}",
                    notify=notify,
                )
            followup_message = (
                f"에디터 생성 완료. 공개 링크 생성 실패. LAN URL: {fallback_url}"
                if fallback_url
                else f"에디터 생성 완료. 공개 링크 생성 실패: {error_message}"
            )
            await _send_followup_safely(
                interaction,
                followup_message,
                ephemeral=True,
            )
            return
        else:
            _ACTIVE_EDITOR_SERVERS.append(server)
            public_url = _replace_editor_url_base(_load_job_editor_url(db_path, job_id), tailscale_base_url)
            final_url = public_url
            final_url_message = "Tailscale Funnel editor URL recorded"
            final_url_event_metadata = {"tailscale_funnel_base_url": tailscale_base_url}
            try:
                await _wait_for_public_editor_url(public_url)
            except Exception as exc:
                error_message = _format_exception(exc)
                final_url = _build_lan_editor_url(public_url, host=public_host, port=port)
                final_url_message = "LAN fallback editor URL recorded after public URL check failure"
                final_url_event_metadata = {
                    "tailscale_funnel_base_url": tailscale_base_url,
                    "failed_public_editor_url": public_url,
                    "fallback_editor_url": final_url,
                }
                print(
                    f"tailscale_funnel_editor_url_check_failed job_id={job_id} "
                    f"url={public_url} error={error_message}",
                    file=sys.stderr,
                )
                with AutomationJobRepository(db_path) as repository:
                    repository.record_event(
                        job_id,
                        "editor_ready_public_url_check_failed",
                        message=error_message,
                        metadata=final_url_event_metadata,
                    )
                if notify:
                    _send_channel_message_safely(
                        "[Tailscale Funnel URL 실패 - LAN URL로 대체]\n"
                        f"job_id: {job_id}\n"
                        f"Editor: {final_url}\n\n"
                        f"실패한 Funnel URL: {public_url}\n"
                        f"실패 원인: {error_message}"
                    )
            with AutomationJobRepository(db_path) as repository:
                updated = repository.update_job_paths(
                    job_id,
                    editor_url=final_url,
                    message=final_url_message,
                )
                if notify:
                    notification = send_editor_ready_notification(updated)
                    repository.record_event(
                        job_id,
                        "editor_ready_notification_sent" if notification.ok else "editor_ready_notification_failed",
                        message="Discord editor-ready notification sent",
                        metadata={
                            "discord_status_code": notification.status_code,
                            **final_url_event_metadata,
                        },
                    )
            await _send_followup_safely(
                interaction,
                f"에디터 생성 완료. URL: {final_url}",
                ephemeral=True,
            )
        print(stdout.decode("utf-8", errors="replace")[-4000:])
        return
    message = stderr.decode("utf-8", errors="replace") or stdout.decode("utf-8", errors="replace")
    failure_message = f"에디터 생성 실패: {message[-1500:]}"
    _notify_build_failure(db_path=db_path, job_id=job_id, message=failure_message, notify=notify)
    await _send_followup_safely(interaction, failure_message, ephemeral=True)
    print(f"discord_auto_build_failed returncode={build_returncode} message={message[-4000:]}")


async def _send_followup_safely(
    interaction: discord.Interaction,
    message: str,
    *,
    ephemeral: bool,
) -> None:
    try:
        await interaction.followup.send(message, ephemeral=ephemeral)
    except Exception as exc:
        print(f"discord_followup_send_failed error={_format_exception(exc)}", file=sys.stderr)


def _notify_build_failure(
    *,
    db_path: str | Path,
    job_id: str,
    message: str,
    notify: bool,
) -> None:
    with AutomationJobRepository(db_path) as repository:
        repository.record_event(
            job_id,
            "discord_auto_build_failed",
            message=message,
        )
    if notify:
        _send_channel_message_safely(f"[에디터 생성 실패]\njob_id: {job_id}\n{message}")


def _notify_public_link_failure(
    *,
    db_path: str | Path,
    job_id: str,
    message: str,
    notify: bool,
) -> None:
    with AutomationJobRepository(db_path) as repository:
        repository.record_event(
            job_id,
            "editor_ready_public_link_failed",
            message=message,
        )
    if notify:
        _send_channel_message_safely(f"[에디터 공개 링크 생성 실패]\njob_id: {job_id}\n{message}")


def _send_channel_message_safely(message: str) -> None:
    try:
        result = send_channel_message(message)
        if not result.ok:
            print(f"discord_channel_message_failed status={result.status_code} message={result.message}", file=sys.stderr)
    except Exception as exc:
        print(f"discord_channel_message_exception error={_format_exception(exc)}", file=sys.stderr)


async def _build_editor_if_needed(
    *,
    db_path: str | Path,
    job_id: str,
    host: str,
    public_host: str | None,
    port: int,
    lock_path: str | None,
) -> tuple[int | None, bytes, bytes]:
    with AutomationJobRepository(db_path) as repository:
        job = repository.get_job(job_id)
        if job is not None and job.status == "editor_ready":
            return 0, b"editor already ready; starting public link only", b""
    command = [
        sys.executable,
        "-m",
        "kbo_card_news.automation.cli",
        "--db",
        str(db_path),
        "build-approved-editor",
        job_id,
        "--host",
        host,
        "--port",
        str(port),
    ]
    if public_host:
        command.extend(["--public-host", public_host])
    if lock_path:
        command.extend(["--lock-path", lock_path])
    process = await asyncio.create_subprocess_exec(
        *command,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await process.communicate()
    return process.returncode, stdout, stderr


async def _start_editor_server(
    *,
    db_path: str | Path,
    job_id: str,
    host: str,
    port: int,
    notify_render: bool,
) -> asyncio.subprocess.Process:
    for attempt in range(2):
        process = await _start_editor_server_once(
            db_path=db_path,
            job_id=job_id,
            host=host,
            port=port,
            notify_render=notify_render,
        )
        await asyncio.sleep(1)
        if process.returncode is None:
            print(f"editor_server_started job_id={job_id} host={host} port={port} pid={process.pid}")
            return process
        if attempt == 0:
            killed_pids = await _terminate_existing_editor_servers_on_port(port)
            if killed_pids:
                print(
                    "editor_server_port_busy_recovered "
                    f"job_id={job_id} port={port} killed_pids={','.join(str(pid) for pid in killed_pids)}",
                    file=sys.stderr,
                )
                await _wait_for_port_release(port)
                continue
        raise RuntimeError(f"editor server failed to start: returncode={process.returncode}")
    raise RuntimeError("editor server failed to start")


async def _start_editor_server_once(
    *,
    db_path: str | Path,
    job_id: str,
    host: str,
    port: int,
    notify_render: bool,
) -> asyncio.subprocess.Process:
    command = [
        sys.executable,
        "-m",
        "kbo_card_news.automation.cli",
        "--db",
        str(db_path),
        "serve-job-editor",
        job_id,
        "--host",
        host,
        "--port",
        str(port),
    ]
    if notify_render:
        command.append("--notify-render")
    process = await asyncio.create_subprocess_exec(
        *command,
    )
    return process


async def _resolve_tailscale_funnel_base_url(
    *,
    configured_base_url: str | None,
    port: int,
    timeout_seconds: int = 30,
) -> str:
    base_url = _normalize_public_base_url(configured_base_url or os.environ.get("TAILSCALE_FUNNEL_BASE_URL"))
    if base_url:
        refreshed_url = await asyncio.to_thread(_refresh_tailscale_funnel_base_url, base_url)
        await _ensure_tailscale_funnel(port=port, timeout_seconds=timeout_seconds)
        print(f"tailscale_funnel_base_url_configured url={refreshed_url}", file=sys.stderr)
        return refreshed_url
    return await _start_tailscale_funnel(port=port, timeout_seconds=timeout_seconds)


async def _wait_for_public_editor_url(
    public_url: str,
    *,
    timeout_seconds: int = 30,
) -> None:
    deadline = asyncio.get_running_loop().time() + timeout_seconds
    last_error = ""
    while asyncio.get_running_loop().time() < deadline:
        ok, message = await asyncio.to_thread(_check_public_editor_url_once, public_url)
        if ok:
            print(f"public_editor_url_ready url={public_url}", file=sys.stderr)
            return
        last_error = message
        print(f"public_editor_url_not_ready url={public_url} error={message}", file=sys.stderr)
        await asyncio.sleep(2)
    raise RuntimeError(f"public editor URL was not reachable: {last_error}")


def _check_public_editor_url_once(public_url: str) -> tuple[bool, str]:
    request = urllib.request.Request(public_url, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=8) as response:
            status = int(getattr(response, "status", 0) or 0)
            return 200 <= status < 400, f"status={status}"
    except urllib.error.HTTPError as exc:
        return 200 <= int(exc.code) < 400, f"status={exc.code}"
    except Exception as exc:
        return False, _format_exception(exc)


async def _start_tailscale_funnel(
    *,
    port: int,
    timeout_seconds: int,
) -> str:
    return await _ensure_tailscale_funnel(port=port, timeout_seconds=timeout_seconds)


async def _ensure_tailscale_funnel(
    *,
    port: int,
    timeout_seconds: int,
) -> str:
    tailscale_path = _resolve_tailscale_path()
    command = _tailscale_command(
        tailscale_path,
        "funnel",
        "--bg",
        "--yes",
        str(port),
    )
    print(f"tailscale_funnel_starting command={command}", file=sys.stderr)
    output = await _run_command_capture(command, timeout_seconds=timeout_seconds)
    base_url = _extract_tailscale_funnel_url(output)
    if base_url:
        print(f"tailscale_funnel_started url={base_url}", file=sys.stderr)
        return base_url
    status_output = await _run_command_capture(
        _tailscale_command(tailscale_path, "funnel", "status"),
        timeout_seconds=10,
    )
    base_url = _extract_tailscale_funnel_url(status_output)
    if base_url:
        print(f"tailscale_funnel_status_url url={base_url}", file=sys.stderr)
        return base_url
    raise RuntimeError("Tailscale Funnel URL was not created: " + "\n--- status ---\n".join([output, status_output]))


async def _run_command_capture(command: list[str], *, timeout_seconds: int) -> str:
    process = await asyncio.create_subprocess_exec(
        *command,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    try:
        stdout, _ = await asyncio.wait_for(process.communicate(), timeout=timeout_seconds)
    except asyncio.TimeoutError:
        await _terminate_process(process)
        raise RuntimeError(f"command timed out: {' '.join(command)}")
    output = stdout.decode("utf-8", errors="replace")
    print(f"command_output command={command} output={output[-2000:]}", file=sys.stderr)
    if process.returncode not in (0, None):
        raise RuntimeError(f"command failed returncode={process.returncode}: {' '.join(command)}\n{output[-2000:]}")
    return output


def _extract_tailscale_funnel_url(output: str) -> str:
    match = re.search(r"https://[A-Za-z0-9.-]+\.ts\.net(?::(?:443|8443|10000))?", output)
    if not match:
        return ""
    return _normalize_public_base_url(match.group(0))


def _normalize_public_base_url(value: str | None) -> str:
    text = str(value or "").strip().rstrip("/")
    if not text:
        return ""
    parsed = urlsplit(text)
    if not parsed.scheme or not parsed.netloc:
        raise ValueError(f"public base URL must include scheme and host: {text}")
    if parsed.scheme != "https":
        raise ValueError(f"Tailscale Funnel base URL must use https: {text}")
    return urlunsplit((parsed.scheme, parsed.netloc, "", "", ""))


def _refresh_tailscale_funnel_base_url(base_url: str) -> str:
    parsed = urlsplit(base_url)
    hostname = parsed.hostname or ""
    if not hostname.endswith(".ts.net"):
        return base_url
    socket_dns_name = _tailscale_socket_dns_name()
    if socket_dns_name:
        if socket_dns_name == hostname:
            return base_url
        netloc = socket_dns_name if parsed.port is None else f"{socket_dns_name}:{parsed.port}"
        refreshed_url = urlunsplit((parsed.scheme, netloc, "", "", ""))
        print(
            "tailscale_funnel_base_url_refreshed_from_socket "
            f"configured_url={base_url} refreshed_url={refreshed_url}",
            file=sys.stderr,
        )
        return refreshed_url
    current_ips = _current_tailscale_ipv4s()
    if not current_ips:
        print(f"tailscale_current_ip_unavailable configured_url={base_url}", file=sys.stderr)
        return base_url
    configured_ips = _resolve_ipv4s(hostname)
    if configured_ips & current_ips:
        return base_url
    current_name = _reverse_tailscale_dns_name(next(iter(current_ips)))
    if not current_name:
        print(
            "tailscale_funnel_base_url_stale "
            f"configured_url={base_url} configured_ips={sorted(configured_ips)} current_ips={sorted(current_ips)}",
            file=sys.stderr,
        )
        return base_url
    netloc = current_name if parsed.port is None else f"{current_name}:{parsed.port}"
    refreshed_url = urlunsplit((parsed.scheme, netloc, "", "", ""))
    print(
        "tailscale_funnel_base_url_refreshed "
        f"configured_url={base_url} refreshed_url={refreshed_url} "
        f"configured_ips={sorted(configured_ips)} current_ips={sorted(current_ips)}",
        file=sys.stderr,
    )
    return refreshed_url


def _current_tailscale_ipv4s() -> set[str]:
    try:
        output = subprocess.run(
            ["ifconfig"],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=5,
        ).stdout
    except Exception:
        return set()
    tailscale_range = ipaddress.ip_network("100.64.0.0/10")
    ips: set[str] = set()
    current_interface = ""
    for line in output.splitlines():
        if line and not line.startswith(("\t", " ")):
            current_interface = line.split(":", 1)[0]
            continue
        if not current_interface.startswith("utun"):
            continue
        match = re.search(r"\binet\s+(\d+\.\d+\.\d+\.\d+)\b", line)
        if not match:
            continue
        ip_text = match.group(1)
        try:
            ip = ipaddress.ip_address(ip_text)
        except ValueError:
            continue
        if ip in tailscale_range:
            ips.add(ip_text)
    return ips


def _resolve_ipv4s(hostname: str) -> set[str]:
    try:
        return {
            result[4][0]
            for result in socket.getaddrinfo(hostname, None, socket.AF_INET, socket.SOCK_STREAM)
        }
    except socket.gaierror:
        return set()


def _reverse_tailscale_dns_name(ip_address: str) -> str:
    names: list[str] = []
    try:
        primary, aliases, _ = socket.gethostbyaddr(ip_address)
        names.extend([primary, *aliases])
    except (OSError, socket.herror):
        pass
    names.extend(_reverse_tailscale_dns_names_with_dscacheutil(ip_address))
    for name in names:
        normalized = name.strip().rstrip(".").lower()
        if normalized.endswith(".ts.net"):
            return normalized
    return ""


def _reverse_tailscale_dns_names_with_dscacheutil(ip_address: str) -> list[str]:
    dscacheutil_path = shutil.which("dscacheutil")
    if not dscacheutil_path:
        return []
    try:
        output = subprocess.run(
            [dscacheutil_path, "-q", "host", "-a", "ip_address", ip_address],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=5,
        ).stdout
    except Exception:
        return []
    names: list[str] = []
    for line in output.splitlines():
        match = re.match(r"\s*name:\s*(\S+)", line)
        if match:
            names.append(match.group(1))
    return names


def _format_exception(exc: BaseException) -> str:
    text = str(exc).strip()
    if text:
        return text
    return f"{type(exc).__name__}: {exc!r}"


def _resolve_tailscale_path() -> str:
    configured_path = os.environ.get("TAILSCALE_CLI_PATH", "").strip()
    if configured_path:
        return configured_path
    path = shutil.which("tailscale")
    if path:
        return path
    for candidate in (
        "/usr/local/bin/tailscale",
        "/opt/homebrew/bin/tailscale",
    ):
        if Path(candidate).exists():
            return candidate
    raise FileNotFoundError("tailscale executable not found in PATH or Homebrew default paths")


def _tailscale_command(tailscale_path: str, *args: str) -> list[str]:
    command = [tailscale_path]
    socket_path = os.environ.get("TAILSCALE_SOCKET", "").strip()
    if socket_path:
        command.append(f"--socket={socket_path}")
    command.extend(args)
    return command


def _tailscale_socket_dns_name() -> str:
    socket_path = os.environ.get("TAILSCALE_SOCKET", "").strip()
    if not socket_path:
        return ""
    tailscale_path = _resolve_tailscale_path()
    try:
        output = subprocess.run(
            _tailscale_command(tailscale_path, "status", "--json"),
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=5,
        ).stdout
    except Exception:
        return ""
    try:
        payload = json.loads(output)
    except json.JSONDecodeError:
        return ""
    dns_name = str(payload.get("Self", {}).get("DNSName") or "").strip().rstrip(".").lower()
    return dns_name if dns_name.endswith(".ts.net") else ""


async def _terminate_existing_editor_servers_on_port(port: int) -> list[int]:
    pids = await _list_listening_pids(port)
    killed_pids: list[int] = []
    for pid in pids:
        command = await _process_command(pid)
        if not _is_project_editor_server_command(command):
            print(
                f"editor_server_port_busy_unmanaged port={port} pid={pid} command={command}",
                file=sys.stderr,
            )
            continue
        try:
            os.kill(pid, 15)
        except ProcessLookupError:
            continue
        killed_pids.append(pid)
    if killed_pids:
        await _wait_for_pids_exit(killed_pids)
    return killed_pids


async def _list_listening_pids(port: int) -> list[int]:
    process = await asyncio.create_subprocess_exec(
        "lsof",
        "-nP",
        f"-iTCP:{port}",
        "-sTCP:LISTEN",
        "-t",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    stdout, _ = await process.communicate()
    pids: list[int] = []
    for line in stdout.decode("utf-8", errors="replace").splitlines():
        text = line.strip()
        if not text:
            continue
        try:
            pids.append(int(text))
        except ValueError:
            continue
    return pids


async def _process_command(pid: int) -> str:
    process = await asyncio.create_subprocess_exec(
        "ps",
        "-p",
        str(pid),
        "-o",
        "command=",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    stdout, _ = await process.communicate()
    return stdout.decode("utf-8", errors="replace").strip()


def _is_project_editor_server_command(command: str) -> bool:
    return "kbo_card_news.automation.cli" in command and "serve-job-editor" in command


async def _wait_for_port_release(port: int, timeout_seconds: float = 5.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout_seconds
    while asyncio.get_running_loop().time() < deadline:
        if not await _list_listening_pids(port):
            return
        await asyncio.sleep(0.2)


async def _wait_for_pids_exit(pids: list[int], timeout_seconds: float = 5.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout_seconds
    remaining = set(pids)
    while remaining and asyncio.get_running_loop().time() < deadline:
        for pid in list(remaining):
            if not await _pid_exists(pid):
                remaining.remove(pid)
        if remaining:
            await asyncio.sleep(0.2)
    for pid in remaining:
        try:
            os.kill(pid, 9)
        except ProcessLookupError:
            continue


async def _pid_exists(pid: int) -> bool:
    process = await asyncio.create_subprocess_exec(
        "ps",
        "-p",
        str(pid),
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    await process.wait()
    return process.returncode == 0


async def _terminate_process(process: asyncio.subprocess.Process) -> None:
    if process.returncode is not None:
        return
    try:
        process.terminate()
    except ProcessLookupError:
        return
    try:
        await asyncio.wait_for(process.wait(), timeout=5)
    except asyncio.TimeoutError:
        try:
            process.kill()
        except ProcessLookupError:
            return
        await process.wait()


def _load_job_editor_url(db_path: str | Path, job_id: str) -> str:
    with AutomationJobRepository(db_path) as repository:
        job = repository.get_job(job_id)
        if job is None or not job.editor_url:
            raise RuntimeError(f"job has no editor URL: {job_id}")
        return job.editor_url


def _replace_editor_url_base(editor_url: str, public_base_url: str) -> str:
    original = urlsplit(editor_url)
    public = urlsplit(public_base_url)
    return urlunsplit((public.scheme, public.netloc, original.path, original.query, original.fragment))


def _build_lan_editor_url(editor_url: str, *, host: str | None, port: int) -> str:
    original = urlsplit(editor_url)
    resolved_host = _refresh_tailscale_public_host(str(host or "").strip()) or _resolve_lan_ip()
    return urlunsplit(("http", f"{resolved_host}:{port}", original.path, original.query, original.fragment))


def _refresh_tailscale_public_host(host: str) -> str:
    if not host:
        return ""
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return host
    if ip not in ipaddress.ip_network("100.64.0.0/10"):
        return host
    current_ips = _current_tailscale_ipv4s()
    if host in current_ips or not current_ips:
        return host
    refreshed_host = next(iter(current_ips))
    print(f"tailscale_public_host_refreshed configured_host={host} refreshed_host={refreshed_host}", file=sys.stderr)
    return refreshed_host


def _resolve_lan_ip() -> str:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect(("1.1.1.1", 80))
            return str(sock.getsockname()[0])
    except OSError:
        pass
    try:
        return socket.gethostbyname(socket.gethostname())
    except OSError:
        return "127.0.0.1"
