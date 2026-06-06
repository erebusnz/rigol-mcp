"""Rigol DS1000Z MCP server."""

import base64
import json
import os
from datetime import datetime
from pathlib import Path


import asyncio
import time

from dotenv import load_dotenv

# Load configuration from a local .env file (e.g. RIGOL_IP, RIGOL_USB, RIGOL_USB_SERIAL)
# before anything reads os.environ. Existing environment variables (e.g. those passed via
# the MCP client's `env` block) take precedence and are not overridden.
load_dotenv()

import mcp.types as types
import pyvisa
from mcp.server import Server
from mcp.server.stdio import stdio_server

from rigol_mcp.waveform_analysis import describe_waveform as _describe_waveform
from rigol_mcp.scope import (
    get_scope, invalidate_scope, usb_in_use, active_backend,
    screenshot_png,
    get_cursor_mode, set_cursor_mode, set_cursor_positions, get_cursor_values,
    send_raw, check_scpi_error,
    run, stop, single, autoscale,
    idn, measure, measure_between, MEASURE_ITEMS, MEASURE_ITEMS_TWO_SOURCE,
    get_scope_state, set_channel, set_timebase, set_trigger, get_waveform,
)

server = Server("rigol-mcp")

# Serialises all VISA operations — the underlying TCP socket is not thread/async safe.
_scope_lock = asyncio.Lock()
_last_call_time: float = 0.0
_MIN_INTERVAL = 0.1          # 100 ms minimum between SCPI operations
_POST_SCREENSHOT_DELAY = 2.0 # scope needs recovery time after large display transfer
_MAX_ATTEMPTS = 3            # initial try + 2 reconnect-and-retry attempts
_RETRY_BACKOFF = 0.2         # seconds to let a flaky USB link settle before retrying

# Communication faults that warrant a reconnect-and-retry. Other exceptions (e.g. bad
# arguments, SCPI errors) are bugs/usage errors and must propagate unretried.
_RETRYABLE = (pyvisa.errors.VisaIOError, UnicodeDecodeError, OSError)

# send_raw sends arbitrary SCPI and can put the scope in any state, so it is opt-in:
# it is only advertised and accepted when RIGOL_ENABLE_SEND_RAW is set to a truthy value.
_SEND_RAW_ENV = "RIGOL_ENABLE_SEND_RAW"


def _send_raw_enabled() -> bool:
    return os.environ.get(_SEND_RAW_ENV, "").strip().lower() not in ("", "0", "false", "no", "off")


async def _call(fn, *args, **kwargs):
    """Call fn(scope, *args, **kwargs) with the cached connection.

    Serialises concurrent calls via a lock, enforces a minimum inter-command gap, and
    recovers from communication faults by reconnecting and retrying (up to _MAX_ATTEMPTS).
    USBTMC links in particular occasionally drop the first access after opening or after a
    large transfer; a reconnect on a fresh session clears it. The last failure propagates.
    """
    global _last_call_time
    async with _scope_lock:
        elapsed = time.monotonic() - _last_call_time
        if elapsed < _MIN_INTERVAL:
            await asyncio.sleep(_MIN_INTERVAL - elapsed)
        try:
            for attempt in range(1, _MAX_ATTEMPTS + 1):
                try:
                    return fn(get_scope(), *args, **kwargs)
                except _RETRYABLE:
                    invalidate_scope()  # drop the session so the next attempt reconnects
                    if attempt == _MAX_ATTEMPTS:
                        raise
                    await asyncio.sleep(_RETRY_BACKOFF)
        finally:
            _last_call_time = time.monotonic()


@server.list_tools()
async def list_tools() -> list[types.Tool]:
    tools = [
        types.Tool(
            name="screenshot",
            description=(
                "Capture a screenshot of the oscilloscope display. "
                "Returns the image and the absolute path where the PNG was saved. "
                "Do not call concurrently with any other rigol tool."
            ),
            inputSchema={"type": "object", "properties": {}, "required": []},
        ),
        types.Tool(
            name="idn",
            description=(
                "Identify the instrument and report connection details. "
                "Returns the *IDN? string (make, model, serial, firmware) plus: "
                "transport (LAN or USB), backend (@py=pyvisa-py/WinUSB or @ivi=NI-VISA/USBTMC), "
                "resource string, and the detected dialect driver (DS1000Z, DHO, …). "
                "Call this first to verify connectivity and confirm the correct driver was selected. "
                "Do not call concurrently with any other rigol tool."
            ),
            inputSchema={"type": "object", "properties": {}, "required": []},
        ),
        types.Tool(
            name="get_scope_state",
            description=(
                "Return a snapshot of the scope's current configuration: "
                "active channels (scale, offset, coupling, probe), timebase, and trigger. "
                "Call this at the start of a session to understand the current setup. "
                "Do not call concurrently with any other rigol tool."
            ),
            inputSchema={"type": "object", "properties": {}, "required": []},
        ),
        types.Tool(
            name="set_channel",
            description=(
                "Configure a channel. Only specified parameters are changed. "
                "channel: CHAN1–CHAN4. "
                "scale_v_div: V/div. offset_v: volts. coupling: AC, DC, or GND. "
                "probe: attenuation ratio (1, 10, 100, …). "
                "Parameter names match get_scope_state output for easy round-tripping. "
                "Returns the resulting channel configuration. "
                "Do not call concurrently with any other rigol tool."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "channel":    {"type": "string", "enum": ["CHAN1", "CHAN2", "CHAN3", "CHAN4"]},
                    "display":    {"type": "boolean", "description": "Turn channel on/off"},
                    "scale_v_div": {"type": ["number", "string"], "description": "Vertical scale in V/div"},
                    "offset_v":   {"type": ["number", "string"], "description": "Vertical offset in volts"},
                    "coupling":   {"type": "string", "enum": ["AC", "DC", "GND"]},
                    "probe":      {"type": ["number", "string"], "description": "Probe attenuation ratio (e.g. 1, 10, 100)"},
                },
                "required": ["channel"],
            },
        ),
        types.Tool(
            name="set_timebase",
            description=(
                "Set the horizontal timebase. "
                "scale_s_div: seconds per division (e.g. 0.001 for 1 ms/div). "
                "offset_s: shifts the display window; time_start = offset_s − 6×scale_s_div, time_end = offset_s + 6×scale_s_div. "
                "Trigger (t=0) is always a zero crossing when using edge trigger. "
                "To align the right edge to a zero crossing at time T: set offset_s = T − 6×scale_s_div. "
                "To put the trigger at the left edge of the screen: set offset_s = +6×scale_s_div. "
                "Parameter names match get_scope_state output for easy round-tripping. "
                "Returns the resulting timebase configuration. "
                "Do not call concurrently with any other rigol tool."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "scale_s_div": {"type": ["number", "string"], "description": "Time per division in seconds"},
                    "offset_s":    {"type": ["number", "string"], "description": "Trigger offset in seconds"},
                },
                "required": [],
            },
        ),
        types.Tool(
            name="set_trigger",
            description=(
                "Configure edge trigger. "
                "source: CHAN1–CHAN4 or EXT. "
                "slope: POS (rising), NEG (falling), or RFAL (either). "
                "level: trigger level in volts. "
                "Returns the resulting trigger configuration. "
                "Do not call concurrently with any other rigol tool."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "source": {"type": "string", "description": "Trigger source, e.g. CHAN1"},
                    "slope":  {"type": "string", "enum": ["POS", "NEG", "RFAL"]},
                    "level":  {"type": ["number", "string"], "description": "Trigger level in volts"},
                },
                "required": [],
            },
        ),
        types.Tool(
            name="measure",
            description=(
                "Query a single-source built-in measurement on a channel. "
                "For stable readings: on DS1000Z, stop acquisition first. "
                "On DHO, keep acquisition running — the DHO measurement engine only populates "
                "item values from live acquisitions; some items (VMAX/VMIN/VTOP/FREQUENCY/…) "
                "return 9.9E37 if first queried on a stopped scope. "
                "channel: CHAN1–CHAN4. "
                "item: VMAX, VMIN, VPP, "
                "VTOP (pulse top flat level, histogram-derived — not the same as VMAX), "
                "VBASE (pulse base flat level — not the same as VMIN), "
                "VAMP (=VTOP−VBASE — not the same as VPP=VMAX−VMIN), "
                "VAVG, VRMS (RMS over screen window), PVRMS (RMS over one period), "
                "VUPPER/VMID/VLOWER (timing thresholds at 90%/50%/10% of VAMP by default), "
                "VARIANCE (statistical variance of voltage samples), "
                "FREQUENCY, PERIOD, PWIDTH, NWIDTH, PDUTY, NDUTY, "
                "RTIME, FTIME, OVERSHOOT, PRESHOOT, "
                "PSLEWRATE, NSLEWRATE (slew rate, V/s), "
                "TVMAX, TVMIN (time position at which VMAX/VMIN occurs), "
                "MAREA (waveform area, V·s over screen window), MPAREA (area per period, V·s), "
                "PPULSES, NPULSES, PEDGES, NEDGES. "
                "A return value of 9.9E37 is the scope's invalid/overflow sentinel — "
                "it means the measurement could not be computed (e.g. FREQUENCY returns 9.9E37 "
                "when the timebase is too narrow to show a complete cycle; widen scale and retry). "
                "For delay or phase between two channels use measure_between. "
                "Do not call concurrently with any other rigol tool."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "channel": {"type": "string", "enum": ["CHAN1", "CHAN2", "CHAN3", "CHAN4"]},
                    "item":    {"type": "string", "description": "Measurement item (e.g. FREQUENCY, VPP, VRMS)"},
                },
                "required": ["channel", "item"],
            },
        ),
        types.Tool(
            name="measure_between",
            description=(
                "Query a two-source delay or phase measurement between two channels. "
                "source1 is the reference channel, source2 is the measured channel. "
                "DS1000Z items: RDELAY (rising-edge delay, seconds), FDELAY (falling-edge delay, seconds), "
                "RPHASE (rising-edge phase, degrees), FPHASE (falling-edge phase, degrees). "
                "DHO series exposes a 4-way matrix: RRDELAY/RFDELAY/FRDELAY/FFDELAY and "
                "RRPHASE/RFPHASE/FRPHASE/FFPHASE (first letter = source1 edge, second = source2 edge). "
                "On DHO the DS1000Z names are auto-mapped to their homogeneous equivalents "
                "(RDELAY→RRDELAY, FDELAY→FFDELAY, RPHASE→RRPHASE, FPHASE→FFPHASE). "
                "For stable readings: on DS1000Z, stop acquisition first. "
                "On DHO, keep acquisition running (see `measure` for details). "
                "Do not call concurrently with any other rigol tool."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "source1": {"type": "string", "enum": ["CHAN1", "CHAN2", "CHAN3", "CHAN4"], "description": "Reference channel"},
                    "source2": {"type": "string", "enum": ["CHAN1", "CHAN2", "CHAN3", "CHAN4"], "description": "Measured channel"},
                    "item":    {"type": "string", "enum": [
                        "RDELAY", "FDELAY", "RPHASE", "FPHASE",
                        "RRDELAY", "RFDELAY", "FRDELAY", "FFDELAY",
                        "RRPHASE", "RFPHASE", "FRPHASE", "FFPHASE",
                    ]},
                },
                "required": ["source1", "source2", "item"],
            },
        ),
        types.Tool(
            name="get_waveform",
            description=(
                "Download and analyse the current waveform for a channel (NORM screen buffer, up to ~1000–1200 points depending on scope). "
                "Stop or single-trigger the scope first for consistent data. "
                "By default returns a plain-text analysis: signal shape, frequency/period, amplitude, "
                "DC offset, cycle count, and data-quality warnings (e.g. mid-cycle edges, invalid frequency). "
                "Amplitude is judged against the channel's V/div: a trace filling under ~10% of the vertical "
                "screen is flagged as noise floor and its shape/frequency are not reported, and one filling "
                "under ~20% gets a low-amplitude warning (reduce V/div and re-capture for a clean signal). "
                "Set raw_data=true to get the full time/voltage JSON arrays instead. "
                "After reading, act on any warnings — if FREQUENCY would be 9.9E37 widen the timebase; "
                "if edges are not near the DC mean, adjust offset so right edge = N×(period/2) − 6×scale. "
                "Do not call concurrently with any other rigol tool."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "channel":  {"type": "string", "enum": ["CHAN1", "CHAN2", "CHAN3", "CHAN4"]},
                    "raw_data": {"type": "boolean", "description": "Return raw time/voltage JSON arrays instead of text analysis (default false)"},
                },
                "required": ["channel"],
            },
        ),
        types.Tool(
            name="set_cursors",
            description=(
                "Set cursor mode and/or X positions. "
                "mode: OFF, MANUAL (fixed time positions, reads voltage at those X points), "
                "TRACK (cursors snap to and follow the waveform at the X position). "
                "Omit mode to keep current mode. "
                "ax/bx: cursor A/B time positions in seconds. "
                "Returns the resulting cursor readouts. "
                "Do not call concurrently with any other rigol tool."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "mode": {"type": "string", "enum": ["OFF", "MANUAL", "TRACK"]},
                    "ax":   {"type": ["number", "string"], "description": "Cursor A X position in seconds"},
                    "bx":   {"type": ["number", "string"], "description": "Cursor B X position in seconds"},
                },
                "required": [],
            },
        ),
        types.Tool(
            name="get_cursor_values",
            description=(
                "Read current cursor mode and all cursor readouts. "
                "AX_s and BX_s are time positions in seconds. "
                "inv_delta_x is 1/Δt — the frequency between the two cursors. "
                "Do not call concurrently with any other rigol tool."
            ),
            inputSchema={"type": "object", "properties": {}, "required": []},
        ),
        types.Tool(
            name="send_raw",
            description=(
                "Send an arbitrary SCPI command. "
                "Queries (ending with '?') return the response string; "
                "writes return empty string and auto-check the error queue. "
                "Use as an escape hatch when no dedicated tool covers the operation. "
                "Do not call concurrently with any other rigol tool."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "command": {"type": "string", "description": "SCPI command, e.g. ':CHAN1:SCAL?' or ':CHAN1:DISP ON'"},
                },
                "required": ["command"],
            },
        ),
        types.Tool(
            name="check_error",
            description="Query the SCPI error queue. Returns the error if present, or 'No error' if clear. Do not call concurrently with any other rigol tool.",
            inputSchema={"type": "object", "properties": {}, "required": []},
        ),
        types.Tool(
            name="run",
            description="Start continuous acquisition. Returns trigger status after the command. Do not call concurrently with any other rigol tool.",
            inputSchema={"type": "object", "properties": {}, "required": []},
        ),
        types.Tool(
            name="stop",
            description=(
                "Stop acquisition and freeze the display. "
                "Use before reading measurements or cursors for stable values. "
                "Returns trigger status after the command. "
                "Do not call concurrently with any other rigol tool."
            ),
            inputSchema={"type": "object", "properties": {}, "required": []},
        ),
        types.Tool(
            name="single",
            description=(
                "Arm the scope for a single acquisition; stops automatically after one trigger event. "
                "Returns trigger status. "
                "Note: acquisition does not complete until a trigger occurs — "
                "call stop or check trigger status before reading measurements. "
                "Do not call concurrently with any other rigol tool."
            ),
            inputSchema={"type": "object", "properties": {}, "required": []},
        ),
        types.Tool(
            name="autoscale",
            description=(
                "Run the scope's auto-setup (timebase, vertical scale, trigger). "
                "Takes a few seconds; call get_scope_state afterwards to see the resulting configuration. "
                "Do not call concurrently with any other rigol tool."
            ),
            inputSchema={"type": "object", "properties": {}, "required": []},
        ),
    ]
    # send_raw is an arbitrary-SCPI escape hatch — only expose it when explicitly enabled.
    if not _send_raw_enabled():
        tools = [t for t in tools if t.name != "send_raw"]
    return tools


@server.call_tool()
async def call_tool(name: str, arguments: dict) -> list[types.ContentBlock]:
    global _last_call_time
    if name == "screenshot":
        png_bytes = await _call(screenshot_png)
        # pyvisa-py (@py, used with the WinUSB driver) can leave the USBTMC stream in a
        # state where the command right after a large screenshot transfer intermittently
        # times out; dropping the connection forces a clean USBTMC session on the next
        # call (~1 s, within the recovery window below). NI-VISA (@ivi, native USBTMC
        # driver) recovers on its own and a reconnect there actually disrupts the next
        # command — so reconnect only on @py.
        if usb_in_use() and active_backend() == "@py":
            async with _scope_lock:
                invalidate_scope()
        # Advance the cooldown timestamp so the next _call waits for the scope to recover
        # after the large display transfer before sending further SCPI commands.
        _last_call_time = time.monotonic() + _POST_SCREENSHOT_DELAY

        save_dir = Path(os.environ.get("RIGOL_SCREENSHOT_DIR", "screenshots")).resolve()
        save_dir.mkdir(parents=True, exist_ok=True)
        filename = save_dir / f"screenshot_{datetime.now().strftime('%Y%m%d_%H%M%S_%f')}.png"
        filename.write_bytes(png_bytes)

        b64 = base64.standard_b64encode(png_bytes).decode("ascii")
        return [
            types.TextContent(type="text", text=f"Saved: {filename}"),
            types.ImageContent(type="image", data=b64, mimeType="image/png"),
        ]

    if name == "idn":
        info = await _call(idn)
        text = (
            f"IDN      : {info['idn']}\n"
            f"Transport: {info['transport']}\n"
            f"Backend  : {info['backend']}\n"
            f"Resource : {info['resource']}\n"
            f"Driver   : {info['driver']}"
        )
        return [types.TextContent(type="text", text=text)]

    if name == "get_scope_state":
        state = await _call(get_scope_state)
        return [types.TextContent(type="text", text=json.dumps(state, indent=2))]

    if name == "set_channel":
        def _f(key):
            v = arguments.get(key)
            return float(v) if v is not None else None

        await _call(
            set_channel,
            arguments["channel"],
            display=arguments.get("display"),
            scale=_f("scale_v_div"),
            offset=_f("offset_v"),
            coupling=arguments.get("coupling"),
            probe=_f("probe"),
        )
        state = await _call(get_scope_state)
        ch = arguments["channel"].upper()
        return [types.TextContent(type="text", text=json.dumps(state["channels"][ch], indent=2))]

    if name == "set_timebase":
        def _f(key):
            v = arguments.get(key)
            return float(v) if v is not None else None

        await _call(set_timebase, scale=_f("scale_s_div"), offset=_f("offset_s"))
        state = await _call(get_scope_state)
        return [types.TextContent(type="text", text=json.dumps(state["timebase"], indent=2))]

    if name == "set_trigger":
        level = arguments.get("level")
        await _call(
            set_trigger,
            source=arguments.get("source"),
            slope=arguments.get("slope"),
            level=float(level) if level is not None else None,
        )
        state = await _call(get_scope_state)
        return [types.TextContent(type="text", text=json.dumps(state["trigger"], indent=2))]

    if name == "measure":
        value = await _call(measure, arguments["channel"], arguments["item"])
        return [types.TextContent(
            type="text",
            text=f"{arguments['item']} on {arguments['channel']}: {value}",
        )]

    if name == "measure_between":
        value = await _call(measure_between, arguments["source1"], arguments["source2"], arguments["item"])
        return [types.TextContent(
            type="text",
            text=f"{arguments['item']} from {arguments['source1']} to {arguments['source2']}: {value}",
        )]

    if name == "get_waveform":
        data = await _call(get_waveform, arguments["channel"])
        if arguments.get("raw_data"):
            return [types.TextContent(type="text", text=json.dumps(data))]
        return [types.TextContent(type="text", text=_describe_waveform(data))]

    if name == "set_cursors":
        mode = arguments.get("mode")
        ax = float(arguments["ax"]) if "ax" in arguments else None
        bx = float(arguments["bx"]) if "bx" in arguments else None
        if mode is not None:
            await _call(set_cursor_mode, mode)
        else:
            mode = await _call(get_cursor_mode)
        if mode.upper() != "OFF" and (ax is not None or bx is not None):
            await _call(set_cursor_positions, mode, ax=ax, bx=bx)
        values = await _call(get_cursor_values)
        lines = "\n".join(f"{k}: {v}" for k, v in values.items())
        return [types.TextContent(type="text", text=lines)]

    if name == "get_cursor_values":
        values = await _call(get_cursor_values)
        lines = "\n".join(f"{k}: {v}" for k, v in values.items())
        return [types.TextContent(type="text", text=lines)]

    if name == "send_raw":
        if not _send_raw_enabled():
            raise ValueError(
                f"send_raw is disabled. Set {_SEND_RAW_ENV}=1 to enable arbitrary SCPI commands."
            )
        response = await _call(send_raw, arguments["command"])
        return [types.TextContent(type="text", text=response or "(no response)")]

    if name == "check_error":
        err = await _call(check_scpi_error)
        return [types.TextContent(type="text", text=err or "No error")]

    if name in ("run", "stop", "single"):
        fn = {"run": run, "stop": stop, "single": single}[name]
        status = await _call(fn)
        return [types.TextContent(type="text", text=f"trigger status: {status}")]

    if name == "autoscale":
        await _call(autoscale)
        state = await _call(get_scope_state)
        return [types.TextContent(type="text", text=json.dumps(state, indent=2))]

    raise ValueError(f"Unknown tool: {name}")


def main() -> None:
    asyncio.run(_run())


async def _run() -> None:
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


if __name__ == "__main__":
    main()
