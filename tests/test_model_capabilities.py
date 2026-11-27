"""Model-aware channel behavior with no instrument access."""

import pytest

from rigol_mcp import drivers
from rigol_mcp import scope as sc
from rigol_mcp.models import capabilities_for
from tests.conftest import FakeScope


class RecordingScope(FakeScope):
    def __init__(self, model="DHO802", responses=None):
        super().__init__({"*IDN?": f"RIGOL TECHNOLOGIES,{model},SERIAL,1.0", **(responses or {})})
        self.queries = []

    def query(self, command):
        self.queries.append(command)
        return super().query(command)


@pytest.mark.parametrize("model,channels", [
    ("DHO802", ("CHAN1", "CHAN2")),
    ("DS2202A", ("CHAN1", "CHAN2")),
    ("DS1054Z", ("CHAN1", "CHAN2", "CHAN3", "CHAN4")),
])
def test_state_queries_only_existing_analog_channels(model, channels):
    responses = {
        ":TIM:SCAL?": "0.001", ":TIM:OFFS?": "0", ":TIM:MODE?": "MAIN",
        ":TRIGger:MODE?": "EDGE", ":TRIGger:STATus?": "STOP",
        ":TRIGger:EDGE:SOURce?": "CHAN2", ":TRIGger:EDGE:SLOPe?": "POS",
        ":TRIGger:EDGE:LEVel?": "0.5",
    }
    for channel in channels:
        for setting, value in {"DISP": "1", "SCAL": "1", "OFFS": "0", "COUP": "DC", "PROB": "10"}.items():
            responses[f":{channel}:{setting}?"] = value
    scope = RecordingScope(model, responses)

    result = sc.get_scope_state(scope)

    assert tuple(result["channels"]) == channels
    queried_channels = {command.split(":")[1] for command in scope.queries if command.startswith(":CHAN")}
    assert queried_channels == set(channels)
    assert not scope.written
    assert scope.queries.count("*IDN?") == 1


@pytest.mark.parametrize("operation", [
    lambda scope: sc.get_scope_state(scope),
    lambda scope: sc.set_channel(scope, "CHAN1", display=True),
    lambda scope: sc.measure(scope, "CHAN1", "VPP"),
    lambda scope: sc.get_waveform(scope, "CHAN1"),
])
def test_unknown_model_fails_before_channel_io(operation):
    scope = RecordingScope("DHO9994")
    with pytest.raises(RuntimeError, match="Unsupported instrument"):
        operation(scope)
    assert scope.queries == ["*IDN?"]
    assert not scope.written


@pytest.mark.parametrize("channel", ["CHAN3", "CHAN4"])
@pytest.mark.parametrize("operation", [
    lambda scope, channel: sc.set_channel(scope, channel, display=True, scale=0.5),
    lambda scope, channel: sc.measure(scope, channel, "VPP"),
    lambda scope, channel: sc.get_waveform(scope, channel),
    lambda scope, channel: sc.measure_between(scope, channel, "CHAN1", "RDELAY"),
    lambda scope, channel: sc.measure_between(scope, "CHAN1", channel, "RDELAY"),
    lambda scope, channel: sc.set_trigger(scope, source=channel, level=0.5),
])
def test_unavailable_channel_is_rejected_before_queries_or_mutations(operation, channel):
    # CHAN1 is disabled: an invalid second source must be caught before auto-enabling it.
    scope = RecordingScope(responses={":CHAN1:DISP?": "0"})
    with pytest.raises(ValueError, match=rf"{channel} is not available on DHO802"):
        operation(scope, channel)
    assert scope.queries == ["*IDN?"]
    assert not scope.written


def test_valid_second_channel_can_be_configured_and_measured():
    scope = RecordingScope(responses={":SYSTem:ERRor?": "0", ":CHAN2:DISP?": "1", ":MEASure:ITEM?": "2.5"})
    sc.set_channel(scope, "chan2", display=True, scale=0.5)
    assert sc.measure(scope, "chan2", "vpp") == "2.5"
    assert scope.written == [":CHAN2:DISP ON", ":CHAN2:SCAL 0.5", ":MEASure:STATistic:ITEM VPP,CHAN2"]
    assert scope.queries.count("*IDN?") == 1


@pytest.mark.parametrize("close", [sc.invalidate_scope, sc.disconnect_scope])
@pytest.mark.parametrize("capabilities_first", [False, True])
def test_reconnection_refreshes_capabilities_without_changing_shared_driver(close, capabilities_first):
    first = RecordingScope("DHO924S")
    sc._scope = first
    if capabilities_first:
        assert sc.get_capabilities(first).analog_channel_count == 4
    first_driver = sc.get_driver(first)
    assert sc.get_capabilities(first).analog_channel_count == 4
    assert first.queries == ["*IDN?"]

    close()
    assert first.closed and sc._idn is None

    second = RecordingScope("DHO802")
    sc._scope = second
    assert sc.get_driver(second) is first_driver
    assert sc.get_capabilities(second).analog_channels == ("CHAN1", "CHAN2")
    with pytest.raises(ValueError, match="CHAN4 is not available"):
        sc.validate_analog_channel(second, "CHAN4")
    assert second.queries == ["*IDN?"]
    info = sc.connection_info()
    assert info["model"] == "DHO802"
    assert info["analog_channels"] == ["CHAN1", "CHAN2"]


def test_known_channel_count_does_not_imply_scpi_driver_support():
    idn = "RIGOL TECHNOLOGIES,DS2202A,SERIAL,1.0"
    assert capabilities_for(idn).analog_channels == ("CHAN1", "CHAN2")
    with pytest.raises(RuntimeError, match="No dialect driver matched"):
        drivers.driver_for(idn)


@pytest.mark.parametrize("model", ["DS1102E", "DHO9994", "UNKNOWN"])
def test_unknown_model_is_not_guessed_from_digits_or_serial_number(model):
    idn = f"RIGOL TECHNOLOGIES,{model},DS1054Z-DHO924S-SERIAL,1.0"
    with pytest.raises(RuntimeError, match="Unsupported instrument"):
        capabilities_for(idn)
    with pytest.raises(RuntimeError, match="Unsupported instrument"):
        drivers.driver_for(idn)


def test_idn_model_field_is_normalized_and_serial_cannot_override_it():
    model = capabilities_for(" rigol technologies , dho802 , DS1054Z-SERIAL,1.0")
    assert model.model == "DHO802" and model.analog_channel_count == 2


@pytest.mark.parametrize("spelling", ["DS1074Z-S Plus", "ds1074z-s-plus"])
def test_documented_plus_spellings_share_capabilities(spelling):
    identity = f"RIGOL TECHNOLOGIES,{spelling},SERIAL,1.0"
    model = capabilities_for(identity)
    assert model.model == "DS1074Z-S PLUS" and model.analog_channel_count == 4
    assert drivers.driver_for(identity) is drivers.DS1000Z


@pytest.mark.parametrize("model", ["DHO802-S", "DS2202A-EDU", "DHO924S EXTRA"])
def test_undocumented_suffixes_are_not_guessed(model):
    with pytest.raises(RuntimeError, match="Unsupported instrument"):
        capabilities_for(f"RIGOL TECHNOLOGIES,{model},SERIAL,1.0")


@pytest.mark.parametrize("identity", ["DS1054Z", "", "OTHER,DS1054Z,SERIAL,1.0"])
def test_malformed_or_foreign_identity_cannot_select_a_rigol_model(identity):
    with pytest.raises(RuntimeError, match="Unsupported instrument"):
        capabilities_for(identity)


def test_explicit_identification_seeds_capabilities_and_clears_stale_driver():
    scope = RecordingScope()
    assert sc.set_driver_from_idn("RIGOL TECHNOLOGIES,DHO924S,SERIAL,1.0") is drivers.DHO
    assert sc.get_capabilities(scope).analog_channel_count == 4
    assert sc.set_driver_from_idn("RIGOL TECHNOLOGIES,DS2202A,SERIAL,1.0") is None
    assert sc._driver is None
    assert sc.get_capabilities(scope).analog_channel_count == 2
    assert not scope.queries


async def test_mcp_reports_channels_and_rejects_missing_input(monkeypatch):
    import asyncio
    from datetime import timedelta
    from mcp.shared.memory import create_connected_server_and_client_session
    from rigol_mcp import server as srv

    scope = RecordingScope("DHO802")
    sc._scope = scope
    accesses = []

    def get_scope():
        accesses.append(True)
        return scope

    monkeypatch.setattr(srv, "get_scope", get_scope)
    monkeypatch.setattr(srv, "_scope_lock", asyncio.Lock())
    monkeypatch.setattr(srv, "_MIN_INTERVAL", 0)
    async with create_connected_server_and_client_session(
        srv.server, read_timeout_seconds=timedelta(seconds=5)
    ) as client:
        await client.list_tools()
        assert not accesses, "Listing tools must not contact the instrument"
        identity = await client.call_tool("idn", {})
        assert not identity.isError
        assert "analog_channels" in identity.content[0].text
        assert "['CHAN1', 'CHAN2']" in identity.content[0].text
        result = await client.call_tool("set_channel", {"channel": "CHAN3", "display": True})
        assert result.isError
        assert "CHAN3 is not available on DHO802" in result.content[0].text
        assert not scope.written and scope.queries == ["*IDN?"]
        assert not (await client.call_tool("disconnect", {})).isError
        assert scope.closed
