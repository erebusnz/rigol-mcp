"""Unit tests for rigol_mcp.scope — transport selection, backend-aware block reads,
SCPI helpers and parsing. All VISA interaction is faked (see conftest)."""

import pytest

from rigol_mcp import scope as sc
from tests.conftest import FakeScope, FakeResourceManager, make_block


# --------------------------------------------------------------------------- env / transport

@pytest.mark.parametrize("value,expected", [
    ("1", True), ("true", True), ("yes", True), ("on", True), ("anything", True),
    ("0", False), ("false", False), ("no", False), ("off", False), ("", False),
    ("  ", False), ("FALSE", False),
])
def test_usb_preferred(monkeypatch, value, expected):
    monkeypatch.setenv("RIGOL_USB", value)
    assert sc._usb_preferred() is expected
    assert sc.usb_in_use() is expected


def test_usb_preferred_unset():
    assert sc._usb_preferred() is False


def test_lan_resource_string(monkeypatch):
    monkeypatch.setenv("RIGOL_IP", "192.168.1.50")
    assert sc.get_lan_resource_string() == "TCPIP0::192.168.1.50::5555::SOCKET"


def test_lan_resource_string_missing_ip_raises():
    with pytest.raises(RuntimeError, match="RIGOL_IP"):
        sc.get_lan_resource_string()


# --------------------------------------------------------------------------- USB discovery

def _rm(*resources):
    return FakeResourceManager(resources=resources)


def test_find_usb_decimal_format():
    # pyvisa-py style: decimal VID/PID, trailing interface field
    rm = _rm("USB0::6833::1230::DS1ZA111::0::INSTR", "ASRL1::INSTR")
    assert sc.find_usb_resource_string(rm) == "USB0::6833::1230::DS1ZA111::0::INSTR"


def test_find_usb_hex_format():
    # NI-VISA style: 0x-prefixed VID/PID
    rm = _rm("USB0::0x1AB1::0x04CE::DS1ZA222::INSTR")
    assert sc.find_usb_resource_string(rm) == "USB0::0x1AB1::0x04CE::DS1ZA222::INSTR"


def test_find_usb_ignores_non_rigol():
    rm = _rm("USB0::0x0699::0x0368::TEK1::INSTR")  # Tektronix VID
    with pytest.raises(RuntimeError, match="no Rigol USB scope"):
        sc.find_usb_resource_string(rm)


def test_find_usb_none_present():
    rm = _rm("ASRL3::INSTR")
    with pytest.raises(RuntimeError, match="no Rigol USB scope"):
        sc.find_usb_resource_string(rm)


def test_find_usb_multiple_without_serial_raises():
    rm = _rm("USB0::0x1AB1::0x04CE::DS1ZA111::INSTR",
             "USB0::0x1AB1::0x04CE::DS1ZA222::INSTR")
    with pytest.raises(RuntimeError, match="Multiple Rigol USB scopes"):
        sc.find_usb_resource_string(rm)


def test_find_usb_serial_override_selects(monkeypatch):
    monkeypatch.setenv("RIGOL_USB_SERIAL", "DS1ZA222")
    rm = _rm("USB0::0x1AB1::0x04CE::DS1ZA111::INSTR",
             "USB0::0x1AB1::0x04CE::DS1ZA222::INSTR")
    assert sc.find_usb_resource_string(rm).endswith("DS1ZA222::INSTR")


def test_find_usb_serial_override_not_found(monkeypatch):
    monkeypatch.setenv("RIGOL_USB_SERIAL", "NOPE")
    rm = _rm("USB0::0x1AB1::0x04CE::DS1ZA111::INSTR")
    with pytest.raises(RuntimeError, match="RIGOL_USB_SERIAL='NOPE'"):
        sc.find_usb_resource_string(rm)


# --------------------------------------------------------------------------- backend selection

def _patch_rms(monkeypatch, mapping):
    """Patch pyvisa.ResourceManager(backend) -> mapping[backend]."""
    def factory(backend=None):
        if backend not in mapping:
            raise AssertionError(f"unexpected backend requested: {backend!r}")
        return mapping[backend]
    monkeypatch.setattr(sc.pyvisa, "ResourceManager", factory)


def test_open_usb_prefers_py_when_present(monkeypatch):
    winusb_scope = FakeScope(resource_name="USB0::6833::1230::DS1ZA1::0::INSTR")
    mapping = {
        "@py": FakeResourceManager(("USB0::6833::1230::DS1ZA1::0::INSTR",), scope=winusb_scope),
        "@ivi": FakeResourceManager(()),  # not consulted
    }
    _patch_rms(monkeypatch, mapping)
    rm, scope = sc._open_usb_scope()
    assert scope is winusb_scope
    assert sc.active_backend() == "@py"


def test_open_usb_falls_back_to_ivi(monkeypatch):
    ivi_scope = FakeScope(resource_name="USB0::0x1AB1::0x04CE::DS1ZA1::INSTR")
    mapping = {
        "@py": FakeResourceManager(()),  # WinUSB sees nothing
        "@ivi": FakeResourceManager(("USB0::0x1AB1::0x04CE::DS1ZA1::INSTR",), scope=ivi_scope),
    }
    _patch_rms(monkeypatch, mapping)
    rm, scope = sc._open_usb_scope()
    assert scope is ivi_scope
    assert sc.active_backend() == "@ivi"


def test_open_usb_none_found_raises_with_guidance(monkeypatch):
    mapping = {"@py": FakeResourceManager(()), "@ivi": FakeResourceManager(())}
    _patch_rms(monkeypatch, mapping)
    with pytest.raises(RuntimeError) as exc:
        sc._open_usb_scope()
    msg = str(exc.value)
    assert "WinUSB" in msg and "USBTMC" in msg  # actionable driver guidance


def test_open_usb_backend_unavailable_is_skipped(monkeypatch):
    ivi_scope = FakeScope()
    def factory(backend=None):
        if backend == "@py":
            raise OSError("libusb not found")
        return FakeResourceManager(("USB0::0x1AB1::0x04CE::DS1ZA1::INSTR",), scope=ivi_scope)
    monkeypatch.setattr(sc.pyvisa, "ResourceManager", factory)
    rm, scope = sc._open_usb_scope()
    assert scope is ivi_scope
    assert sc.active_backend() == "@ivi"


# --------------------------------------------------------------------------- get_scope

def test_get_scope_lan_configures_session(monkeypatch):
    monkeypatch.setenv("RIGOL_IP", "10.0.0.9")
    fake = FakeScope()
    _patch_rms(monkeypatch, {"@py": FakeResourceManager(scope=fake)})
    s = sc.get_scope()
    assert s is fake
    assert s.timeout == sc._LAN_TIMEOUT_MS
    assert s.write_termination == "\n" and s.read_termination == "\n"
    assert s.chunk_size == 1024 * 1024
    assert s.cleared == 1            # initial flush attempted
    # cached on second call
    assert sc.get_scope() is fake


def test_get_scope_usb_uses_usb_timeout(monkeypatch):
    monkeypatch.setenv("RIGOL_USB", "1")
    ivi_scope = FakeScope(resource_name="USB0::0x1AB1::0x04CE::DS1ZA1::INSTR")
    mapping = {
        "@py": FakeResourceManager(()),
        "@ivi": FakeResourceManager(("USB0::0x1AB1::0x04CE::DS1ZA1::INSTR",), scope=ivi_scope),
    }
    _patch_rms(monkeypatch, mapping)
    s = sc.get_scope()
    assert s is ivi_scope
    assert s.timeout == sc._USB_TIMEOUT_MS
    assert sc.active_backend() == "@ivi"


def test_get_scope_clear_unsupported_is_tolerated(monkeypatch):
    import pyvisa
    monkeypatch.setenv("RIGOL_IP", "10.0.0.9")
    fake = FakeScope()
    def boom():
        raise pyvisa.errors.VisaIOError(-1073807257)  # VI_ERROR_NSUP_OPER
    fake.clear = boom
    _patch_rms(monkeypatch, {"@py": FakeResourceManager(scope=fake)})
    # must not raise despite clear() being unsupported
    assert sc.get_scope() is fake


def test_invalidate_scope_closes_and_resets(monkeypatch):
    monkeypatch.setenv("RIGOL_IP", "10.0.0.9")
    fake = FakeScope()
    _patch_rms(monkeypatch, {"@py": FakeResourceManager(scope=fake)})
    sc.get_scope()
    sc.invalidate_scope()
    assert fake.closed is True
    assert sc._scope is None


# --------------------------------------------------------------------------- connection_info

def test_connection_info_lan_unconfigured():
    """No env vars set: report LAN as the default with the unset markers visible."""
    info = sc.connection_info()
    assert info["transport"] == "LAN"
    assert "unset" in info["RIGOL_USB"]
    assert "(unset)" in info["RIGOL_IP"]
    assert "RIGOL_IP not set" in info["lan_target"]
    assert info["session"] == "not yet opened"


def test_connection_info_lan_configured(monkeypatch):
    monkeypatch.setenv("RIGOL_IP", "192.168.1.47")
    info = sc.connection_info()
    assert info["transport"] == "LAN"
    assert info["RIGOL_IP"] == "192.168.1.47"
    # The full resource string is exposed so users can spot a wrong IP at a glance.
    assert info["lan_target"] == "TCPIP0::192.168.1.47::5555::SOCKET"


def test_connection_info_usb_configured(monkeypatch):
    monkeypatch.setenv("RIGOL_USB", "1")
    monkeypatch.setenv("RIGOL_IP", "192.168.1.47")  # kept but not used
    info = sc.connection_info()
    assert info["transport"] == "USB"
    assert info["RIGOL_USB"] == "1"
    # RIGOL_IP is still reported even when unused — surprising values stay visible.
    assert info["RIGOL_IP"] == "192.168.1.47"
    assert "any Rigol" in info["RIGOL_USB_SERIAL"]
    assert "lan_target" not in info  # USB mode hides LAN target


def test_connection_info_reflects_open_session(monkeypatch):
    """Once a session is open, the resource string is exposed for debugging."""
    monkeypatch.setenv("RIGOL_IP", "10.0.0.9")
    fake = FakeScope(resource_name="TCPIP0::10.0.0.9::5555::SOCKET")
    _patch_rms(monkeypatch, {"@py": FakeResourceManager(scope=fake)})
    sc.get_scope()  # populates the cached session
    info = sc.connection_info()
    assert info["session"] == "cached/open"
    assert info["resource"] == "TCPIP0::10.0.0.9::5555::SOCKET"


def test_connection_info_never_raises_on_any_env(monkeypatch):
    """connection_info is the diagnostic of last resort — it must survive any env state."""
    for var in ("RIGOL_USB", "RIGOL_IP", "RIGOL_USB_SERIAL"):
        monkeypatch.delenv(var, raising=False)
    sc.connection_info()  # no env vars at all
    monkeypatch.setenv("RIGOL_USB", "weird-value")
    monkeypatch.setenv("RIGOL_IP", "   ")
    sc.connection_info()  # garbled values too — still must not raise


# --------------------------------------------------------------------------- block-read framing

def test_parse_definite_block_header():
    assert sc._parse_definite_block_header(b"#9000016199ABC") == (11, 16199)
    assert sc._parse_definite_block_header(b"#800001024XY") == (10, 1024)


def test_parse_definite_block_header_bad():
    with pytest.raises(ValueError, match="TMC block header"):
        sc._parse_definite_block_header(b"NOTBLOCK")


def test_read_block_via_bytecount():
    payload = b"1.0,2.0,3.0"
    s = FakeScope(read_buffer=make_block(payload))
    assert sc._read_block_via_bytecount(s) == payload


def test_read_block_via_message_returns_payload_and_restores_termination():
    payload = bytes(range(256))  # contains 0x0A, would break termchar-based reads
    seen = {}

    class TermRecordingScope(FakeScope):
        def read_raw(self, size=None):
            seen["term_during_read"] = self.read_termination
            return super().read_raw(size)

    s = TermRecordingScope(read_buffer=make_block(payload))
    s.read_termination = "\n"
    assert sc._read_block_via_message(s) == payload
    assert seen["term_during_read"] is None      # disabled during the raw read
    assert s.read_termination == "\n"            # restored afterwards


def test_read_definite_block_dispatches_on_backend(monkeypatch):
    payload = b"\x89PNG\n\x0a binary"
    # @ivi -> message/read_raw path
    monkeypatch.setattr(sc, "_usb_backend_hint", "@ivi")
    s_ivi = FakeScope(read_buffer=make_block(payload))
    assert sc._read_definite_block(s_ivi) == payload
    # @py -> byte-count path
    monkeypatch.setattr(sc, "_usb_backend_hint", "@py")
    s_py = FakeScope(read_buffer=make_block(payload))
    assert sc._read_definite_block(s_py) == payload


# --------------------------------------------------------------------------- screenshot / waveform

def test_screenshot_png_bytecount(monkeypatch):
    png = b"\x89PNG\r\n\x1a\n" + bytes(range(64))
    monkeypatch.setattr(sc, "_usb_backend_hint", "@py")
    s = FakeScope(read_buffer=make_block(png))
    assert sc.screenshot_png(s) == png
    assert s.written == [":DISPlay:DATA? ON,OFF,PNG"]


def test_screenshot_png_message(monkeypatch):
    png = b"\x89PNG\r\n\x1a\n" + bytes(range(64))
    monkeypatch.setattr(sc, "_usb_backend_hint", "@ivi")
    s = FakeScope(read_buffer=make_block(png))
    assert sc.screenshot_png(s) == png


def test_get_waveform_parses_block_and_stats():
    # PRE fields: [fmt,type,points,count,x_inc,x_origin,x_ref,...]
    pre = "0,0,5,1,1.000000e-06,-2.000000e-06,0,1,0,0"
    block = make_block(b"-1.0,0.0,2.5,1.0,-0.5")
    s = FakeScope(
        responses={":WAV:PRE?": pre, ":CHAN1:SCAL?": "1.000000e+00", ":CHAN1:OFFS?": "0"},
        read_buffer=block,
    )
    out = sc.get_waveform(s, "chan1")
    assert out["channel"] == "CHAN1"
    assert out["points"] == 5
    assert out["vmin_v"] == -1.0
    assert out["vmax_v"] == 2.5
    assert out["voltages_v"] == [-1.0, 0.0, 2.5, 1.0, -0.5]
    assert out["time_increment_s"] == 1e-6
    # times derived from x_origin + (i - x_ref) * x_inc
    assert out["time_start_s"] == pytest.approx(-2e-6)
    # vertical scale/offset captured for the analyser's noise-floor check
    assert out["y_scale_v_per_div"] == 1.0
    assert out["y_offset_v"] == 0.0


def test_get_waveform_tolerates_missing_vertical_scale():
    # If the scope doesn't answer :SCAL?/:OFFS?, the fields degrade to None (no crash).
    pre = "0,0,5,1,1.000000e-06,-2.000000e-06,0,1,0,0"
    block = make_block(b"-1.0,0.0,2.5,1.0,-0.5")
    s = FakeScope(responses={":WAV:PRE?": pre}, read_buffer=block)
    out = sc.get_waveform(s, "chan1")
    assert out["y_scale_v_per_div"] is None
    assert out["y_offset_v"] is None


# --------------------------------------------------------------------------- cursor math

def test_time_to_screen_x_roundtrip():
    s = FakeScope(responses={":TIM:SCAL?": "1.000000e-03", ":TIM:OFFS?": "0"})
    # center of screen maps to offset (=0) time
    assert sc.time_to_screen_x(s, 0.0) == sc._SCREEN_CENTER
    # round-trip a representative time
    t = 0.002
    x = sc.time_to_screen_x(s, t)
    assert sc.screen_x_to_time(s, x) == pytest.approx(t, abs=1e-4)


def test_time_to_screen_x_clamps_to_range():
    s = FakeScope(responses={":TIM:SCAL?": "1.000000e-03", ":TIM:OFFS?": "0"})
    assert sc.time_to_screen_x(s, +10.0) == 594   # far right clamps
    assert sc.time_to_screen_x(s, -10.0) == 5     # far left clamps


# --------------------------------------------------------------------------- check_scpi_error

@pytest.mark.parametrize("resp,expected", [
    ("0", None),
    ('0,"No error"', None),
    ('-113,"Undefined header"', '-113,"Undefined header"'),
])
def test_check_scpi_error(resp, expected):
    s = FakeScope(responses={":SYSTem:ERRor?": resp})
    assert sc.check_scpi_error(s) == expected


# --------------------------------------------------------------------------- measure validation

def test_measure_valid():
    s = FakeScope(responses={":MEASure:ITEM?": "1.234000e+00"})
    assert sc.measure(s, "chan1", "vpp") == "1.234000e+00"
    assert ":MEASure:ITEM VPP,CHAN1" in s.written


def test_measure_rejects_two_source_item():
    s = FakeScope()
    with pytest.raises(ValueError, match="two sources"):
        sc.measure(s, "CHAN1", "RDELAY")


def test_measure_rejects_unknown_item():
    s = FakeScope()
    with pytest.raises(ValueError, match="Unknown item"):
        sc.measure(s, "CHAN1", "BOGUS")


def test_measure_between_rejects_single_source_item():
    s = FakeScope()
    with pytest.raises(ValueError, match="not a two-source"):
        sc.measure_between(s, "CHAN1", "CHAN2", "VPP")


# --------------------------------------------------------------------------- autoscale timeout guard

def test_autoscale_raises_timeout_during_call_and_restores():
    seen = {}

    class TimeoutRecordingScope(FakeScope):
        def query(self, cmd):
            if cmd.startswith(":AUToscale"):
                seen["timeout_during"] = self.timeout
            return super().query(cmd)

    s = TimeoutRecordingScope(responses={
        ":AUToscale;*OPC?": "1",
        ":SYSTem:ERRor?": "0",
    })
    s.timeout = sc._USB_TIMEOUT_MS  # the short USB default
    sc.autoscale(s)
    assert seen["timeout_during"] == sc._SLOW_OP_TIMEOUT_MS   # bumped for the slow op
    assert s.timeout == sc._USB_TIMEOUT_MS                    # restored afterwards
