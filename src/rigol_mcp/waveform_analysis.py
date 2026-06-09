"""Deterministic heuristics for waveform analysis."""

# Rigol scope screens are 8 vertical divisions tall, so full-scale Vpp = scale × 8.
SCREEN_V_DIVISIONS = 8
# A capture whose Vpp fills less than this fraction of the vertical screen is treated as
# noise-floor: the trace is dominated by ADC quantisation and front-end noise, so any
# frequency/shape reading would be spurious. Below this we suppress the interpretation
# entirely (and the secondary warnings it would otherwise spawn from noise crossings).
NOISE_FILL_FRACTION = 0.10
# Between NOISE_FILL_FRACTION and this, the signal is real but small relative to full
# scale (so noisy and imprecise): still analysed, but flagged with a low-amplitude warning.
LOW_FILL_FRACTION = 0.20
# Fraction of the half-amplitude the signal must travel past the mean before a mean-crossing
# is committed (a Schmitt-trigger band, the analogue of the scope's own trigger hysteresis).
# Without it, a noisy trace wobbles across the mean several times at each true crossing and
# the crossing count — and therefore the frequency — is inflated by an integer-ish factor.
CROSSING_HYSTERESIS = 0.25


def _fmt_si(value: float, unit: str) -> str:
    """Format a value with SI prefix (e.g. 1350000 Hz → '1.35 MHz')."""
    if value == 0:
        return f"0 {unit}"
    abs_v = abs(value)
    for threshold, prefix in ((1e9, "G"), (1e6, "M"), (1e3, "k"), (1.0, ""), (1e-3, "m"), (1e-6, "µ"), (1e-9, "n")):
        if abs_v >= threshold:
            return f"{value / threshold:.4g} {prefix}{unit}"
    return f"{value:.4g} {unit}"


def _hysteretic_crossings(v_c: list, times: list, n: int, hyst: float) -> list:
    """Mean-crossing instants detected with Schmitt-trigger hysteresis.

    A bare sign-change test (``v_c[i-1] * v_c[i] <= 0``) counts every wobble of a noisy
    signal across the mean, inflating the crossing count and hence the frequency. Here a
    crossing is only committed once the signal has travelled beyond ``±hyst`` of the mean,
    so noise wobble within the band is ignored. Returns interpolated true zero-crossing
    times, suitable for period estimation."""
    crossings = []
    state = 0        # +1 once above +hyst, -1 once below -hyst, 0 until the first commit
    pending = None   # latest raw mean-crossing time, awaiting confirmation by an extreme
    for i in range(1, n):
        if v_c[i - 1] * v_c[i] <= 0 and v_c[i] != v_c[i - 1]:
            pending = times[i - 1] + (times[i] - times[i - 1]) * (-v_c[i - 1]) / (v_c[i] - v_c[i - 1])
        if v_c[i] > hyst:
            if state == -1 and pending is not None:
                crossings.append(pending)
                pending = None
            state = 1
        elif v_c[i] < -hyst:
            if state == 1 and pending is not None:
                crossings.append(pending)
                pending = None
            state = -1
    return crossings


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

    # --- Vertical scale context (optional: only when the caller passes the channel scale) ---
    # Judging amplitude against the configured V/div is what tells noise apart from signal:
    # 320 mVpp is a real trace at 50 mV/div but pure noise at 1.25 V/div. Without this the
    # analyser would confidently report a frequency for what is just the noise floor.
    y_scale = data.get("y_scale_v_per_div")
    full_scale_vpp = y_scale * SCREEN_V_DIVISIONS if y_scale else None
    fill_frac = vpp / full_scale_vpp if full_scale_vpp else None
    if full_scale_vpp:
        lines.append(
            f"Vert   : {_fmt_si(y_scale,'V')}/div, {_fmt_si(full_scale_vpp,'V')} full screen "
            f"({SCREEN_V_DIVISIONS} div) — signal fills {fill_frac*100:.1f}% of vertical range"
        )

    # --- Noise-floor guard: a trace that barely fills the screen is almost certainly noise
    # (or an unconnected input). Flag it and suppress the shape/frequency interpretation
    # rather than reporting a spurious oscillation. ---
    if fill_frac is not None and fill_frac < NOISE_FILL_FRACTION:
        divs_pp = vpp / y_scale
        lines.append("Shape  : low-amplitude / likely noise — frequency & shape interpretation suppressed")
        lines.append("")
        lines.append("Warnings:")
        lines.append(
            f"  ⚠ Vpp ({_fmt_si(vpp,'V')}) is only {fill_frac*100:.1f}% of the {_fmt_si(full_scale_vpp,'V')} "
            f"vertical full-scale window ({_fmt_si(y_scale,'V')}/div, ≈{divs_pp:.2f} divisions peak-to-peak). "
            "At this level the trace is dominated by noise / ADC quantisation, so any frequency or shape "
            "reading would be meaningless. Reduce V/div (zoom in vertically) until the signal fills a few "
            "divisions, then re-capture — or check the probe/connection if you expected a larger signal."
        )
        return "\n".join(lines)

    # --- Zero crossings relative to mean (handles DC offset), with hysteresis so noise
    # wobble near the mean does not spawn spurious crossings and inflate the frequency. ---
    v_c = [v - vmean for v in voltages]
    hyst = (vpp / 2) * CROSSING_HYSTERESIS
    crossings = _hysteretic_crossings(v_c, times, n, hyst)

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

    # Low amplitude: real signal, but small relative to full scale so noisy and imprecise.
    # (Below NOISE_FILL_FRACTION we already returned above; this is the 10–20% band.)
    if fill_frac is not None and fill_frac < LOW_FILL_FRACTION:
        warnings.append(
            f"Low amplitude: Vpp ({_fmt_si(vpp,'V')}) fills only {fill_frac*100:.0f}% of the "
            f"{_fmt_si(full_scale_vpp,'V')} vertical window ({_fmt_si(y_scale,'V')}/div). The reading is "
            "usable but noisy — reduce V/div so the signal fills more of the screen for a cleaner measurement."
        )

    # Clipping: a genuinely clipped signal has a FLAT top/bottom — a run of consecutive
    # samples pinned at (nearly) the same extreme voltage. Proximity to a rail alone is not
    # enough: a clean sine dwells near its peaks by curvature (~15% of its samples land within
    # 2% of a rail) without ever going flat, so a near-rail count fires on every clean sine.
    # Look for flat runs instead, where successive samples barely change near the extreme.
    flat_eps  = max(vpp * 0.001, 1e-4)   # max adjacent-sample change within a "flat" run
    rail_band = max(vpp * 0.03, 1e-3)    # how close to the rail the run must sit
    min_run   = max(int(n * 0.01), 8)    # min consecutive flat samples to call it clipping

    def _max_flat_run(target):
        best = run = 0
        for i in range(n):
            if abs(voltages[i] - target) < rail_band:
                run = run + 1 if (run and abs(voltages[i] - voltages[i - 1]) < flat_eps) else 1
                best = max(best, run)
            else:
                run = 0
        return best

    if not is_pulse and vpp > 1e-3:
        flat_run = max(_max_flat_run(vmax), _max_flat_run(vmin))
        if flat_run >= min_run:
            warnings.append(
                f"Possible clipping: flat run of {flat_run} samples pinned at the voltage rail. "
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

    # DC baseline wander: compare head vs tail means, but average each over an integer number
    # of detected cycles rather than a fixed 10% slice. A periodic capture that simply spans a
    # non-integer number of cycles has head/tail slices that differ purely from partial-cycle
    # averaging (e.g. a window starting near +peak and ending near −peak) — that is not a
    # baseline shift. Averaging whole cycles cancels the AC content so each window reflects the
    # true local DC. Needs a trustworthy period (post-hysteresis) and at least ~3 cycles.
    if period_est and period_est > 0 and x_inc > 0:
        cyc_samples = int(round(period_est / x_inc))
        num_cycles = window_s / period_est
        if cyc_samples >= 4 and num_cycles >= 3 and 2 * cyc_samples <= n:
            mean_head = sum(voltages[:cyc_samples]) / cyc_samples
            mean_tail = sum(voltages[-cyc_samples:]) / cyc_samples
            if abs(mean_tail - mean_head) > vpp * 0.15 and vpp > 1e-3:
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
