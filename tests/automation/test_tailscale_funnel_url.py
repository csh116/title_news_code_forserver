from __future__ import annotations

from kbo_card_news.automation import discord_bot_runner


def test_refresh_tailscale_funnel_base_url_uses_current_magicdns_name(monkeypatch):
    monkeypatch.setattr(discord_bot_runner, "_current_tailscale_ipv4s", lambda: {"100.73.65.42"})
    monkeypatch.setattr(
        discord_bot_runner,
        "_resolve_ipv4s",
        lambda hostname: {"100.123.62.78"} if hostname == "old-node.tailfb7825.ts.net" else set(),
    )
    monkeypatch.setattr(
        discord_bot_runner,
        "_reverse_tailscale_dns_name",
        lambda ip_address: "current-node.tailfb7825.ts.net" if ip_address == "100.73.65.42" else "",
    )

    refreshed = discord_bot_runner._refresh_tailscale_funnel_base_url(
        "https://old-node.tailfb7825.ts.net"
    )

    assert refreshed == "https://current-node.tailfb7825.ts.net"


def test_refresh_tailscale_funnel_base_url_keeps_matching_config(monkeypatch):
    monkeypatch.setattr(discord_bot_runner, "_current_tailscale_ipv4s", lambda: {"100.73.65.42"})
    monkeypatch.setattr(discord_bot_runner, "_resolve_ipv4s", lambda hostname: {"100.73.65.42"})
    monkeypatch.setattr(discord_bot_runner, "_reverse_tailscale_dns_name", lambda ip_address: "")

    refreshed = discord_bot_runner._refresh_tailscale_funnel_base_url(
        "https://current-node.tailfb7825.ts.net"
    )

    assert refreshed == "https://current-node.tailfb7825.ts.net"


def test_refresh_tailscale_funnel_base_url_prefers_socket_dns_name(monkeypatch):
    monkeypatch.setattr(discord_bot_runner, "_tailscale_socket_dns_name", lambda: "socket-node.tailfb7825.ts.net")
    monkeypatch.setattr(discord_bot_runner, "_current_tailscale_ipv4s", lambda: {"100.73.65.42"})

    refreshed = discord_bot_runner._refresh_tailscale_funnel_base_url(
        "https://old-node.tailfb7825.ts.net"
    )

    assert refreshed == "https://socket-node.tailfb7825.ts.net"


def test_tailscale_command_adds_socket(monkeypatch):
    monkeypatch.setenv("TAILSCALE_SOCKET", "/tmp/tailscaled.sock")

    command = discord_bot_runner._tailscale_command("/usr/local/bin/tailscale", "funnel", "status")

    assert command == [
        "/usr/local/bin/tailscale",
        "--socket=/tmp/tailscaled.sock",
        "funnel",
        "status",
    ]
