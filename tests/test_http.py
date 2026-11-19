"""Tests for the streamable HTTP transport. Fully offline: no scope is opened."""

import contextlib
import json
import socket
import threading
import time

import httpx
import pytest
import uvicorn
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

import rigol_mcp.server as srv

TOKEN = "test-token"


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@contextlib.contextmanager
def _serve(token):
    port = _free_port()
    config = uvicorn.Config(srv.build_http_app(token), host="127.0.0.1", port=port, log_level="warning")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    for _ in range(100):
        if server.started:
            break
        time.sleep(0.05)
    try:
        yield f"http://127.0.0.1:{port}/mcp"
    finally:
        server.should_exit = True
        thread.join(timeout=5)


@pytest.fixture()
def http_url():
    with _serve(TOKEN) as url:
        yield url


@pytest.fixture()
def http_url_no_token():
    with _serve(None) as url:
        yield url


@contextlib.asynccontextmanager
async def _session(url, headers):
    async with (
        httpx.AsyncClient(headers=headers, timeout=30) as http,
        streamable_http_client(url, http_client=http) as (read, write, _),
        ClientSession(read, write) as session,
    ):
        await session.initialize()
        yield session


async def test_list_tools_over_http(http_url):
    async with _session(http_url, {"Authorization": f"Bearer {TOKEN}"}) as session:
        names = {t.name for t in (await session.list_tools()).tools}
    assert {"idn", "measure", "get_waveform", "screenshot"} <= names


async def test_tool_call_over_http(http_url):
    # disconnect never opens a scope, so it works without hardware
    async with _session(http_url, {"Authorization": f"Bearer {TOKEN}"}) as session:
        result = await session.call_tool("disconnect", {})
    data = json.loads(result.content[0].text)
    assert data["connected"] is False
    assert data["disconnected"] is False  # nothing was open


async def test_no_token_configured_allows_access(http_url_no_token):
    async with _session(http_url_no_token, {}) as session:
        tools = (await session.list_tools()).tools
    assert tools


def test_missing_token_rejected(http_url):
    assert httpx.post(http_url, json={}).status_code == 401


def test_wrong_token_rejected(http_url):
    assert httpx.post(http_url, json={}, headers={"Authorization": "Bearer nope"}).status_code == 401


class TestExposureGuard:
    def test_loopback_without_token_ok(self):
        srv.check_http_exposure("127.0.0.1", None, False)
        srv.check_http_exposure("localhost", None, False)

    def test_network_without_token_refused(self):
        with pytest.raises(SystemExit, match="without authentication"):
            srv.check_http_exposure("0.0.0.0", None, False)

    def test_network_with_token_ok(self):
        srv.check_http_exposure("0.0.0.0", "secret", False)

    def test_network_without_token_explicit_opt_out(self):
        srv.check_http_exposure("0.0.0.0", None, True)


class TestArgs:
    def test_defaults(self, monkeypatch):
        for var in ("RIGOL_MCP_TRANSPORT", "RIGOL_MCP_HOST", "RIGOL_MCP_PORT", "RIGOL_MCP_ALLOW_NO_AUTH",
                    "RIGOL_MCP_ALLOWED_HOSTS"):
            monkeypatch.delenv(var, raising=False)
        args = srv._parse_args([])
        assert (args.transport, args.host, args.port, args.allow_no_auth) == ("stdio", "127.0.0.1", 8000, False)

    def test_env(self, monkeypatch):
        monkeypatch.setenv("RIGOL_MCP_TRANSPORT", "http")
        monkeypatch.setenv("RIGOL_MCP_HOST", "0.0.0.0")
        monkeypatch.setenv("RIGOL_MCP_PORT", "8102")
        monkeypatch.setenv("RIGOL_MCP_ALLOW_NO_AUTH", "true")
        args = srv._parse_args([])
        assert (args.transport, args.host, args.port, args.allow_no_auth) == ("http", "0.0.0.0", 8102, True)

    def test_cli_overrides_env(self, monkeypatch):
        monkeypatch.setenv("RIGOL_MCP_TRANSPORT", "http")
        assert srv._parse_args(["--transport", "stdio"]).transport == "stdio"

ACCEPT = "application/json, text/event-stream"


def test_no_token_refuses_foreign_host(http_url_no_token):
    # DNS rebinding: a page on attacker.example resolving to 127.0.0.1 sends its own Host.
    response = httpx.post(
        http_url_no_token,
        json={},
        headers={"Host": "attacker.example:8000", "Accept": ACCEPT},
    )
    assert response.status_code == 421


def test_no_token_refuses_foreign_origin(http_url_no_token):
    response = httpx.post(
        http_url_no_token,
        json={},
        headers={"Origin": "http://attacker.example", "Accept": ACCEPT},
    )
    assert response.status_code == 403


def test_token_accepts_proxy_host(http_url):
    # Behind a reverse proxy the Host is the proxy's name; the token is the check.
    response = httpx.post(
        http_url,
        json={"jsonrpc": "2.0", "id": 1, "method": "ping"},
        headers={
            "Host": "labpi.lan",
            "Authorization": f"Bearer {TOKEN}",
            "Accept": ACCEPT,
        },
    )
    assert response.status_code not in (401, 403, 421)


class TestRebindingSettings:
    def test_loopback_and_bind_host(self):
        s = srv.dns_rebinding_settings("192.168.1.5")
        assert {"127.0.0.1:*", "localhost:*", "[::1]:*", "192.168.1.5:*"} == set(
            s.allowed_hosts
        )
        assert "http://localhost:*" in s.allowed_origins

    def test_wildcard_bind_adds_nothing(self):
        assert "0.0.0.0:*" not in srv.dns_rebinding_settings("0.0.0.0").allowed_hosts

    def test_extra_hosts(self):
        s = srv.dns_rebinding_settings("0.0.0.0", ["bench-pi.lan", "fe80::1"])
        assert {"bench-pi.lan:*", "[fe80::1]:*"} <= set(s.allowed_hosts)
