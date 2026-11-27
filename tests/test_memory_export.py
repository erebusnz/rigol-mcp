"""Offline RAW transfer tests with a stateful scope and real binary block readers."""

import json
from pathlib import Path

import pytest
import pyvisa

from conftest import FakeScope, make_block
from rigol_mcp import memory_export as mem
from rigol_mcp import scope as sc


class MemoryScope(FakeScope):
    def __init__(self, points=500_003, status="STOP"):
        super().__init__()
        self.points = points
        self.status = status
        self.transfer = dict(SOUR="CHAN2", MODE="NORM", FORM="ASC", STAR="5", STOP="100")
        self.original = self.transfer.copy()
        self.reads = []
        self.fail_at = None
        self.clamp = False
        self.reject_raw = False
        self.disabled = set()
        self.axis_offset = {}
        self.restore_fail = False

    def query(self, cmd):
        if cmd == "*IDN?":
            return "RIGOL TECHNOLOGIES,DS1104Z,DS1ZA000000000,00.04.05.SP1"
        if cmd == ":TRIGger:STATus?":
            return self.status
        if cmd == ":SYSTem:ERRor?":
            return "0"
        if cmd == ":WAV:PRE?":
            origin = -.08 + self.axis_offset.get(self.transfer["SOUR"], 0)
            return f"0,2,{self.points},1,5e-7,{origin},3,0.04,-4,127"
        if cmd.startswith(":WAV:"):
            return self.transfer[cmd[5:-1]]
        if cmd.startswith(":CHAN"):
            ch, key = cmd[1:-1].split(":")
            return {"DISP": "0" if ch in self.disabled else "1", "SCAL": "1",
                    "OFFS": "0.2", "COUP": "DC", "PROB": "10", "UNIT": "VOLT"}[key]
        return {":TIM:SCAL?": "0.02", ":TIM:OFFS?": "0.01", ":TIM:MODE?": "MAIN",
                ":TRIGger:MODE?": "EDGE", ":TRIGger:EDGE:SOURce?": "CHAN4",
                ":TRIGger:EDGE:SLOPe?": "POS", ":TRIGger:EDGE:LEVel?": "1.2",
                ":ACQ:SRAT?": "2000000", ":ACQ:MDEP?": "AUTO",
                ":ACQ:TYPE?": "NORM", ":ACQ:AVER?": "2"}[cmd]

    def write(self, cmd):
        super().write(cmd)
        if cmd == ":WAV:DATA?":
            start, stop = int(self.transfer["STAR"]), int(self.transfer["STOP"])
            self.reads.append((self.transfer["SOUR"], start, stop))
            if self.fail_at == len(self.reads):
                raise OSError("USB disconnected")
            self.load(make_block(bytes(i % 251 for i in range(start - 1, stop))))
        elif cmd.startswith(":WAV:"):
            key, val = cmd[5:].split()
            if self.restore_fail and key == "MODE" and val == "NORM":
                raise OSError("Restore failed")
            if self.reject_raw and key == "MODE" and val == "RAW":
                return
            self.transfer[key] = val
            if self.clamp and key == "STOP" and int(val) > 100:
                self.transfer[key] = "100"


def manifest(result):
    return json.loads(Path(result["manifest_path"]).read_text())


@pytest.mark.parametrize("backend", ["@py", "@ivi"])
def test_complete_four_channel_export(tmp_path, monkeypatch, backend):
    monkeypatch.setattr(sc, "_usb_backend_hint", backend)
    scope = MemoryScope()
    result = mem.export_acquisition(scope, output_dir=tmp_path)
    saved = manifest(result)
    assert result["complete"] and saved["timestamps_aligned"]
    assert saved["acquisition_timestamp"] is None
    assert saved["acquisition"]["sample_rate_hz"] == 2_000_000
    assert saved["transfer_settings_restored"]
    assert scope.transfer == scope.original
    assert len(scope.reads) == 12
    for ch, meta in saved["channels"].items():
        assert [r[1:] for r in scope.reads if r[0] == ch] == [
            (1, 250000), (250001, 500000), (500001, 500003)]
        data = (Path(result["manifest_path"]).parent / meta["file"]).read_bytes()
        assert data == bytes(i % 251 for i in range(scope.points))
        assert len(data) == meta["points"] == result["channels"][ch]["points"]
        assert meta["time_start_s"] == pytest.approx(-.0800015)
        assert meta["duration_s"] == pytest.approx((scope.points - 1) / 2e6)
        assert meta["settings"]["probe"] == "10"
        pre = meta["preamble"]
        assert (148 - pre["yorigin"] - pre["yreference"]) * pre["yincrement"] == 1.0
    assert all(cmd.startswith(":WAV:") for cmd in scope.written)
    assert len(json.dumps(result)) < 3000  # no arrays in MCP response


@pytest.mark.parametrize("status", ["WAIT", "RUN", "TD", "AUTO"])
def test_running_refused_without_mutation(tmp_path, status):
    scope = MemoryScope(status=status)
    with pytest.raises(RuntimeError, match=status):
        mem.export_acquisition(scope, output_dir=tmp_path)
    assert not scope.written and not list(tmp_path.iterdir())


def test_disabled_channel_refused(tmp_path):
    scope = MemoryScope()
    scope.disabled = {"CHAN2"}
    with pytest.raises(ValueError, match="disabled"):
        mem.export_acquisition(scope, ["CHAN2"], tmp_path)
    assert not scope.written


@pytest.mark.parametrize("channels", [[], ["CHAN1", "CHAN1"], ["CHAN5"], "CHAN1"])
def test_invalid_channels(tmp_path, channels):
    with pytest.raises(ValueError, match="channels"):
        mem.export_acquisition(MemoryScope(), channels, tmp_path)


def test_missing_model_channel_is_rejected_before_export_io(tmp_path):
    # A valid global schema choice can still be unavailable on the connected scope.
    class TwoChannelScope(FakeScope):
        def query(self, command):
            assert command == "*IDN?", "Reject the channel before acquisition or channel queries"
            return "RIGOL TECHNOLOGIES,DHO802,SERIAL,1.0"

    scope = TwoChannelScope()
    with pytest.raises(ValueError, match="CHAN3 is not available on DHO802"):
        mem.export_acquisition(scope, ["CHAN3"], tmp_path)
    assert not scope.written and not list(tmp_path.iterdir())


def test_interrupted_export_is_partial(tmp_path):
    scope = MemoryScope()
    scope.fail_at = 2
    result = mem.export_acquisition(scope, output_dir=tmp_path)
    saved = manifest(result)
    assert not result["complete"] and "USB disconnected" in result["error"]
    assert saved["channels"]["CHAN1"]["points"] == 250000
    assert saved["channels"]["CHAN1"]["exported_range"] == [1, 250000]
    assert saved["channels"]["CHAN2"]["points"] == 0
    assert (Path(result["manifest_path"]).parent / "CHAN1.bin").stat().st_size == 250000
    assert not saved["transfer_settings_restored"]
    assert scope.written[-1] == ":WAV:DATA?"  # no more I/O on an uncertain stream
    assert len(scope.reads) == 2


@pytest.mark.parametrize("fault", ["clamp", "reject_raw"])
def test_rejected_transfer_never_claims_complete(tmp_path, fault):
    scope = MemoryScope()
    setattr(scope, fault, True)
    result = mem.export_acquisition(scope, output_dir=tmp_path)
    assert not result["complete"] and result["error"]
    assert not scope.reads
    assert scope.transfer == scope.original


def test_alignment_differences_reported(tmp_path):
    scope = MemoryScope(points=65536)
    scope.axis_offset["CHAN2"] = .01
    result = mem.export_acquisition(scope, output_dir=tmp_path)
    assert result["complete"]
    assert not manifest(result)["timestamps_aligned"]
    assert any("different time axes" in w for w in result["warnings"])


def test_restoration_failure_reported(tmp_path):
    scope = MemoryScope(points=65536)
    scope.restore_fail = True
    result = mem.export_acquisition(scope, ["CHAN1"], tmp_path)
    assert result["complete"]  # complete data, restoration is separately reported
    assert not manifest(result)["transfer_settings_restored"]
    assert any("Could not restore" in w for w in result["warnings"])


def test_unsupported_family(tmp_path):
    scope = FakeScope({"*IDN?": "RIGOL TECHNOLOGIES,DHO924S,serial,firmware"})
    with pytest.raises(ValueError, match="unsupported for DHO"):
        mem.export_acquisition(scope, output_dir=tmp_path)
    assert not scope.written


def test_info_read_only():
    scope = MemoryScope(status="WAIT")
    info = mem.get_acquisition_info(scope)
    assert info["status"] == "WAIT"
    assert info["memory_depth_setting"] == "AUTO"
    assert info["supported_transfer_modes"] == ["screen", "acquisition_memory"]
    assert not scope.written


def test_truncated_chunk_retains_only_prior_validated_chunks(tmp_path):
    class ShortScope(MemoryScope):
        def write(self, cmd):
            super().write(cmd)
            if cmd == ":WAV:DATA?" and len(self.reads) == 2:
                self.load(make_block(b"short"))

    result = mem.export_acquisition(ShortScope(), ["CHAN1"], tmp_path)
    assert not result["complete"]
    assert "Unexpected payload length" in result["error"]
    assert result["channels"]["CHAN1"]["points"] == 250000


def test_scope_resumed_during_export(tmp_path):
    class ResumedScope(MemoryScope):
        def write(self, cmd):
            super().write(cmd)
            if cmd == ":WAV:DATA?":
                self.status = "RUN"

    scope = ResumedScope()
    result = mem.export_acquisition(scope, ["CHAN1"], tmp_path)
    assert not result["complete"]
    assert "status is RUN" in result["error"]
    assert len(scope.reads) == 1
    assert not any(cmd == ":STOP" for cmd in scope.written)


def test_preamble_changed_during_transfer(tmp_path):
    class ChangedScope(MemoryScope):
        def write(self, cmd):
            super().write(cmd)
            if cmd == ":WAV:DATA?":
                self.axis_offset["CHAN1"] = 1

    result = mem.export_acquisition(ChangedScope(points=65536), ["CHAN1"], tmp_path)
    assert not result["complete"]
    assert "preamble changed" in result["error"]


@pytest.mark.parametrize("pre", ["bad", "0,0,1200,1,1e-6,0,0,1,0,127",
                                 "0,2,0,1,1e-6,0,0,1,0,127",
                                 "0,2,65536,1,nan,0,0,1,0,127"])
def test_bad_preamble_is_incomplete(tmp_path, pre):
    class BadPreamble(MemoryScope):
        def query(self, cmd):
            return pre if cmd == ":WAV:PRE?" else super().query(cmd)

    scope = BadPreamble()
    result = mem.export_acquisition(scope, ["CHAN1"], tmp_path)
    assert not result["complete"] and not scope.reads
    assert scope.transfer == scope.original


def test_default_channels_excludes_disabled(tmp_path):
    scope = MemoryScope(points=65536)
    scope.disabled = {"CHAN4"}
    result = mem.export_acquisition(scope, output_dir=tmp_path)
    assert result["complete"]
    assert set(result["channels"]) == {"CHAN1", "CHAN2", "CHAN3"}


def test_acquisition_metadata_change_invalidates_export(tmp_path):
    class ChangedAcquisition(MemoryScope):
        def query(self, cmd):
            if cmd == ":ACQ:MDEP?" and self.reads:
                return "6000000"
            return super().query(cmd)

    result = mem.export_acquisition(ChangedAcquisition(points=65536), ["CHAN1"], tmp_path)
    assert not result["complete"]
    assert "settings changed" in result["error"]


def test_export_directories_are_unique(tmp_path):
    scope = MemoryScope(points=65536)
    first = mem.export_acquisition(scope, ["CHAN1"], tmp_path)
    second = mem.export_acquisition(scope, ["CHAN1"], tmp_path)
    assert first["manifest_path"] != second["manifest_path"]
    assert manifest(first)["complete"] and manifest(second)["complete"]


@pytest.mark.parametrize("backend", ["@py", "@ivi"])
def test_short_binary_block_rejected(monkeypatch, backend):
    monkeypatch.setattr(sc, "_usb_backend_hint", backend)
    scope = FakeScope(read_buffer=b"#210abc\n")
    with pytest.raises(ValueError, match="Truncated"):
        sc._read_definite_block(scope)


@pytest.mark.parametrize("operation", ["write", "replace"])
def test_manifest_failure_returns_recovery_metadata(tmp_path, monkeypatch, operation):
    original = Path.write_text if operation == "write" else Path.replace
    checkpoints = 0

    def fail_checkpoint(path, *args, **kwargs):
        nonlocal checkpoints
        if path.name == "manifest.tmp":
            checkpoints += 1
            if checkpoints >= 3:
                raise OSError("disk unavailable")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "write_text" if operation == "write" else "replace", fail_checkpoint)
    scope = MemoryScope()
    result = mem.export_acquisition(scope, ["CHAN1"], tmp_path)
    assert not result["complete"] and not result["manifest_current"]
    assert "disk unavailable" in result["error"]
    assert result["persisted_points"] == {"CHAN1": 0}
    assert result["channels"]["CHAN1"]["points"] == 250000
    assert manifest(result)["channels"]["CHAN1"]["points"] == 0
    recovery = result["recovery_manifest"]["channels"]["CHAN1"]
    assert recovery["preamble"]["xincrement"] == 5e-7
    assert recovery["points"] == (Path(result["manifest_path"]).parent / recovery["file"]).stat().st_size
    assert scope.transfer == scope.original  # local disk fault does not poison VISA
    assert len(scope.reads) == 1


def test_final_manifest_failure_cannot_claim_complete(tmp_path, monkeypatch):
    original = Path.write_text

    def fail_final(path, text, *args, **kwargs):
        if path.name == "manifest.tmp" and "export_finished_at" in json.loads(text):
            raise OSError("final checkpoint failed")
        return original(path, text, *args, **kwargs)

    monkeypatch.setattr(Path, "write_text", fail_final)
    result = mem.export_acquisition(MemoryScope(points=65536), ["CHAN1"], tmp_path)
    assert not result["complete"] and not result["manifest_current"]
    assert result["channels"]["CHAN1"]["complete"]  # samples themselves were validated
    assert result["persisted_points"] == {"CHAN1": 65536}
    assert "Final manifest update failed" in result["error"]
    assert not manifest(result)["complete"]


@pytest.mark.parametrize("backend", ["@py", "@ivi"])
def test_mid_block_timeout_abandons_stream(tmp_path, monkeypatch, backend):
    class TimeoutScope(MemoryScope):
        def read_bytes(self, count):
            if len(self.reads) == 2 and count > 9:
                super().read_bytes(100)  # some of this chunk has already arrived
                raise pyvisa.errors.VisaIOError(-1073807339)
            return super().read_bytes(count)

        def read_raw(self, size=None):
            if len(self.reads) == 2:
                raise pyvisa.errors.VisaIOError(-1073807339)
            return super().read_raw(size)

        def query(self, cmd):
            if len(self.reads) >= 2:
                pytest.fail("SCPI query attempted on desynchronized connection")
            return super().query(cmd)

    invalidated = []
    monkeypatch.setattr(mem, "invalidate_scope", lambda: invalidated.append(True))
    monkeypatch.setattr(sc, "_usb_backend_hint", backend)
    scope = TimeoutScope()
    result = mem.export_acquisition(scope, ["CHAN1"], tmp_path)
    assert not result["complete"] and result["manifest_current"]
    assert result["channels"]["CHAN1"]["points"] == 250000
    assert "VI_ERROR_TMO" in result["error"]
    assert not result["transfer_settings_restored"]
    assert scope.written[-1] == ":WAV:DATA?"
    assert len(scope.reads) == 2 and invalidated == [True]


def test_transfer_error_survives_final_manifest_failure(tmp_path, monkeypatch):
    original = Path.write_text

    def fail_final(path, text, *args, **kwargs):
        if path.name == "manifest.tmp" and "export_finished_at" in json.loads(text):
            raise OSError("disk full")
        return original(path, text, *args, **kwargs)

    monkeypatch.setattr(Path, "write_text", fail_final)
    scope = MemoryScope()
    scope.fail_at = 2
    result = mem.export_acquisition(scope, ["CHAN1"], tmp_path)
    assert "USB disconnected" in result["error"]
    assert "disk full" in result["manifest_error"]
    assert not result["complete"] and not result["manifest_current"]


@pytest.mark.parametrize("trailer", [b"EXTRA\n", b"\nEXTRA", b"\x00"])
def test_export_rejects_excess_native_message_bytes(tmp_path, monkeypatch, trailer):
    class ExcessScope(MemoryScope):
        def write(self, cmd):
            super().write(cmd)
            if cmd == ":WAV:DATA?":
                self.load(self._buf[:-1] + trailer)

    monkeypatch.setattr(sc, "_usb_backend_hint", "@ivi")
    result = mem.export_acquisition(ExcessScope(points=65536), ["CHAN1"], tmp_path)
    assert not result["complete"]
    assert "Unexpected bytes after" in result["error"]
    assert result["channels"]["CHAN1"]["points"] == 0


@pytest.mark.parametrize("trailer", [b"", b"\n", b"\r\n"])
def test_export_accepts_native_message_terminators(tmp_path, monkeypatch, trailer):
    class TerminatorScope(MemoryScope):
        def write(self, cmd):
            super().write(cmd)
            if cmd == ":WAV:DATA?":
                self.load(self._buf[:-1] + trailer)

    monkeypatch.setattr(sc, "_usb_backend_hint", "@ivi")
    result = mem.export_acquisition(TerminatorScope(points=65536), ["CHAN1"], tmp_path)
    assert result["complete"] and result["manifest_current"]
    assert "recovery_manifest" not in result


def test_export_rejects_corrupt_length_before_reading_payload(tmp_path):
    class CorruptLengthScope(MemoryScope):
        def write(self, cmd):
            super().write(cmd)
            if cmd == ":WAV:DATA?":
                self.load(b"#9999999999")

        def read_bytes(self, count):
            assert count <= 9, "should reject header before trying a huge payload read"
            return super().read_bytes(count)

    result = mem.export_acquisition(CorruptLengthScope(points=65536), ["CHAN1"], tmp_path)
    assert not result["complete"]
    assert "Unexpected payload length" in result["error"]


def test_info_reports_unsupported_dialect_without_memory_queries(monkeypatch):
    from rigol_mcp.drivers import DHO
    monkeypatch.setattr(sc, "_driver", DHO)

    class DHOScope(MemoryScope):
        def query(self, cmd):
            assert not cmd.startswith(":ACQ:")
            if cmd == "*IDN?":
                return "RIGOL TECHNOLOGIES,DHO924S,serial,firmware"
            return super().query(cmd)

    scope = DHOScope()
    result = mem.get_acquisition_info(scope)
    assert result["supported_transfer_modes"] == ["screen"]
    assert result["dialect"] == "DHO" and result["warnings"]
    assert not scope.written


@pytest.mark.parametrize("failure", ["visa", "os", "decode"])
@pytest.mark.parametrize("stage,points", [
    ("prepare", 0), ("preamble", 0), ("range_query", 250000),
    ("range_write", 250000), ("status", 250000), ("final_metadata", 500003),
    ("restore", 500003),
])
def test_transport_failure_stops_all_further_scpi(tmp_path, monkeypatch, failure, stage, points):
    class FailedSession(MemoryScope):
        failed = False

        def __init__(self):
            super().__init__()
            self.after_failure = []

        def check(self, operation, cmd):
            if self.failed:
                self.after_failure.append((operation, cmd))
            assert not self.failed, f"{operation} after transport failure: {cmd}"
            matches = {
                "prepare": operation == "write" and cmd == ":WAV:MODE RAW",
                "preamble": cmd == ":WAV:PRE?",
                "range_query": cmd == ":WAV:STAR?" and self.transfer["STAR"] == "250001",
                "range_write": cmd == ":WAV:STOP 500000",
                "status": cmd == ":TRIGger:STATus?" and len(self.reads) == 1,
                "final_metadata": cmd == ":ACQ:SRAT?" and bool(self.reads),
                "restore": cmd == ":WAV:MODE?" and self.transfer["MODE"] == "NORM" and bool(self.reads),
            }
            if matches[stage]:
                self.failed = True
                if failure == "visa":
                    raise pyvisa.errors.VisaIOError(-1073807339)
                if failure == "os":
                    raise OSError("USB connection lost")
                raise UnicodeDecodeError("ascii", b"\xff", 0, 1, "bad response")

        def query(self, cmd):
            self.check("query", cmd)
            return super().query(cmd)

        def write(self, cmd):
            self.check("write", cmd)
            return super().write(cmd)

    invalidated = []
    monkeypatch.setattr(mem, "invalidate_scope", lambda: invalidated.append(True))
    scope = FailedSession()
    result = mem.export_acquisition(scope, ["CHAN1"], tmp_path)
    assert scope.failed and invalidated == [True] and scope.after_failure == []
    assert result["manifest_current"] and not result["transfer_settings_restored"]
    assert sum(c["points"] for c in result["channels"].values()) == points
    # A failure only during restoration does not invalidate already verified data.
    assert result["complete"] == (stage == "restore")
    assert manifest(result)["complete"] == result["complete"]


@pytest.mark.parametrize("query", ["*IDN?", ":TRIGger:STATus?", ":WAV:FORM?"])
def test_preflight_timeout_discards_connection_without_export(tmp_path, monkeypatch, query):
    class FailedPreflight(MemoryScope):
        failed = False

        def query(self, cmd):
            assert not self.failed
            if cmd == query:
                self.failed = True
                raise pyvisa.errors.VisaIOError(-1073807339)
            return super().query(cmd)

    invalidated = []
    monkeypatch.setattr(mem, "invalidate_scope", lambda: invalidated.append(True))
    scope = FailedPreflight()
    with pytest.raises(pyvisa.errors.VisaIOError):
        mem.export_acquisition(scope, ["CHAN1"], tmp_path)
    assert invalidated == [True] and not scope.written
    assert not list(tmp_path.iterdir())
