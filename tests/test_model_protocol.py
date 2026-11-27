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


@pytest.mark.parametrize("model", ["MHO98", "MHO984"])
async def test_mcp_mho900_identification_measurement_waveform_and_limits(usb_connections, tmp_path, model):
    scope = state_scope(model, ("CHAN1", "CHAN2", "CHAN3", "CHAN4"))
    scope.responses.update({
        ":CHAN1:DISP?": "1", ":CHAN4:DISP?": "1",
        ":MEASure:ITEM? VPP,CHAN4": "2.5",
        ":MEASure:ITEM? RRDELAY,CHAN1,CHAN4": "1e-6",
        ":WAV:PRE?": "2,0,3,1,1e-6,0,0,1,0,0", ":WAV:DATA?": "1,2,3\n",
        ":ACQ:SRAT?": "4e9", ":ACQ:MDEP?": "1e6", ":ACQ:TYPE?": "NORM", ":ACQ:AVER?": "2",
    })
    usb_connections.append(scope)
    async with create_connected_server_and_client_session(
        srv.server, read_timeout_seconds=timedelta(seconds=5)
    ) as client:
        await client.list_tools()
        assert not scope.queries and not scope.written
        identity = await client.call_tool("idn", {})
        assert not identity.isError and "MHO900" in identity.content[0].text
        assert "['CHAN1', 'CHAN2', 'CHAN3', 'CHAN4']" in identity.content[0].text

        measured = await client.call_tool("measure", {"channel": "CHAN4", "item": "VPP"})
        assert not measured.isError and "2.5" in measured.content[0].text
        delay = await client.call_tool("measure_between", {
            "source1": "CHAN1", "source2": "CHAN4", "item": "RDELAY",
        })
        assert not delay.isError and "1e-6" in delay.content[0].text
        waveform = await client.call_tool("get_waveform", {"channel": "CHAN4", "raw_data": True})
        assert not waveform.isError
        assert json.loads(waveform.content[0].text)["voltages_v"] == [1, 2, 3]
        assert scope.queries.count("*IDN?") == 1

        info = await client.call_tool("get_acquisition_info", {})
        assert not info.isError
        metadata = json.loads(info.content[0].text)
        assert metadata["dialect"] == "MHO900" and metadata["sample_rate_hz"] == 4e9
        assert metadata["supported_transfer_modes"] == ["screen"]

        before = (list(scope.queries), list(scope.written))
        rejected = await client.call_tool("export_acquisition", {
            "channels": ["CHAN1"], "output_dir": str(tmp_path),
        })
        assert rejected.isError and "unsupported for MHO900" in rejected.content[0].text
        rejected = await client.call_tool("set_channel", {"channel": "CHAN5", "display": True})
        assert rejected.isError and f"not available on {model}" in rejected.content[0].text
        assert (scope.queries, scope.written) == before
        assert not list(tmp_path.iterdir())

        rejected = await client.call_tool("set_channel", {"channel": "CHAN2", "scale_v_div": 0.5})
        assert rejected.isError and "display=true" in rejected.content[0].text
        assert scope.written == before[1]
        assert scope.queries == before[0] + [":CHAN2:DISP?"]
        assert not (await client.call_tool("disconnect", {})).isError
        assert scope.closed and sc._driver is None
