"""Deterministic heuristics for waveform analysis."""


def _fmt_si(value: float, unit: str) -> str:
    """Format a value with SI prefix (e.g. 1350000 Hz → '1.35 MHz')."""
    if value == 0:
        return f"0 {unit}"
    abs_v = abs(value)
    for threshold, prefix in ((1e9, "G"), (1e6, "M"), (1e3, "k"), (1.0, ""), (1e-3, "m"), (1e-6, "µ"), (1e-9, "n")):
        if abs_v >= threshold:
            return f"{value / threshold:.4g} {prefix}{unit}"
    return f"{value:.4g} {unit}"


def describe_waveform(data: dict) -> str:
    """Produce a human-readable analysis of a waveform capture."""
    voltages = data["voltages_v"]
    times    = data["times_s"]
    n        = len(voltages)
    vmin     = data["vmin_v"]
    vmax     = data["vmax_v"]
    vmean    = data["vmean_v"]
    vpp      = vmax - vmin
    t_start  = data["time_start_s"]
    t_end    = data["time_end_s"]
    window_s = t_end - t_start
    x_inc    = data["time_increment_s"]
    ch       = data["channel"]

    lines = [f"=== Waveform: {ch} ==="]

    # --- Time window ---
    lines.append(
        f"Window : {_fmt_si(t_start,'s')} → {_fmt_si(t_end,'s')}  "
        f"({_fmt_si(window_s,'s')} total, {_fmt_si(x_inc,'s')}/point, {n} pts)"
    )

    # --- Amplitude ---
    lines.append(
        f"Voltage: Vpp={_fmt_si(vpp,'V')}, Vmin={_fmt_si(vmin,'V')}, "
        f"Vmax={_fmt_si(vmax,'V')}, DC offset={_fmt_si(vmean,'V')}"
    )

    # --- Zero crossings relative to mean (handles DC offset) ---
    v_c = [v - vmean for v in voltages]
    crossings = []
    for i in range(1, n):
        if v_c[i - 1] * v_c[i] <= 0 and v_c[i] != v_c[i - 1]:
            t_x = times[i - 1] + (times[i] - times[i - 1]) * (-v_c[i - 1]) / (v_c[i] - v_c[i - 1])
            crossings.append(t_x)

    # --- Pulse / square wave detection (bimodal: most points near rails) ---
    rail_thr = vpp * 0.15
    near_rail = sum(
        1 for v in voltages
        if abs(v - vmin) < rail_thr or abs(v - vmax) < rail_thr
    )
    is_pulse = (near_rail / n) > 0.70 and vpp > 1e-3

    # --- Signal classification ---
    freq_est = None
    period_est = None
    half_periods = []

    if vpp < 1e-3:
        lines.append("Shape  : DC / flat (Vpp < 1 mV)")

    elif is_pulse:
        duty = sum(1 for v in voltages if v > vmean) / n * 100
        lines.append(f"Shape  : pulse / square wave (~{duty:.0f}% duty cycle)")

    elif len(crossings) < 2:
        # No crossings → ramp or very slow signal
        diffs = [voltages[i] - voltages[i - 1] for i in range(1, min(50, n))]
        pos = sum(1 for d in diffs if d > 0)
        neg = sum(1 for d in diffs if d < 0)
        if pos > len(diffs) * 0.7:
            shape = "rising ramp / positive slope"
        elif neg > len(diffs) * 0.7:
            shape = "falling ramp / negative slope"
        else:
            shape = "non-periodic / complex"
        lines.append(f"Shape  : {shape} — timebase likely too narrow; widen to see complete cycles")

    else:
        half_periods = [crossings[i + 1] - crossings[i] for i in range(len(crossings) - 1)]
        avg_hp     = sum(half_periods) / len(half_periods)
        period_est = avg_hp * 2
        freq_est   = 1.0 / period_est if period_est > 0 else None
        num_cycles = window_s / period_est if period_est > 0 else 0

        # Detect envelope trend via RMS of first vs last third
        third = n // 3
        def _rms(seg):
            m = sum(seg) / len(seg)
            return (sum((v - m) ** 2 for v in seg) / len(seg)) ** 0.5

        rms_first = _rms(voltages[:third])
        rms_last  = _rms(voltages[-third:])
        envelope_ratio = rms_last / rms_first if rms_first > 0 else 1.0

        if envelope_ratio < 0.7:
            decay_pct = (1 - envelope_ratio) * 100
            shape = f"damped oscillation (RMS amplitude decays ~{decay_pct:.0f}% over capture)"
        elif envelope_ratio > 1.4:
            grow_pct = (envelope_ratio - 1) * 100
            shape = f"growing oscillation (RMS amplitude grows ~{grow_pct:.0f}% — capture may include startup/ramp-up)"
        else:
            shape = "sustained oscillation"

        lines.append(f"Shape  : {shape}")
        if freq_est:
            lines.append(
                f"Freq   : ~{_fmt_si(freq_est,'Hz')}  (period ~{_fmt_si(period_est,'s')},  "
                f"{num_cycles:.1f} cycles visible,  {len(crossings)} zero crossings)"
            )

    # --- Data quality warnings ---
    warnings = []
    edge_thr = max(vpp * 0.05, 4e-3)

    # Clipping: many points within 2% of Vmin or Vmax rail
    clip_thr = max(vpp * 0.02, 1e-3)
    n_clip = sum(1 for v in voltages if abs(v - vmax) < clip_thr or abs(v - vmin) < clip_thr)
    if n_clip / n > 0.03 and not is_pulse:
        warnings.append(
            f"Possible clipping: {n_clip/n*100:.0f}% of points at voltage rail. "
            "Increase V/div or reduce probe attenuation."
        )

    # Period jitter: high CV on half-period spacings, computed only over crossings where
    # the local signal amplitude is significant (filters out noise-floor crossings in damped signals)
    if period_est and len(half_periods) >= 4:
        sig_thr = vpp * 0.15
        sig_hps = [
            half_periods[i]
            for i in range(len(half_periods))
            if max((abs(v_c[j]) for j in range(n)
                    if crossings[i] <= times[j] <= crossings[i + 1]), default=0) > sig_thr
        ]
        if len(sig_hps) >= 4:
            hp_mean = sum(sig_hps) / len(sig_hps)
            hp_std = (sum((x - hp_mean) ** 2 for x in sig_hps) / len(sig_hps)) ** 0.5
            jitter_cv = hp_std / hp_mean if hp_mean > 0 else 0
            if jitter_cv > 0.20:
                warnings.append(
                    f"Period spacing jitter CV={jitter_cv:.0%} (over {len(sig_hps)} significant half-cycles) — "
                    "signal may be non-periodic, frequency-modulated, or aliased. "
                    "Verify sample rate vs signal frequency."
                )

    # Burst / partial capture: quiet segments at start or end
    if n >= 10:
        seg = max(n // 5, 2)
        def _seg_rms(s): return (sum((v - vmean) ** 2 for v in s) / len(s)) ** 0.5
        rms_head = _seg_rms(voltages[:seg])
        rms_tail = _seg_rms(voltages[-seg:])
        rms_body = _seg_rms(voltages[seg:-seg]) if n > 2 * seg else _seg_rms(voltages)
        if rms_body > 0:
            if rms_head < rms_body * 0.15:
                warnings.append(
                    "Signal is quiet at start then becomes active — burst/transient starts mid-capture. "
                    "Move trigger point earlier or use pre-trigger."
                )
            if rms_tail < rms_body * 0.15:
                warnings.append(
                    "Signal becomes quiet before capture ends — burst/transient ends mid-capture. "
                    "Widen timebase or move trigger point later."
                )

    # DC baseline wander: mean of first 10% vs last 10% differs, but only when both ends
    # have meaningful signal amplitude (suppresses false positives from damped signals)
    tenth = max(n // 10, 1)
    mean_head = sum(voltages[:tenth]) / tenth
    mean_tail = sum(voltages[-tenth:]) / tenth
    head_ac = max(abs(v - mean_head) for v in voltages[:tenth])
    tail_ac = max(abs(v - mean_tail) for v in voltages[-tenth:])
    if (abs(mean_tail - mean_head) > vpp * 0.15 and vpp > 1e-3
            and head_ac > vpp * 0.10 and tail_ac > vpp * 0.10):
        warnings.append(
            f"DC baseline shifts {_fmt_si(mean_tail - mean_head, 'V')} from start to end — "
            "capture may span a transient or settling event."
        )

    if len(crossings) < 4:
        warnings.append(
            "Fewer than 2 complete cycles captured — FREQUENCY measurement may return "
            "9.9E37 (scope's invalid sentinel). Widen timebase scale."
        )

    if abs(voltages[0] - vmean) > edge_thr:
        warnings.append(
            f"Left edge = {_fmt_si(voltages[0],'V')} (not at mean) — waveform starts mid-cycle. "
            "Trigger offset or timebase may need adjustment."
        )

    if abs(voltages[-1] - vmean) > edge_thr:
        if period_est:
            suggested = f"  To fix: set offset = N×{_fmt_si(period_est/2,'s')} − 6×scale for integer N."
        else:
            suggested = ""
        warnings.append(
            f"Right edge = {_fmt_si(voltages[-1],'V')} (not at mean) — waveform ends mid-cycle.{suggested}"
        )

    if warnings:
        lines.append("")
        lines.append("Warnings:")
        for w in warnings:
            lines.append(f"  ⚠ {w}")

    return "\n".join(lines)
