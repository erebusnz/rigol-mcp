"""Unit tests for the dispatcher, retry/recovery wrapper and screenshot reconnect.

These exercise the worker-side handlers directly; test_http and test_worker cover
the public dispatch queue. VISA access is faked; no hardware involved.
"""

import time
import asyncio

import pyvisa
import pytest

import rigol_mcp.server as srv


@pytest.fixture(autouse=True)
def fast_and_isolated(monkeypatch):
    """Remove real timing and connection management from _call for unit testing."""
    monkeypatch.setattr(srv, "_RETRY_BACKOFF", 0)
    monkeypatch.setattr(srv, "get_scope", lambda: "FAKE_SCOPE")
    monkeypatch.setattr(srv, "invalidate_scope", lambda: invalidated.append(1))
    # advance the clock baseline so the min-interval gate doesn't sleep by default
    srv._last_call_time = time.monotonic() - 10.0
    invalidated.clear()


invalidated: list = []


def _tmo():
    return pyvisa.errors.VisaIOError(-1073807339)  # VI_ERROR_TMO


async def test_call_returns_result():
    assert await srv._call(lambda scope: f"ok:{scope}") == "ok:FAKE_SCOPE"


async def test_disconnect_does_not_open_scope(monkeypatch):
    def forbidden():
        pytest.fail("disconnect must not open a scope")

    def disconnect():
        assert srv._scope_lock.locked()
        return {"connected": False}

    monkeypatch.setattr(srv, "get_scope", forbidden)
    monkeypatch.setattr(srv, "disconnect_scope", disconnect)
    assert "disconnect" in {t.name for t in await srv.list_tools()}
    result = await srv._dispatch_tool("disconnect", {})
    assert '"connected": false' in result[0].text


async def test_disconnect_waits_until_retries_finish(monkeypatch):
    # Retry backoff yields while retaining ownership; queued disconnects and
    # calls must not touch VISA during this interval. No hardware is involved.
    monkeypatch.setattr(srv, "_scope_lock", asyncio.Lock())
    monkeypatch.setattr(srv, "_MIN_INTERVAL", 0)
    backoff_started, release = asyncio.Event(), asyncio.Event()
    real_sleep = asyncio.sleep
    events = []

    async def backoff(_):
        backoff_started.set()
        await release.wait()

    def operation(scope):
        assert srv._scope_lock.locked()
        events.append("operation")
        if len(events) == 1:
            raise _tmo()

    def disconnect():
        assert srv._scope_lock.locked()
        events.append("disconnect")
        return {"connected": False}

    monkeypatch.setattr(srv.asyncio, "sleep", backoff)
    monkeypatch.setattr(srv, "disconnect_scope", disconnect)
    active = asyncio.create_task(srv._call(operation))
    await backoff_started.wait()
    closing = asyncio.create_task(srv._dispatch_tool("disconnect", {}))
    queued = asyncio.create_task(srv._call(lambda scope: events.append("next")))
    await real_sleep(0)
    assert events == ["operation"] and not closing.done() and not queued.done()
    release.set()
    await asyncio.gather(active, closing, queued)
    assert events == ["operation", "operation", "disconnect", "next"]


async def test_cancelled_queued_disconnect_never_touches_scope(monkeypatch):
    monkeypatch.setattr(srv, "_scope_lock", asyncio.Lock())

    def forbidden():
        pytest.fail("cancelled queued disconnect must not touch scope")

    monkeypatch.setattr(srv, "get_scope", forbidden)
    monkeypatch.setattr(srv, "disconnect_scope", forbidden)
    async with srv._scope_lock:
        task = asyncio.create_task(srv._dispatch_tool("disconnect", {}))
        await asyncio.sleep(0)
        assert not task.done()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert not srv._scope_lock.locked()


async def test_export_cleanup_retains_scope_lock(monkeypatch, tmp_path):
    from test_memory_export import MemoryScope
    from rigol_mcp import memory_export as mem
    scope = MemoryScope()
    scope.fail_at = 2
    cleaned = []

    def invalidate():
        assert srv._scope_lock.locked()
        cleaned.append(True)

    monkeypatch.setattr(srv, "get_scope", lambda: scope)
    monkeypatch.setattr(mem, "invalidate_scope", invalidate)
    result = await srv._dispatch_tool("export_acquisition", {
        "channels": ["CHAN1"], "output_dir": str(tmp_path),
    })
    assert '"complete": false' in result[0].text
    assert cleaned == [True] and len(scope.reads) == 2
    assert not srv._scope_lock.locked()


async def test_call_retries_then_succeeds():
    calls = {"n": 0}

    def flaky(scope):
        calls["n"] += 1
        if calls["n"] < 3:
            raise _tmo()
        return "recovered"

    assert await srv._call(flaky) == "recovered"
    assert calls["n"] == 3
    assert len(invalidated) == 2          # reconnected before each of the 2 retries


@pytest.mark.parametrize("direct", [False, True])
@pytest.mark.parametrize("exc_factory", [_tmo, lambda: OSError("USB temporarily unavailable")])
async def test_call_retries_usb_open_failure(monkeypatch, direct, exc_factory):
    from rigol_mcp import scope as sc
    from tests.conftest import FakeResourceManager, FakeScope

    resource = "USB0::0x1AB1::0x04CE::DS1ZA1::INSTR"
    monkeypatch.setenv("RIGOL_USB", "1")
    if direct:
        monkeypatch.setenv("RIGOL_USB_RESOURCE", resource)
    opened = []
    managers = []
    scope = FakeScope(resource_name=resource)

    def open_scope(name):
        opened.append(name)
        if len(opened) == 1:
            raise exc_factory()
        return scope

    class Manager(FakeResourceManager):
        def list_resources(self):
            assert not direct, "Direct addressing must also bypass discovery during retries"
            return super().list_resources()

    def manager(backend):
        if backend == "@ivi":
            raise OSError("NI-VISA not installed")
        rm = Manager(resources=(resource,), scope_factory=open_scope)
        managers.append(rm)
        return rm

    monkeypatch.setattr(sc.pyvisa, "ResourceManager", manager)
    monkeypatch.setattr(srv, "get_scope", sc.get_scope)
    monkeypatch.setattr(srv, "invalidate_scope", sc.invalidate_scope)
    assert await srv._call(sc.idn) == FakeScope.DEFAULT_IDN
    assert opened == [resource, resource]
    assert managers[0].closed
    assert sc.active_backend() == "@py"


async def test_call_recovers_usb_query_failure_without_device_clear(monkeypatch):
    from rigol_mcp import scope as sc
    from tests.conftest import FakeResourceManager, FakeScope

    resource = "USB0::0x1AB1::0x04CE::DS1ZA1::INSTR"
    monkeypatch.setenv("RIGOL_USB", "1")
    monkeypatch.setenv("RIGOL_USB_RESOURCE", resource)
    monkeypatch.setenv("RIGOL_USB_SKIP_CLEAR", "1")
    opened = []

    class Scope(FakeScope):
        def clear(self):
            pytest.fail("Device clear must not run during USB setup or retry cleanup")

        def query(self, command):
            if self is opened[0]:
                raise _tmo()
            return super().query(command)

    def open_scope(name):
        scope = Scope(resource_name=name)
        opened.append(scope)
        return scope

    rm = FakeResourceManager(scope_factory=open_scope)
    monkeypatch.setattr(sc.pyvisa, "ResourceManager", lambda backend: rm)
    monkeypatch.setattr(srv, "get_scope", sc.get_scope)
    monkeypatch.setattr(srv, "invalidate_scope", sc.invalidate_scope)
    assert await srv._call(sc.idn) == FakeScope.DEFAULT_IDN
    assert len(opened) == 2 and opened[0].closed and not opened[1].closed
    assert all(scope.written == ["*CLS"] for scope in opened)
    sc.disconnect_scope()
    assert opened[1].closed and rm.closed


async def test_call_exhausts_attempts_then_raises():
    calls = {"n": 0}

    def always(scope):
        calls["n"] += 1
        raise _tmo()

    with pytest.raises(pyvisa.errors.VisaIOError):
        await srv._call(always)
    assert calls["n"] == srv._MAX_ATTEMPTS
    assert len(invalidated) == srv._MAX_ATTEMPTS  # invalidate after every failed attempt


async def test_call_does_not_retry_non_communication_errors():
    calls = {"n": 0}

    def bad(scope):
        calls["n"] += 1
        raise ValueError("bad argument")

    with pytest.raises(ValueError):
        await srv._call(bad)
    assert calls["n"] == 1                 # no retry
    assert invalidated == []               # no reconnect


async def test_export_no_retry_option():
    calls = []

    def fail(scope):
        calls.append(scope)
        raise _tmo()

    with pytest.raises(pyvisa.errors.VisaIOError):
        await srv._call(fail, _retry=False)
    assert len(calls) == 1


async def test_export_tool_routing(monkeypatch):
    calls = []

    async def fake_call(fn, *args, **kwargs):
        calls.append((fn, kwargs))
        return {"manifest_path": "capture/manifest.json", "complete": True}

    monkeypatch.setattr(srv, "_call", fake_call)
    tools = {t.name for t in await srv.list_tools()}
    assert {"export_acquisition", "get_acquisition_info"} <= tools
    result = await srv._dispatch_tool("export_acquisition", {"channels": ["CHAN1"]})
    assert '"complete": true' in result[0].text
    assert calls[0][0] is srv.export_acquisition
    assert calls[0][1]["_retry"] is False
    with pytest.raises(ValueError, match="only complete records"):
        await srv._dispatch_tool("export_acquisition", {"start": -1})


async def test_acquisition_info_tool_routing(monkeypatch):
    calls = []

    async def fake_call(fn, *args, **kwargs):
        calls.append((fn, args, kwargs))
        return {"status": "WAIT", "supported_transfer_modes": ["screen", "acquisition_memory"]}

    monkeypatch.setattr(srv, "_call", fake_call)
    result = await srv._dispatch_tool("get_acquisition_info", {})
    assert '"status": "WAIT"' in result[0].text
    assert calls == [(srv.get_acquisition_info, (), {})]


async def test_partial_export_response_survives_tool_routing(monkeypatch):
    import json
    partial = {"complete": False, "manifest_current": False,
               "manifest_path": "capture/manifest.json", "error": "USB disconnected",
               "manifest_error": "disk full", "channels": {"CHAN1": {"points": 250000}},
               "recovery_manifest": {"complete": False}}

    def fake_export(scope, **kwargs):
        return partial

    monkeypatch.setattr(srv, "export_acquisition", fake_export)
    result = await srv._dispatch_tool("export_acquisition", {"channels": ["CHAN1"]})
    assert json.loads(result[0].text) == partial


@pytest.mark.parametrize("exc_factory", [
    _tmo,
    lambda: UnicodeDecodeError("utf-8", b"", 0, 1, "boom"),
    lambda: OSError("usb gone"),
])
async def test_call_retries_each_retryable_type(exc_factory):
    calls = {"n": 0}

    def flaky(scope):
        calls["n"] += 1
        if calls["n"] == 1:
            raise exc_factory()
        return "ok"

    assert await srv._call(flaky) == "ok"
    assert calls["n"] == 2


async def test_call_enforces_min_interval(monkeypatch):
    slept = []

    async def fake_sleep(seconds):
        slept.append(seconds)

    monkeypatch.setattr(srv.asyncio, "sleep", fake_sleep)
    srv._last_call_time = time.monotonic()   # just ran -> next call must wait
    await srv._call(lambda scope: "ok")
    assert slept and slept[0] > 0
    assert slept[0] <= srv._MIN_INTERVAL + 0.01


# --------------------------------------------------------------------------- screenshot reconnect

async def _run_screenshot(monkeypatch, tmp_path, *, usb, backend):
    """Drive call_tool('screenshot') with a faked _call and capture invalidate calls."""
    monkeypatch.setenv("RIGOL_SCREENSHOT_DIR", str(tmp_path))
    png = b"\x89PNG\r\n\x1a\n" + b"fakeimage"

    async def fake_call(fn, *a, **k):
        return png

    calls = {"invalidate": 0}
    monkeypatch.setattr(srv, "_call", fake_call)
    monkeypatch.setattr(srv, "usb_in_use", lambda: usb)
    monkeypatch.setattr(srv, "active_backend", lambda: backend)
    monkeypatch.setattr(srv, "invalidate_scope", lambda: calls.__setitem__("invalidate", calls["invalidate"] + 1))

    result = await srv._dispatch_tool("screenshot", {})
    return result, calls, tmp_path


async def test_screenshot_reconnects_on_py_backend(monkeypatch, tmp_path):
    result, calls, out = await _run_screenshot(monkeypatch, tmp_path, usb=True, backend="@py")
    assert calls["invalidate"] == 1                      # @py: reconnect to reset USBTMC
    assert any(getattr(b, "type", None) == "image" for b in result)
    assert list(out.glob("*.png"))                       # saved to disk


async def test_screenshot_no_reconnect_on_ivi_backend(monkeypatch, tmp_path):
    _, calls, _ = await _run_screenshot(monkeypatch, tmp_path, usb=True, backend="@ivi")
    assert calls["invalidate"] == 0                      # @ivi recovers on its own


async def test_screenshot_no_reconnect_on_lan(monkeypatch, tmp_path):
    _, calls, _ = await _run_screenshot(monkeypatch, tmp_path, usb=False, backend=None)
    assert calls["invalidate"] == 0


# --------------------------------------------------------------------------- send_raw gating

async def test_send_raw_hidden_by_default():
    names = {t.name for t in await srv.list_tools()}
    assert "send_raw" not in names


async def test_send_raw_listed_when_enabled(monkeypatch):
    monkeypatch.setenv("RIGOL_ENABLE_SEND_RAW", "1")
    names = {t.name for t in await srv.list_tools()}
    assert "send_raw" in names


async def test_send_raw_call_rejected_when_disabled():
    with pytest.raises(ValueError, match="send_raw is disabled"):
        await srv._dispatch_tool("send_raw", {"command": ":CHAN1:SCAL?"})


async def test_send_raw_call_works_when_enabled(monkeypatch):
    monkeypatch.setenv("RIGOL_ENABLE_SEND_RAW", "1")

    async def fake_call(fn, *a, **k):
        return "1.000000e+00"

    monkeypatch.setattr(srv, "_call", fake_call)
    result = await srv._dispatch_tool("send_raw", {"command": ":CHAN1:SCAL?"})
    assert result[0].text == "1.000000e+00"
