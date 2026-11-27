"""Frozen acquisition export: streamed uint8 files and a self-describing manifest.

Only validated, contiguous chunks are committed. No acquisition control commands
are sent, and a failed transfer is never retried against a potentially different record.
"""

import json
import math
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import pyvisa

from rigol_mcp.drivers import MemoryTransferError
from rigol_mcp.scope import (
    get_driver, get_scope_state, check_scpi_error, invalidate_scope, validate_analog_channel,
)
from rigol_mcp.models import ANALOG_CHANNELS


class _ExportSession:
    """Track transport failures separately from local filesystem failures.

    All operations remain synchronous on the caller's thread. The server holds
    its scope lock for the entire export, including cleanup.
    """

    def __init__(self, resource):
        object.__setattr__(self, "_resource", resource)
        object.__setattr__(self, "transport_failed", False)

    def __getattr__(self, name):
        value = getattr(self._resource, name)
        if name not in ("query", "write", "read_bytes", "read_raw"):
            return value

        def guarded(*args, **kwargs):
            try:
                return value(*args, **kwargs)
            except (pyvisa.errors.VisaIOError, OSError, UnicodeError):
                object.__setattr__(self, "transport_failed", True)
                raise

        return guarded

    def __setattr__(self, name, value):
        setattr(self._resource, name, value)


def get_acquisition_info(scope) -> dict:
    driver = get_driver(scope)
    state = get_scope_state(scope)
    result = {
        "instrument": scope.query("*IDN?").strip(), "dialect": driver.name,
        "status": state["trigger"]["status"],
        "enabled_channels": [ch for ch, cfg in state["channels"].items()
                             if cfg["display"].upper() in ("1", "ON")],
        "supported_transfer_modes": ["screen"] + (
            ["acquisition_memory"] if driver.memory_chunk_points else []),
        "settings": state,
    }
    if driver.supports_acquisition_info:
        result.update(driver.acquisition_info(scope))
    if not driver.supports_acquisition_info:
        result["warnings"] = [f"Acquisition metadata/RAW export not implemented for {driver.name}"]
    elif not driver.memory_chunk_points:
        result["warnings"] = [f"RAW export not implemented for {driver.name}"]
    return result


def _preamble(scope) -> dict:
    raw = scope.query(":WAV:PRE?").strip()
    fields = raw.split(",")
    if len(fields) != 10:
        raise ValueError(f"Invalid waveform preamble: {raw!r}")
    keys = ("format", "type", "points", "count", "xincrement", "xorigin",
            "xreference", "yincrement", "yorigin", "yreference")
    pre = dict(zip(keys, map(float, fields)))
    if (not all(math.isfinite(v) for v in pre.values())
            or pre["format"] != 0 or pre["type"] != 2
            or pre["points"] < 1 or not pre["points"].is_integer()
            or pre["xincrement"] <= 0 or pre["yincrement"] <= 0):
        raise ValueError(f"Invalid RAW BYTE preamble: {raw!r}")
    pre["points"] = int(pre["points"])
    pre["raw"] = raw
    return pre


def _require_stopped(scope):
    status = scope.query(":TRIGger:STATus?").strip().upper()
    if status != "STOP":
        raise RuntimeError(f"Acquisition status is {status}; wait for single acquisition to "
                           "complete, or explicitly use stop before exporting. Transfer aborted.")


def export_acquisition(scope, channels=None, output_dir=None) -> dict:
    session = _ExportSession(scope)
    try:
        return _export_acquisition(session, channels, output_dir)
    except Exception:
        # Preflight failures happen before a manifest/transfer exists. Discard a
        # failed session anyway; never reconnect or retry within this export.
        if session.transport_failed:
            invalidate_scope()
        raise


def _export_acquisition(scope, channels=None, output_dir=None) -> dict:
    """Export complete records; intentionally no stop, rearm, or range arguments."""
    if channels is not None:
        if (not isinstance(channels, list) or not channels
                or any(ch not in ANALOG_CHANNELS for ch in channels)
                or len(set(channels)) != len(channels)):
            raise ValueError(f"channels must be a nonempty unique list drawn from {ANALOG_CHANNELS}")
        for channel in channels:
            validate_analog_channel(scope, channel)
    driver = get_driver(scope)
    if not driver.memory_chunk_points:
        raise ValueError(f"Acquisition-memory export unsupported for {driver.name}; no screen fallback")
    _require_stopped(scope)
    info = get_acquisition_info(scope)
    channels = info["enabled_channels"] if channels is None else channels
    if not channels or any(ch not in info["enabled_channels"] for ch in channels):
        raise ValueError("Requested channels are disabled or none are enabled; export never enables channels")
    _require_stopped(scope)
    saved = {key: scope.query(f":WAV:{key}?").strip()
             for key in ("SOUR", "MODE", "FORM", "STAR", "STOP")}
    root = Path(output_dir or os.environ.get("RIGOL_WAVEFORM_DIR", "waveforms")).resolve()
    root.mkdir(parents=True, exist_ok=True)
    folder = Path(tempfile.mkdtemp(prefix="acquisition-", dir=root))
    manifest_path = folder / "manifest.json"
    manifest = {
        "schema_version": 1, "source": "acquisition_memory", "transfer_format": "BYTE",
        "dtype": "uint8", "export_started_at": datetime.now(timezone.utc).isoformat(),
        "acquisition_timestamp": None, "acquisition": info, "complete": False,
        "requested_channels": channels, "requested_range": "complete_record",
        "channels": {}, "warnings": [
            "Acquisition timestamp and completion history are unavailable; STOP and RAW metadata "
            "are checked. Do not operate the scope during export."],
        "time_formula": "xorigin + (zero_based_sample_index - xreference) * xincrement",
        "value_formula": "(uint8_sample - yorigin - yreference) * yincrement",
        "probe_scaling_applied": True,
    }

    persisted_points = {}
    manifest_current = False
    manifest_error = None
    transfer_failed = False

    def save():
        nonlocal persisted_points
        temp = manifest_path.with_suffix(".tmp")
        temp.write_text(json.dumps(manifest, indent=2, allow_nan=False), encoding="utf-8")
        temp.replace(manifest_path)
        persisted_points = {ch: meta["points"] for ch, meta in manifest["channels"].items()}

    save()  # A process interruption leaves an explicitly incomplete manifest.
    try:
        # Read every preamble before transferring any samples.
        for ch in channels:
            driver.prepare_memory(scope, ch)
            pre = _preamble(scope)
            cfg = dict(info["settings"]["channels"][ch])
            cfg["units"] = scope.query(f":{ch}:UNIT?").strip()
            manifest["channels"][ch] = {
                "file": f"{ch}.bin", "preamble": pre, "settings": cfg,
                "expected_points": pre["points"], "points": 0, "chunks": 0,
                "complete": False, "exported_range": None,
                "time_increment_s": pre["xincrement"],
                "time_start_s": pre["xorigin"] - pre["xreference"] * pre["xincrement"],
                "duration_s": 0,
            }
            if not math.isclose(1 / pre["xincrement"], info["sample_rate_hz"], rel_tol=1e-5):
                manifest["warnings"].append(f"{ch}: preamble sample interval differs from acquisition sample rate")
        axes = {(v["expected_points"], v["time_increment_s"], v["time_start_s"])
                for v in manifest["channels"].values()}
        manifest["timestamps_aligned"] = len(axes) == 1
        if len(axes) != 1:
            manifest["warnings"].append("Channels have different time axes; use each channel's preamble")
        save()
        for ch, meta in manifest["channels"].items():
            driver.prepare_memory(scope, ch)
            if _preamble(scope) != meta["preamble"]:
                raise RuntimeError(f"{ch}: preamble changed before transfer")
            with (folder / meta["file"]).open("wb") as output:
                for start in range(1, meta["expected_points"] + 1, driver.memory_chunk_points):
                    _require_stopped(scope)
                    stop = min(start + driver.memory_chunk_points - 1, meta["expected_points"])
                    data = driver.read_memory_chunk(scope, start, stop)
                    output.write(data)
                    output.flush()
                    meta["points"] += len(data)
                    meta["chunks"] += 1
                    meta["exported_range"] = [1, meta["points"]]  # inclusive SCPI indices
                    meta["duration_s"] = (meta["points"] - 1) * meta["time_increment_s"]
                    save()
            if (folder / meta["file"]).stat().st_size != meta["points"]:
                raise RuntimeError(f"{ch}: file sample count mismatch")
            if _preamble(scope) != meta["preamble"]:
                raise RuntimeError(f"{ch}: preamble changed during transfer")
            meta["complete"] = True
        _require_stopped(scope)
        if get_acquisition_info(scope) != info:
            raise RuntimeError("Acquisition settings changed during export; record consistency is unverified")
        manifest["complete"] = True
    except Exception as exc:
        transfer_failed = scope.transport_failed or isinstance(exc, MemoryTransferError)
        manifest["error"] = f"{type(exc).__name__}: {exc}"
        manifest["warnings"].append("Export incomplete or inconsistent; only validated chunks were saved. "
                                    "Check connection/status before requesting a new export.")
    finally:
        try:
            if transfer_failed:
                # Do not send commands after a failed binary or text transaction.
                # Reconnecting here could attach to a different acquisition.
                raise RuntimeError("Skipped because scope I/O failed; VISA stream may be desynchronized")
            # Reset the range before changing mode/source (e.g. back to MATH/NORM).
            scope.write(":WAV:STAR 1")
            scope.write(":WAV:STOP 1")
            for key in ("MODE", "SOUR", "FORM", "STOP", "STAR"):
                scope.write(f":WAV:{key} {saved[key]}")
            if err := check_scpi_error(scope):
                raise RuntimeError(err)
            actual = {key: scope.query(f":WAV:{key}?").strip() for key in saved}
            if actual != saved:
                raise RuntimeError(f"Transfer settings readback differs: {actual}")
            manifest["transfer_settings_restored"] = True
        except Exception as exc:
            manifest["transfer_settings_restored"] = False
            manifest["warnings"].append(f"Could not restore waveform transfer settings: {exc}")
        if not manifest["complete"] or not manifest["transfer_settings_restored"]:
            invalidate_scope()  # next tool reconnects; never retry this export automatically
        manifest["export_finished_at"] = datetime.now(timezone.utc).isoformat()
        try:
            save()
            manifest_current = True
        except OSError as exc:
            manifest_error = f"{type(exc).__name__}: {exc}"
            manifest["complete"] = False
            manifest.setdefault("error", f"Final manifest update failed: {manifest_error}")
            manifest["warnings"].append(
                f"Manifest update failed: {manifest_error}. The on-disk manifest is stale; "
                "use recovery_manifest and the reported validated counts, not extra file bytes."
            )
    result = {
        "manifest_path": str(manifest_path), "file_uri": manifest_path.as_uri(),
        "manifest_current": manifest_current, "manifest_error": manifest_error,
        "persisted_points": persisted_points,
        "complete": manifest["complete"], "source": manifest["source"],
        "timestamps_aligned": manifest.get("timestamps_aligned"),
        "transfer_settings_restored": manifest["transfer_settings_restored"],
        "channels": {ch: {k: meta[k] for k in (
            "points", "expected_points", "chunks", "complete", "time_increment_s",
            "time_start_s", "duration_s")} for ch, meta in manifest["channels"].items()},
        "warnings": manifest["warnings"], "error": manifest.get("error"),
    }
    if not manifest_current:
        # Include conversion metadata for recovery when the latest preambles or
        # counts could not be persisted. This contains no waveform arrays.
        result["recovery_manifest"] = manifest
    return result
