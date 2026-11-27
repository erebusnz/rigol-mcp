"""Tests for the streamable HTTP transport. Fully offline: no scope is opened."""

import contextlib
import asyncio
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


@pytest.mark.parametrize("host", ["attacker.example:8000", "LOCALHOST.attacker.example:8000"])
def test_no_token_refuses_foreign_host(http_url_no_token, host):
    # DNS rebinding: a page on attacker.example resolving to 127.0.0.1 sends its own Host.
    response = httpx.post(
        http_url_no_token,
        json={},
        headers={"Host": host, "Accept": ACCEPT},
    )
    assert response.status_code == 421


@pytest.mark.parametrize("origin", ["http://attacker.example", "http://LOCALHOST.attacker.example"])
def test_no_token_refuses_foreign_origin(http_url_no_token, origin):
    response = httpx.post(
        http_url_no_token,
        json={},
        headers={"Origin": origin, "Accept": ACCEPT},
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
        assert {"127.0.0.1:*", "localhost:*", "[::1]:*", "192.168.1.5:*", "localhost"} <= set(
            s.allowed_hosts
        )
        assert "http://localhost:*" in s.allowed_origins

    def test_wildcard_bind_adds_nothing(self):
        assert "0.0.0.0:*" not in srv.dns_rebinding_settings("0.0.0.0").allowed_hosts

    def test_extra_hosts(self):
        s = srv.dns_rebinding_settings("0.0.0.0", ["bench-pi.lan", "fe80::1"])
        assert {"bench-pi.lan:*", "[fe80::1]:*"} <= set(s.allowed_hosts)


INIT = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": "2025-06-18",
        "capabilities": {},
        "clientInfo": {"name": "test", "version": "1"},
    },
}


def _client(app, host):
    from starlette.testclient import TestClient

    return TestClient(app, base_url=f"http://{host}")


class TestHostCheck:
    @pytest.mark.parametrize("host", [
        "localhost", "localhost:8000", "LOCALHOST", "LocalHost:8000", "127.0.0.1:8000", "[::1]:8000",
    ])
    def test_loopback_accepted(self, host):
        with _client(srv.build_http_app(None), "localhost") as client:
            response = client.post("/mcp", json=INIT, headers={"Accept": ACCEPT, "Host": host})
        assert response.status_code == 200, response.text

    @pytest.mark.parametrize("host", [
        "bench-pi.lan", "bench-pi.lan:8000", "BENCH-PI.lan", "BENCH-PI.lan:8000",
    ])
    def test_allowed_host_with_or_without_port(self, host):
        app = srv.build_http_app(None, bind_host="0.0.0.0", extra_hosts=["Bench-Pi.LAN"])
        with _client(app, "localhost") as client:
            # An explicit header preserves case; httpx normalizes hostnames in URLs.
            response = client.post("/mcp", json=INIT, headers={"Accept": ACCEPT, "Host": host})
        assert response.status_code == 200, response.text

    @pytest.mark.parametrize("origin", [
        "http://BENCH-PI.lan", "http://BENCH-PI.lan:8000",
        "HTTPS://Bench-Pi.LAN", "HTTPS://Bench-Pi.LAN:8443",
    ])
    def test_allowed_origin_case_insensitive(self, origin):
        app = srv.build_http_app(None, bind_host="0.0.0.0", extra_hosts=["Bench-Pi.LAN"])
        with _client(app, "localhost") as client:
            response = client.post("/mcp", json=INIT, headers={"Accept": ACCEPT, "Origin": origin})
        assert response.status_code == 200, response.text

    def test_wildcard_bind_refuses_lan_address(self):
        app = srv.build_http_app(None, bind_host="0.0.0.0")
        with _client(app, "192.168.1.5:8000") as client:
            response = client.post("/mcp", json=INIT, headers={"Accept": ACCEPT})
        assert response.status_code == 421

    @pytest.mark.parametrize("method", ["GET", "DELETE"])
    def test_foreign_host_refused_on_open_session(self, method):
        # A rebinding page that learned a session id still can't use it from its own Host.
        with _client(srv.build_http_app(None), "localhost:8000") as client:
            opened = client.post("/mcp", json=INIT, headers={"Accept": ACCEPT})
            session = opened.headers["mcp-session-id"]
            response = client.request(
                method,
                "/mcp",
                headers={
                    "Host": "attacker.example:8000",
                    "Accept": "text/event-stream",
                    "mcp-session-id": session,
                },
            )
        assert response.status_code == 421


class TestBearer:
    def test_scheme_case_insensitive(self):
        app = srv.build_http_app("secret")
        with _client(app, "labpi.lan") as client:
            response = client.post(
                "/mcp", json=INIT, headers={"Accept": ACCEPT, "Authorization": "bearer secret"}
            )
        assert response.status_code == 200, response.text

    def test_token_alone_refused(self):
        app = srv.build_http_app("secret")
        with _client(app, "labpi.lan") as client:
            response = client.post(
                "/mcp", json=INIT, headers={"Accept": ACCEPT, "Authorization": "secret"}
            )
        assert response.status_code == 401


@pytest.fixture
def submitted_tools(monkeypatch):
    """Synchronize concurrent clients on server receipt, rather than arbitrary sleeps."""
    submitted = {}
    submit = srv.InstrumentWorker.submit

    def tracked_submit(worker, function, name, arguments):
        future = submit(worker, function, name, arguments)
        if name in submitted:
            submitted[name].set()
        return future

    monkeypatch.setattr(srv.InstrumentWorker, "submit", tracked_submit)
    return submitted


async def test_slow_tool_keeps_http_responsive_and_serializes_other_clients(monkeypatch, submitted_tools):
    entered, release = threading.Event(), threading.Event()
    events = []
    mode = "OFF"

    def set_mode(scope, value):
        nonlocal mode
        mode = value
        events.append(("mode", value))
        if value == "MANUAL":
            entered.set()
            assert release.wait(5)

    def set_positions(scope, requested_mode, **kwargs):
        assert requested_mode == mode
        events.append(("position", mode))

    def values(scope):
        events.append(("readback", mode))
        return {"mode": mode}

    monkeypatch.setattr(srv, "get_scope", lambda: object())
    monkeypatch.setattr(srv, "set_cursor_mode", set_mode)
    monkeypatch.setattr(srv, "set_cursor_positions", set_positions)
    monkeypatch.setattr(srv, "get_cursor_values", values)
    monkeypatch.setattr(srv, "_MIN_INTERVAL", 0)
    with _serve(TOKEN) as url:
        async with (
            _session(url, {"Authorization": f"Bearer {TOKEN}"}) as first,
            _session(url, {"Authorization": f"Bearer {TOKEN}"}) as second,
        ):
            a = asyncio.create_task(first.call_tool("set_cursors", {"mode": "MANUAL", "ax": 1}))
            try:
                assert await asyncio.to_thread(entered.wait, 3)
                # These calls need no instrument access and must finish while A is blocked.
                assert (await asyncio.wait_for(second.list_tools(), 1)).tools
                await asyncio.wait_for(second.send_ping(), 1)
                async with asyncio.timeout(1):
                    async with _session(url, {"Authorization": f"Bearer {TOKEN}"}) as newcomer:
                        await newcomer.send_ping()
                submitted_tools["set_cursors"] = queued = threading.Event()
                b = asyncio.create_task(second.call_tool("set_cursors", {"mode": "TRACK", "ax": 2}))
                assert await asyncio.to_thread(queued.wait, 3)
                assert not b.done()
            finally:
                release.set()
            results = await asyncio.gather(a, b)
            assert all(not result.isError for result in results)
            assert "MANUAL" in results[0].content[0].text
            assert "TRACK" in results[1].content[0].text
    assert events == [
        ("mode", "MANUAL"), ("position", "MANUAL"), ("readback", "MANUAL"),
        ("mode", "TRACK"), ("position", "TRACK"), ("readback", "TRACK"),
    ]


async def test_cancelled_http_screenshot_finishes_recovery_before_next_tool(
    monkeypatch, tmp_path, submitted_tools,
):
    entered, release = threading.Event(), threading.Event()
    events = []

    def capture(scope):
        events.append(("capture", time.monotonic(), threading.get_ident()))
        entered.set()
        assert release.wait(5)
        return b"fake png"

    def record(name):
        events.append((name, time.monotonic(), threading.get_ident()))
        return {}

    monkeypatch.setenv("RIGOL_SCREENSHOT_DIR", str(tmp_path))
    monkeypatch.setattr(srv, "get_scope", lambda: object())
    monkeypatch.setattr(srv, "screenshot_png", capture)
    monkeypatch.setattr(srv, "usb_in_use", lambda: True)
    monkeypatch.setattr(srv, "active_backend", lambda: "@py")
    monkeypatch.setattr(srv, "invalidate_scope", lambda: record("invalidate"))
    monkeypatch.setattr(srv, "get_scope_state", lambda scope: record("read"))
    monkeypatch.setattr(srv, "disconnect_scope", lambda: record("disconnect"))
    monkeypatch.setattr(srv, "_MIN_INTERVAL", 0)
    monkeypatch.setattr(srv, "_POST_SCREENSHOT_DELAY", 0.05)
    with _serve(TOKEN) as url:
        async with _session(url, {"Authorization": f"Bearer {TOKEN}"}) as second:
            async with _session(url, {"Authorization": f"Bearer {TOKEN}"}) as first:
                a = asyncio.create_task(first.call_tool("screenshot", {}))
                try:
                    assert await asyncio.to_thread(entered.wait, 3)
                    a.cancel()
                    with pytest.raises(asyncio.CancelledError):
                        await a
                except BaseException:
                    release.set()
                    raise
            # Closing A's session must neither stop the active job nor close B's connection.
            try:
                assert [e[0] for e in events] == ["capture"]
                submitted_tools["disconnect"] = closing = threading.Event()
                b = asyncio.create_task(second.call_tool("disconnect", {}))
                assert await asyncio.to_thread(closing.wait, 3)
                submitted_tools["get_scope_state"] = reading = threading.Event()
                c = asyncio.create_task(second.call_tool("get_scope_state", {}))
                assert await asyncio.to_thread(reading.wait, 3)
            finally:
                release.set()
            results = await asyncio.gather(b, c)
            assert all(not result.isError for result in results)
    names = [e[0] for e in events]
    assert names == ["capture", "invalidate", "disconnect", "read", "disconnect"]
    assert events[3][1] - events[1][1] >= 0.05
    assert len({e[2] for e in events}) == 1
    assert list(tmp_path.glob("*.png")), "Cancelled active tool must still finish saving its result"
