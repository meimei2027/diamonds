"""
cw_odmr_lock_in.py -- CW-ODMR frequency-resolved spectrum scan, same
resonance-sweep + inline-interlock design as cw_odmr.py's run_spectrum(),
but reading out via lock-in (SR830) synchronous detection instead of scope
segments.

See notes.md's "cw_odmr_lock_in.py operation notes" section for hardware
wiring, how lock-in readout works, phase calibration, and design rationale
for cmd_single/cmd_sweep_average -- and its "CW-ODMR contrast_check" /
"SR830 lock-in amplifier" sections for chop-synchronized pickup artifacts
found on this signal path and the controls needed to rule them out before
trusting a result.

Usage:
    python cw_odmr_lock_in.py run <file_name> [key=value ...]
        Sweeps microwave frequency across the resonance's FWHM (same
        coarse-then-fine resonance_sweep() as run_spectrum()), gating the
        microwave with a chopped square wave on AWG CH2 throughout, and
        records the SR830's X/Y lock-in output at each frequency -- same
        inline reflected-power interlock check per point as run_spectrum
        (generator kept at fixed CW power/frequency between checks, only
        the frequency changes). See cmd_run()'s docstring for the full
        list of key=value overrides. Saves
        data/<file_name>/<file_name>_lockin_spectrum_freqs_hz.npy,
        _lockin_spectrum_x.npy, _lockin_spectrum_y.npy,
        _lockin_spectrum_reflected_dbm.npy, _lockin_spectrum_metadata.txt.

    python cw_odmr_lock_in.py single <file_name> [key=value ...]
        Holds the generator at ONE fixed frequency (default 2.87 GHz, no
        resonance sweep) with the microwave chopped, and continuously reads
        the lock-in's X/Y until stopped (Ctrl+C) or duration_s elapses.
        Useful for phase-null calibration and for dialing in
        sensitivity/auto_gain live. See cmd_single()'s docstring for the
        full list of key=value overrides. Saves
        data/<file_name>/<file_name>_lockin_single_elapsed_s.npy,
        _lockin_single_x.npy, _lockin_single_y.npy,
        _lockin_single_reflected_dbm.npy, _lockin_single_metadata.txt.

    python cw_odmr_lock_in.py sweep-average <file_name> [key=value ...]
        Sweeps a FIXED frequency range (no resonance_sweep()/f0-finding)
        n_repeats times in a row, saving each repeat's raw sweep to disk
        immediately, then averaging X and Y (not R) across all completed
        repeats -- a way to beat down noise across many faster sweeps
        instead of (or in addition to) one very-long-time-constant sweep,
        with visibility into each repeat as it finishes. See
        cmd_sweep_average()'s docstring for the full list of key=value
        overrides. Also supports drive_power_dbm_list (e.g.
        "-40,-20,-10,0") to repeat the whole averaged sweep once per power
        level in the list, for a power-dependence study -- output files get
        a "_p{power_tag}" segment when this is used. Saves per repeat i:
        <file_name>_repeat{i}_freqs_hz.npy, _x.npy, _y.npy,
        _reflected_dbm.npy, and after all repeats:
        <file_name>_avg_freqs_hz.npy, _avg_x.npy, _avg_y.npy, _avg_r.npy,
        _avg_metadata.txt.

Example:
    python cw_odmr_lock_in.py run lockin1 chop_freq_hz=1000 phase_deg=42.3
    python cw_odmr_lock_in.py single calib1 freq_hz=2.87e9 auto_sensitivity=true
"""
import os
import sys
import time
from types import SimpleNamespace

import numpy as np

import ks33600a
from hp8673h import HP8673H
from sr830 import SR830
from spd1305x import SPD1305X, voltage_for_current
from spd1168x import SPD1168X
from cw_odmr import parse_kv_args, _tee_stdout

AWG_RESOURCE = "USB0::0x0957::0x5707::MY53800810::INSTR"
GEN_RESOURCE = "GPIB1::19::INSTR"
SA_RESOURCE = "GPIB0::18::INSTR"
SR830_RESOURCE = "GPIB2::2::INSTR"
# SPD1305X driving the static-field coil (see spd1305x.py) -- chosen for
# this role over the SPD1168X for its higher current limit.
COIL_PSU_RESOURCE = "USB0::0xF4EC::0x1410::SPD13DCD7R1877::INSTR"
# SPD1168X driving the RF amplifier supply -- confirmed via
# spd1168x_test.ipynb's rm.list_resources() output.
AMP_PSU_RESOURCE = "USB0::0xF4EC::0x1410::SPD13DCQ7R0986::INSTR"

DATA_DIR = "D:\\cw_odmr_lock_in"

USAGE = """\
python cw_odmr_lock_in.py run <file_name>
    use_resonance_sweep=true res_start_hz=2e9 res_stop_hz=3e9 coarse_step_hz=6.7e6
    fine_span_hz=20e6 fine_step_hz=50e3 res_power_dbm=-40 res_cal_dir=None
    start_hz=None stop_hz=None drive_power_dbm=0 threshold_dbm=-10 freq_step_hz=10e3
    fwhm_margin=1.0 settle_s=0.05 chop_freq_hz=1000 chop_duty_pct=50
    time_constant_s=0.1 settle_time_constants=5 sensitivity_v=5e-3
    auto_sensitivity=true phase_deg=0 input_coupling=ac ch1_carrier_freq_hz=80e6
    ch1_carrier_vpp=0.632 psu_voltage_v=12 psu_current_limit_a=1.9
    coil_current_a=1.5 coil_voltage_margin=1.5

python cw_odmr_lock_in.py single <file_name>
    freq_hz=2.87e9 drive_power_dbm=0 threshold_dbm=-10 carrier_off=false
    duration_s=0 sample_interval_s=1.0 chop_freq_hz=1000 chop_duty_pct=50
    time_constant_s=0.1 settle_time_constants=5 sensitivity_v=5e-3 auto_sensitivity=true phase_deg=0
    input_coupling=ac ch1_carrier_freq_hz=80e6 ch1_carrier_vpp=0.632
    coil_current_a=1.5 coil_voltage_margin=1.5

python cw_odmr_lock_in.py sweep-average <file_name>
    use_resonance_sweep=true res_start_hz=2e9 res_stop_hz=3e9 coarse_step_hz=6.7e6
    fine_span_hz=20e6 fine_step_hz=50e3 res_power_dbm=-40 res_cal_dir=None
    fwhm_margin=1.0 start_hz=None stop_hz=None freq_step_hz=1e6 n_repeats=30
    check_resonance_before_sweep=true print_points=false drive_power_dbm=0
    drive_power_dbm_list=None threshold_dbm=-10 auto_rescale_on_overload=true
    max_rescale_attempts=10 rescan_backoff_points=2 settle_s=0.05 chop_freq_hz=1000 chop_duty_pct=50
    time_constant_s=0.1 settle_time_constants=5 sensitivity_v=5e-3
    auto_sensitivity=true phase_deg=0 input_coupling=ac ch1_carrier_freq_hz=80e6
    ch1_carrier_vpp=0.632 psu_voltage_v=12 psu_current_limit_a=1.9
    coil_current_a=1.5 coil_voltage_margin=1.5"""


def check_resonance_position(gen, ilock_sa, freqs_hz, settle_s):
    """
    Quick reflected-power sweep across freqs_hz -- the SAME frequency range
    about to be used for the real measurement sweep -- purely for
    recording/reporting whether the resonance dip is still roughly where
    it's expected to be. This is NOT a new interlock gate (the existing
    inline interlock check during the real sweep still owns safety) --
    it's here because at low scan power, reflected power can stay well
    under the interlock threshold even after the resonance has drifted
    well away from the intended window, since low drive power keeps
    reflected power low either way regardless of how well-matched the
    resonator actually is at that frequency. Confirmed on the bench: real
    physical resonance drift went undetected for exactly this reason.

    Returns a dict: {"freqs_hz", "reflected_dbm", "dip_freq_hz", "dip_dbm",
    "offset_from_center_hz"} -- the last one is how far the dip is from the
    center of freqs_hz, the simplest signal that the resonance has moved
    relative to where this sweep assumes it is.
    """
    reflected_dbm_arr = np.full(len(freqs_hz), np.nan)
    for i, f in enumerate(freqs_hz):
        gen.set_frequency_hz(f)
        time.sleep(settle_s)
        reflected_dbm = HP8673H.read_reflected_power_dbm(ilock_sa, f)
        reflected_dbm_arr[i] = reflected_dbm if reflected_dbm is not None else np.nan

    if np.all(np.isnan(reflected_dbm_arr)):
        return {"freqs_hz": freqs_hz, "reflected_dbm": reflected_dbm_arr,
                "dip_freq_hz": np.nan, "dip_dbm": np.nan,
                "offset_from_center_hz": np.nan}

    dip_idx = int(np.nanargmin(reflected_dbm_arr))
    dip_freq_hz = freqs_hz[dip_idx]
    dip_dbm = reflected_dbm_arr[dip_idx]
    center_hz = (freqs_hz[0] + freqs_hz[-1]) / 2
    return {
        "freqs_hz": freqs_hz,
        "reflected_dbm": reflected_dbm_arr,
        "dip_freq_hz": dip_freq_hz,
        "dip_dbm": dip_dbm,
        "offset_from_center_hz": dip_freq_hz - center_hz,
    }


def _step_sensitivity_coarser(lia):
    """Step the SR830 to the next LESS sensitive (larger full-scale) range,
    one step at a time -- used to back out of a real-time overload
    condition mid-sweep without jumping straight to auto_gain() (which
    could overshoot to an overly-insensitive range based on whatever the
    signal happens to be doing at that specific frequency point). Returns
    the new sensitivity in volts; unchanged if already at the least
    sensitive range."""
    current_v = lia.get_sensitivity_v()
    idx = SR830.SENSITIVITY_V.index(current_v)
    new_idx = min(idx + 1, len(SR830.SENSITIVITY_V) - 1)
    new_v = SR830.SENSITIVITY_V[new_idx]
    if new_idx != idx:
        lia.set_sensitivity_v(new_v)
    return new_v


def _power_tag(power_dbm):
    """Filesystem-safe tag for a dBm value, e.g. -40.0 -> 'm40dBm',
    0.0 -> '0dBm', -3.5 -> 'm3p5dBm' -- used to disambiguate per-power
    output files in cmd_sweep_average()'s drive_power_dbm_list mode."""
    sign = "m" if power_dbm < 0 else ""
    return f"{sign}{abs(power_dbm):g}dBm".replace(".", "p")


def setup_ch1_carrier(awg, carrier_freq_hz=80e6, carrier_vpp=0.632):
    """CH1: continuous, unmodulated sine carrier, independent of CH2's chop
    -- unrelated to the alignment tone in run_alignment.py, just needs to
    stay running throughout the measurement. Set once right after
    connecting to the AWG; not touched again for the rest of the run."""
    awg.write("OUTP1:LOAD 50")
    awg.write("SOUR1:FUNC SIN")
    awg.write(f"SOUR1:FREQ {carrier_freq_hz}")
    awg.write("SOUR1:VOLT:UNIT VPP")
    awg.write(f"SOUR1:VOLT {carrier_vpp}")
    awg.write("OUTPUT1 ON")


def setup_chop(awg, chop_freq_hz, chop_duty_pct):
    """CH2: TTL-level square wave gating the microwave via the ZYSWA switch,
    also fed to the SR830's external reference input (see module docstring
    for the physical tee)."""
    # Zero the offset BEFORE switching function -- if CH2 is coming out of
    # set_switch_static()'s DC mode (offset 5V, amplitude ~0), switching
    # straight to SQU with that stale 5V offset momentarily violates the
    # new function's offset+amplitude/2 limit. The instrument auto-clips it
    # and reports that as a settings-conflict error (-221, "offset changed
    # on exit from DC function") -- which write() treats as fatal since it
    # raises on ANY nonzero error code. Confirmed on real hardware. Offset 0
    # is safe regardless of function/amplitude, so reset it first, then
    # switch function, then set amplitude and only THEN the real offset.
    awg.write("SOUR2:VOLT:OFFS 0")
    awg.write("SOUR2:FUNC SQU")
    awg.write(f"SOUR2:FUNC:SQU:DCYCle {chop_duty_pct}")
    awg.write(f"SOUR2:FREQ {chop_freq_hz}")
    awg.write("OUTP2:LOAD INF")
    awg.write("SOUR2:VOLT:UNIT VPP")
    awg.write("SOUR2:VOLT 5.0")
    awg.write("SOUR2:VOLT:OFFS 2.5")
    awg.write("TRIG2:SOUR IMM")
    awg.write("OUTPUT2 ON")


def set_switch_static(awg, route_to_sample=True):
    """Hold CH2 at a fixed DC level instead of chopping, parking the ZYSWA
    switch on one port continuously -- per the truth table (see
    pulsed_odmr.py / notes.md), control HIGH routes RF IN -> RF2 (the
    sample path, onward to the amp/resonator/analyzer chain) and LOW routes
    to RF1 (dummy load). Used during the resonance sweep, BEFORE chopping
    starts: if CH2 is already chopping while resonance_sweep() reads the
    analyzer, the analyzer sees RF flipping between the sample path and the
    dump path on every chop cycle -- amplitude-modulation sidebands/garbage
    riding on top of the resonance dip, not a clean reflected-power trace."""
    level_v = 5.0 if route_to_sample else 0.0
    awg.write("SOUR2:FUNC DC")
    awg.write(f"SOUR2:VOLT:OFFS {level_v}")
    awg.write("OUTP2:LOAD INF")
    awg.write("OUTPUT2 ON")


def setup_lock_in(lia, chop_freq_hz, time_constant_s, sensitivity_v, phase_deg,
                   input_coupling, auto_sensitivity=False):
    lia.set_reference_external(slope="ttl_rising")
    lia.set_phase_deg(phase_deg)
    lia.set_input_config("a")  # voltage input -- PMT signal already amplified
    lia.set_input_coupling(ac=(input_coupling == "ac"))
    lia.set_time_constant_s(time_constant_s)
    if not auto_sensitivity:
        lia.set_sensitivity_v(sensitivity_v)
    # else: leave sensitivity as-is for now -- cmd_run calls auto_gain() once
    # RF/chop is actually running and a real signal is present at the input,
    # which AGAN needs in order to pick a sensible range.
    lia.set_filter_slope_db_oct(24)


# ---------------------------------------------------------------------------
# Shared steps -- cmd_run, cmd_single and cmd_sweep_average are built from
# these, so a fix to one step (interlock, resonance sweep, shutdown order...)
# applies to every command.
# ---------------------------------------------------------------------------

def _flag(kw, key, default):
    return str(kw.get(key, default)).lower() == "true"


def _parse_common(kw):
    """Options every command shares: AWG, lock-in, supplies, interlock, settle times."""
    return SimpleNamespace(
        threshold_dbm=float(kw.get("threshold_dbm", -10.0)),
        settle_s=float(kw.get("settle_s", 0.05)),  # NEVER 0 -- see hp8673h.py's frequency_sweep()
        settle_time_constants=float(kw.get("settle_time_constants", 5.0)),
        chop_freq_hz=float(kw.get("chop_freq_hz", 1000.0)),
        chop_duty_pct=float(kw.get("chop_duty_pct", 50.0)),
        time_constant_s=float(kw.get("time_constant_s", 0.1)),
        sensitivity_v=float(kw.get("sensitivity_v", 5e-3)),
        auto_sensitivity=_flag(kw, "auto_sensitivity", "true"),
        phase_deg=float(kw.get("phase_deg", 0.0)),
        input_coupling=str(kw.get("input_coupling", "ac")),
        ch1_carrier_freq_hz=float(kw.get("ch1_carrier_freq_hz", 80e6)),
        ch1_carrier_vpp=float(kw.get("ch1_carrier_vpp", 0.632)),
        psu_voltage_v=float(kw.get("psu_voltage_v", 12.0)),
        psu_current_limit_a=float(kw.get("psu_current_limit_a", 1.9)),
        coil_current_a=float(kw.get("coil_current_a", 1.5)),
        coil_voltage_margin=float(kw.get("coil_voltage_margin", 1.5)),
    )


def _parse_scan_range(kw):
    """Options that decide the frequency range: resonance sweep and/or manual start/stop."""
    r = SimpleNamespace(
        use_resonance_sweep=_flag(kw, "use_resonance_sweep", "true"),
        res_start_hz=float(kw.get("res_start_hz", 2.0e9)),
        res_stop_hz=float(kw.get("res_stop_hz", 3.0e9)),
        coarse_step_hz=float(kw.get("coarse_step_hz", 6.7e6)),
        fine_span_hz=float(kw.get("fine_span_hz", 20e6)),
        fine_step_hz=float(kw.get("fine_step_hz", 50e3)),
        res_power_dbm=float(kw.get("res_power_dbm", -40.0)),
        res_cal_dir=kw.get("res_cal_dir", None),
        fwhm_margin=float(kw.get("fwhm_margin", 1.0)),
        manual_start_hz=kw.get("start_hz", None),
        manual_stop_hz=kw.get("stop_hz", None),
    )
    if not r.use_resonance_sweep and (r.manual_start_hz is None or r.manual_stop_hz is None):
        raise ValueError("start_hz and stop_hz must both be given when use_resonance_sweep=false")
    return r


def _run_paths(file_name, log_suffix):
    """Create DATA_DIR/<file_name>/ and DATA_DIR/logs/; return (run_path, log_path)."""
    run_path = f"{DATA_DIR}/{file_name}"
    os.makedirs(run_path, exist_ok=True)
    log_dir = f"{DATA_DIR}/logs"
    os.makedirs(log_dir, exist_ok=True)
    return run_path, f"{log_dir}/{file_name}_{log_suffix}.txt"


def _save_arrays(prefix, **arrays):
    """np.save each array to <prefix>_<key>.npy."""
    for key, values in arrays.items():
        np.save(f"{prefix}_{key}.npy", np.asarray(values))


def _write_metadata(path, **values):
    with open(path, "w") as fh:
        for key, value in values.items():
            fh.write(f"{key}={value}\n")


def _setup_awg_and_lockin(o, chop_now, step):
    """CH1 continuous carrier; CH2 chopping (chop_now) or parked on the sample
    path for a resonance sweep; SR830 configured. Returns (awg, lia).

    The AWG session stays open until _shutdown() so CH2 can be switched later
    (resonance sweep -> chop, resonance checks) -- reconnecting would *RST the
    AWG and drop the CH1 carrier."""
    print(f"[cw_odmr_lock_in] {step}: configuring AWG CH1 (continuous carrier) + CH2 "
          f"({'chop' if chop_now else 'static, not chopping yet'}) + SR830 lock-in")
    awg = ks33600a.KS33600A(AWG_RESOURCE, debug=True)
    setup_ch1_carrier(awg, o.ch1_carrier_freq_hz, o.ch1_carrier_vpp)
    if chop_now:
        setup_chop(awg, o.chop_freq_hz, o.chop_duty_pct)
    else:
        # Hold the switch parked on the sample path (RF2) instead of chopping
        # for now -- resonance_sweep() reads the analyzer and needs a clean,
        # unmodulated signal. Chopping starts once resonance is found (see
        # cw_odmr_lock_in's notes.md entry on why this ordering matters).
        set_switch_static(awg, route_to_sample=True)

    lia = SR830(SR830_RESOURCE, debug=True)
    setup_lock_in(lia, o.chop_freq_hz, o.time_constant_s, o.sensitivity_v, o.phase_deg,
                  o.input_coupling, auto_sensitivity=o.auto_sensitivity)
    chop_note = (f"{o.chop_duty_pct:.0f}% duty cycle chop @ {o.chop_freq_hz:.0f} Hz, "
                 if chop_now else "")
    print(f"[cw_odmr_lock_in] {step} done: CH1 continuous carrier "
          f"{o.ch1_carrier_freq_hz/1e6:.1f} MHz / {o.ch1_carrier_vpp*1e3:.0f} mVpp; "
          f"{chop_note}time constant {o.time_constant_s*1e3:.1f} ms, phase {o.phase_deg} deg "
          f"(NOT auto-calibrated -- see module docstring)")
    return awg, lia


def _turn_on_supplies(o, amp_psu, coil_psu):
    """Amplifier supply (skipped if amp_psu is None) and coil supply on."""
    if amp_psu is not None:
        amp_psu.turn_on(o.psu_voltage_v, o.psu_current_limit_a)
    coil_voltage_v = voltage_for_current(o.coil_current_a) * o.coil_voltage_margin
    coil_psu.turn_on(coil_voltage_v, o.coil_current_a)


def _find_scan_range(gen, awg, o, r, output_prefix, step):
    """Return (start_hz, stop_hz). With use_resonance_sweep: run the coarse-then-
    fine resonance_sweep() (switch still static), start the chop, and take the
    range from f0 +/- fwhm_margin*FWHM/2 unless start_hz/stop_hz were given."""
    if not r.use_resonance_sweep:
        return float(r.manual_start_hz), float(r.manual_stop_hz)

    print(f"[cw_odmr_lock_in] {step}: sweeping for resonance "
          f"({r.res_start_hz/1e9:.4f}-{r.res_stop_hz/1e9:.4f} GHz, {r.res_power_dbm} dBm)")
    from e4403b import E4403B
    sa = E4403B(SA_RESOURCE)
    result = gen.resonance_sweep(
        sa, r.res_start_hz, r.res_stop_hz, r.coarse_step_hz, r.fine_span_hz, r.fine_step_hz,
        r.res_power_dbm, output_prefix=output_prefix, cal_dir=r.res_cal_dir,
    )
    f0_hz = result["f0_hz"]
    fwhm_hz = result["fwhm_hz"]
    print(f"[cw_odmr_lock_in] {step} done: f0 = {f0_hz/1e9:.5f} GHz, "
          f"FWHM = {fwhm_hz/1e6:.3f} MHz, Q ~= {result['Q']:.0f}")
    sa.go_to_local()
    sa.close()

    print(f"[cw_odmr_lock_in] enabling switch chop now that resonance is located "
          f"({o.chop_duty_pct:.0f}% duty @ {o.chop_freq_hz:.0f} Hz)")
    setup_chop(awg, o.chop_freq_hz, o.chop_duty_pct)

    if r.manual_start_hz is not None and r.manual_stop_hz is not None:
        start_hz, stop_hz = float(r.manual_start_hz), float(r.manual_stop_hz)
        print(f"[cw_odmr_lock_in] using manually-given range {start_hz/1e9:.5f}-"
              f"{stop_hz/1e9:.5f} GHz instead of the resonance-sweep-derived one")
        return start_hz, stop_hz
    span_hz = fwhm_hz * r.fwhm_margin
    return f0_hz - span_hz / 2, f0_hz + span_hz / 2


def _arm_generator(gen, power_dbm, freq_hz, rf_on=True):
    """Preset, set power and frequency, RF on (or explicitly off), wait 1 s to settle."""
    gen.preset()
    gen.set_power_dbm(power_dbm)
    gen.set_frequency_hz(freq_hz)
    if rf_on:
        gen.rf_on()
    else:
        gen.rf_off()  # explicit, don't rely on preset()'s default state
    time.sleep(1.0)  # let the initial frequency/level settle


def _auto_gain(lia, wait_s, note=""):
    """AGAN with a real signal present, wait, and return the chosen sensitivity."""
    lia.auto_gain()
    time.sleep(wait_s)
    sensitivity_v = lia.get_sensitivity_v()
    print(f"[cw_odmr_lock_in] auto_sensitivity: AGAN selected {sensitivity_v:.3e} V full scale{note}")
    return sensitivity_v


def _connect_interlock(gen):
    """Open the interlock analyzer session; trip the generator and return None if unreachable."""
    ilock_sa = HP8673H.try_connect_analyzer(SA_RESOURCE)
    if ilock_sa is None:
        gen.trip_interlock("spectrum analyzer not reachable at startup")
    return ilock_sa


def _check_interlock(gen, ilock_sa, freq_hz, threshold_dbm, where):
    """Read reflected power at freq_hz; trip the generator if the analyzer is
    unreachable or the reading is above threshold. Returns (reflected_dbm, ok);
    reflected_dbm is None if there was no reading."""
    reflected_dbm = HP8673H.read_reflected_power_dbm(ilock_sa, freq_hz)
    if reflected_dbm is not None and reflected_dbm <= threshold_dbm:
        return reflected_dbm, True
    reason = ("spectrum analyzer unreachable" if reflected_dbm is None else
              f"reflected power {reflected_dbm:.2f} dBm exceeds threshold {threshold_dbm} dBm")
    gen.trip_interlock(f"{reason} at {freq_hz/1e9:.5f} GHz ({where})")
    return reflected_dbm, False


def _measure_point(gen, lia, ilock_sa, freq_hz, o, where):
    """Step to freq_hz, interlock-check it, let the lock-in settle, read X/Y.
    Returns (reflected_dbm, x, y); x and y are None if the interlock tripped."""
    gen.set_frequency_hz(freq_hz)
    time.sleep(o.settle_s)
    reflected_dbm, ok = _check_interlock(gen, ilock_sa, freq_hz, o.threshold_dbm, where)
    if not ok:
        return reflected_dbm, None, None
    # Let the lock-in's own low-pass filter settle to the new steady-state
    # value before reading -- NOT the same as settle_s (the generator's own
    # retuning settle): changing frequency changes what's being demodulated,
    # and the filter needs several time constants to converge regardless of
    # how fast the generator itself settled.
    time.sleep(o.settle_time_constants * o.time_constant_s)
    x, y = lia.read_xy()
    return reflected_dbm, x, y


def _try(action, warning=None):
    """Run one shutdown step; print warning (if given) instead of raising on failure."""
    try:
        action()
    except Exception as e:
        if warning:
            print(f"[cw_odmr_lock_in] WARNING: {warning} ({e})")


def _shutdown(gen, coil_psu, lia, awg, amp_psu=None, ilock_sa=None):
    """RF off first, then supplies, then release every instrument -- one step
    failing doesn't skip the rest."""
    _try(gen.rf_off, "failed to turn off RF cleanly")
    _try(gen.go_to_local)
    gen.close()
    if amp_psu is not None:
        _try(amp_psu.turn_off, "failed to turn off amplifier supply cleanly")
        amp_psu.close()
    _try(coil_psu.turn_off, "failed to turn off coil supply cleanly")
    _try(coil_psu.go_to_local)
    coil_psu.close()
    if ilock_sa is not None:
        ilock_sa.close()
    _try(lia.go_to_local)
    lia.close()
    _try(awg.close)


def cmd_run(file_name, **kw):
    """
    Sweep microwave frequency across the resonance's FWHM (coarse-then-fine
    resonance_sweep() first to find it), gating the microwave with a
    chopped square wave on AWG CH2 throughout, and record the SR830's X/Y
    at each frequency. Same inline reflected-power interlock check per
    point as run_spectrum -- generator held at fixed CW power/frequency
    between checks, only the frequency changes.

    Recognized key=value overrides (all optional):
      use_resonance_sweep=true  run resonance_sweep() first to find f0/FWHM
                                (switch held static, not chopping) and
                                derive the scan range from it -- set false
                                to skip and require start_hz/stop_hz
      res_start_hz=2e9        resonance sweep start frequency
      res_stop_hz=3e9         resonance sweep stop frequency
      coarse_step_hz=6.7e6    coarse sweep step (track expected FWHM)
      fine_span_hz=20e6       fine sweep span around the coarse dip
      fine_step_hz=50e3       fine sweep step
      res_power_dbm=-40.0     drive power during the resonance sweep
      res_cal_dir=None        open-short-load calibration folder (see
                              hp8673h.py's calibrate_osl()) -- omit for
                              uncalibrated raw reflected power
      drive_power_dbm=0.0     (constant) drive power during the scan
      threshold_dbm=-10.0     interlock trip threshold, in dBm
      freq_step_hz=10e3       frequency step across the scan range
      fwhm_margin=1.0         scan span = FWHM * this margin (only used
                              when start_hz/stop_hz aren't given)
      start_hz=None           explicit scan start frequency -- overrides
                              the resonance-sweep-derived range if given;
                              required if use_resonance_sweep=false
      stop_hz=None            explicit scan stop frequency -- overrides
                              the resonance-sweep-derived range if given;
                              required if use_resonance_sweep=false
      settle_s=0.05           settle time after each frequency change --
                              NEVER 0, see hp8673h.py's frequency_sweep()
      chop_freq_hz=1000.0     AWG CH2 / lock-in reference frequency
      chop_duty_pct=50.0      AWG CH2 duty cycle
      time_constant_s=0.1     SR830 time constant
      settle_time_constants=5.0  time constants to wait after each
                              frequency change before reading (on top of
                              settle_s)
      sensitivity_v=5e-3      SR830 sensitivity target -- ignored unless
                              auto_sensitivity=false
      auto_sensitivity=true   call SR830 AGAN once RF/chop is running,
                              before the sweep starts, instead of trusting
                              a fixed sensitivity_v; picked range is
                              printed and saved to the metadata file
      phase_deg=0.0           pre-calibrated SR830 phase (NOT
                              auto-calibrated by this script)
      input_coupling=ac       SR830 input coupling, 'ac' or 'dc'
      ch1_carrier_freq_hz=80e6  AWG CH1 continuous carrier frequency,
                              unrelated to the chop
      ch1_carrier_vpp=0.632   AWG CH1 carrier amplitude
      psu_voltage_v=12.0      SPD1168X output voltage for the RF amplifier
                              supply (see spd1168x.py) -- on before the
                              resonance sweep, off when the run ends
      psu_current_limit_a=1.9  SPD1168X current limit
      coil_current_a=1.5      SPD1305X current setpoint for the static-
                              field coil (see spd1305x.py; chosen over the
                              SPD1168X for its higher current limit) --
                              turned on/off alongside the amplifier supply
      coil_voltage_margin=1.5  voltage headroom above the calibration's
                              expected coil voltage drop at coil_current_a,
                              so the supply regulates in constant-current
                              mode (same convention as spd1305x.py's
                              set_field())

    Saves data/<file_name>/<file_name>_lockin_spectrum_freqs_hz.npy,
    _lockin_spectrum_x.npy, _lockin_spectrum_y.npy,
    _lockin_spectrum_reflected_dbm.npy, _lockin_spectrum_metadata.txt.
    """
    r = _parse_scan_range(kw)
    o = _parse_common(kw)
    drive_power_dbm = float(kw.get("drive_power_dbm", 0.0))
    freq_step_hz = float(kw.get("freq_step_hz", 10e3))
    run_path, log_path = _run_paths(file_name, "lockin_spectrum")

    with _tee_stdout(log_path):
        awg, lia = _setup_awg_and_lockin(o, chop_now=not r.use_resonance_sweep, step="step 1/4")

        print("[cw_odmr_lock_in] step 2/4: connecting to HP8673H + E4403B (interlock) "
              "+ SPD1168X (amplifier supply) + SPD1305X (coil supply)")
        gen = HP8673H(GEN_RESOURCE)
        amp_psu = SPD1168X(AMP_PSU_RESOURCE)
        coil_psu = SPD1305X(COIL_PSU_RESOURCE)
        ilock_sa = None
        try:
            _turn_on_supplies(o, amp_psu, coil_psu)
            start_hz, stop_hz = _find_scan_range(gen, awg, o, r, f"{run_path}/{file_name}_resonance",
                                                 step="step 2/4")

            freqs_hz = np.arange(start_hz, stop_hz + freq_step_hz / 2, freq_step_hz)
            print(f"[cw_odmr_lock_in] step 3/4: lock-in spectrum scan "
                  f"{freqs_hz[0]/1e9:.5f}-{freqs_hz[-1]/1e9:.5f} GHz "
                  f"({len(freqs_hz)} points, {freq_step_hz/1e3:.1f} kHz step), "
                  f"drive {drive_power_dbm} dBm, threshold {o.threshold_dbm} dBm")

            _arm_generator(gen, drive_power_dbm, freqs_hz[0])
            sensitivity_v = o.sensitivity_v
            if o.auto_sensitivity:
                # A real signal (chop + RF) is present now -- let AGAN pick a
                # range, then wait the same settle margin used between points.
                sensitivity_v = _auto_gain(lia, o.settle_time_constants * o.time_constant_s)

            ilock_sa = _connect_interlock(gen)
            if ilock_sa is None:
                return

            x_values = np.full(len(freqs_hz), np.nan)
            y_values = np.full(len(freqs_hz), np.nan)
            reflected_dbm_arr = np.full(len(freqs_hz), np.nan)
            n_completed = 0

            for i, f in enumerate(freqs_hz):
                reflected_dbm, x, y = _measure_point(gen, lia, ilock_sa, f, o,
                                                     f"point {i + 1}/{len(freqs_hz)}")
                reflected_dbm_arr[i] = reflected_dbm if reflected_dbm is not None else np.nan
                if x is None:
                    break
                x_values[i] = x
                y_values[i] = y
                n_completed = i + 1
                print(f"[cw_odmr_lock_in] point {n_completed}/{len(freqs_hz)}: "
                      f"f={f/1e9:.5f} GHz, X={x:.6e} V, Y={y:.6e} V, "
                      f"reflected={reflected_dbm:.2f} dBm")

            tripped = n_completed < len(freqs_hz)
            if n_completed == 0:
                print("[cw_odmr_lock_in] step 3/4 FAILED: no points completed -- nothing to save")
            else:
                prefix = f"{run_path}/{file_name}_lockin_spectrum"
                _save_arrays(prefix, freqs_hz=freqs_hz[:n_completed], x=x_values[:n_completed],
                             y=y_values[:n_completed], reflected_dbm=reflected_dbm_arr[:n_completed])
                _write_metadata(f"{prefix}_metadata.txt",
                                chop_freq_hz=o.chop_freq_hz, chop_duty_pct=o.chop_duty_pct,
                                time_constant_s=o.time_constant_s,
                                settle_time_constants=o.settle_time_constants,
                                sensitivity_v=sensitivity_v, phase_deg=o.phase_deg,
                                drive_power_dbm=drive_power_dbm)
                print(f"[cw_odmr_lock_in] step 3/4 done"
                      f"{' (PARTIAL -- interlock tripped)' if tripped else ''}: "
                      f"saved {prefix}_freqs_hz.npy ({n_completed} points), "
                      f"_lockin_spectrum_x.npy, _lockin_spectrum_y.npy, "
                      f"_lockin_spectrum_reflected_dbm.npy, _lockin_spectrum_metadata.txt")
        finally:
            print("[cw_odmr_lock_in] step 4/4: shutting down")
            _shutdown(gen, coil_psu, lia, awg, amp_psu=amp_psu, ilock_sa=ilock_sa)

    print("[cw_odmr_lock_in] done")


def cmd_single(file_name, **kw):
    """
    Hold the generator at a single fixed frequency (default 2.87 GHz) with
    the microwave chopped, and continuously read the lock-in's X/Y at a
    fixed interval until stopped -- no resonance sweep, no frequency
    stepping. Useful for phase-null calibration and for dialing in
    sensitivity/auto_gain live (watch OVLD-adjacent numbers change in real
    time) without running a full spectrum scan.

    Same inline reflected-power interlock check as cmd_run, re-checked
    every sample. Stop with Ctrl+C -- partial data is still saved.

    carrier_off=true is a diagnostic for isolating real-RF-power effects
    from AWG CH2 control-line crosstalk -- see notes.md's
    "cw_odmr_lock_in.py operation notes" section.

    Recognized key=value overrides (all optional):
      freq_hz=2.87e9          fixed generator frequency (tuned even with
                               carrier_off=true, in case you flip it back on)
      drive_power_dbm=0.0     generator CW power
      threshold_dbm=-10.0     interlock trip threshold, in dBm
      carrier_off=false       keep RF output off throughout -- see above
      duration_s=0            total run time in seconds; 0 = run until Ctrl+C
      sample_interval_s=1.0   time between lock-in reads (on top of the
                               lock-in's own time-constant settle)

    See cmd_run() above: threshold_dbm, chop_freq_hz, chop_duty_pct,
    time_constant_s, settle_time_constants (here only the wait after AGAN),
    sensitivity_v, auto_sensitivity, phase_deg, input_coupling,
    ch1_carrier_freq_hz, ch1_carrier_vpp, coil_current_a, coil_voltage_margin.
    No amplifier supply in this mode, so no psu_voltage_v/psu_current_limit_a.
    """
    o = _parse_common(kw)
    freq_hz = float(kw.get("freq_hz", 2.87e9))
    drive_power_dbm = float(kw.get("drive_power_dbm", 0.0))
    carrier_off = _flag(kw, "carrier_off", "false")
    duration_s = float(kw.get("duration_s", 0.0))  # 0 = run until Ctrl+C
    sample_interval_s = float(kw.get("sample_interval_s", 1.0))
    run_path, log_path = _run_paths(file_name, "lockin_single")

    with _tee_stdout(log_path):
        awg, lia = _setup_awg_and_lockin(o, chop_now=True, step="step 1/3")

        print(f"[cw_odmr_lock_in] step 2/3: connecting to HP8673H + E4403B (interlock) "
              f"+ SPD1305X (coil supply), fixed frequency {freq_hz/1e9:.5f} GHz, "
              f"{drive_power_dbm} dBm"
              f"{' -- CARRIER OFF (control-line-pickup diagnostic)' if carrier_off else ''}")
        gen = HP8673H(GEN_RESOURCE)
        coil_psu = SPD1305X(COIL_PSU_RESOURCE)
        ilock_sa = None
        try:
            _turn_on_supplies(o, None, coil_psu)  # no amplifier supply in this mode
            _arm_generator(gen, drive_power_dbm, freq_hz, rf_on=not carrier_off)
            if carrier_off:
                print("[cw_odmr_lock_in] CARRIER OFF: RF output intentionally "
                      "disabled -- chop control line and lock-in demodulation "
                      "still running normally, testing for control-line pickup "
                      "with no real RF power present")

            sensitivity_v = o.sensitivity_v
            if o.auto_sensitivity:
                sensitivity_v = _auto_gain(lia, o.settle_time_constants * o.time_constant_s)

            ilock_sa = _connect_interlock(gen)
            if ilock_sa is None:
                return

            print(f"[cw_odmr_lock_in] step 3/3: reading every {sample_interval_s:.2f}s"
                  f"{f' for {duration_s:.0f}s' if duration_s else ' until Ctrl+C'}, "
                  f"threshold {o.threshold_dbm} dBm")

            elapsed_s_list = []
            x_list = []
            y_list = []
            reflected_dbm_list = []
            start_time = time.time()
            tripped = False

            try:
                while True:
                    elapsed_s = time.time() - start_time
                    if duration_s and elapsed_s >= duration_s:
                        break

                    reflected_dbm, ok = _check_interlock(gen, ilock_sa, freq_hz, o.threshold_dbm,
                                                         f"t={elapsed_s:.1f}s")
                    if not ok:
                        tripped = True
                        break

                    x, y = lia.read_xy()
                    elapsed_s_list.append(elapsed_s)
                    x_list.append(x)
                    y_list.append(y)
                    reflected_dbm_list.append(reflected_dbm)
                    print(f"[cw_odmr_lock_in] t={elapsed_s:6.1f}s: X={x:.6e} V, "
                          f"Y={y:.6e} V, R={np.hypot(x, y):.6e} V, "
                          f"reflected={reflected_dbm:.2f} dBm")

                    time.sleep(sample_interval_s)
            except KeyboardInterrupt:
                print("[cw_odmr_lock_in] stopped by user (Ctrl+C)")

            n_completed = len(x_list)
            if n_completed == 0:
                print("[cw_odmr_lock_in] step 3/3 FAILED: no points completed -- nothing to save")
            else:
                prefix = f"{run_path}/{file_name}_lockin_single"
                _save_arrays(prefix, elapsed_s=elapsed_s_list, x=x_list, y=y_list,
                             reflected_dbm=reflected_dbm_list)
                _write_metadata(f"{prefix}_metadata.txt",
                                freq_hz=freq_hz, chop_freq_hz=o.chop_freq_hz,
                                chop_duty_pct=o.chop_duty_pct, time_constant_s=o.time_constant_s,
                                sensitivity_v=sensitivity_v, phase_deg=o.phase_deg,
                                drive_power_dbm=drive_power_dbm, carrier_off=carrier_off)
                print(f"[cw_odmr_lock_in] step 3/3 done"
                      f"{' (PARTIAL -- interlock tripped)' if tripped else ''}: "
                      f"saved {prefix}_elapsed_s.npy ({n_completed} points), "
                      f"_lockin_single_x.npy, _lockin_single_y.npy, "
                      f"_lockin_single_reflected_dbm.npy, _lockin_single_metadata.txt")
        finally:
            print("[cw_odmr_lock_in] shutting down")
            _shutdown(gen, coil_psu, lia, awg, ilock_sa=ilock_sa)

    print("[cw_odmr_lock_in] done")


def cmd_sweep_average(file_name, **kw):
    """
    Sweep a frequency range n_repeats times in a row, saving each repeat's
    raw sweep to disk immediately, then averaging X and Y elementwise
    across all completed repeats (then computing R from the averaged X/Y,
    not by averaging R directly -- see notes.md's "cw_odmr_lock_in.py
    operation notes" section for why).

    By default (use_resonance_sweep=true) runs the same coarse-then-fine
    resonance_sweep() as cmd_run() first to derive [start_hz, stop_hz] from
    f0 +/- fwhm_margin*FWHM/2; pass start_hz/stop_hz explicitly to override
    with a fixed manual range instead (the resonance sweep still runs and
    is logged/saved either way). Set use_resonance_sweep=false to skip it
    entirely and require start_hz/stop_hz manually.

    Same inline reflected-power interlock check as cmd_run/cmd_single,
    re-checked at every point of every repeat; a trip or Ctrl+C stops
    immediately, with completed repeats still averaged/saved and the
    partial one saved-but-excluded. Also does real-time SR830 overload
    detection with auto-rescale (auto_rescale_on_overload, default true) --
    see notes.md for the rescale/rescan rationale.

    Recognized key=value overrides (all optional):
      freq_step_hz=1e6        frequency step for the averaged sweep (coarser
                              default than cmd_run's)
      n_repeats=30            number of full sweeps to average together, PER
                              power level
      check_resonance_before_sweep=true  before each power level's repeats
                              start, sweep reflected power across this same
                              freqs_hz range and record/report where the
                              dip actually is -- diagnostic only, doesn't
                              gate or abort anything (guards against
                              resonance drift going undetected at low scan
                              power, where reflected power stays under the
                              interlock threshold regardless). Saves
                              <file_prefix>_resonance_check_freqs_hz.npy /
                              _resonance_check_reflected_dbm.npy per power
                              level.
      print_points=false      print X/Y/reflected power at every point as
                              it's read, like cmd_run does
      drive_power_dbm=0.0     (constant) drive power during the scan --
                              ignored if drive_power_dbm_list is given
      drive_power_dbm_list=None  comma-separated list of drive powers, e.g.
                              "-40,-20,-10,0" -- repeats the whole
                              n_repeats-averaged sweep once per power level
                              (for a power-dependence study). A Ctrl+C or
                              interlock trip stops the whole list, not just
                              the current power -- powers already completed
                              are still saved.
      auto_rescale_on_overload=true  detect SR830 overload in real time and
                              auto-rescale + restart the current repeat
      max_rescale_attempts=10  rescale-and-rescan attempts per repeat (not per
                              point) before giving up and treating it as PARTIAL
      rescan_backoff_points=2  points to back up (not restart from 0)
                              before rescanning after an overload

    See cmd_run() above: use_resonance_sweep, res_start_hz, res_stop_hz,
    coarse_step_hz, fine_span_hz, fine_step_hz, res_power_dbm, res_cal_dir,
    fwhm_margin, start_hz, stop_hz, threshold_dbm, settle_s, chop_freq_hz,
    chop_duty_pct, time_constant_s, settle_time_constants, sensitivity_v,
    auto_sensitivity (here AGAN runs once per power level), phase_deg,
    input_coupling, ch1_carrier_freq_hz, ch1_carrier_vpp, psu_voltage_v,
    psu_current_limit_a, coil_current_a, coil_voltage_margin.

    Saves resonance_sweep()'s own {file_name}_resonance_coarse.csv/
    _fine.csv (if use_resonance_sweep=true). If drive_power_dbm_list is NOT
    given (single power, the default), file names are unchanged from
    before: each repeat i (0-indexed):
      <file_name>_repeat{i}_freqs_hz.npy, _x.npy, _y.npy, _reflected_dbm.npy
    and after all repeats (or upon early stop, if at least one repeat
    completed fully):
      <file_name>_avg_freqs_hz.npy, _avg_x.npy, _avg_y.npy, _avg_r.npy,
      _avg_metadata.txt (records n_repeats_averaged vs. n_repeats_requested)
    If drive_power_dbm_list IS given, every file name gets a
    "_p{power_tag}" segment identifying which power it's from (e.g.
    -40.0 dBm -> "m40dBm", 0.0 dBm -> "0dBm"), one full set of the above
    per power level:
      <file_name>_p{power_tag}_repeat{i}_freqs_hz.npy, ...
      <file_name>_p{power_tag}_avg_freqs_hz.npy, ...,
      _avg_metadata.txt (also records drive_power_dbm for that power level)
    """
    r = _parse_scan_range(kw)
    o = _parse_common(kw)
    freq_step_hz = float(kw.get("freq_step_hz", 1e6))
    n_repeats = int(kw.get("n_repeats", 30))
    check_resonance_before_sweep = _flag(kw, "check_resonance_before_sweep", "true")
    print_points = _flag(kw, "print_points", "false")
    drive_power_dbm_list_raw = kw.get("drive_power_dbm_list", None)
    multi_power = drive_power_dbm_list_raw is not None
    if multi_power:
        power_list = [float(p) for p in str(drive_power_dbm_list_raw).split(",")]
    else:
        power_list = [float(kw.get("drive_power_dbm", 0.0))]
    auto_rescale_on_overload = _flag(kw, "auto_rescale_on_overload", "true")
    max_rescale_attempts = int(kw.get("max_rescale_attempts", 10))
    rescan_backoff_points = int(kw.get("rescan_backoff_points", 2))
    run_path, log_path = _run_paths(file_name, "sweep_average")

    with _tee_stdout(log_path):
        awg, lia = _setup_awg_and_lockin(o, chop_now=not r.use_resonance_sweep, step="step 1/3")

        print("[cw_odmr_lock_in] step 2/3: connecting to HP8673H + E4403B (interlock) "
              "+ SPD1168X (amplifier supply) + SPD1305X (coil supply)")
        gen = HP8673H(GEN_RESOURCE)
        amp_psu = SPD1168X(AMP_PSU_RESOURCE)
        coil_psu = SPD1305X(COIL_PSU_RESOURCE)
        ilock_sa = None
        try:
            _turn_on_supplies(o, amp_psu, coil_psu)
            start_hz, stop_hz = _find_scan_range(gen, awg, o, r, f"{run_path}/{file_name}_resonance",
                                                 step="step 2/3")

            freqs_hz = np.arange(start_hz, stop_hz + freq_step_hz / 2, freq_step_hz)
            print(f"[cw_odmr_lock_in] step 3/3: sweeping "
                  f"{start_hz/1e9:.5f}-{stop_hz/1e9:.5f} GHz "
                  f"({len(freqs_hz)} points, {freq_step_hz/1e6:.3f} MHz step), "
                  f"{n_repeats} repeats to average, threshold {o.threshold_dbm} dBm, "
                  f"power(s): {', '.join(f'{p:g} dBm' for p in power_list)}")

            _arm_generator(gen, power_list[0], freqs_hz[0])
            ilock_sa = _connect_interlock(gen)
            if ilock_sa is None:
                return

            settle_after_step_s = o.settle_time_constants * o.time_constant_s
            sensitivity_v = o.sensitivity_v
            stop_all = False

            for power_dbm in power_list:
                if stop_all:
                    break

                file_prefix = f"{file_name}_p{_power_tag(power_dbm)}" if multi_power else file_name

                gen.set_power_dbm(power_dbm)
                time.sleep(1.0)  # let the new power level settle
                print(f"[cw_odmr_lock_in] drive power {power_dbm:g} dBm: settled")

                if check_resonance_before_sweep:
                    # Park the switch on the sample path for the check -- while
                    # CH2 chops, each analyzer reading lands in the RF-on or
                    # RF-off half at random (readings alternating ~-35/-75 dBm),
                    # and the "dip" becomes the lowest RF-off reading.
                    set_switch_static(awg, route_to_sample=True)
                    res_check = check_resonance_position(gen, ilock_sa, freqs_hz, o.settle_s)
                    setup_chop(awg, o.chop_freq_hz, o.chop_duty_pct)
                    time.sleep(settle_after_step_s)  # let the lock-in relock to the restarted chop
                    _save_arrays(f"{run_path}/{file_prefix}_resonance_check",
                                 freqs_hz=res_check["freqs_hz"],
                                 reflected_dbm=res_check["reflected_dbm"])
                    offset_mhz = res_check["offset_from_center_hz"] / 1e6
                    print(f"[cw_odmr_lock_in] resonance check @ {power_dbm:g} dBm: "
                          f"dip at {res_check['dip_freq_hz']/1e9:.5f} GHz "
                          f"({res_check['dip_dbm']:.2f} dBm), "
                          f"{offset_mhz:+.3f} MHz from the center of this sweep's range"
                          + (" -- LOOKS OFF-CENTER, resonance may have drifted"
                             if abs(offset_mhz) > (freqs_hz[-1] - freqs_hz[0]) / 1e6 / 4
                             else ""))

                if o.auto_sensitivity:
                    sensitivity_v = _auto_gain(lia, settle_after_step_s, f" at {power_dbm:g} dBm")

                x_repeats = []
                y_repeats = []
                tripped = False

                try:
                    for rep in range(n_repeats):
                        x_values = np.full(len(freqs_hz), np.nan)
                        y_values = np.full(len(freqs_hz), np.nan)
                        reflected_dbm_arr = np.full(len(freqs_hz), np.nan)
                        n_completed = 0
                        rescale_attempts_used = 0
                        repeat_label = f"{power_dbm:g} dBm, repeat {rep + 1}/{n_repeats}"

                        print(f"[cw_odmr_lock_in] {repeat_label}: starting sweep")

                        i = 0
                        while i < len(freqs_hz):
                            f = freqs_hz[i]
                            where = f"{repeat_label}, point {i + 1}/{len(freqs_hz)}"
                            reflected_dbm, x, y = _measure_point(gen, lia, ilock_sa, f, o, where)
                            reflected_dbm_arr[i] = (reflected_dbm if reflected_dbm is not None
                                                    else np.nan)
                            if x is None:
                                tripped = True
                                break

                            if auto_rescale_on_overload:
                                overload_status = lia.read_overload_status()
                                if overload_status["any"]:
                                    print(f"[cw_odmr_lock_in] OVERLOAD at {f/1e9:.5f} GHz "
                                          f"({where}): {overload_status}")

                                    if rescale_attempts_used < max_rescale_attempts:
                                        rescale_attempts_used += 1
                                        old_v = lia.get_sensitivity_v()
                                        new_v = _step_sensitivity_coarser(lia)
                                        backoff = min(rescan_backoff_points, i)
                                        resume_i = i - backoff
                                        print(f"[cw_odmr_lock_in] rescaling "
                                              f"sensitivity {old_v:.3e} V -> "
                                              f"{new_v:.3e} V full scale, rescanning "
                                              f"from point {resume_i + 1}/"
                                              f"{len(freqs_hz)} (backing up {backoff} "
                                              f"points, not the whole repeat -- "
                                              f"attempt {rescale_attempts_used}/"
                                              f"{max_rescale_attempts})")
                                        sensitivity_v = new_v
                                        time.sleep(settle_after_step_s)
                                        i = resume_i
                                        continue  # re-measure from resume_i onward
                                    else:
                                        print(f"[cw_odmr_lock_in] {repeat_label}: still "
                                              f"overloading after {max_rescale_attempts} "
                                              f"rescale attempts -- stopping here, "
                                              f"treating as PARTIAL "
                                              f"({n_completed}/{len(freqs_hz)} points)")
                                        break

                            x_values[i] = x
                            y_values[i] = y
                            n_completed = i + 1
                            if print_points:
                                print(f"[cw_odmr_lock_in] {repeat_label}, point "
                                      f"{n_completed}/{len(freqs_hz)}: "
                                      f"f={f/1e9:.5f} GHz, X={x:.6e} V, Y={y:.6e} V, "
                                      f"reflected={reflected_dbm:.2f} dBm")
                            i += 1

                        _save_arrays(f"{run_path}/{file_prefix}_repeat{rep}",
                                     freqs_hz=freqs_hz[:n_completed], x=x_values[:n_completed],
                                     y=y_values[:n_completed],
                                     reflected_dbm=reflected_dbm_arr[:n_completed])

                        if n_completed == len(freqs_hz):
                            x_repeats.append(x_values)
                            y_repeats.append(y_values)
                            print(f"[cw_odmr_lock_in] {repeat_label} done: "
                                  f"{n_completed}/{len(freqs_hz)} points, saved "
                                  f"{file_prefix}_repeat{rep}_*.npy")
                        else:
                            # A partial repeat (interlock tripped mid-sweep) isn't a
                            # like-for-like full-range sweep -- still saved to disk
                            # above, but excluded from the average (a short/NaN-
                            # padded array averaged against full ones would bias
                            # the result rather than just reducing its noise).
                            print(f"[cw_odmr_lock_in] {repeat_label} PARTIAL "
                                  f"({n_completed}/{len(freqs_hz)} points) -- saved "
                                  f"but excluded from the average")

                        if tripped:
                            stop_all = True
                            break
                except KeyboardInterrupt:
                    print("[cw_odmr_lock_in] stopped by user (Ctrl+C)")
                    stop_all = True

                n_good_repeats = len(x_repeats)
                if n_good_repeats == 0:
                    print(f"[cw_odmr_lock_in] {power_dbm:g} dBm: FAILED -- no complete "
                          f"repeats, nothing to average")
                else:
                    x_avg = np.mean(np.stack(x_repeats), axis=0)
                    y_avg = np.mean(np.stack(y_repeats), axis=0)
                    avg_prefix = f"{run_path}/{file_prefix}_avg"
                    _save_arrays(avg_prefix, freqs_hz=freqs_hz, x=x_avg, y=y_avg,
                                 r=np.hypot(x_avg, y_avg))
                    _write_metadata(f"{avg_prefix}_metadata.txt",
                                    start_hz=start_hz, stop_hz=stop_hz, freq_step_hz=freq_step_hz,
                                    n_repeats_requested=n_repeats, n_repeats_averaged=n_good_repeats,
                                    chop_freq_hz=o.chop_freq_hz, chop_duty_pct=o.chop_duty_pct,
                                    time_constant_s=o.time_constant_s,
                                    settle_time_constants=o.settle_time_constants,
                                    sensitivity_v=sensitivity_v, phase_deg=o.phase_deg,
                                    drive_power_dbm=power_dbm)
                    print(f"[cw_odmr_lock_in] {power_dbm:g} dBm: averaging done: "
                          f"{n_good_repeats}/{n_repeats} complete repeats averaged, "
                          f"saved {file_prefix}_avg_freqs_hz.npy, _avg_x.npy, "
                          f"_avg_y.npy, _avg_r.npy, _avg_metadata.txt")
        finally:
            print("[cw_odmr_lock_in] shutting down")
            _shutdown(gen, coil_psu, lia, awg, amp_psu=amp_psu, ilock_sa=ilock_sa)

    print("[cw_odmr_lock_in] done")


def main():
    if len(sys.argv) < 3:
        print(USAGE)
        raise SystemExit(1)

    command = sys.argv[1]
    file_name = sys.argv[2]
    extra = parse_kv_args(sys.argv[3:])

    if command == "run":
        cmd_run(file_name, **extra)
    elif command == "single":
        cmd_single(file_name, **extra)
    elif command == "sweep-average":
        cmd_sweep_average(file_name, **extra)
    else:
        raise SystemExit(f"unknown command {command!r} "
                          f"(expected 'run', 'single', or 'sweep-average')")


if __name__ == "__main__":
    main()
