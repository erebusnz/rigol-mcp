"""Documented MHO900/MHO98 SCPI behavior; all instrument I/O is simulated."""

import pytest

from rigol_mcp import drivers
from rigol_mcp import memory_export as mem
from rigol_mcp import scope as sc
from tests.conftest import make_block
from tests.test_model_capabilities import RecordingScope
from tests.test_model_protocol import state_scope


@pytest.mark.parametrize("model", ["MHO934", "MHO954", "MHO984", "MHO98"])
def test_exact_models_select_mho900_and_cache_four_channels(model):
    scope = RecordingScope(model.lower())
    assert sc.get_driver(scope) is drivers.MHO900
    assert sc.get_capabilities(scope).analog_channels == ("CHAN1", "CHAN2", "CHAN3", "CHAN4")
    scope.responses["*IDN?"] = "RIGOL,DHO802,SERIAL,1"
    assert sc.get_driver(scope) is drivers.MHO900
    assert scope.queries == ["*IDN?"]
    assert not scope.written


@pytest.mark.parametrize("model", [
    "MHO900", "MHO999", "MHO98-S", "MHO984 EXTRA", "MHO984X",
    "MHO2024", "MHO2034", "MHO5054", "MHO5106", "DHO5108", "DS9604",
])
def test_shared_commands_do_not_infer_additional_models_or_families(model):
    identity = f"RIGOL TECHNOLOGIES,{model},MHO98-SERIAL,1.0"
    with pytest.raises(RuntimeError, match="No dialect driver matched"):
        drivers.driver_for(identity)


@pytest.mark.parametrize("close", [sc.invalidate_scope, sc.disconnect_scope])
def test_reconnect_replaces_mho_driver_and_channel_count(close):
    first = RecordingScope("MHO98")
    sc._scope = first
    assert sc.get_driver(first) is drivers.MHO900
    close()
    assert first.closed
    second = RecordingScope("DHO802")
    sc._scope = second
    assert sc.get_driver(second) is drivers.DHO
    assert sc.get_capabilities(second).analog_channels == ("CHAN1", "CHAN2")
    assert first.queries == second.queries == ["*IDN?"]


@pytest.mark.parametrize("model", ["DHO924S", "MHO98", "MHO984"])
def test_shared_screen_waveform_uses_csv_and_preamble_time_axis(model):
    scope = RecordingScope(model, {
        ":WAV:PRE?": "2,0,3,1,0.000002,0.001,1,1,0,0",
        ":WAV:DATA?": "1.5,-2.5,3.0\n",
        ":CHAN4:DISP?": "1", ":CHAN4:SCAL?": "1", ":CHAN4:OFFS?": "0",
    })
    result = sc.get_waveform(scope, "chan4")
    assert result["voltages_v"] == [1.5, -2.5, 3.0]
    assert result["times_s"] == pytest.approx([0.000998, 0.001, 0.001002])
    assert result["points"] == 3
    assert scope.written == [
        ":WAV:SOUR CHAN4", ":WAV:MODE NORM", ":WAV:FORM ASC",
        ":WAV:STAR 1", ":WAV:STOP 1000",
    ]
    assert scope.queries.count(":WAV:DATA?") == 1


@pytest.mark.parametrize("model", ["DHO924S", "MHO98", "MHO934"])
@pytest.mark.parametrize("backend", ["@py", "@ivi"])
def test_shared_png_screenshot_uses_single_format_and_binary_block(model, backend, monkeypatch):
    monkeypatch.setattr(sc, "_usb_backend_hint", backend)
    png = b"\x89PNG\r\n\x1a\n" + bytes(range(32))
    scope = RecordingScope(model)
    scope.load(make_block(png))
    assert sc.screenshot_png(scope) == png
    assert scope.written == [":DISPlay:DATA? PNG"]


@pytest.mark.parametrize("model", ["DHO924S", "MHO98"])
@pytest.mark.parametrize("fail", [False, True])
def test_shared_autoset_waits_separately_and_restores_timeout(model, fail):
    scope = RecordingScope(model, {":SYSTem:ERRor?": "0"})
    scope.timeout = 5000

    def opc():
        assert scope.written == [":AUToset"]
        assert scope.timeout == sc._SLOW_OP_TIMEOUT_MS
        if fail:
            raise TimeoutError("autoset did not finish")
        return "1"

    scope.responses["*OPC?"] = opc
    if fail:
        with pytest.raises(TimeoutError, match="autoset did not finish"):
            sc.autoscale(scope)
    else:
        sc.autoscale(scope)
    assert scope.timeout == 5000
    assert scope.queries[:2] == ["*IDN?", "*OPC?"]


@pytest.mark.parametrize("model", ["DHO924S", "MHO98"])
def test_shared_measurement_registers_statistic_item(model):
    scope = RecordingScope(model, {":CHAN4:DISP?": "1", ":MEASure:ITEM? VPP,CHAN4": "2.5"})
    assert sc.measure(scope, "chan4", "vpp") == "2.5"
    assert scope.written == [":MEASure:STATistic:ITEM VPP,CHAN4"]


@pytest.mark.parametrize("model", ["MHO934", "MHO954", "MHO984", "MHO98"])
def test_mho_variance_is_rejected_before_enabling_channel(model, monkeypatch):
    monkeypatch.setattr(sc.time, "sleep", lambda _: None)
    scope = RecordingScope(model, {
        ":CHAN4:DISP?": "0", ":SYSTem:ERRor?": "0",
        ":MEASure:ITEM? VARIANCE,CHAN4": "1.25",
    })
    with pytest.raises(ValueError, match="VARIANCE.*MHO900"):
        sc.measure(scope, "chan4", "variance")
    assert scope.queries == ["*IDN?"]
    assert not scope.written


@pytest.mark.parametrize("model,command", [
    ("DS1104Z", ":MEASure:ITEM VARIANCE,CHAN4"),
    ("DHO924S", ":MEASure:STATistic:ITEM VARIANCE,CHAN4"),
])
def test_existing_variance_measurements_keep_their_commands(model, command):
    scope = RecordingScope(model, {
        ":CHAN4:DISP?": "1", ":MEASure:ITEM? VARIANCE,CHAN4": "1.25",
    })
    assert sc.measure(scope, "chan4", "variance") == "1.25"
    assert scope.written == [command]


@pytest.mark.parametrize("item,wire_item", [
    ("RDELAY", "RRDELAY"), ("FDELAY", "FFDELAY"),
    ("RPHASE", "RRPHASE"), ("FPHASE", "FFPHASE"), ("RFPHASE", "RFPHASE"),
])
def test_mho_two_source_measurements_use_edge_matrix(item, wire_item):
    scope = RecordingScope("MHO98", {
        ":CHAN1:DISP?": "1", ":CHAN4:DISP?": "1",
        f":MEASure:ITEM? {wire_item},CHAN1,CHAN4": "1e-6",
    })
    assert sc.measure_between(scope, "CHAN1", "CHAN4", item) == "1e-6"
    assert scope.written == [f":MEASure:STATistic:ITEM {wire_item},CHAN1,CHAN4"]


@pytest.mark.parametrize("mode,prefix", [("MANUAL", ":CURSor:MANual"), ("TRACK", ":CURSor:TRACk")])
def test_mho_cursor_positions_and_readouts_use_seconds(mode, prefix):
    scope = RecordingScope("MHO98", {
        ":SYSTem:ERRor?": "0", ":CURSor:MODE?": mode,
        f"{prefix}:CAX?": "0.001", f"{prefix}:CBX?": "0.002",
        **{f"{prefix}:{field}?": "1" for field in
           ("AXValue", "BXValue", "XDELta", "IXDELta", "AYValue", "BYValue", "YDELta")},
    })
    sc.set_cursor_positions(scope, mode, ax=0.001, bx=0.002)
    result = sc.get_cursor_values(scope)
    assert result["AX_s"] == 0.001 and result["BX_s"] == 0.002
    assert scope.written == [f"{prefix}:CAX 0.001", f"{prefix}:CBX 0.002"]
    assert not any(q.startswith(":TIM:") for q in scope.queries)


@pytest.mark.parametrize("display", [None, False])
@pytest.mark.parametrize("setting", [{"scale": 0.5}, {"offset": 0.0}])
def test_hidden_vertical_settings_fail_before_any_writes(display, setting):
    scope = RecordingScope("MHO98", {":CHAN2:DISP?": "0"})
    with pytest.raises(ValueError, match="Set display=true"):
        sc.set_channel(scope, "CHAN2", display=display, probe=10, **setting)
    assert not scope.written


@pytest.mark.parametrize("display", [None, True])
def test_visible_vertical_configuration_preserves_probe_order(display):
    scope = RecordingScope("MHO98", {":CHAN2:DISP?": "ON", ":SYSTem:ERRor?": "0"})
    sc.set_channel(scope, "CHAN2", display=display, probe=10.0, scale=0.5, offset=0)
    assert scope.written == ([":CHAN2:DISP ON"] if display else []) + [
        ":CHAN2:PROB 10", ":CHAN2:SCAL 0.5", ":CHAN2:OFFS 0",
    ]


def test_hidden_channel_can_be_explicitly_enabled_with_vertical_settings():
    scope = RecordingScope("MHO98", {":CHAN2:DISP?": "0", ":SYSTem:ERRor?": "0"})
    sc.set_channel(scope, "CHAN2", display=True, scale=0.5)
    assert scope.written == [":CHAN2:DISP ON", ":CHAN2:SCAL 0.5"]


def test_hidden_nonvertical_configuration_is_allowed():
    scope = RecordingScope("MHO98", {":SYSTem:ERRor?": "0"})
    sc.set_channel(scope, "CHAN2", display=False, coupling="AC", probe=10)
    assert scope.written == [":CHAN2:DISP OFF", ":CHAN2:PROB 10", ":CHAN2:COUP AC"]


def test_catalog_only_models_keep_generic_channel_configuration():
    scope = RecordingScope("DS2202A", {":SYSTem:ERRor?": "0"})
    sc.set_channel(scope, "CHAN2", scale=0.5)
    assert scope.written == [":CHAN2:SCAL 0.5"]


@pytest.mark.parametrize("channel", ["CHAN5", "CHAN8", "D0"])
@pytest.mark.parametrize("operation", [
    lambda s, ch: sc.set_channel(s, ch, display=True),
    lambda s, ch: sc.get_waveform(s, ch),
    lambda s, ch: sc.measure_between(s, "CHAN1", ch, "RDELAY"),
])
def test_mho_rejects_unavailable_or_digital_inputs_before_channel_io(channel, operation):
    scope = RecordingScope("MHO98")
    with pytest.raises(ValueError, match="not available on MHO98"):
        operation(scope, channel)
    assert scope.queries == ["*IDN?"] and not scope.written


def test_mho_info_reports_acquisition_metadata_without_enabling_raw_export():
    scope = state_scope("MHO98", ("CHAN1", "CHAN2", "CHAN3", "CHAN4"))
    scope.responses.update({
        ":CHAN1:DISP?": "1", ":ACQ:SRAT?": "4e9", ":ACQ:MDEP?": "1e6",
        ":ACQ:TYPE?": "NORM", ":ACQ:AVER?": "2",
    })
    result = mem.get_acquisition_info(scope)
    assert result["dialect"] == "MHO900" and result["enabled_channels"] == ["CHAN1"]
    assert result["sample_rate_hz"] == 4e9 and result["memory_depth_setting"] == "1e6"
    assert result["acquisition_type"] == "NORM" and result["averages"] == "2"
    assert result["supported_transfer_modes"] == ["screen"]
    assert result["warnings"] == ["RAW export not implemented for MHO900"]
    assert not scope.written


def test_mho_raw_export_fails_before_queries_writes_or_file_creation(tmp_path):
    scope = RecordingScope("MHO98")
    with pytest.raises(ValueError, match="unsupported for MHO900"):
        mem.export_acquisition(scope, ["CHAN1"], tmp_path)
    assert scope.queries == ["*IDN?"] and not scope.written
    assert not list(tmp_path.iterdir())
