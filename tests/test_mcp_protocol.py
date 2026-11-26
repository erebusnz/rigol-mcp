"""SDK v2 protocol coverage, including legacy clients and real stdio startup.

Instrument access is forbidden by default or replaced with a fake. The stdio
tests only list tools, reject invalid calls, and disconnect an unopened session.
"""

import asyncio
import base64
import json
import sys
from pathlib import Path

import pytest
from mcp import Client
from mcp.client.stdio import StdioServerParameters

from conftest import FakeScope
from rigol_mcp import scope as scope_mod
from rigol_mcp import server as srv


@pytest.fixture(params=["legacy", "auto"])
def protocol_mode(request):
    return request.param


@pytest.fixture
def client(protocol_mode, monkeypatch):
    accesses = []

    def forbidden():
        accesses.append(True)
        raise RuntimeError("Unexpected instrument access")

    monkeypatch.setattr(srv, "get_scope", forbidden)
    monkeypatch.setattr(srv, "_scope_lock", asyncio.Lock())
    monkeypatch.setattr(srv, "_MIN_INTERVAL", 0)
    # Each test enters/exits the client in one task: AnyIO cancel scopes cannot
    # span pytest-asyncio's separate fixture setup and teardown tasks.
    yield Client(srv.server, mode=protocol_mode, read_timeout_seconds=5)
    assert not accesses, "Invalid or hardware-free calls must not open an instrument"


async def test_list_tools_and_disconnect(client):
    async with client:
        listing = await client.list_tools()
        tools = {tool.name: tool for tool in listing.tools}
        assert {"idn", "measure", "get_waveform", "screenshot", "disconnect"} <= tools.keys()
        assert "send_raw" not in tools
        assert tools["measure"].input_schema["required"] == ["channel", "item"]
        assert "inputSchema" in tools["measure"].model_dump(by_alias=True)

        result = await client.call_tool("disconnect")  # omitted arguments mean {}
        assert not result.is_error
        assert json.loads(result.content[0].text)["connected"] is False


@pytest.mark.parametrize("name, arguments, message", [
    ("set_channel", {}, "'channel' is a required property"),
    ("measure", {"channel": "CHAN9", "item": "VPP"}, "'CHAN9' is not one of"),
    ("set_channel", {"channel": "CHAN1", "display": "yes"}, "is not of type 'boolean'"),
    ("disconnect", {"unexpected": True}, "Additional properties are not allowed"),
    ("export_acquisition", {"channels": ["CHAN1", "CHAN1"]}, "has non-unique elements"),
])
async def test_invalid_arguments_are_tool_errors(client, name, arguments, message):
    async with client:
        result = await client.call_tool(name, arguments)
        assert result.is_error
        assert result.content[0].text.startswith("Input validation error:")
        assert message in result.content[0].text


@pytest.mark.parametrize("name, arguments, message", [
    ("missing_tool", {}, "Unknown tool: missing_tool"),
    ("send_raw", {"command": "*RST"}, "send_raw is disabled"),
])
async def test_unavailable_tools_are_rejected(client, name, arguments, message):
    async with client:
        result = await client.call_tool(name, arguments)
        assert result.is_error
        assert message in result.content[0].text


async def test_mocked_identification(client, monkeypatch):
    scope = FakeScope()
    scope_mod._scope = scope
    monkeypatch.setattr(srv, "get_scope", lambda: scope)
    async with client:
        result = await client.call_tool("idn", {})
        assert not result.is_error
        assert FakeScope.DEFAULT_IDN in result.content[0].text

        result = await client.call_tool("disconnect", {})
        assert not result.is_error
        assert json.loads(result.content[0].text)["disconnected"] is True
        assert scope.closed


async def test_handler_failure_is_a_tool_error_and_session_survives(client, monkeypatch):
    async def fail(*args, **kwargs):
        raise RuntimeError("Mock instrument failure")

    monkeypatch.setattr(srv, "_call", fail)
    async with client:
        result = await client.call_tool("run", {})
        assert result.is_error
        assert result.content[0].text == "Mock instrument failure"
        assert not (await client.call_tool("disconnect", {})).is_error


async def test_screenshot_preserves_image_content(client, monkeypatch, tmp_path):
    png = b"\x89PNG\r\n\x1a\n" + b"fake image"

    async def capture(*args, **kwargs):
        return png

    monkeypatch.setattr(srv, "_call", capture)
    monkeypatch.setenv("RIGOL_SCREENSHOT_DIR", str(tmp_path))
    async with client:
        result = await client.call_tool("screenshot", {})
        assert not result.is_error
        picture = next(block for block in result.content if block.type == "image")
        assert picture.mime_type == "image/png"
        assert base64.b64decode(picture.data) == png
        assert next(tmp_path.glob("*.png")).read_bytes() == png


async def test_enabled_raw_tool_remains_validated(client, monkeypatch):
    monkeypatch.setenv("RIGOL_ENABLE_SEND_RAW", "1")

    async def respond(fn, command):
        assert fn is srv.send_raw
        assert command == ":CHAN1:SCAL?"
        return "1.000000e+00"

    monkeypatch.setattr(srv, "_call", respond)
    async with client:
        tools = await client.list_tools()
        assert "send_raw" in {tool.name for tool in tools.tools}

        result = await client.call_tool("send_raw", {})
        assert result.is_error
        assert "'command' is a required property" in result.content[0].text

        result = await client.call_tool("send_raw", {"command": ":CHAN1:SCAL?"})
        assert not result.is_error
        assert result.content[0].text == "1.000000e+00"


async def test_installed_command_over_stdio(protocol_mode, tmp_path):
    command = Path(sys.executable).with_name("rigol-mcp.exe" if sys.platform == "win32" else "rigol-mcp")
    parameters = StdioServerParameters(
        command=str(command), cwd=tmp_path,
        env={"PYTHON_DOTENV_DISABLED": "1", "RIGOL_ENABLE_SEND_RAW": "0"},
    )
    async with asyncio.timeout(30):
        async with Client(parameters, mode=protocol_mode, read_timeout_seconds=5) as session:
            if protocol_mode == "auto":
                assert session.protocol_version == "2026-07-28"
            else:
                assert session.protocol_version < "2026-07-28"
            assert session.server_info.name == "rigol-mcp"
            tools = await session.list_tools()
            assert {"idn", "measure", "disconnect"} <= {tool.name for tool in tools.tools}
            assert "send_raw" not in {tool.name for tool in tools.tools}

            invalid = await session.call_tool("set_channel", {})
            assert invalid.is_error
            assert "Input validation error:" in invalid.content[0].text

            disabled = await session.call_tool("send_raw", {"command": "*RST"})
            assert disabled.is_error
            assert "send_raw is disabled" in disabled.content[0].text

            # The schema accepts numeric strings; conversion fails in the handler
            # before scope access. It must still arrive as a tool error on the wire.
            failed = await session.call_tool("set_timebase", {"scale_s_div": "not-a-number"})
            assert failed.is_error
            assert "could not convert string to float" in failed.content[0].text

            result = await session.call_tool("disconnect", {})
            assert not result.is_error
            assert json.loads(result.content[0].text)["connected"] is False
