"""
pulsed_odmr.py -- pulsed ODMR: sweep MW frequency (structure copied from
cw_odmr_lock_in.py's lock-in frequency spectrum) using rabi.py's PULSED
sequence (laser gated via CH1's AOM, MW gated via CH2 driving the ZYSWA
switch through a combined on/off arb, both channels synchronized via the
onceWaitTrig anchor mechanism) instead of cw_odmr_lock_in.py's simple
continuous CH1 carrier + static CH2 square-wave chop.

tau_mw_us (the MW pulse duration within each rep) is FIXED for the whole
run (default 5.0 us) -- only freq_hz varies point to point, the opposite
of rabi.py's cmd_run() (which fixes freq_hz and sweeps tau_mw_us).

Since the AWG's pulse sequence never changes across frequency points (only
the generator's frequency does, which is set on the GENERATOR, not the
AWG), setup_awg_sequences() runs ONCE before the frequency loop, not per
point like rabi.py's tau_mw sweep -- this sidesteps essentially the entire
reconfigure-per-point background-artifact investigation documented in
notes.md's "spurious off-resonance/no-MW-near-sample signal" entry, by
construction, since there's nothing to reconfigure between points. The
periodic reflected-power interlock check (still needed -- reflected power
genuinely varies with frequency near resonance) still races with the AWG's
own anchor-wrap timing the same way it did in rabi.py, so the same ABOR-
plus-extra_settle_s fix after that check is copied over unchanged.

See notes.md's "Pulsed ODMR (pulsed_odmr.py)" section for prior-session
design history -- in particular: do NOT use PHASe:SYNChronize to align
CH1/CH2 (confirmed on real hardware to kill all output entirely under this
sequence-table setup); the onceWaitTrig anchor mechanism setup_awg_
sequences() already uses is the validated fix for that instead. Confining
the MW pulse to the dark period (laser already off by the time the MW
pulse plays, never overlapping polarize/readout) was the whole point of
that design and is preserved unchanged by reusing setup_awg_sequences()
as-is.

Usage:
    python pulsed_odmr.py run <file_name> [key=value ...]

Run with no arguments (or a bare command name with no file_name) to print
the full list of recognized key=value overrides with their current
defaults.
"""
import os
import sys
import time

import numpy as np

import ks33600a
import rabi
from hp8673h import HP8673H
from sr830 import SR830
from sdg1062x import SDG1062X
from spd1305x import SPD1305X, voltage_for_current
from spd1168x import SPD1168X
from cw_odmr import parse_kv_args, _tee_stdout

DATA_DIR = rabi.DATA_DIR

AWG_RESOURCE = rabi.AWG_RESOURCE
SDG_RESOURCE = rabi.SDG_RESOURCE
SR830_RESOURCE = rabi.SR830_RESOURCE
GEN_RESOURCE = rabi.GEN_RESOURCE
SA_RESOURCE = rabi.SA_RESOURCE
AMP_PSU_RESOURCE = rabi.AMP_PSU_RESOURCE
COIL_PSU_RESOURCE = rabi.COIL_PSU_RESOURCE


PARAMS_HELP = """
'run' key=value overrides, with their current defaults:

Resonance pre-scan (locates f0/FWHM to derive the frequency scan range --
same idea as cw_odmr_lock_in.py's use_resonance_sweep):
    use_resonance_sweep=true   find f0/FWHM first; false requires start_hz/
                               stop_hz to be given explicitly instead
    res_start_hz=2.0e9
    res_stop_hz=3.0e9
    coarse_step_hz=6.7e6
    fine_span_hz=20e6
    fine_step_hz=50e3
    res_power_dbm=-40.0
    res_cal_dir=None
    fwhm_margin=1.0            scan span = FWHM * this margin (ignored if
                               start_hz/stop_hz are given)
    start_hz=None              explicit scan start -- overrides the
                               resonance-sweep-derived range if given
    stop_hz=None               explicit scan stop

Frequency sweep:
    freq_step_hz=10e3
    drive_power_dbm=0.0
    threshold_dbm=-10.0
    settle_s=0.05              generator retune settle, NEVER 0 (see
                               hp8673h.py's frequency_sweep() docstring)

Pulse sequence (from rabi.py's setup_awg_sequences() -- FIXED for the
whole run, unlike rabi.py's own tau_mw sweep):
    tau_mw_us=5.0              fixed MW pulse duration within each rep
    n_reps=250
    laser_us=2.0
    pre_us=1.0
    post_us=1.0

Lock-in:
    time_constant_s=0.1
    settle_periods=5.0
    settle_time_constants=9.0
    sensitivity_v=5e-3
    auto_sensitivity=true      AGAN once, after RF+pulsing starts, before
                               the frequency loop begins
    phase_deg=0.0
    input_coupling=ac

Overload/underload auto-rescaling (same as rabi.py's cmd_run()):
    auto_rescale_on_overload=true
    max_rescale_attempts=3
    auto_rescale_on_underload=true
    underload_margin=0.5
    underload_persistence=3
    fixed_sensitivity=false

Amplifier/coil PSUs:
    psu_voltage_v=12.0
    psu_current_limit_a=1.9
    coil_current_a=2.0
    coil_voltage_margin=1.5

Interlock:
    interlock_check_interval=5
    interlock_hold_periods=3.0
    interlock_during_sweep=true

AWG sequencing / background-artifact avoidance (see notes.md -- sized
once here for the fixed tau_mw_us, not per point like rabi.py):
    ch1_vpp=0.632
    ch2_vpp=5.0
    ch2_offset_v=2.5
    trigger_margin=3.0
    anchor_free_reps=1000      if you raise extra_settle_s, raise this too
                               so anchor_period_s still exceeds extra_
                               settle_s + settle_s
    extra_settle_s=0.0         extra wait after the initial setup_awg_
                               sequences() call AND after the interlock
                               check's own ABOR -- see rabi.py's notes.md
                               entry for why both places need it
""".strip("\n")


def cmd_run(file_name, **kw):
    use_resonance_sweep = str(kw.get("use_resonance_sweep", "true")).lower() == "true"
    res_start_hz = float(kw.get("res_start_hz", 2.0e9))
    res_stop_hz = float(kw.get("res_stop_hz", 3.0e9))
    coarse_step_hz = float(kw.get("coarse_step_hz", 6.7e6))
    fine_span_hz = float(kw.get("fine_span_hz", 20e6))
    fine_step_hz = float(kw.get("fine_step_hz", 50e3))
    res_power_dbm = float(kw.get("res_power_dbm", -40.0))
    res_cal_dir = kw.get("res_cal_dir", None)
    manual_start_hz = kw.get("start_hz", None)
    manual_stop_hz = kw.get("stop_hz", None)
    if not use_resonance_sweep and (manual_start_hz is None or manual_stop_hz is None):
        raise ValueError("start_hz and stop_hz must both be given when "
                          "use_resonance_sweep=false")
    fwhm_margin = float(kw.get("fwhm_margin", 1.0))

    freq_step_hz = float(kw.get("freq_step_hz", 10e3))
    drive_power_dbm = float(kw.get("drive_power_dbm", 0.0))
    threshold_dbm = float(kw.get("threshold_dbm", -10.0))
    settle_s = float(kw.get("settle_s", 0.05))  # NEVER 0 -- see
                                                  # hp8673h.py's
                                                  # frequency_sweep()
                                                  # docstring

    tau_mw_us = float(kw.get("tau_mw_us", 5.0))
    n_reps = int(kw.get("n_reps", 250))
    laser_us = float(kw.get("laser_us", 2.0))
    pre_us = float(kw.get("pre_us", 1.0))
    post_us = float(kw.get("post_us", 1.0))

    time_constant_s = float(kw.get("time_constant_s", 0.1))
    settle_periods = float(kw.get("settle_periods", 5.0))
    settle_time_constants = float(kw.get("settle_time_constants", 9.0))
    sensitivity_v = float(kw.get("sensitivity_v", 5e-3))
    auto_sensitivity = str(kw.get("auto_sensitivity", "true")).lower() == "true"
    phase_deg = float(kw.get("phase_deg", 0.0))
    input_coupling = str(kw.get("input_coupling", "ac"))

    auto_rescale_on_overload = str(kw.get("auto_rescale_on_overload", "true")).lower() == "true"
    max_rescale_attempts = int(kw.get("max_rescale_attempts", 3))
    auto_rescale_on_underload = str(kw.get("auto_rescale_on_underload", "true")).lower() == "true"
    underload_margin = float(kw.get("underload_margin", 0.5))
    underload_persistence = int(kw.get("underload_persistence", 3))
    # See rabi.py's fixed_sensitivity comment -- same convenience override,
    # same ordering requirement (must come after all three flags above).
    fixed_sensitivity = str(kw.get("fixed_sensitivity", "false")).lower() == "true"
    if fixed_sensitivity:
        auto_sensitivity = False
        auto_rescale_on_overload = False
        auto_rescale_on_underload = False

    psu_voltage_v = float(kw.get("psu_voltage_v", 12.0))
    psu_current_limit_a = float(kw.get("psu_current_limit_a", 1.9))
    coil_current_a = float(kw.get("coil_current_a", 2.0))
    coil_voltage_margin = float(kw.get("coil_voltage_margin", 1.5))

    interlock_check_interval = int(kw.get("interlock_check_interval", 5))
    interlock_hold_periods = float(kw.get("interlock_hold_periods", 3.0))
    interlock_during_sweep = str(kw.get("interlock_during_sweep", "true")).lower() == "true"

    ch1_vpp = float(kw.get("ch1_vpp", 0.632))
    ch2_vpp = float(kw.get("ch2_vpp", 5.0))
    ch2_offset_v = float(kw.get("ch2_offset_v", 2.5))
    trigger_margin = float(kw.get("trigger_margin", 3.0))
    anchor_free_reps = int(kw.get("anchor_free_reps", 1000))
    extra_settle_s = float(kw.get("extra_settle_s", 0.0))

    run_path = f"{DATA_DIR}/{file_name}"
    os.makedirs(run_path, exist_ok=True)
    log_dir = f"{DATA_DIR}/logs"
    os.makedirs(log_dir, exist_ok=True)
    log_path = f"{log_dir}/{file_name}_pulsed_odmr.txt"

    # rep_us/ref_period_s/settle_s/anchor_period_s are all CONSTANT for the
    # whole run, unlike rabi.py's per-point recompute -- tau_mw_us never
    # changes here.
    rep_us = laser_us + pre_us + tau_mw_us + post_us
    ref_period_s = 2 * n_reps * rep_us * 1e-6
    dwell_settle_s = max(settle_periods * ref_period_s,
                          settle_time_constants * time_constant_s)
    anchor_period_s = ref_period_s * anchor_free_reps

    with _tee_stdout(log_path):
        print(f"[pulsed_odmr] tau_mw fixed at {tau_mw_us} us, n_reps={n_reps} "
              f"-- ref_period_s={ref_period_s:.6f}, settle_s={dwell_settle_s:.3f}, "
              f"extra_settle_s={extra_settle_s:.3f}, "
              f"anchor_period_s={anchor_period_s:.3f}")
        if anchor_period_s < dwell_settle_s + extra_settle_s:
            print(f"[pulsed_odmr] WARNING: anchor_period_s ({anchor_period_s:.3f} s) "
                  f"is SHORTER than settle_s + extra_settle_s "
                  f"({dwell_settle_s + extra_settle_s:.3f} s) -- the sequence will "
                  f"wrap and need an uncontrolled retrigger mid-window. Raise "
                  f"anchor_free_reps.")

        print("[pulsed_odmr] step 1/4: configuring AWG (fixed pulse sequence) "
              "+ SDG1062X (external trigger) + SR830 lock-in")
        awg = ks33600a.KS33600A(AWG_RESOURCE, debug=False)
        sdg = SDG1062X(SDG_RESOURCE, debug=False)
        lia = SR830(SR830_RESOURCE, debug=True)
        rabi.setup_lock_in(lia, time_constant_s, sensitivity_v, phase_deg,
                            input_coupling, auto_sensitivity=auto_sensitivity)

        fixed_trigger_freq_hz = rabi._configure_external_trigger(
            sdg, anchor_period_s, margin=trigger_margin)
        rabi.setup_awg_sequences(
            awg, tau_mw_us, n_reps, laser_us, pre_us, post_us,
            sequence_name_ch1="pulsed_odmr_ch1",
            sequence_name_ch2="pulsed_odmr_ch2",
            ch1_vpp=ch1_vpp, ch2_vpp=ch2_vpp, ch2_offset_v=ch2_offset_v,
            anchor_free_reps=anchor_free_reps,
        )
        if extra_settle_s > 0:
            time.sleep(extra_settle_s)
        print(f"[pulsed_odmr] step 1/4 done: trigger fixed at "
              f"{fixed_trigger_freq_hz/1e3:.3f} kHz, pulse sequence uploaded ONCE "
              f"(never reconfigured again -- only freq_hz changes per point)")

        print("[pulsed_odmr] step 2/4: connecting to HP8673H + E4403B (interlock) "
              "+ SPD1168X (amplifier supply) + SPD1305X (coil supply)")
        gen = HP8673H(GEN_RESOURCE)
        amp_psu = SPD1168X(AMP_PSU_RESOURCE)
        coil_psu = SPD1305X(COIL_PSU_RESOURCE)
        ilock_sa = None
        try:
            amp_psu.turn_on(psu_voltage_v, psu_current_limit_a)
            coil_voltage_v = voltage_for_current(coil_current_a) * coil_voltage_margin
            coil_psu.turn_on(coil_voltage_v, coil_current_a)

            if use_resonance_sweep:
                print(f"[pulsed_odmr] step 2/4: sweeping for resonance "
                      f"({res_start_hz/1e9:.4f}-{res_stop_hz/1e9:.4f} GHz, "
                      f"{res_power_dbm} dBm)")
                from e4403b import E4403B
                sa = E4403B(SA_RESOURCE)
                result = gen.resonance_sweep(
                    sa, res_start_hz, res_stop_hz, coarse_step_hz, fine_span_hz,
                    fine_step_hz, res_power_dbm,
                    output_prefix=f"{run_path}/{file_name}_resonance",
                    cal_dir=res_cal_dir,
                )
                f0_hz = result["f0_hz"]
                fwhm_hz = result["fwhm_hz"]
                print(f"[pulsed_odmr] step 2/4 done: f0 = {f0_hz/1e9:.5f} GHz, "
                      f"FWHM = {fwhm_hz/1e6:.3f} MHz, Q ~= {result['Q']:.0f}")
                sa.go_to_local()
                sa.close()

                if manual_start_hz is not None and manual_stop_hz is not None:
                    start_hz = float(manual_start_hz)
                    stop_hz = float(manual_stop_hz)
                    print(f"[pulsed_odmr] using manually-given range "
                          f"{start_hz/1e9:.5f}-{stop_hz/1e9:.5f} GHz instead of "
                          f"the resonance-sweep-derived one")
                else:
                    span_hz = fwhm_hz * fwhm_margin
                    start_hz = f0_hz - span_hz / 2
                    stop_hz = f0_hz + span_hz / 2
            else:
                start_hz = float(manual_start_hz)
                stop_hz = float(manual_stop_hz)

            freqs_hz = np.arange(start_hz, stop_hz + freq_step_hz / 2, freq_step_hz)
            print(f"[pulsed_odmr] step 3/4: pulsed ODMR frequency scan "
                  f"{freqs_hz[0]/1e9:.5f}-{freqs_hz[-1]/1e9:.5f} GHz "
                  f"({len(freqs_hz)} points, {freq_step_hz/1e3:.1f} kHz step), "
                  f"tau_mw={tau_mw_us} us, drive {drive_power_dbm} dBm, "
                  f"threshold {threshold_dbm} dBm")

            gen.preset()
            gen.set_power_dbm(drive_power_dbm)
            gen.set_frequency_hz(freqs_hz[0])
            gen.rf_on()
            time.sleep(1.0)  # let the initial frequency/level settle

            if auto_sensitivity:
                # Real, pulsed signal is present now (RF + pulsing both
                # running) -- let AGAN pick a range, then wait the same
                # settle margin used between points before trusting it.
                time.sleep(dwell_settle_s)
                lia.auto_gain()
                time.sleep(dwell_settle_s)
                actual_sensitivity_v = lia.get_sensitivity_v()
                print(f"[pulsed_odmr] auto_sensitivity: AGAN selected "
                      f"{actual_sensitivity_v:.3e} V full scale")
                sensitivity_v = actual_sensitivity_v

            ilock_sa = HP8673H.try_connect_analyzer(SA_RESOURCE)
            if ilock_sa is None:
                gen.trip_interlock("spectrum analyzer not reachable at startup")
                return

            x_values = np.full(len(freqs_hz), np.nan)
            y_values = np.full(len(freqs_hz), np.nan)
            reflected_dbm_arr = np.full(len(freqs_hz), np.nan)
            ref_freq_hz_arr = np.full(len(freqs_hz), np.nan)
            reference_unlock_arr = np.zeros(len(freqs_hz), dtype=bool)
            n_completed = 0
            tripped = False
            underload_streak = 0

            try:
                for i, f in enumerate(freqs_hz):
                    gen.set_frequency_hz(f)
                    time.sleep(settle_s)

                    if interlock_during_sweep and i % interlock_check_interval == 0:
                        hold_s = interlock_hold_periods * ref_period_s
                        power_dbm = HP8673H.read_max_hold_reflected_power_dbm(
                            ilock_sa, f, hold_s)
                        reflected_dbm_arr[i] = power_dbm if power_dbm is not None else np.nan

                        if power_dbm is not None:
                            print(f"[pulsed_odmr] interlock check (point {i + 1}/"
                                  f"{len(freqs_hz)}, f={f/1e9:.5f} GHz): "
                                  f"reflected power {power_dbm:.2f} dBm "
                                  f"(threshold {threshold_dbm} dBm) -- "
                                  f"{'OK' if power_dbm <= threshold_dbm else 'OVER THRESHOLD'}")

                        if power_dbm is None or power_dbm > threshold_dbm:
                            reason = (
                                "spectrum analyzer unreachable"
                                if power_dbm is None else
                                f"reflected power {power_dbm:.2f} dBm exceeds "
                                f"threshold {threshold_dbm} dBm"
                            )
                            gen.trip_interlock(f"{reason} at f={f/1e9:.5f} GHz "
                                                f"(point {i + 1}/{len(freqs_hz)})")
                            tripped = True
                            break

                        # Same race as rabi.py's interlock check: the GPIB
                        # round trip above is comparable to anchor_period_s,
                        # so ABOR forces a known (idle-at-anchor) state
                        # instead of leaving "mid-run or already re-idled?"
                        # to chance, and extra_settle_s gives it the same
                        # margin the initial setup_awg_sequences() call got.
                        # See rabi.py's notes.md entry -- this exact gap
                        # (ABOR with no extra_settle_s after it) was found
                        # and fixed there.
                        awg.write("ABOR")
                        if extra_settle_s > 0:
                            time.sleep(extra_settle_s)

                    rabi._wait_settle_discarding_transient_overload(lia, dwell_settle_s)

                    x, y = lia.read_xy()
                    ref_freq_hz = lia.get_frequency_hz()
                    ref_freq_hz_arr[i] = ref_freq_hz
                    expected_ref_freq_hz = 1.0 / ref_period_s

                    initial_status = lia.read_overload_status()
                    reference_unlock_arr[i] = initial_status["reference_unlock"]
                    print(f"[pulsed_odmr] point {i + 1}/{len(freqs_hz)}: SR830 reference "
                          f"reads {ref_freq_hz:.6f} Hz (expected {expected_ref_freq_hz:.6f} Hz), "
                          f"reference_unlock={initial_status['reference_unlock']}, "
                          f"input_overload={initial_status['input']}, "
                          f"filter_overload={initial_status['filter']}, "
                          f"output_overload={initial_status['output']}")

                    overloaded_this_point = False
                    if auto_rescale_on_overload:
                        overload_status = initial_status
                        for attempt in range(max_rescale_attempts):
                            if attempt > 0:
                                overload_status = lia.read_overload_status()
                            if not overload_status["any"]:
                                break
                            overloaded_this_point = True
                            old_v = lia.get_sensitivity_v()
                            new_v = rabi._step_sensitivity_coarser(lia)
                            lia.read_overload_status()
                            print(f"[pulsed_odmr] OVERLOAD at f={f/1e9:.5f} GHz "
                                  f"(point {i + 1}/{len(freqs_hz)}): rescaling "
                                  f"sensitivity {old_v:.3e} V -> {new_v:.3e} V full "
                                  f"scale, re-reading (attempt {attempt + 1}/"
                                  f"{max_rescale_attempts})")
                            sensitivity_v = new_v
                            rabi._wait_settle_discarding_transient_overload(lia, dwell_settle_s)
                            x, y = lia.read_xy()
                        else:
                            print(f"[pulsed_odmr] f={f/1e9:.5f} GHz: still overloading "
                                  f"after {max_rescale_attempts} rescale attempts -- "
                                  f"saving as-is")

                    if overloaded_this_point:
                        underload_streak = 0
                    elif auto_rescale_on_underload:
                        idx = SR830.SENSITIVITY_V.index(sensitivity_v)
                        if idx > 0:
                            r = (x ** 2 + y ** 2) ** 0.5
                            next_v = SR830.SENSITIVITY_V[idx - 1]
                            if r < underload_margin * next_v:
                                underload_streak += 1
                                if underload_streak >= underload_persistence:
                                    old_v = sensitivity_v
                                    new_v = rabi._step_sensitivity_finer(lia)
                                    print(f"[pulsed_odmr] f={f/1e9:.5f} GHz (point {i + 1}/"
                                          f"{len(freqs_hz)}): R={r:.3e} V well under "
                                          f"the current {old_v:.3e} V full scale for "
                                          f"{underload_streak} consecutive points -- "
                                          f"stepping sensitivity down to {new_v:.3e} V "
                                          f"full scale for the next point")
                                    sensitivity_v = new_v
                                    underload_streak = 0
                            else:
                                underload_streak = 0
                        else:
                            underload_streak = 0
                    else:
                        underload_streak = 0

                    x_values[i] = x
                    y_values[i] = y
                    n_completed = i + 1
                    print(f"[pulsed_odmr] point {n_completed}/{len(freqs_hz)}: "
                          f"f={f/1e9:.5f} GHz, X={x:.6e} V, Y={y:.6e} V")
            except KeyboardInterrupt:
                print("[pulsed_odmr] stopped by user (Ctrl+C)")

            freqs_hz = freqs_hz[:n_completed]
            x_values = x_values[:n_completed]
            y_values = y_values[:n_completed]
            reflected_dbm_arr = reflected_dbm_arr[:n_completed]
            ref_freq_hz_arr = ref_freq_hz_arr[:n_completed]
            reference_unlock_arr = reference_unlock_arr[:n_completed]

            if n_completed == 0:
                print("[pulsed_odmr] step 3/4 FAILED: no points completed -- nothing to save")
            else:
                np.save(f"{run_path}/{file_name}_pulsed_odmr_freqs_hz.npy", freqs_hz)
                np.save(f"{run_path}/{file_name}_pulsed_odmr_x.npy", x_values)
                np.save(f"{run_path}/{file_name}_pulsed_odmr_y.npy", y_values)
                np.save(f"{run_path}/{file_name}_pulsed_odmr_reflected_dbm.npy",
                        reflected_dbm_arr)
                np.save(f"{run_path}/{file_name}_pulsed_odmr_ref_freq_hz.npy",
                        ref_freq_hz_arr)
                np.save(f"{run_path}/{file_name}_pulsed_odmr_reference_unlock.npy",
                        reference_unlock_arr)
                with open(f"{run_path}/{file_name}_pulsed_odmr_metadata.txt", "w") as fh:
                    fh.write(f"tau_mw_us={tau_mw_us}\n")
                    fh.write(f"n_reps={n_reps}\n")
                    fh.write(f"laser_us={laser_us}\n")
                    fh.write(f"pre_us={pre_us}\n")
                    fh.write(f"post_us={post_us}\n")
                    fh.write(f"drive_power_dbm={drive_power_dbm}\n")
                    fh.write(f"time_constant_s={time_constant_s}\n")
                    fh.write(f"settle_periods={settle_periods}\n")
                    fh.write(f"settle_time_constants={settle_time_constants}\n")
                    fh.write(f"sensitivity_v={sensitivity_v}\n")
                    fh.write(f"phase_deg={phase_deg}\n")
                    fh.write(f"anchor_free_reps={anchor_free_reps}\n")
                    fh.write(f"extra_settle_s={extra_settle_s}\n")
                print(f"[pulsed_odmr] step 3/4 done"
                      f"{' (PARTIAL -- interlock tripped)' if tripped else ''}: "
                      f"saved {run_path}/{file_name}_pulsed_odmr_freqs_hz.npy "
                      f"({n_completed} points), _pulsed_odmr_x.npy, _pulsed_odmr_y.npy, "
                      f"_pulsed_odmr_reflected_dbm.npy, _pulsed_odmr_ref_freq_hz.npy, "
                      f"_pulsed_odmr_reference_unlock.npy, _pulsed_odmr_metadata.txt")
        finally:
            print("[pulsed_odmr] step 4/4: shutting down")
            try:
                gen.rf_off()
            except Exception as e:
                print(f"[pulsed_odmr] WARNING: failed to turn off RF cleanly ({e})")
            try:
                gen.go_to_local()
            except Exception:
                pass
            gen.close()
            try:
                amp_psu.turn_off()
            except Exception as e:
                print(f"[pulsed_odmr] WARNING: failed to turn off amplifier "
                      f"supply cleanly ({e})")
            amp_psu.close()
            try:
                coil_psu.turn_off()
            except Exception as e:
                print(f"[pulsed_odmr] WARNING: failed to turn off coil "
                      f"supply cleanly ({e})")
            try:
                coil_psu.go_to_local()
            except Exception:
                pass
            coil_psu.close()
            if ilock_sa is not None:
                ilock_sa.close()
            try:
                awg.write("OUTPUT1 OFF")
                awg.write("OUTPUT2 OFF")
            except Exception as e:
                print(f"[pulsed_odmr] WARNING: failed to turn off AWG outputs "
                      f"cleanly ({e})")
            try:
                awg.close()
            except Exception:
                pass
            try:
                sdg.write("C1:OUTP OFF")
                sdg.close()
            except Exception:
                pass
            try:
                lia.go_to_local()
            except Exception:
                pass
            lia.close()

    print("[pulsed_odmr] done")


def main():
    if len(sys.argv) < 3:
        print(__doc__)
        print(PARAMS_HELP)
        raise SystemExit(1)

    command = sys.argv[1]
    file_name = sys.argv[2]
    extra = parse_kv_args(sys.argv[3:])

    if command == "run":
        cmd_run(file_name, **extra)
    else:
        raise SystemExit(f"unknown command {command!r} (expected 'run')")


if __name__ == "__main__":
    main()
