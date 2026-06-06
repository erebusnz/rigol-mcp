"""VISA connection and SCPI helpers for Rigol DS1000Z."""

import os
import pyvisa

# Screen geometry constants (DS1000Z series)
_SCREEN_DIVISIONS = 12
_POINTS_PER_DIV = 50          # empirically confirmed: 600 points across 12 divisions
_SCREEN_POINTS = _SCREEN_DIVISIONS * _POINTS_PER_DIV  # 600
_SCREEN_CENTER = _SCREEN_POINTS // 2                  # 300

# Rigol Technologies USB vendor ID — used to identify the scope among USB devices.
_RIGOL_USB_VID = 0x1AB1

# Per-transport default I/O timeout (ms). USB uses a shorter timeout so the rare USBTMC
# read that hangs recovers quickly: pyvisa-py sends the USBTMC Bulk-IN abort on read
# failure (pyvisa-py PR #179) and our caller reconnects, so a stuck read costs ~10 s
# instead of 30 s. LAN keeps the longer timeout. Genuinely slow operations (autoscale)
# raise the timeout locally — see autoscale().
_USB_TIMEOUT_MS = 10_000
_LAN_TIMEOUT_MS = 30_000
# Headroom for blocking operations such as :AUToscale;*OPC? (measured ~4 s over USB).
_SLOW_OP_TIMEOUT_MS = 30_000

# Module-level cached connection
_rm: pyvisa.ResourceManager | None = None
_scope: pyvisa.resources.Resource | None = None


def _usb_preferred() -> bool:
    """True if RIGOL_USB is set to a truthy value, requesting USB transport."""
    return os.environ.get("RIGOL_USB", "").strip().lower() not in ("", "0", "false", "no", "off")


def usb_in_use() -> bool:
    """True when the active transport is USB (USBTMC) rather than LAN."""
    return _usb_preferred()


def active_backend() -> str | None:
    """The pyvisa backend the live USB connection was opened on ("@py" or "@ivi"),
    or None if no USB connection has been established yet."""
    return _usb_backend_hint


def get_lan_resource_string() -> str:
    ip = os.environ.get("RIGOL_IP")
    if not ip:
        raise RuntimeError("RIGOL_IP environment variable is not set")
    return f"TCPIP0::{ip}::5555::SOCKET"


def find_usb_resource_string(rm: pyvisa.ResourceManager) -> str:
    """Find a Rigol USBTMC resource string.

    Honours RIGOL_USB_SERIAL when set to disambiguate between multiple scopes.
    Raises RuntimeError if no matching Rigol USB device is present.
    """
    wanted_serial = os.environ.get("RIGOL_USB_SERIAL", "").strip()
    all_resources = rm.list_resources()
    usb_resources = [r for r in all_resources if r.upper().startswith("USB")]

    rigol: list[tuple[str, str]] = []  # (resource_string, serial)
    for r in usb_resources:
        # USB resource format varies by backend:
        #   pyvisa-py : USB[board]::<vid_dec>::<pid_dec>::<serial>::<interface>::INSTR
        #   NI-VISA   : USB[board]::0x<vid_hex>::0x<pid_hex>::<serial>::INSTR
        # In both, field 1 is the vendor id and field 3 is the serial.
        parts = r.split("::")
        if len(parts) < 5:
            continue
        try:
            # base 0 parses decimal "6833" and "0x1AB1" alike.
            vid = int(parts[1], 0)
        except ValueError:
            continue
        if vid == _RIGOL_USB_VID:
            rigol.append((r, parts[3]))

    if wanted_serial:
        for resource, serial in rigol:
            if serial == wanted_serial:
                return resource
        found = [s for _, s in rigol] or ["none"]
        raise RuntimeError(
            f"RIGOL_USB_SERIAL='{wanted_serial}' requested but no matching Rigol USB "
            f"scope was found. Detected Rigol serials: {found}"
        )

    if not rigol:
        detected = ", ".join(usb_resources) if usb_resources else "none"
        raise RuntimeError(
            "RIGOL_USB is set but no Rigol USB scope (vendor 0x1AB1) was found. "
            f"USB resources detected: {detected}"
        )
    if len(rigol) > 1:
        serials = [s for _, s in rigol]
        raise RuntimeError(
            f"Multiple Rigol USB scopes found (serials: {serials}). "
            "Set RIGOL_USB_SERIAL to choose one."
        )
    return rigol[0][0]


# USB backends tried in priority order. The right one depends on which driver is bound
# to the scope's USB interface, so we auto-detect rather than assume:
#   "@py"  -> pyvisa-py + pyusb; works when the interface is bound to WinUSB (e.g. Zadig)
#   "@ivi" -> NI-VISA / IVI VISA (visa32.dll); works with the native USBTMC driver
_USB_BACKENDS = ("@py", "@ivi")
_usb_backend_hint: str | None = None  # last backend that worked; tried first on reconnect


def _close_rm(rm: pyvisa.ResourceManager) -> None:
    try:
        rm.close()
    except Exception:
        pass


def _open_usb_scope() -> tuple[pyvisa.ResourceManager, pyvisa.resources.Resource]:
    """Open the Rigol USB scope, auto-selecting the backend that can see it.

    A scope whose USB interface is bound to WinUSB is reachable via pyvisa-py (@py);
    a scope on the native USBTMC driver is reachable via an installed NI-VISA backend
    (@ivi). Each backend only sees devices bound to its kind of driver, so we try them
    in turn and use the first that finds the scope. Returns (resource_manager, resource).
    """
    global _usb_backend_hint
    # Try the last-known-good backend first, then the rest (dedup, preserve order).
    order = list(dict.fromkeys(
        ([_usb_backend_hint] if _usb_backend_hint else []) + list(_USB_BACKENDS)
    ))
    problems: list[str] = []
    for backend in order:
        try:
            rm = pyvisa.ResourceManager(backend)
        except Exception as exc:  # backend or its VISA library is not installed
            problems.append(f"{backend}: backend unavailable ({type(exc).__name__}: {exc})")
            continue
        try:
            resource = find_usb_resource_string(rm)
        except RuntimeError as exc:           # backend works but scope not found on it
            problems.append(f"{backend}: {exc}")
            _close_rm(rm)
            continue
        except Exception as exc:              # enumeration itself failed
            problems.append(f"{backend}: enumeration failed ({type(exc).__name__}: {exc})")
            _close_rm(rm)
            continue
        scope = rm.open_resource(resource)
        _usb_backend_hint = backend
        return rm, scope

    raise RuntimeError(
        "RIGOL_USB is set but no Rigol USB scope could be opened on any backend:\n  "
        + "\n  ".join(problems)
        + "\n\nThe scope's USB interface must be bound to a supported driver:\n"
        "  - WinUSB (install via Zadig) -> handled by the built-in pyvisa-py backend\n"
        "  - USBTMC (native driver)     -> requires NI-VISA / IVI VISA (visa32.dll) installed"
    )


def get_scope() -> pyvisa.resources.Resource:
    """Return cached VISA connection, opening it if needed."""
    global _rm, _scope
    if _scope is None:
        if usb_in_use():
            # USB: auto-detect WinUSB (@py) vs the native USBTMC driver (@ivi/NI-VISA).
            _rm, _scope = _open_usb_scope()
        else:
            # LAN: raw socket over the pure-Python backend (no NI-VISA dependency).
            _rm = pyvisa.ResourceManager("@py")
            _scope = _rm.open_resource(get_lan_resource_string())
        _scope.timeout = _USB_TIMEOUT_MS if usb_in_use() else _LAN_TIMEOUT_MS
        # chunk_size has no measurable effect on USB read reliability here (block reads
        # use exact byte counts), and a large value keeps the LAN socket fast.
        _scope.chunk_size = 1024 * 1024
        _scope.write_termination = "\n"
        _scope.read_termination = "\n"
        try:
            _scope.clear()  # flush any stale data left in the receive buffer
        except pyvisa.errors.VisaIOError:
            # USBTMC under pyvisa-py does not support the clear operation; the
            # initial flush is only a best-effort cleanup, so skip it on that backend.
            pass
    return _scope


def invalidate_scope() -> None:
    """Close and discard the cached connection so the next call reconnects."""
    global _scope
    if _scope is not None:
        try:
            _scope.clear()
        except Exception:
            pass
        try:
            _scope.close()
        except Exception:
            pass
        _scope = None


def check_scpi_error(scope: pyvisa.resources.Resource) -> str | None:
    """Query the SCPI error queue. Returns error string if an error is present, None if clear."""
    response = scope.query(":SYSTem:ERRor?").strip()
    # No error returns '0' or '0,"No error"'
    if response == "0" or response.startswith("0,"):
        return None
    return response


def screen_x_to_time(scope: pyvisa.resources.Resource, screen_x: int) -> float:
    """Convert a screen X pixel position back to time in seconds."""
    scale = float(scope.query(":TIM:SCAL?").strip())
    offset = float(scope.query(":TIM:OFFS?").strip())
    return (screen_x - _SCREEN_CENTER) * scale / _POINTS_PER_DIV + offset


def time_to_screen_x(scope: pyvisa.resources.Resource, time_s: float) -> int:
    """Convert a time value (seconds) to a screen X position integer for cursor commands.

    Formula derived from DS1000Z geometry (50 pts/div, 12 div, 600 total):
        screen_x = (time - offset) * points_per_div / scale + screen_center
    """
    scale = float(scope.query(":TIM:SCAL?").strip())
    offset = float(scope.query(":TIM:OFFS?").strip())
    screen_x = int(round((time_s - offset) * _POINTS_PER_DIV / scale + _SCREEN_CENTER))
    return max(5, min(594, screen_x))  # DS1000Z cursor range per manual: 5–594


def get_cursor_mode(scope: pyvisa.resources.Resource) -> str:
    return scope.query(":CURSor:MODE?").strip()


def set_cursor_mode(scope: pyvisa.resources.Resource, mode: str) -> None:
    """Set cursor mode: OFF, MANUAL, TRACK."""
    scope.write(f":CURSor:MODE {mode.upper()}")
    if err := check_scpi_error(scope):
        raise RuntimeError(f"SCPI error after :CURSor:MODE: {err}")


def set_cursor_positions(
    scope: pyvisa.resources.Resource,
    mode: str,
    ax: float | None = None,
    bx: float | None = None,
) -> None:
    """Set cursor A and/or B X positions (in seconds). mode: MANUAL or TRACK.
    Time values are automatically converted to screen pixel positions."""
    prefix = ":CURSor:TRACk" if mode.upper() in ("TRACK", "TRAC") else ":CURSor:MANual"
    if ax is not None:
        screen_x = time_to_screen_x(scope, ax)
        scope.write(f"{prefix}:AX {screen_x}")
        if err := check_scpi_error(scope):
            raise RuntimeError(f"SCPI error after {prefix}:AX: {err}")
    if bx is not None:
        screen_x = time_to_screen_x(scope, bx)
        scope.write(f"{prefix}:BX {screen_x}")
        if err := check_scpi_error(scope):
            raise RuntimeError(f"SCPI error after {prefix}:BX: {err}")


def get_cursor_values(scope: pyvisa.resources.Resource) -> dict:
    """Read current cursor mode and all available readouts. AX_s/BX_s are in seconds."""
    mode = scope.query(":CURSor:MODE?").strip()
    result: dict = {"mode": mode}
    if mode in ("MANUAL", "MAN"):
        p = ":CURSor:MANual"
        ax_px = int(float(scope.query(f"{p}:AX?").strip()))
        bx_px = int(float(scope.query(f"{p}:BX?").strip()))
        result.update({
            "AX_s":        screen_x_to_time(scope, ax_px),
            "BX_s":        screen_x_to_time(scope, bx_px),
            "AX_value":    scope.query(f"{p}:AXValue?").strip(),
            "BX_value":    scope.query(f"{p}:BXValue?").strip(),
            "delta_x":     scope.query(f"{p}:XDELta?").strip(),
            "inv_delta_x": scope.query(f"{p}:IXDELta?").strip(),
        })
    elif mode in ("TRACK", "TRAC"):
        p = ":CURSor:TRACk"
        ax_px = int(float(scope.query(f"{p}:AX?").strip()))
        bx_px = int(float(scope.query(f"{p}:BX?").strip()))
        result.update({
            "AX_s":        screen_x_to_time(scope, ax_px),
            "BX_s":        screen_x_to_time(scope, bx_px),
            "AX_value":    scope.query(f"{p}:AXValue?").strip(),
            "AY_value":    scope.query(f"{p}:AYValue?").strip(),
            "BX_value":    scope.query(f"{p}:BXValue?").strip(),
            "BY_value":    scope.query(f"{p}:BYValue?").strip(),
            "delta_x":     scope.query(f"{p}:XDELta?").strip(),
            "delta_y":     scope.query(f"{p}:YDELta?").strip(),
            "inv_delta_x": scope.query(f"{p}:IXDELta?").strip(),
        })
    return result


def send_raw(scope: pyvisa.resources.Resource, command: str) -> str:
    """Send an arbitrary SCPI command; returns response for queries, empty string otherwise.
    Automatically checks the error queue after writes and raises on SCPI errors."""
    if command.strip().endswith("?"):
        return scope.query(command).strip()
    scope.write(command)
    if err := check_scpi_error(scope):
        raise RuntimeError(f"SCPI error after '{command}': {err}")
    return ""


def run(scope: pyvisa.resources.Resource) -> str:
    """Start continuous acquisition. Returns trigger status."""
    scope.write(":RUN")
    if err := check_scpi_error(scope):
        raise RuntimeError(f"SCPI error after :RUN: {err}")
    return scope.query(":TRIGger:STATus?").strip()


def stop(scope: pyvisa.resources.Resource) -> str:
    """Stop acquisition and freeze the display. Returns trigger status."""
    scope.write(":STOP")
    if err := check_scpi_error(scope):
        raise RuntimeError(f"SCPI error after :STOP: {err}")
    return scope.query(":TRIGger:STATus?").strip()


def single(scope: pyvisa.resources.Resource) -> str:
    """Capture a single acquisition then stop. Returns trigger status."""
    scope.write(":SINGle")
    if err := check_scpi_error(scope):
        raise RuntimeError(f"SCPI error after :SINGle: {err}")
    return scope.query(":TRIGger:STATus?").strip()


def autoscale(scope: pyvisa.resources.Resource) -> None:
    """Run the scope's auto-setup (timebase, vertical scale, trigger)."""
    # Autoscale blocks for several seconds; raise the timeout for this one call so the
    # shorter USB default doesn't abort the :AUToscale;*OPC? wait, then restore it.
    prev_timeout = scope.timeout
    scope.timeout = max(prev_timeout, _SLOW_OP_TIMEOUT_MS)
    try:
        scope.query(":AUToscale;*OPC?")  # chain OPC? so we block until autoscale completes
    finally:
        scope.timeout = prev_timeout
    if err := check_scpi_error(scope):
        raise RuntimeError(f"SCPI error after :AUToscale: {err}")


def idn(scope: pyvisa.resources.Resource) -> str:
    """Return the instrument identification string."""
    return scope.query("*IDN?").strip()


def connection_info() -> dict:
    """Report connection configuration and current session state.

    Performs NO device I/O and never raises — safe to call before, during, or after a
    connection failure. Use to build diagnostic output that surfaces what the server was
    *trying* to do, separately from whether the device responded. Without this, every
    failure surfaces as the same opaque ``VI_ERROR_TMO`` regardless of whether the server
    was configured for LAN-to-an-unreachable-IP or USB-with-a-wedged-endpoint.
    """
    rigol_usb    = os.environ.get("RIGOL_USB", "").strip()
    rigol_ip     = os.environ.get("RIGOL_IP", "").strip()
    rigol_serial = os.environ.get("RIGOL_USB_SERIAL", "").strip()
    info: dict = {
        "transport": "USB" if usb_in_use() else "LAN",
        "RIGOL_USB": rigol_usb or "(unset → LAN)",
        "RIGOL_IP":  rigol_ip or "(unset)",
    }
    if usb_in_use():
        info["RIGOL_USB_SERIAL"] = rigol_serial or "(unset → any Rigol scope)"
        info["backend_hint"]     = _usb_backend_hint or "(none yet — will auto-detect)"
    else:
        info["lan_target"] = (f"TCPIP0::{rigol_ip}::5555::SOCKET"
                              if rigol_ip else "(RIGOL_IP not set)")
    if _scope is not None:
        info["session"]  = "cached/open"
        info["resource"] = getattr(_scope, "resource_name", "?")
    else:
        info["session"] = "not yet opened"
    return info


# All measurement items supported by DS1000Z :MEASure:ITEM
MEASURE_ITEMS = frozenset({
    # Voltage
    "VMAX", "VMIN", "VPP", "VTOP", "VBASE", "VAMP", "VAVG", "VRMS",
    "OVERSHOOT", "PRESHOOT", "MAREA", "MPAREA",
    "VUPPER", "VMID", "VLOWER", "VARIANCE", "PVRMS",
    # Time (single-source)
    "PERIOD", "FREQUENCY", "RTIME", "FTIME",
    "PWIDTH", "NWIDTH", "PDUTY", "NDUTY",
    "TVMAX", "TVMIN", "PSLEWRATE", "NSLEWRATE",
    "PPULSES", "NPULSES", "PEDGES", "NEDGES",
})

# Two-source measurements — require both source1 and source2
MEASURE_ITEMS_TWO_SOURCE = frozenset({
    "RDELAY", "FDELAY", "RPHASE", "FPHASE",
})


def measure(scope: pyvisa.resources.Resource, channel: str, item: str) -> str:
    """Query a single-source built-in measurement. Returns the raw value string."""
    ch = channel.upper()
    it = item.upper()
    if it in MEASURE_ITEMS_TWO_SOURCE:
        raise ValueError(f"'{item}' requires two sources — use measure_between()")
    if it not in MEASURE_ITEMS:
        raise ValueError(f"Unknown item '{item}'. Valid: {sorted(MEASURE_ITEMS)}")
    scope.write(f":MEASure:ITEM {it},{ch}")
    return scope.query(f":MEASure:ITEM? {it},{ch}").strip()


def measure_between(
    scope: pyvisa.resources.Resource,
    source1: str,
    source2: str,
    item: str,
) -> str:
    """Query a two-source measurement (delay or phase) between two channels.

    item: RDELAY (rising-edge delay), FDELAY (falling-edge delay),
          RPHASE (rising-edge phase), FPHASE (falling-edge phase).
    source1/source2: CHAN1–CHAN4.
    Returns the raw value string (delay in seconds, phase in degrees).
    """
    it = item.upper()
    if it not in MEASURE_ITEMS_TWO_SOURCE:
        raise ValueError(
            f"'{item}' is not a two-source item. Valid: {sorted(MEASURE_ITEMS_TWO_SOURCE)}"
        )
    s1 = source1.upper()
    s2 = source2.upper()
    scope.write(f":MEASure:ITEM {it},{s1},{s2}")
    return scope.query(f":MEASure:ITEM? {it},{s1},{s2}").strip()


def get_scope_state(scope: pyvisa.resources.Resource) -> dict:
    """Return a snapshot of the scope's current configuration."""
    state: dict = {}

    state["timebase"] = {
        "scale_s_div": scope.query(":TIM:SCAL?").strip(),
        "offset_s":    scope.query(":TIM:OFFS?").strip(),
        "mode":        scope.query(":TIM:MODE?").strip(),
    }

    state["channels"] = {}
    for i in range(1, 5):
        ch = f"CHAN{i}"
        state["channels"][ch] = {
            "display":    scope.query(f":{ch}:DISP?").strip(),
            "scale_v_div": scope.query(f":{ch}:SCAL?").strip(),
            "offset_v":   scope.query(f":{ch}:OFFS?").strip(),
            "coupling":   scope.query(f":{ch}:COUP?").strip(),
            "probe":      scope.query(f":{ch}:PROB?").strip(),
        }

    trig_mode = scope.query(":TRIGger:MODE?").strip()
    state["trigger"] = {
        "mode":   trig_mode,
        "status": scope.query(":TRIGger:STATus?").strip(),
    }
    if trig_mode.upper() in ("EDGE", "EDGMODE"):
        state["trigger"].update({
            "source":  scope.query(":TRIGger:EDGE:SOURce?").strip(),
            "slope":   scope.query(":TRIGger:EDGE:SLOPe?").strip(),
            "level_v": scope.query(":TRIGger:EDGE:LEVel?").strip(),
        })

    return state


def set_channel(
    scope: pyvisa.resources.Resource,
    channel: str,
    display: bool | None = None,
    scale: float | None = None,
    offset: float | None = None,
    coupling: str | None = None,
    probe: float | None = None,
) -> None:
    """Configure a channel. Only specified parameters are changed."""
    ch = channel.upper()
    if display is not None:
        scope.write(f":{ch}:DISP {'ON' if display else 'OFF'}")
    if scale is not None:
        scope.write(f":{ch}:SCAL {scale}")
    if offset is not None:
        scope.write(f":{ch}:OFFS {offset}")
    if coupling is not None:
        scope.write(f":{ch}:COUP {coupling.upper()}")
    if probe is not None:
        scope.write(f":{ch}:PROB {probe}")
    if err := check_scpi_error(scope):
        raise RuntimeError(f"SCPI error in set_channel({ch}): {err}")


def set_timebase(
    scope: pyvisa.resources.Resource,
    scale: float | None = None,
    offset: float | None = None,
) -> None:
    """Set timebase scale (s/div) and/or offset (s)."""
    if scale is not None:
        scope.write(f":TIM:SCAL {scale}")
    if offset is not None:
        scope.write(f":TIM:OFFS {offset}")
    if err := check_scpi_error(scope):
        raise RuntimeError(f"SCPI error in set_timebase: {err}")


def set_trigger(
    scope: pyvisa.resources.Resource,
    source: str | None = None,
    slope: str | None = None,
    level: float | None = None,
) -> None:
    """Configure edge trigger. source: CHAN1–CHAN4, EXT. slope: POS, NEG, RFAL."""
    scope.write(":TRIGger:MODE EDGE")
    if source is not None:
        scope.write(f":TRIGger:EDGE:SOURce {source.upper()}")
    if slope is not None:
        scope.write(f":TRIGger:EDGE:SLOPe {slope.upper()}")
    if level is not None:
        scope.write(f":TRIGger:EDGE:LEVel {level}")
    if err := check_scpi_error(scope):
        raise RuntimeError(f"SCPI error in set_trigger: {err}")


def get_waveform(scope: pyvisa.resources.Resource, channel: str) -> dict:
    """Download waveform data for a channel (screen buffer, NORM mode).

    Returns time/voltage arrays plus summary statistics.
    Stop or single-trigger the scope first for consistent data.
    """
    ch = channel.upper()
    scope.write(f":WAV:SOUR {ch}")
    scope.write(":WAV:MODE NORM")
    scope.write(":WAV:FORM ASC")

    pre_str = scope.query(":WAV:PRE?").strip()
    pre = pre_str.split(",")
    x_inc   = float(pre[4])
    x_origin = float(pre[5])
    x_ref   = float(pre[6])

    # Read the definite-length data block (backend-aware: native message read on NI-VISA,
    # exact byte count on pyvisa-py — see _read_definite_block).
    scope.write(":WAV:DATA?")
    data_str = _read_definite_block(scope).decode("ascii")
    voltages = [float(v) for v in data_str.split(",")]
    n = len(voltages)
    times = [x_origin + (i - x_ref) * x_inc for i in range(n)]

    # Vertical scale/offset let the analyser judge amplitude against the full-screen range
    # (8 vertical divisions) and flag noise-floor captures. Best-effort: if the scope does
    # not answer, the analysis degrades gracefully to amplitude-only reporting.
    try:
        y_scale = float(scope.query(f":{ch}:SCAL?"))
    except Exception:
        y_scale = None
    try:
        y_offset = float(scope.query(f":{ch}:OFFS?"))
    except Exception:
        y_offset = None

    return {
        "channel":        ch,
        "points":         n,
        "time_increment_s": x_inc,
        "time_start_s":   times[0] if times else 0.0,
        "time_end_s":     times[-1] if times else 0.0,
        "vmin_v":         min(voltages),
        "vmax_v":         max(voltages),
        "vmean_v":        sum(voltages) / n if n else 0.0,
        "y_scale_v_per_div": y_scale,
        "y_offset_v":     y_offset,
        "times_s":        times,
        "voltages_v":     voltages,
    }


def _read_definite_block(scope: pyvisa.resources.Resource) -> bytes:
    """Read an IEEE 488.2 definite-length block response (``#N<length><data>\\n``).

    The safe way to read this depends on the backend, so dispatch on it. Issue the query
    (e.g. ``:WAV:DATA?``) with scope.write() before calling this. Returns the payload.

    NI-VISA (@ivi) frames every USBTMC read as a message transaction terminated by an EOM
    bit. Reading raw byte counts that don't land on the device's message boundary leaves a
    transaction half-complete with no abort, which desynchronises and can *wedge the scope*
    — so on @ivi we read the whole message natively (read_raw) and slice the payload from
    the parsed header. pyvisa-py (@py, used for WinUSB and LAN sockets) is the opposite:
    read_raw()/query time out on large blocks there, so read by exact byte count instead.
    """
    if active_backend() == "@ivi":
        return _read_block_via_message(scope)
    return _read_block_via_bytecount(scope)


def _parse_definite_block_header(raw: bytes) -> tuple[int, int]:
    """Return (data_start_index, data_length) for a ``#N<length>...`` block prefix."""
    if raw[0:1] != b"#":
        raise ValueError(f"Expected TMC block header starting with '#', got {raw[:8]!r}")
    n = int(raw[1:2])
    data_length = int(raw[2:2 + n])
    return 2 + n, data_length


def _read_block_via_message(scope: pyvisa.resources.Resource) -> bytes:
    """Read the block using the backend's native message framing (NI-VISA / USBTMC).

    read_raw() reads the complete USBTMC message (it stops on the device's EOM, not on a
    byte count), so the transaction always completes cleanly. Termination matching is
    disabled for the read because the binary payload can contain 0x0A bytes.
    """
    saved_read_termination = scope.read_termination
    scope.read_termination = None  # do not stop at a 0x0A inside the block
    try:
        raw = scope.read_raw()
    finally:
        scope.read_termination = saved_read_termination
    start, data_length = _parse_definite_block_header(raw)
    data = raw[start:start + data_length]
    if len(data) != data_length:
        raise ValueError(
            f"Truncated block: header declared {data_length} bytes, received {len(data)}"
        )
    return data


def _read_block_via_bytecount(scope: pyvisa.resources.Resource) -> bytes:
    """Read the block by exact byte count (required for the pyvisa-py backend).

    PNG data contains 0x0A bytes and large blocks over pyvisa-py USBTMC time out on
    termination/EOM-based reads, so read precisely the declared number of bytes.
    """
    prefix = scope.read_bytes(2)  # '#' + digit-count byte
    if prefix[0:1] != b"#":
        raise ValueError(f"Expected TMC block header starting with '#', got {prefix!r}")
    n = int(prefix[1:2])
    data_length = int(scope.read_bytes(n))
    raw = scope.read_bytes(data_length + 1)  # +1 for the trailing newline
    if raw[-1:] != b'\n':
        raise ValueError(f"Expected \\n after definite-length block, got {raw[-1:]!r}")
    return raw[:-1]


def screenshot_png(scope: pyvisa.resources.Resource) -> bytes:
    """Return raw PNG bytes from the scope display.

    Requests PNG directly from the scope (DS1000Z supports BMP24/BMP8/PNG/JPEG/TIFF).
    Strips the IEEE 488.2 TMC block header (#NXXXXXXXXX) and trailing \\n.
    """
    scope.write(":DISPlay:DATA? ON,OFF,PNG")
    return _read_definite_block(scope)
