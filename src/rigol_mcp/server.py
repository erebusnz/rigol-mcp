"""Rigol DS1000Z MCP server."""

import base64
import json
import os
from datetime import datetime
from pathlib import Path


import asyncio

import mcp.types as types
import pyvisa
from mcp.server import Server
from mcp.server.stdio import stdio_server

from rigol_mcp.waveform_analysis import describe_waveform as _describe_waveform
from rigol_mcp.scope import (
    get_scope, invalidate_scope,
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


async def _call(fn, *args, **kwargs):
    """Call fn(scope, *args, **kwargs) with the cached connection.
    Serialises concurrent calls via a lock, and reconnects on communication errors."""
    async with _scope_lock:
        try:
            return fn(get_scope(), *args, **kwargs)
        except (pyvisa.errors.VisaIOError, UnicodeDecodeError):
            invalidate_scope()
            return fn(get_scope(), *args, **kwargs)


@server.list_tools()
async def list_tools() -> list[types.Tool]:
    return [
        types.Tool(
            name="screenshot",
            description=(
                "Capture a screenshot of the oscilloscope display. "
                "Returns the image and the absolute path where the PNG was saved."
            ),
            inputSchema={"type": "object", "properties": {}, "required": []},
        ),
        types.Tool(
            name="idn",
            description=(
                "Identify the instrument. Returns make, model, serial, and firmware version. "
                "Use to verify connectivity before starting a measurement session."
            ),
            inputSchema={"type": "object", "properties": {}, "required": []},
        ),
        types.Tool(
            name="get_scope_state",
            description=(
                "Return a snapshot of the scope's current configuration: "
                "active channels (scale, offset, coupling, probe), timebase, and trigger. "
                "Call this at the start of a session to understand the current setup."
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
                "Returns the resulting channel configuration."
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
                "Returns the resulting timebase configuration."
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
                "Returns the resulting trigger configuration."
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
                "Stop acquisition first for stable readings. "
                "channel: CHAN1–CHAN4. "
                "item: VMAX, VMIN, VPP, VTOP, VBASE, VAMP, VAVG, VRMS, "
                "FREQUENCY, PERIOD, PWIDTH, NWIDTH, PDUTY, NDUTY, "
                "RTIME, FTIME, OVERSHOOT, PRESHOOT, PSLEWRATE, NSLEWRATE, "
                "TVMAX, TVMIN, VUPPER, VMID, VLOWER, VARIANCE, PVRMS, "
                "PPULSES, NPULSES, PEDGES, NEDGES. "
                "A return value of 9.9E37 is the scope's invalid/overflow sentinel — "
                "it means the measurement could not be computed (e.g. FREQUENCY returns 9.9E37 "
                "when the timebase is too narrow to show a complete cycle; widen scale and retry). "
                "For delay or phase between two channels use measure_between."
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
                "item: RDELAY (rising-edge delay, seconds), FDELAY (falling-edge delay, seconds), "
                "RPHASE (rising-edge phase, degrees), FPHASE (falling-edge phase, degrees). "
                "Stop acquisition first for stable readings."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "source1": {"type": "string", "enum": ["CHAN1", "CHAN2", "CHAN3", "CHAN4"], "description": "Reference channel"},
                    "source2": {"type": "string", "enum": ["CHAN1", "CHAN2", "CHAN3", "CHAN4"], "description": "Measured channel"},
                    "item":    {"type": "string", "enum": ["RDELAY", "FDELAY", "RPHASE", "FPHASE"]},
                },
                "required": ["source1", "source2", "item"],
            },
        ),
        types.Tool(
            name="get_waveform",
            description=(
                "Download and analyse the current waveform for a channel (screen buffer, ~1200 points). "
                "Stop or single-trigger the scope first for consistent data. "
                "By default returns a plain-text analysis: signal shape, frequency/period, amplitude, "
                "DC offset, cycle count, and data-quality warnings (e.g. mid-cycle edges, invalid frequency). "
                "Set raw_data=true to get the full time/voltage JSON arrays instead. "
                "After reading, act on any warnings — if FREQUENCY would be 9.9E37 widen the timebase; "
                "if edges are not near the DC mean, adjust offset so right edge = N×(period/2) − 6×scale."
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
                "mode: OFF, MANUAL, TRACK (omit to keep current mode). "
                "ax/bx: cursor A/B time positions in seconds. "
                "Returns the resulting cursor readouts."
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
                "AX_s and BX_s are time positions in seconds."
            ),
            inputSchema={"type": "object", "properties": {}, "required": []},
        ),
        types.Tool(
            name="send_raw",
            description=(
                "Send an arbitrary SCPI command. "
                "Queries (ending with '?') return the response string; "
                "writes return empty string and auto-check the error queue. "
                "Use as an escape hatch when no dedicated tool covers the operation."
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
            description="Query the SCPI error queue. Returns the error if present, or 'No error' if clear.",
            inputSchema={"type": "object", "properties": {}, "required": []},
        ),
        types.Tool(
            name="run",
            description="Start continuous acquisition. Returns trigger status after the command.",
            inputSchema={"type": "object", "properties": {}, "required": []},
        ),
        types.Tool(
            name="stop",
            description=(
                "Stop acquisition and freeze the display. "
                "Use before reading measurements or cursors for stable values. "
                "Returns trigger status after the command."
            ),
            inputSchema={"type": "object", "properties": {}, "required": []},
        ),
        types.Tool(
            name="single",
            description=(
                "Arm the scope for a single acquisition; stops automatically after one trigger event. "
                "Returns trigger status. "
                "Note: acquisition does not complete until a trigger occurs — "
                "call stop or check trigger status before reading measurements."
            ),
            inputSchema={"type": "object", "properties": {}, "required": []},
        ),
        types.Tool(
            name="autoscale",
            description=(
                "Run the scope's auto-setup (timebase, vertical scale, trigger). "
                "Takes a few seconds; call get_scope_state afterwards to see the resulting configuration."
            ),
            inputSchema={"type": "object", "properties": {}, "required": []},
        ),
    ]


@server.call_tool()
async def call_tool(name: str, arguments: dict) -> list[types.ContentBlock]:
    if name == "screenshot":
        png_bytes = await _call(screenshot_png)

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
        return [types.TextContent(type="text", text=await _call(idn))]

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
