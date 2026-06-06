"""Unit tests for rigol_mcp.waveform_analysis — pure deterministic heuristics."""

import math

import pytest

from rigol_mcp.waveform_analysis import _fmt_si, describe_waveform


# --------------------------------------------------------------------------- SI formatting

@pytest.mark.parametrize("value,unit,expected", [
    (0, "Hz", "0 Hz"),
    (1_350_000, "Hz", "1.35 MHz"),
    (1_000, "Hz", "1 kHz"),
    (2.5, "V", "2.5 V"),
    (0.001, "s", "1 ms"),
    (1e-6, "s", "1 µs"),
    (1e-9, "s", "1 ns"),
    (-0.005, "V", "-5 mV"),
])
def test_fmt_si(value, unit, expected):
    assert _fmt_si(value, unit) == expected


# --------------------------------------------------------------------------- helpers

def _make_capture(voltages, x_inc=1e-6, x_origin=0.0, channel="CHAN1"):
    n = len(voltages)
    times = [x_origin + i * x_inc for i in range(n)]
    return {
        "channel": channel,
        "points": n,
        "time_increment_s": x_inc,
        "time_start_s": times[0] if times else 0.0,
        "time_end_s": times[-1] if times else 0.0,
        "vmin_v": min(voltages),
        "vmax_v": max(voltages),
        "vmean_v": sum(voltages) / n,
        "times_s": times,
        "voltages_v": voltages,
    }


def _sine(n=1000, cycles=5, amp=1.0, offset=0.0):
    return [offset + amp * math.sin(2 * math.pi * cycles * i / n) for i in range(n)]


# --------------------------------------------------------------------------- shape classification

def test_describe_sine_reports_frequency():
    # 5 cycles over a 1000-point, 1 µs/point window = 1 ms total -> 5 kHz
    data = _make_capture(_sine(n=1000, cycles=5), x_inc=1e-6)
    text = describe_waveform(data)
    assert "=== Waveform: CHAN1 ===" in text
    assert "oscillation" in text
    assert "kHz" in text  # frequency estimated and SI-formatted


def test_describe_dc_flat():
    data = _make_capture([0.5] * 500)
    text = describe_waveform(data)
    assert "DC / flat" in text


def test_describe_square_wave_detected():
    n = 1000
    volts = [1.0 if (i // 100) % 2 == 0 else -1.0 for i in range(n)]
    text = describe_waveform(_make_capture(volts))
    assert "pulse / square wave" in text
    assert "duty cycle" in text


def test_describe_rising_ramp():
    volts = [i / 100.0 for i in range(100)]  # monotonic rise, no crossings
    text = describe_waveform(_make_capture(volts))
    assert "rising ramp" in text


def test_describe_damped_oscillation():
    n = 1000
    volts = [math.exp(-3 * i / n) * math.sin(2 * math.pi * 6 * i / n) for i in range(n)]
    text = describe_waveform(_make_capture(volts))
    assert "damped oscillation" in text


# --------------------------------------------------------------------------- warnings

def test_warns_on_too_few_cycles():
    # less than 2 cycles -> FREQUENCY-sentinel warning
    data = _make_capture(_sine(n=200, cycles=1, amp=1.0))
    text = describe_waveform(data)
    assert "Fewer than 2 complete cycles" in text


def test_warns_on_mid_cycle_edges():
    # a sine shifted so it starts/ends away from its mean
    n = 600
    volts = [math.sin(2 * math.pi * 5 * i / n + 1.0) for i in range(n)]
    text = describe_waveform(_make_capture(volts))
    assert "mid-cycle" in text


def test_output_is_multiline_string():
    text = describe_waveform(_make_capture(_sine()))
    assert isinstance(text, str)
    assert text.count("\n") >= 2


# --------------------------------------------------------------------------- vertical-scale / noise floor

def test_low_amplitude_flagged_as_noise_and_frequency_suppressed():
    # 320 mVpp "signal" on a 1.25 V/div setting -> 10 V full screen -> 3.2% fill.
    # This is the reported bug: noise was being read as a ~225 kHz oscillation.
    data = _make_capture(_sine(n=1000, cycles=50, amp=0.16), x_inc=2e-6)
    data["y_scale_v_per_div"] = 1.25
    text = describe_waveform(data)
    assert "likely noise" in text
    assert "%" in text                 # fill fraction is referenced
    assert "kHz" not in text           # spurious frequency is NOT reported
    assert "oscillation" not in text   # spurious shape is NOT reported


def test_real_signal_with_scale_still_analysed():
    # 2 Vpp signal on 0.5 V/div -> 4 V full screen -> 50% fill: a genuine signal.
    data = _make_capture(_sine(n=1000, cycles=5, amp=1.0), x_inc=1e-6)
    data["y_scale_v_per_div"] = 0.5
    text = describe_waveform(data)
    assert "oscillation" in text
    assert "kHz" in text
    assert "full screen" in text       # vertical context line is shown
    assert "Low amplitude" not in text  # 50% fill is comfortably large


def test_small_but_real_signal_warns_low_amplitude():
    # 1.5 Vpp on 1.25 V/div -> 10 V full screen -> 15% fill: real but small (10-20% band).
    # Still analysed (shape + frequency reported) but flagged with a low-amplitude warning.
    data = _make_capture(_sine(n=1000, cycles=5, amp=0.75), x_inc=1e-6)
    data["y_scale_v_per_div"] = 1.25
    text = describe_waveform(data)
    assert "oscillation" in text       # interpretation NOT suppressed
    assert "kHz" in text
    assert "Low amplitude" in text      # but warned


def test_noise_floor_check_skipped_without_scale():
    # No vertical scale supplied -> behaviour unchanged (no noise warning, freq still reported).
    data = _make_capture(_sine(n=1000, cycles=50, amp=0.16), x_inc=2e-6)
    text = describe_waveform(data)
    assert "likely noise" not in text
    assert "oscillation" in text
