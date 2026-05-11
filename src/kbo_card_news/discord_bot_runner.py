from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import discord

from kbo_card_news.automation.discord_actions import handle_component_action
from kbo_card_news.automation.discord_bot import resolve_discord_bot_config, send_editor_ready_notification
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
            and result.job.status == "approved"
        )
        if should_build:
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
    notify: bool,
    lock_path: str | None,
) -> None:
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
    if process.returncode == 0:
        server: asyncio.subprocess.Process | None = None
        try:
            server = await _start_editor_server(
                db_path=db_path,
                job_id=job_id,
                host=host,
                port=port,
                notify_render=notify,
            )
            tunnel = await _start_quick_tunnel(port=port)
        except Exception as exc:
            if server is not None:
                await _terminate_process(server)
            error_message = _format_exception(exc)
            print(f"discord_auto_public_link_failed job_id={job_id} error={error_message}", file=sys.stderr)
            await interaction.followup.send(f"에디터 생성 완료. 공개 링크 생성 실패: {error_message}", ephemeral=True)
            return
        else:
            _ACTIVE_EDITOR_SERVERS.append(server)
            asyncio.create_task(_stop_tunnel_when_server_exits(server, tunnel.process))
            public_url = _replace_editor_url_base(_load_job_editor_url(db_path, job_id), tunnel.url)
            with AutomationJobRepository(db_path) as repository:
                updated = repository.update_job_paths(
                    job_id,
                    editor_url=public_url,
                    message="Cloudflare quick tunnel editor URL recorded",
                )
                if notify:
                    notification = send_editor_ready_notification(updated)
                    repository.record_event(
                        job_id,
                        "editor_ready_notification_sent" if notification.ok else "editor_ready_notification_failed",
                        message="Discord editor-ready notification sent via quick tunnel"
                        if notification.ok
                        else notification.message,
                        metadata={
                            "discord_status_code": notification.status_code,
                            "quick_tunnel_url": tunnel.url,
                        },
                    )
            await interaction.followup.send(
                f"에디터 생성 완료. 공개 URL: {public_url}",
                ephemeral=True,
            )
        print(stdout.decode("utf-8", errors="replace")[-4000:])
        return
    message = stderr.decode("utf-8", errors="replace") or stdout.decode("utf-8", errors="replace")
    await interaction.followup.send(f"에디터 생성 실패: {message[-1500:]}", ephemeral=True)
    print(f"discord_auto_build_failed returncode={process.returncode} message={message[-4000:]}")


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


@dataclass(slots=True)
class QuickTunnelResult:
    url: str
    process: asyncio.subprocess.Process


async def _start_quick_tunnel(*, port: int, timeout_seconds: int = 30) -> QuickTunnelResult:
    cloudflared_path = _resolve_cloudflared_path()
    print(f"cloudflare_quick_tunnel_starting path={cloudflared_path} port={port}", file=sys.stderr)
    process = await asyncio.create_subprocess_exec(
        cloudflared_path,
        "tunnel",
        "--url",
        f"http://localhost:{port}",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    if process.stdout is None:
        raise RuntimeError("cloudflared stdout is not available")
    pattern = re.compile(r"https://[A-Za-z0-9-]+\.trycloudflare\.com")
    deadline = asyncio.get_running_loop().time() + timeout_seconds
    lines: list[str] = []
    while asyncio.get_running_loop().time() < deadline:
        try:
            raw_line = await asyncio.wait_for(process.stdout.readline(), timeout=1)
        except asyncio.TimeoutError:
            if process.returncode is not None:
                break
            continue
        if not raw_line:
            if process.returncode is not None:
                break
            continue
        line = raw_line.decode("utf-8", errors="replace").strip()
        lines.append(line)
        print(f"cloudflare_quick_tunnel_log {line}", file=sys.stderr)
        match = pattern.search(line)
        if match:
            print(f"cloudflare_quick_tunnel_started url={match.group(0)} pid={process.pid}")
            return QuickTunnelResult(url=match.group(0), process=process)
    try:
        process.terminate()
    except ProcessLookupError:
        pass
    raise RuntimeError("Cloudflare quick tunnel URL was not created: " + "\n".join(lines[-8:]))


def _format_exception(exc: BaseException) -> str:
    text = str(exc).strip()
    if text:
        return text
    return f"{type(exc).__name__}: {exc!r}"


def _resolve_cloudflared_path() -> str:
    path = shutil.which("cloudflared")
    if path:
        return path
    for candidate in (
        "/usr/local/bin/cloudflared",
        "/opt/homebrew/bin/cloudflared",
    ):
        if Path(candidate).exists():
            return candidate
    raise FileNotFoundError("cloudflared executable not found in PATH or Homebrew default paths")


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


async def _stop_tunnel_when_server_exits(
    server_process: asyncio.subprocess.Process,
    tunnel_process: asyncio.subprocess.Process,
) -> None:
    await server_process.wait()
    if tunnel_process.returncode is None:
        try:
            tunnel_process.terminate()
        except ProcessLookupError:
            return
        try:
            await asyncio.wait_for(tunnel_process.wait(), timeout=5)
        except asyncio.TimeoutError:
            try:
                tunnel_process.kill()
            except ProcessLookupError:
                pass
    print("cloudflare_quick_tunnel_stopped")


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
