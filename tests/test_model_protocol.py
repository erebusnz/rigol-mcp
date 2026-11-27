"""Exercise model validation and USB recovery through real MCP client sessions."""

import asyncio
import json
from datetime import timedelta

import pyvisa
import pytest
from mcp.shared.memory import create_connected_server_and_client_session

from rigol_mcp import scope as sc
from rigol_mcp import server as srv
from tests.conftest import FakeResourceManager
from tests.test_model_capabilities import RecordingScope


def state_scope(model, channels):
    responses = {
        ":TIM:SCAL?": "0.001", ":TIM:OFFS?": "0", ":TIM:MODE?": "MAIN",
        ":TRIGger:MODE?": "EDGE", ":TRIGger:STATus?": "STOP",
        ":TRIGger:EDGE:SOURce?": "CHAN2", ":TRIGger:EDGE:SLOPe?": "POS",
        ":TRIGger:EDGE:LEVel?": "0.5",
    }
    for channel in channels:
        for setting, value in {"DISP": "0", "SCAL": "1", "OFFS": "0",
                               "COUP": "DC", "PROB": "10"}.items():
            responses[f":{channel}:{setting}?"] = value
    return RecordingScope(model, responses)


@pytest.fixture
def usb_connections(monkeypatch):
    resource = "USB0::0x1AB1::0x04CE::TEST::INSTR"
    monkeypatch.setenv("RIGOL_USB", "1")
    monkeypatch.setenv("RIGOL_USB_RESOURCE", resource)
    monkeypatch.setenv("RIGOL_USB_SKIP_CLEAR", "1")
    monkeypatch.setattr(srv, "_scope_lock", asyncio.Lock())
    monkeypatch.setattr(srv, "_MIN_INTERVAL", 0)
    monkeypatch.setattr(srv, "_RETRY_BACKOFF", 0)
    monkeypatch.setattr(srv, "_last_call_time", 0)
    scopes = []

    class Manager(FakeResourceManager):
        def list_resources(self):
            pytest.fail("Reconnecting with a direct address must not discover devices")

        def open_resource(self, name):
            assert name == resource
            scope = scopes.pop(0)
            scope.resource_name = name
            return scope

    monkeypatch.setattr(sc.pyvisa, "ResourceManager", lambda backend: Manager())
    return scopes


@pytest.mark.parametrize("model", ["DHO802", "DS2202A", "DHO9994"])
@pytest.mark.parametrize("tool,arguments", [
    ("set_channel", {"channel": "CHAN3", "display": True}),
    ("measure", {"channel": "CHAN3", "item": "VPP"}),
    ("get_waveform", {"channel": "CHAN3"}),
    ("measure_between", {"source1": "CHAN1", "source2": "CHAN3", "item": "RDELAY"}),
    ("set_trigger", {"source": "CHAN3", "level_v": 0.5}),
    ("export_acquisition", {"channels": ["CHAN3"]}),
])
async def test_mcp_rejects_unavailable_channels_before_io(
    usb_connections, tmp_path, model, tool, arguments
):
    scope = state_scope(model, ("CHAN1", "CHAN2"))
    usb_connections.append(scope)
    arguments = dict(arguments)
    if tool == "export_acquisition":
        arguments["output_dir"] = str(tmp_path)

    async with create_connected_server_and_client_session(
        srv.server, read_timeout_seconds=timedelta(seconds=5)
    ) as client:
        await client.list_tools()
        assert not scope.queries and not scope.written
        identity = await client.call_tool("idn", {})
        assert not identity.isError and model in identity.content[0].text
        if model != "DHO9994":
            assert "['CHAN1', 'CHAN2']" in identity.content[0].text
        else:
            assert "unknown model" in identity.content[0].text

        rejected = await client.call_tool(tool, arguments)
        assert rejected.isError and model in rejected.content[0].text
        assert scope.queries == ["*IDN?"]
        assert scope.written == ["*CLS"]
        assert not list(tmp_path.iterdir())
        assert not (await client.call_tool("disconnect", {})).isError
        assert scope.closed and scope.cleared == 0


@pytest.mark.parametrize("reconnect", ["disconnect", "timeout"])
async def test_mcp_reconnect_refreshes_channel_count(usb_connections, reconnect):
    first = state_scope("DHO924S", ("CHAN1", "CHAN2", "CHAN3", "CHAN4"))
    second = state_scope("DHO802", ("CHAN1", "CHAN2"))
    usb_connections.extend([first, second])

    async with create_connected_server_and_client_session(
        srv.server, read_timeout_seconds=timedelta(seconds=5)
    ) as client:
        initial = await client.call_tool("get_scope_state", {})
        assert not initial.isError
        assert len(json.loads(initial.content[0].text)["channels"]) == 4
        assert first.queries.count("*IDN?") == 1

        if reconnect == "disconnect":
            assert not (await client.call_tool("disconnect", {})).isError
            assert sc._idn is None
        else:
            def timeout():
                raise pyvisa.errors.VisaIOError(pyvisa.constants.StatusCode.error_timeout)

            first.responses[":TIM:SCAL?"] = timeout

        refreshed = await client.call_tool("get_scope_state", {})
        assert not refreshed.isError
        assert tuple(json.loads(refreshed.content[0].text)["channels"]) == ("CHAN1", "CHAN2")
        assert first.closed and sc._scope is second
        assert second.queries.count("*IDN?") == 1
        assert not any(":CHAN3:" in query or ":CHAN4:" in query for query in second.queries)

        before = list(second.queries)
        rejected = await client.call_tool("set_channel", {"channel": "CHAN4", "display": True})
        assert rejected.isError and "CHAN4 is not available on DHO802" in rejected.content[0].text
        assert second.queries == before
        assert first.written == second.written == ["*CLS"]
        assert first.cleared == second.cleared == 0
        assert not (await client.call_tool("disconnect", {})).isError
        assert sc._idn is None and second.closed


@pytest.mark.parametrize("model,count", [("MHO5106", 6), ("DHO5108", 8)])
async def test_mcp_reports_extended_channels_without_implying_dialect_support(usb_connections, model, count):
    channels = tuple(f"CHAN{i}" for i in range(1, count + 1))
    scope = state_scope(model, channels)
    usb_connections.append(scope)
    async with create_connected_server_and_client_session(
        srv.server, read_timeout_seconds=timedelta(seconds=5)
    ) as client:
        listed = await client.list_tools()
        assert not scope.queries and not scope.written
        schemas = {tool.name: tool.inputSchema for tool in listed.tools}
        assert "CHAN8" in schemas["get_waveform"]["properties"]["channel"]["enum"]
        assert schemas["export_acquisition"]["properties"]["channels"]["maxItems"] == 8

        result = await client.call_tool("get_scope_state", {})
        assert not result.isError
        assert tuple(json.loads(result.content[0].text)["channels"]) == channels

        before = list(scope.queries)
        waveform = await client.call_tool("get_waveform", {"channel": channels[-1]})
        assert waveform.isError and "No dialect driver matched" in waveform.content[0].text
        assert scope.queries == before and scope.written == ["*CLS"]

        if count == 6:
            rejected = await client.call_tool("set_channel", {"channel": "CHAN7", "display": True})
            assert rejected.isError and "CHAN7 is not available on MHO5106" in rejected.content[0].text
            assert scope.queries == before and scope.written == ["*CLS"]
        assert not (await client.call_tool("disconnect", {})).isError
