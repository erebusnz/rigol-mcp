"""VISA connection and SCPI helpers for Rigol DS1000Z."""

import os
import pyvisa

# Screen geometry constants (DS1000Z series)
_SCREEN_DIVISIONS = 12
_POINTS_PER_DIV = 50          # empirically confirmed: 600 points across 12 divisions
_SCREEN_POINTS = _SCREEN_DIVISIONS * _POINTS_PER_DIV  # 600
_SCREEN_CENTER = _SCREEN_POINTS // 2                  # 300

# Module-level cached connection
_rm: pyvisa.ResourceManager | None = None
_scope: pyvisa.resources.Resource | None = None


def get_resource_string() -> str:
    ip = os.environ.get("RIGOL_IP")
    if not ip:
        raise RuntimeError("RIGOL_IP environment variable is not set")
    return f"TCPIP0::{ip}::5555::SOCKET"


def get_scope() -> pyvisa.resources.Resource:
    """Return cached VISA connection, opening it if needed."""
    global _rm, _scope
    if _scope is None:
        _rm = pyvisa.ResourceManager()
        _scope = _rm.open_resource(get_resource_string())
        _scope.timeout = 30_000
        _scope.chunk_size = 1024 * 1024
        _scope.write_termination = "\n"
        _scope.read_termination = "\n"
        _scope.clear()  # flush any stale data left in the TCP receive buffer
    return _scope


def invalidate_scope() -> None:
    """Close and discard the cached connection so the next call reconnects."""
    global _scope
    if _scope is not None:
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
    scope.query(":AUToscale;*OPC?")  # chain OPC? so we block until autoscale completes
    if err := check_scpi_error(scope):
        raise RuntimeError(f"SCPI error after :AUToscale: {err}")


def idn(scope: pyvisa.resources.Resource) -> str:
    """Return the instrument identification string."""
    return scope.query("*IDN?").strip()


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

    data_str = scope.query(":WAV:DATA?").strip()
    # Strip SCPI definite-length block header (#NXXXXXXXXX...) if present
    if data_str.startswith("#"):
        n_digits = int(data_str[1])
        data_str = data_str[2 + n_digits:]
    voltages = [float(v) for v in data_str.split(",")]
    n = len(voltages)
    times = [x_origin + (i - x_ref) * x_inc for i in range(n)]

    return {
        "channel":        ch,
        "points":         n,
        "time_increment_s": x_inc,
        "time_start_s":   times[0] if times else 0.0,
        "time_end_s":     times[-1] if times else 0.0,
        "vmin_v":         min(voltages),
        "vmax_v":         max(voltages),
        "vmean_v":        sum(voltages) / n if n else 0.0,
        "times_s":        times,
        "voltages_v":     voltages,
    }


def screenshot_png(scope: pyvisa.resources.Resource) -> bytes:
    """Return raw PNG bytes from the scope display.

    Requests PNG directly from the scope (DS1000Z supports BMP24/BMP8/PNG/JPEG/TIFF).
    Strips the IEEE 488.2 TMC block header (#NXXXXXXXXX) and trailing \\n.
    """
    scope.write(":DISPlay:DATA? ON,OFF,PNG")
    # Read the TMC block header: #<n><length_digits><data>
    # Must read by exact byte count — read_raw() stops at 0x0A which appears in PNG data
    prefix = scope.read_bytes(2)  # '#' + digit-count byte
    if prefix[0:1] != b"#":
        raise ValueError(f"Expected TMC block header starting with '#', got {prefix!r}")
    n = int(prefix[1:2])
    data_length = int(scope.read_bytes(n))
    return scope.read_bytes(data_length)
