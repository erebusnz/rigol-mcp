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
        for var in ("RIGOL_MCP_TRANSPORT", "RIGOL_MCP_HOST", "RIGOL_MCP_PORT", "RIGOL_MCP_ALLOW_NO_AUTH"):
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
