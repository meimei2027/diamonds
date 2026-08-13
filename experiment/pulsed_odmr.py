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

    python pulsed_odmr.py run-repeat <file_name> [n_repeats=10] [key=value ...]
        Repeats 'run' n_repeats times (each a genuine subprocess, same
        idea as rabi.py's run-repeat), saving all repeats into ONE shared
        folder and averaging X/Y across completed repeats -- see cmd_
        run_repeat()'s docstring for details, including how use_
        resonance_sweep's resolved frequency range and coil_current_a
        get pinned from repeat 0 for every repeat after it. Also supports
        tau_mw_us_list (e.g. "0.5,1.0,2.0,5.0") and drive_power_dbm_list
        (e.g. "0.0,-5.0,-10.0") to scan either or both across independent
        repeat BATCHES, same idea as cw_odmr_lock_in.py's drive_power_dbm_
        list -- combining both runs the full 2D grid. See cmd_run_repeat()'s
        docstring.

    python pulsed_odmr.py calibrate-phase <file_name> [key=value ...]
        Runs continuously at a single, fixed freq_hz (opposite of
        rabi.py's calibrate-phase, which fixes tau_mw instead) and calls
        the SR830's auto-phase (APHS) to null Y -- see cmd_calibrate_
        phase()'s docstring for key=value overrides. Prints and saves
        phase_deg to {file_name}_calibrate_phase.txt; pass it into a real
        'run' via phase_deg=<value>.
        Example: python pulsed_odmr.py calibrate-phase phasecal1 freq_hz=2.8e9

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
from cw_odmr_lock_in import set_switch_static

# Own dedicated data directory -- previously reused rabi.DATA_DIR
# (D:/rabi), mixing pulsed ODMR frequency sweeps in with rabi.py's own
# tau_mw sweeps. Matches cw_odmr.py's/cw_odmr_lock_in.py's convention of
# each measurement TYPE getting its own top-level folder (D:/cw_odmr,
# D:/cw_odmr_lock_in). Only affects NEW runs -- existing data (pulsed1-5,
# pulsed_repeat, etc.) stays under D:/rabi where it was already saved and
# already analyzed in pulsed_odmr_result.ipynb; not moved automatically.
DATA_DIR = "D:\\pulsed_odmr"

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

Coil current scan (only runs when use_resonance_sweep=true, since it needs
f0_hz -- the resonator's peak frequency -- as the fixed drive point; scans
coil_current_a while driving AT f0_hz with the pulsed sequence already
configured in step 1, using tau_mw_us from the main sweep directly rather
than a separate coil-scan tau_mw parameter, since it's already fixed for
this whole run anyway. Reasoning: the resonance sweep above locates the MW
delivery resonator's own peak (a property of the antenna/circuit, not the
NV), while coil_current_a sets the static field and therefore the NV's
Zeeman-shifted transition frequency -- this scan finds the current that
makes the NV's actual transition coincide with the resonator's peak,
maximizing the pulsed lock-in signal the frequency sweep then traces out
around f0_hz. Same idea as rabi.py's use_resonance_freq coil scan):
    coil_scan_start_a=1.0
    coil_scan_stop_a=4.0
    coil_scan_step_a=0.5
    coil_scan_n_repeats=5
    coil_scan_current_settle_s=2.0

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
                               settle_s + settle_s -- also watch the
                               anchor_period_s-vs-estimated-total-sweep-
                               duration WARNING printed after resonance is
                               found (see notes.md): unlike rabi.py, the
                               sequence here is uploaded ONCE and free-runs
                               for the whole sweep, so this may need to be
                               MUCH larger (10000s) to avoid wrapping
                               mid-sweep at all
    trigger_retrigger_free_reps=1000  DECOUPLED from anchor_free_reps --
                               controls only the SDG1062X external
                               trigger's retrigger rate (fast recovery
                               after ANY reset, natural wrap or a forced
                               ABOR e.g. from the interlock check), NOT how
                               rarely the sequence naturally wraps. Confirmed
                               on real hardware that coupling these (as an
                               earlier version of this script did, deriving
                               the trigger rate straight from a large
                               anchor_free_reps) stretches the trigger
                               period out to match anchor_period_s -- after
                               an interlock-check ABOR, the AWG could then
                               sit idle for that ENTIRE period (~100s at
                               anchor_free_reps=50000) before the next edge
                               arrives, corrupting every point measured
                               during that window. Leave this at its
                               default regardless of anchor_free_reps.
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

    coil_scan_start_a = float(kw.get("coil_scan_start_a", 1.0))
    coil_scan_stop_a = float(kw.get("coil_scan_stop_a", 4.0))
    coil_scan_step_a = float(kw.get("coil_scan_step_a", 0.5))
    coil_scan_n_repeats = int(kw.get("coil_scan_n_repeats", 5))
    coil_scan_current_settle_s = float(kw.get("coil_scan_current_settle_s", 2.0))

    interlock_check_interval = int(kw.get("interlock_check_interval", 5))
    interlock_hold_periods = float(kw.get("interlock_hold_periods", 3.0))
    interlock_during_sweep = str(kw.get("interlock_during_sweep", "true")).lower() == "true"

    ch1_vpp = float(kw.get("ch1_vpp", 0.632))
    ch2_vpp = float(kw.get("ch2_vpp", 5.0))
    ch2_offset_v = float(kw.get("ch2_offset_v", 2.5))
    trigger_margin = float(kw.get("trigger_margin", 3.0))
    anchor_free_reps = int(kw.get("anchor_free_reps", 1000))
    # DECOUPLED from anchor_free_reps -- see trigger_period_s's comment
    # below for why. Confirmed on real hardware that coupling these two
    # (as _configure_external_trigger(sdg, anchor_period_s, ...) originally
    # did) causes catastrophic recovery delays after any ABOR (e.g. the
    # periodic interlock check) once anchor_free_reps is sized large enough
    # to survive a whole sweep (anchor_free_reps=50000 here stretched the
    # SDG's retrigger period to ~100s, so a mid-sweep ABOR left the AWG
    # idling at its anchor -- and every point measured during that window
    # reading garbage -- for up to ~100s, instead of instantly resuming).
    trigger_retrigger_free_reps = int(kw.get("trigger_retrigger_free_reps", 1000))
    extra_settle_s = float(kw.get("extra_settle_s", 0.0))

    # Normally the same as file_name -- lets cmd_run_repeat() point many
    # repeats' saved files at one shared folder (all still prefixed with
    # their own distinct file_name, e.g. "<file_name>_repeat3_pulsed_odmr_
    # x.npy") instead of each repeat getting its own <file_name>_repeat{i}/
    # directory. Same plumbing knob as rabi.py's cmd_run(), not something
    # to set by hand in normal use.
    output_dir = kw.get("output_dir", file_name)
    run_path = f"{DATA_DIR}/{output_dir}"
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
    # The SDG1062X external trigger's job is to keep RE-ARMING the AWG's
    # onceWaitTrig anchor quickly whenever the sequence returns to it --
    # whether that's from a NATURAL wrap (governed by anchor_free_reps,
    # which controls how rarely that happens) or a FORCED reset (any
    # awg.write("ABOR"), e.g. the periodic interlock check). Those are two
    # different concerns: anchor_free_reps should be large (to avoid
    # natural wraps mid-sweep), but the trigger itself should stay fast
    # regardless, so recovery from ANY reset is quick. Capping the period
    # used for the trigger calculation at trigger_retrigger_free_reps
    # (independent of how large anchor_free_reps actually is) keeps the
    # two decoupled -- same anchor_free_reps=1000-equivalent trigger rate
    # this codebase has always validated, even when the sequence's own
    # anchor_free_reps is sized much larger for a long sweep.
    trigger_period_s = ref_period_s * min(anchor_free_reps, trigger_retrigger_free_reps)

    with _tee_stdout(log_path):
        print(f"[pulsed_odmr] tau_mw fixed at {tau_mw_us} us, n_reps={n_reps} "
              f"-- ref_period_s={ref_period_s:.6f}, settle_s={dwell_settle_s:.3f}, "
              f"extra_settle_s={extra_settle_s:.3f}, "
              f"anchor_period_s={anchor_period_s:.3f}, "
              f"trigger_period_s={trigger_period_s:.3f} (decoupled from "
              f"anchor_period_s via trigger_retrigger_free_reps="
              f"{trigger_retrigger_free_reps} -- keeps ABOR recovery fast "
              f"regardless of how large anchor_free_reps is)")
        if anchor_period_s < dwell_settle_s + extra_settle_s:
            print(f"[pulsed_odmr] WARNING: anchor_period_s ({anchor_period_s:.3f} s) "
                  f"is SHORTER than settle_s + extra_settle_s "
                  f"({dwell_settle_s + extra_settle_s:.3f} s) -- the sequence will "
                  f"wrap and need an uncontrolled retrigger mid-window. Raise "
                  f"anchor_free_reps.")

        print("[pulsed_odmr] step 1/4: configuring AWG "
              f"({'CH2 held static for the resonance sweep' if use_resonance_sweep else 'fixed pulse sequence'}) "
              "+ SR830 lock-in")
        awg = ks33600a.KS33600A(AWG_RESOURCE, debug=False)
        sdg = SDG1062X(SDG_RESOURCE, debug=False)
        lia = SR830(SR830_RESOURCE, debug=True)
        rabi.setup_lock_in(lia, time_constant_s, sensitivity_v, phase_deg,
                            input_coupling, auto_sensitivity=auto_sensitivity)

        fixed_trigger_freq_hz = None
        if use_resonance_sweep:
            # Hold the ZYSWA switch parked on the sample path (RF2) instead
            # of running the real gated pulse sequence yet -- resonance_
            # sweep() below reads the spectrum analyzer and needs
            # continuous, unmodulated RF through the switch. If CH2 were
            # already toggling per the pulsed on/off gate, the analyzer
            # would see RF flipping between the sample path and the dump
            # path every chop cycle -- amplitude-modulation sidebands
            # riding on the resonance dip, not a clean reflected-power
            # trace. Same fix cw_odmr_lock_in.py already uses (set_switch_
            # static()) before ITS resonance_sweep() call. rabi.py's
            # setup_awg_sequences() requires CH1/CH2 to wrap together
            # (see its own docstring) -- there's no safe partial state
            # ("CH1 pulsing, CH2 static"), so the real synchronized pulse
            # sequence is deferred entirely until resonance is found,
            # below, rather than started here and reconfigured mid-flight.
            set_switch_static(awg, route_to_sample=True)
            print("[pulsed_odmr] step 1/4 done: CH2 held static (switch on "
                  "the sample path) -- pulse sequence deferred until "
                  "resonance is found")
        else:
            fixed_trigger_freq_hz = rabi._configure_external_trigger(
                sdg, trigger_period_s, margin=trigger_margin)
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

                # Determine the frequency scan range NOW (right after
                # resonance is found) rather than after the coil scan below
                # -- needed up front so the anchor_free_reps sizing check
                # right after this can see the REAL number of sweep points,
                # not just guess.
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
                freqs_hz = np.arange(start_hz, stop_hz + freq_step_hz / 2, freq_step_hz)

                # Resonance found -- now start the REAL gated pulse
                # sequence (both channels together, synchronized from a
                # fresh trigger via the onceWaitTrig anchor) for the coil
                # scan and frequency sweep below, both of which need actual
                # pulsed lock-in detection, not the static switch used for
                # the resonance sweep above.
                #
                # anchor_free_reps sizing here is a DIFFERENT problem than
                # rabi.py's per-point case: rabi.py calls setup_awg_
                # sequences() fresh at the START of every point's settle
                # window, so the sequence's own wrap (anchor_period_s =
                # ref_period_s * anchor_free_reps) simply hasn't happened
                # yet as long as it exceeds THAT ONE settle window --
                # deterministic non-overlap. Here, the sequence is uploaded
                # ONCE and free-runs, wrapping over and over,
                # asynchronously, for the ENTIRE multi-point sweep --
                # whether any given point's settle window happens to
                # overlap a wrap is essentially random, and sizing anchor_
                # free_reps against a single settle window (as rabi.py
                # does) only reduces the wrap RATE, not the total number of
                # wrap events over a long sweep. Confirmed on real hardware
                # (pulsed1/2/3, anchor_free_reps=1000): 4-5 of ~120-140
                # points showed reference_unlock, matching a back-of-
                # envelope estimate of (est. total sweep time / anchor_
                # period_s) wrap events, each with roughly (dwell_settle_s /
                # anchor_period_s) chance of clipping some point's settle
                # window. The only way to make this deterministic (like
                # rabi.py) instead of probabilistic is to size anchor_
                # period_s to exceed the ENTIRE estimated sweep duration, so
                # the sequence never wraps at all during the run --
                # essentially free to do (anchor_free_reps is a sequence-
                # table REPEAT COUNT, not a waveform re-upload -- doesn't
                # cost AWG memory, see setup_awg_sequences()'s docstring).
                # Includes the coil-current scan's own duration -- it runs
                # on this SAME uploaded sequence, before the main frequency
                # loop even starts, so the anchor has to survive that too.
                n_coil_scan_points = len(np.arange(
                    coil_scan_start_a, coil_scan_stop_a + coil_scan_step_a / 2,
                    coil_scan_step_a))
                est_coil_scan_s = n_coil_scan_points * (
                    coil_scan_current_settle_s + coil_scan_n_repeats * dwell_settle_s)
                est_sweep_s = est_coil_scan_s + len(freqs_hz) * (settle_s + dwell_settle_s)
                if anchor_period_s < est_sweep_s:
                    suggested_anchor_free_reps = int(
                        anchor_free_reps * (est_sweep_s / anchor_period_s) * 1.5)
                    print(f"[pulsed_odmr] WARNING: anchor_period_s "
                          f"({anchor_period_s:.1f} s) is SHORTER than the "
                          f"estimated total sweep duration ({est_sweep_s:.1f} "
                          f"s, {len(freqs_hz)} points) -- the sequence will "
                          f"wrap MANY times over the course of this sweep, "
                          f"each wrap risking an uncontrolled retrigger "
                          f"landing inside some point's settle window "
                          f"(reference_unlock). Consider re-running with "
                          f"anchor_free_reps={suggested_anchor_free_reps} "
                          f"(sized to exceed the whole sweep, with 1.5x "
                          f"margin) to eliminate this instead of just "
                          f"reducing its rate.")

                print(f"[pulsed_odmr] step 2/4: resonance found -- starting "
                      f"the pulsed sequence (tau_mw={tau_mw_us} us, "
                      f"n_reps={n_reps}) + SDG1062X external trigger")
                fixed_trigger_freq_hz = rabi._configure_external_trigger(
                    sdg, trigger_period_s, margin=trigger_margin)
                rabi.setup_awg_sequences(
                    awg, tau_mw_us, n_reps, laser_us, pre_us, post_us,
                    sequence_name_ch1="pulsed_odmr_ch1",
                    sequence_name_ch2="pulsed_odmr_ch2",
                    ch1_vpp=ch1_vpp, ch2_vpp=ch2_vpp, ch2_offset_v=ch2_offset_v,
                    anchor_free_reps=anchor_free_reps,
                )
                if extra_settle_s > 0:
                    time.sleep(extra_settle_s)
                print(f"[pulsed_odmr] step 2/4 done: trigger fixed at "
                      f"{fixed_trigger_freq_hz/1e3:.3f} kHz, pulse sequence "
                      f"uploaded (never reconfigured again -- only freq_hz "
                      f"changes per point in the sweep below)")

                # Coil current scan: f0_hz above is the MW delivery
                # resonator's OWN peak (antenna/circuit property, found via
                # the low-power spectrum-analyzer sweep) -- it says nothing
                # about where the NV's Zeeman-shifted transition actually
                # sits. Drive AT f0_hz with the REAL pulsed sequence
                # (already uploaded once in step 1, tau_mw_us fixed for the
                # whole run -- no AWG reconfigure needed here) and vary
                # coil_current_a to find the field that brings the NV
                # transition into coincidence with the resonator peak,
                # maximizing the lock-in signal the frequency sweep below
                # then traces the actual lineshape around. See notes.md /
                # rabi.py's use_resonance_freq coil scan for the same idea
                # applied to the tau_mw sweep instead.
                print(f"[pulsed_odmr] coil current scan: {coil_scan_start_a}-"
                      f"{coil_scan_stop_a} A, step {coil_scan_step_a} A, "
                      f"{coil_scan_n_repeats} repeats/point, driving at "
                      f"f0_hz={f0_hz/1e9:.5f} GHz (resonator peak, tau_mw="
                      f"{tau_mw_us} us)")
                gen.preset()
                gen.set_power_dbm(drive_power_dbm)
                gen.set_frequency_hz(f0_hz)
                gen.rf_on()
                time.sleep(1.0)  # let the initial frequency/level settle

                if auto_sensitivity:
                    time.sleep(dwell_settle_s)
                    lia.auto_gain()
                    time.sleep(dwell_settle_s)
                    actual_sensitivity_v = lia.get_sensitivity_v()
                    print(f"[pulsed_odmr] coil scan auto_sensitivity: AGAN "
                          f"selected {actual_sensitivity_v:.3e} V full scale")

                coil_scan_currents_a = np.arange(
                    coil_scan_start_a,
                    coil_scan_stop_a + coil_scan_step_a / 2,
                    coil_scan_step_a,
                )
                coil_scan_x = np.full(len(coil_scan_currents_a), np.nan)
                coil_scan_y = np.full(len(coil_scan_currents_a), np.nan)
                for ci, current_a in enumerate(coil_scan_currents_a):
                    coil_scan_voltage_v = voltage_for_current(current_a) * coil_voltage_margin
                    coil_psu.turn_on(coil_scan_voltage_v, current_a)
                    time.sleep(coil_scan_current_settle_s)

                    repeat_x = np.empty(coil_scan_n_repeats)
                    repeat_y = np.empty(coil_scan_n_repeats)
                    for ri in range(coil_scan_n_repeats):
                        rabi._wait_settle_discarding_transient_overload(lia, dwell_settle_s)

                        # Same overload gap fixed in rabi.py's coil scan:
                        # signal strength genuinely varies across the coil
                        # current range (that's the whole point of this
                        # scan), and can saturate whatever sensitivity was
                        # picked before the scan started.
                        if auto_rescale_on_overload:
                            status = lia.read_overload_status()
                            for attempt in range(max_rescale_attempts):
                                if not status["any"]:
                                    break
                                old_v = lia.get_sensitivity_v()
                                new_v = rabi._step_sensitivity_coarser(lia)
                                lia.read_overload_status()  # discard the range-switch transient
                                print(f"[pulsed_odmr] coil scan OVERLOAD at current="
                                      f"{current_a:.3f} A: rescaling sensitivity "
                                      f"{old_v:.3e} V -> {new_v:.3e} V full scale, "
                                      f"re-settling (attempt {attempt + 1}/"
                                      f"{max_rescale_attempts})")
                                rabi._wait_settle_discarding_transient_overload(lia, dwell_settle_s)
                                status = lia.read_overload_status()
                            else:
                                print(f"[pulsed_odmr] coil scan: still overloading at "
                                      f"current={current_a:.3f} A after "
                                      f"{max_rescale_attempts} rescale attempts -- "
                                      f"this point's X/Y may be railed/clipped")

                        repeat_x[ri], repeat_y[ri] = lia.read_xy()
                    # Average X/Y across repeats, THEN compute R from the
                    # averaged X/Y -- not averaging R directly, same
                    # convention as rabi.py's coil scan / cw_odmr_lock_in.py.
                    coil_scan_x[ci] = repeat_x.mean()
                    coil_scan_y[ci] = repeat_y.mean()
                    r_here = (coil_scan_x[ci] ** 2 + coil_scan_y[ci] ** 2) ** 0.5
                    print(f"[pulsed_odmr] coil scan {ci + 1}/{len(coil_scan_currents_a)}: "
                          f"current={current_a:.3f} A, "
                          f"X={coil_scan_x[ci]:.6e} V, Y={coil_scan_y[ci]:.6e} V, "
                          f"R={r_here:.6e} V")

                coil_scan_r = np.sqrt(coil_scan_x ** 2 + coil_scan_y ** 2)
                best_idx = int(np.argmax(coil_scan_r))
                old_coil_current_a = coil_current_a
                coil_current_a = float(coil_scan_currents_a[best_idx])
                print(f"[pulsed_odmr] coil current scan done: best current "
                      f"{coil_current_a:.3f} A (R={coil_scan_r[best_idx]:.6e} V) "
                      f"-- overriding coil_current_a from {old_coil_current_a} A "
                      f"for the rest of this run")

                np.save(f"{run_path}/{file_name}_coil_scan_current_a.npy",
                        coil_scan_currents_a)
                np.save(f"{run_path}/{file_name}_coil_scan_x.npy", coil_scan_x)
                np.save(f"{run_path}/{file_name}_coil_scan_y.npy", coil_scan_y)
                np.save(f"{run_path}/{file_name}_coil_scan_r.npy", coil_scan_r)
                print(f"[pulsed_odmr] coil current scan: saved "
                      f"{run_path}/{file_name}_coil_scan_current_a.npy, "
                      f"_coil_scan_x.npy, _coil_scan_y.npy, _coil_scan_r.npy")

                # Actually apply the chosen current -- the loop above may
                # have left the PSU at whatever current it tried LAST, not
                # necessarily the best one. RF/gen state left on at f0_hz
                # here is harmless -- the frequency loop below does its own
                # gen.preset()/set_frequency_hz(freqs_hz[0])/rf_on() before
                # starting anyway.
                coil_voltage_v = voltage_for_current(coil_current_a) * coil_voltage_margin
                coil_psu.turn_on(coil_voltage_v, coil_current_a)
                time.sleep(coil_scan_current_settle_s)
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
                    fh.write(f"coil_current_a={coil_current_a}\n")
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


def _tau_tag(tau_mw_us):
    """Filesystem-safe tag for a tau_mw_us value, e.g. 0.5 -> '0p5us',
    2.0 -> '2us' -- used to disambiguate per-tau_mw output files in
    cmd_run_repeat()'s tau_mw_us_list mode. Same idea as cw_odmr_lock_
    in.py's _power_tag() for its drive_power_dbm_list."""
    return f"{tau_mw_us:g}us".replace(".", "p")


def _power_tag(power_dbm):
    """Filesystem-safe tag for a dBm value, e.g. -40.0 -> 'm40dBm',
    0.0 -> '0dBm' -- same convention as cw_odmr_lock_in.py's _power_tag(),
    used to disambiguate per-power output files in cmd_run_repeat()'s
    drive_power_dbm_list mode."""
    sign = "m" if power_dbm < 0 else ""
    return f"{sign}{abs(power_dbm):g}dBm".replace(".", "p")


def cmd_run_repeat(file_name, **kw):
    """
    Repeat cmd_run() n_repeats times in a row with IDENTICAL settings,
    then average X and Y elementwise across the completed repeats (R
    computed from the averaged X/Y, never averaged directly -- R has a
    positive noise-rectification bias averaging after the fact doesn't
    remove). Adapted from rabi.py's cmd_run_repeat() -- see its docstring
    for the full rationale; the two differences from that version are
    noted below.

    Each repeat runs `python pulsed_odmr.py run <repeat_name> ...` as a
    genuine SEPARATE OS PROCESS (via subprocess), not an in-process
    cmd_run() call -- same reason as rabi.py's version: avoids cross-
    repeat resource accumulation (VISA session state, AWG waveform
    uploads) building up across many repeats within one long-lived Python
    process, at the cost of one extra interpreter startup per repeat
    (negligible next to an actual frequency sweep).

    All repeats' files land in ONE shared folder (D:/pulsed_odmr/
    <file_name>/, same as a plain cmd_run() call would use), distinguished
    by file name (<file_name>_repeat{i}_pulsed_odmr_x.npy etc.) via
    cmd_run()'s output_dir override, not by directory.

    DIFFERENCE 1 from rabi.py's version: rabi.py pins a single resolved
    freq_hz (its use_resonance_freq finds ONE frequency to drive at, for
    a tau_mw sweep). This module sweeps FREQUENCY, so what use_resonance_
    sweep resolves is a whole RANGE (start_hz/stop_hz, derived from f0 +/-
    fwhm_margin*FWHM/2) -- so repeat 0's ACTUAL REALIZED range is read
    back from its saved _pulsed_odmr_freqs_hz.npy (min/max), not from a
    separate metadata field, and passed explicitly (with use_resonance_
    sweep forced false) to every repeat after that. Without this, use_
    resonance_sweep would re-run the whole resonance search independently
    every repeat -- slower, and an averaged batch could drift point-to-
    point if the found f0/FWHM isn't perfectly identical each time.

    DIFFERENCE 2: rabi.py's version keeps the coarse sweep + a separate
    reflected-power-at-operating-point pre-flight check running even on
    pinned repeats (only skips the FINE stage). This module has no
    equivalent standalone pre-flight check outside resonance_sweep()
    itself -- the real per-point safety net here is interlock_during_
    sweep's periodic check during the main frequency loop, which stays
    active on EVERY repeat regardless of pinning, so skipping the WHOLE
    resonance_sweep() for repeats 1+ (rather than just its fine stage)
    doesn't lose any independent safety check.

    coil_current_a is pinned the same way as rabi.py's version (read back
    from repeat 0's metadata), covering the case where use_resonance_
    sweep also ran the coil-current scan.

    tau_mw_us_list (optional, e.g. "0.5,1.0,2.0,5.0"): scans tau_mw_us
    across repeat BATCHES, same idea as cw_odmr_lock_in.py's cmd_sweep_
    average() drive_power_dbm_list -- for EACH tau_mw_us value, runs its
    own independent batch of n_repeats repeats (its own repeat 0 finds/
    pins its own resonance range and coil current, not shared across
    tau_mw_us values, since the resonance frequency itself doesn't depend
    on tau_mw but this keeps the pinning logic simple and matches cw_
    odmr_lock_in.py's per-power independence), and averages within that
    batch alone. Output files get a tau_mw tag via _tau_tag() (e.g.
    "0.5" -> "0p5us") to disambiguate, same idea as cw_odmr_lock_in.py's
    _power_tag() -- <file_name>_tau0p5us_repeat0_..., <file_name>_
    tau0p5us_avg_..., etc. Without tau_mw_us_list, behaves exactly as
    before (no tag, plain <file_name>_repeat{i}_...).

    drive_power_dbm_list (optional, e.g. "0.0,-5.0,-10.0"): scans
    drive_power_dbm across repeat BATCHES too, same mechanism as
    tau_mw_us_list (own _power_tag(), e.g. "-5.0" -> "m5dBm"). Combining
    BOTH tau_mw_us_list and drive_power_dbm_list runs the full 2D grid --
    one independent n_repeats batch per (tau_mw_us, drive_power_dbm) pair,
    tagged <file_name>_tau0p5us_powerm5dBm_repeat0_..., each pair's own
    repeat 0 independently finds/pins its own resonance range and coil
    current (not shared across the grid, same reasoning as tau_mw_us_list
    alone). With only one of the two lists given, behaves exactly as that
    list's 1D case; with neither, behaves exactly as a single plain batch
    (no tags).

    Recognized key=value overrides:
      n_repeats=10  number of times to repeat the sweep, per grid point
                    if tau_mw_us_list/drive_power_dbm_list are given
      tau_mw_us_list=None  comma-separated list of tau_mw_us values, e.g.
                    "0.5,1.0,2.0,5.0" -- overrides tau_mw_us if given
      drive_power_dbm_list=None  comma-separated list of drive_power_dbm
                    values, e.g. "0.0,-5.0,-10.0" -- overrides
                    drive_power_dbm if given; combine with tau_mw_us_list
                    for the full 2D grid
      (everything else passed through to cmd_run() UNCHANGED, once per
      repeat)

    A repeat that trips the interlock partway through saves a SHORTER
    freqs_hz array than a fully-completed one -- excluded from the
    average entirely (its grid won't match the first fully-completed
    repeat's), not padded or partially averaged in.

    Saves each repeat's normal cmd_run() file set (prefixed <file_name>_
    [tau<tag>_]repeat{i}_...) plus, once at least one repeat in a batch
    completes fully, that batch's average (prefixed <file_name>_[tau<tag>_]
    avg_...) -- all under D:/pulsed_odmr/<file_name>/:
      <file_name>_repeat{i}_pulsed_odmr_freqs_hz.npy, _x.npy, _y.npy, ...
      <file_name>_avg_pulsed_odmr_freqs_hz.npy, _avg_pulsed_odmr_x.npy,
      _avg_pulsed_odmr_y.npy, _avg_pulsed_odmr_r.npy, _avg_pulsed_odmr_
      metadata.txt (records n_repeats_requested vs. n_repeats_averaged).
    """
    n_repeats = int(kw.pop("n_repeats", 10))
    tau_mw_us_list_raw = kw.pop("tau_mw_us_list", None)
    multi_tau = tau_mw_us_list_raw is not None
    if multi_tau:
        tau_list = [float(t) for t in str(tau_mw_us_list_raw).split(",")]
    else:
        tau_list = [None]  # single batch, no tag, tau_mw_us left as given (or cmd_run()'s own default)

    drive_power_dbm_list_raw = kw.pop("drive_power_dbm_list", None)
    multi_power = drive_power_dbm_list_raw is not None
    if multi_power:
        power_list = [float(p) for p in str(drive_power_dbm_list_raw).split(",")]
    else:
        power_list = [None]  # single batch, no tag, drive_power_dbm left as given (or cmd_run()'s own default)

    import subprocess
    import glob
    import re

    for tau_mw_us in tau_list:
        for drive_power_dbm in power_list:
            group_prefix = file_name
            if multi_tau:
                group_prefix += f"_tau{_tau_tag(tau_mw_us)}"
            if multi_power:
                group_prefix += f"_power{_power_tag(drive_power_dbm)}"
            if multi_tau or multi_power:
                print(f"[pulsed_odmr] tau_mw_us={tau_mw_us}, drive_power_dbm="
                      f"{drive_power_dbm}: starting batch of {n_repeats} "
                      f"repeats (saved as {group_prefix}_...)")

            resolved_start_hz = None
            resolved_stop_hz = None
            resolved_coil_current_a = None
            try:
                for i in range(n_repeats):
                    repeat_name = f"{group_prefix}_repeat{i}"
                    repeat_kw = dict(kw)
                    if multi_tau:
                        repeat_kw["tau_mw_us"] = tau_mw_us
                    if multi_power:
                        repeat_kw["drive_power_dbm"] = drive_power_dbm
                    pin_note = ""
                    if resolved_start_hz is not None:
                        repeat_kw["start_hz"] = resolved_start_hz
                        repeat_kw["stop_hz"] = resolved_stop_hz
                        repeat_kw["use_resonance_sweep"] = "false"
                        pin_note = (f", pinned to {resolved_start_hz/1e9:.5f}-"
                                    f"{resolved_stop_hz/1e9:.5f} GHz")
                        if resolved_coil_current_a is not None:
                            repeat_kw["coil_current_a"] = resolved_coil_current_a
                            pin_note += f", {resolved_coil_current_a:.3f} A"
                        pin_note += " (from this batch's repeat 0)"
                    print(f"[pulsed_odmr] repeat {i + 1}/{n_repeats}: running "
                          f"{repeat_name} (saved into {file_name}/){pin_note}")

                    repeat_kw["output_dir"] = file_name
                    args = [sys.executable, __file__, "run", repeat_name]
                    args += [f"{k}={v}" for k, v in repeat_kw.items()]
                    # Inherits this process's stdout/stderr (no capture_output)
                    # so the repeat's own live progress prints straight
                    # through, same as watching a single cmd_run() call.
                    result = subprocess.run(args)
                    if result.returncode != 0:
                        print(f"[pulsed_odmr] repeat {i}: subprocess exited "
                              f"with code {result.returncode} -- check its "
                              f"output above; continuing to the next repeat "
                              f"regardless (a repeat with no saved data is "
                              f"just excluded from the average below)")

                    if i == 0:
                        run_path0 = f"{DATA_DIR}/{file_name}"
                        freqs_path = f"{run_path0}/{repeat_name}_pulsed_odmr_freqs_hz.npy"
                        metadata_path = f"{run_path0}/{repeat_name}_pulsed_odmr_metadata.txt"
                        if os.path.exists(freqs_path):
                            freqs0 = np.load(freqs_path)
                            resolved_start_hz = float(freqs0.min())
                            resolved_stop_hz = float(freqs0.max())
                            print(f"[pulsed_odmr] {group_prefix} repeat 0 swept "
                                  f"{resolved_start_hz/1e9:.5f}-{resolved_stop_hz/1e9:.5f} "
                                  f"GHz -- pinning repeats 1..{n_repeats - 1} to "
                                  f"this same range")
                            if os.path.exists(metadata_path):
                                with open(metadata_path) as fh:
                                    metadata_text = fh.read()
                                repeat0_metadata = dict(
                                    line.split("=", 1) for line in metadata_text.strip().splitlines()
                                )
                                if "coil_current_a" in repeat0_metadata:
                                    resolved_coil_current_a = float(repeat0_metadata["coil_current_a"])
                                    print(f"[pulsed_odmr] {group_prefix} repeat 0 "
                                          f"used coil_current_a="
                                          f"{resolved_coil_current_a:.3f} A -- "
                                          f"pinning later repeats to this too")
                        else:
                            print(f"[pulsed_odmr] {group_prefix} repeat 0: no "
                                  f"data saved (0 points completed) -- can't "
                                  f"resolve a range to pin later repeats to; "
                                  f"they'll use their own settings as given")
            except KeyboardInterrupt:
                print("[pulsed_odmr] repeat: stopped by user (Ctrl+C) -- "
                      "averaging whatever repeats completed so far")

            run_path = f"{DATA_DIR}/{file_name}"
            os.makedirs(run_path, exist_ok=True)

            reference_freqs_hz = None
            xs, ys = [], []
            n_averaged = 0
            for i in range(n_repeats):
                repeat_name = f"{group_prefix}_repeat{i}"
                freqs_path = f"{run_path}/{repeat_name}_pulsed_odmr_freqs_hz.npy"
                if not os.path.exists(freqs_path):
                    print(f"[pulsed_odmr] {group_prefix} repeat {i}: no data "
                          f"saved (0 points completed) -- excluded from average")
                    continue
                freqs_i = np.load(freqs_path)
                if reference_freqs_hz is None:
                    reference_freqs_hz = freqs_i
                elif len(freqs_i) != len(reference_freqs_hz) or not np.allclose(freqs_i, reference_freqs_hz):
                    print(f"[pulsed_odmr] {group_prefix} repeat {i}: freqs_hz "
                          f"grid doesn't match the first completed repeat's "
                          f"({len(freqs_i)} vs {len(reference_freqs_hz)} "
                          f"points) -- partial/tripped repeat, excluded from "
                          f"average")
                    continue
                xs.append(np.load(f"{run_path}/{repeat_name}_pulsed_odmr_x.npy"))
                ys.append(np.load(f"{run_path}/{repeat_name}_pulsed_odmr_y.npy"))
                n_averaged += 1

            if n_averaged == 0:
                print(f"[pulsed_odmr] {group_prefix}: no complete repeats to "
                      f"average -- nothing saved for this batch")
                if multi_tau or multi_power:
                    continue
                return

            x_avg = np.mean(xs, axis=0)
            y_avg = np.mean(ys, axis=0)
            r_avg = np.sqrt(x_avg ** 2 + y_avg ** 2)

            np.save(f"{run_path}/{group_prefix}_avg_pulsed_odmr_freqs_hz.npy", reference_freqs_hz)
            np.save(f"{run_path}/{group_prefix}_avg_pulsed_odmr_x.npy", x_avg)
            np.save(f"{run_path}/{group_prefix}_avg_pulsed_odmr_y.npy", y_avg)
            np.save(f"{run_path}/{group_prefix}_avg_pulsed_odmr_r.npy", r_avg)
            with open(f"{run_path}/{group_prefix}_avg_pulsed_odmr_metadata.txt", "w") as fh:
                fh.write(f"n_repeats_requested={n_repeats}\n")
                fh.write(f"n_repeats_averaged={n_averaged}\n")
                if multi_tau:
                    fh.write(f"tau_mw_us={tau_mw_us}\n")
                if multi_power:
                    fh.write(f"drive_power_dbm={drive_power_dbm}\n")

            print(f"[pulsed_odmr] {group_prefix} done: averaged {n_averaged}/"
                  f"{n_repeats} completed repeats, saved {run_path}/"
                  f"{group_prefix}_avg_pulsed_odmr_freqs_hz.npy, "
                  f"_avg_pulsed_odmr_x.npy, _avg_pulsed_odmr_y.npy, "
                  f"_avg_pulsed_odmr_r.npy, _avg_pulsed_odmr_metadata.txt")


def cmd_calibrate_phase(file_name, **kw):
    """
    Runs the AWG/lock-in continuously at a single, fixed freq_hz (the
    opposite of rabi.py's cmd_calibrate_phase(), which fixes tau_mw and
    varies nothing -- here tau_mw_us is what's fixed, matching this
    module's own convention) and calls the SR830's built-in auto-phase
    (APHS) to null Y against the real CH2-sourced block reference. Same
    gap as rabi.py's cmd_run(): phase_deg is NOT auto-calibrated by
    cmd_run() itself. Pick freq_hz at or near resonance (wherever a
    previous cmd_run()'s pulsed ODMR data showed the biggest X/Y swing)
    for a real, reasonably strong signal -- auto_phase() needs that to
    get a stable reading, not an arbitrary off-resonance point.

    Adapted line-for-line from rabi.py's cmd_calibrate_phase(), including
    every fix found there on real hardware (see notes.md): a discard
    read right after setup_awg_sequences() (clears the reconfigure-
    transient reference_unlock/overload latch), a rescale-retry loop
    after auto_gain() (AGAN only steps ONE sensitivity range per call),
    a discard read right after auto_phase() (clears the phase-step
    transient), and a signal-strength check (R as % of full scale --
    catches the TOO-WEAK failure mode overload can't, which corrupts
    auto_phase()'s atan2(Y, X) null into noise-dominated, run-to-run
    inconsistent readings).

    Prints and saves the resulting phase_deg to
    {file_name}_calibrate_phase.txt -- pass it into a real cmd_run() via
    phase_deg=<value>.

    Recognized key=value overrides (all optional): freq_hz,
    drive_power_dbm, tau_mw_us, n_reps, laser_us, pre_us, post_us,
    time_constant_s, settle_periods, settle_time_constants,
    input_coupling, ch1_vpp, ch2_vpp, ch2_offset_v, trigger_margin,
    anchor_free_reps, trigger_retrigger_free_reps, max_rescale_attempts,
    extra_settle_s, psu_voltage_v, psu_current_limit_a, coil_current_a,
    coil_voltage_margin -- same meaning/defaults as cmd_run()'s.
    """
    freq_hz = float(kw.get("freq_hz", 2.843e9))
    drive_power_dbm = float(kw.get("drive_power_dbm", 0.0))
    tau_mw_us = float(kw.get("tau_mw_us", 5.0))
    n_reps = int(kw.get("n_reps", 250))
    laser_us = float(kw.get("laser_us", 2.0))
    pre_us = float(kw.get("pre_us", 1.0))
    post_us = float(kw.get("post_us", 1.0))
    time_constant_s = float(kw.get("time_constant_s", 0.1))
    settle_periods = float(kw.get("settle_periods", 5.0))
    settle_time_constants = float(kw.get("settle_time_constants", 9.0))
    input_coupling = str(kw.get("input_coupling", "ac"))
    ch1_vpp = float(kw.get("ch1_vpp", 0.632))
    ch2_vpp = float(kw.get("ch2_vpp", 5.0))
    ch2_offset_v = float(kw.get("ch2_offset_v", 2.5))
    trigger_margin = float(kw.get("trigger_margin", 3.0))
    anchor_free_reps = int(kw.get("anchor_free_reps", 1000))
    # See cmd_run()'s trigger_period_s comment -- decoupled from
    # anchor_free_reps so the SDG1062X retrigger rate stays fast
    # regardless of how large anchor_free_reps is sized. A single
    # calibration point is short, so this rarely matters in practice
    # (unlike cmd_run()'s whole-sweep case), but kept consistent anyway.
    trigger_retrigger_free_reps = int(kw.get("trigger_retrigger_free_reps", 1000))
    max_rescale_attempts = int(kw.get("max_rescale_attempts", 5))
    extra_settle_s = float(kw.get("extra_settle_s", 0.0))
    psu_voltage_v = float(kw.get("psu_voltage_v", 12.0))
    psu_current_limit_a = float(kw.get("psu_current_limit_a", 1.9))
    coil_current_a = float(kw.get("coil_current_a", 2.0))
    coil_voltage_margin = float(kw.get("coil_voltage_margin", 1.5))

    run_path = f"{DATA_DIR}/{file_name}"
    os.makedirs(run_path, exist_ok=True)
    log_dir = f"{DATA_DIR}/logs"
    os.makedirs(log_dir, exist_ok=True)
    log_path = f"{log_dir}/{file_name}_calibrate_phase.txt"

    with _tee_stdout(log_path):
        print(f"[pulsed_odmr] phase calibration: freq_hz={freq_hz/1e9:.5f} GHz, "
              f"tau_mw={tau_mw_us} us, n_reps={n_reps}, {drive_power_dbm} dBm")

        awg = ks33600a.KS33600A(AWG_RESOURCE, debug=True)
        sdg = SDG1062X(SDG_RESOURCE, debug=True)
        lia = SR830(SR830_RESOURCE, debug=True)
        # phase_deg=0 as a starting point -- auto_phase() below finds the
        # real value; auto_sensitivity=True so the initial sensitivity_v
        # doesn't matter.
        rabi.setup_lock_in(lia, time_constant_s, sensitivity_v=5e-3, phase_deg=0.0,
                            input_coupling=input_coupling, auto_sensitivity=True)

        gen = HP8673H(GEN_RESOURCE)
        amp_psu = SPD1168X(AMP_PSU_RESOURCE)
        coil_psu = SPD1305X(COIL_PSU_RESOURCE)
        try:
            amp_psu.turn_on(psu_voltage_v, psu_current_limit_a)
            coil_voltage_v = voltage_for_current(coil_current_a) * coil_voltage_margin
            coil_psu.turn_on(coil_voltage_v, coil_current_a)

            gen.preset()
            gen.set_power_dbm(drive_power_dbm)
            gen.set_frequency_hz(freq_hz)
            gen.rf_on()
            time.sleep(1.0)  # let the initial frequency/level settle

            rep_us = laser_us + pre_us + tau_mw_us + post_us
            ref_period_s = 2 * n_reps * rep_us * 1e-6
            settle_s = max(settle_periods * ref_period_s,
                            settle_time_constants * time_constant_s)
            anchor_period_s = ref_period_s * anchor_free_reps
            trigger_period_s = ref_period_s * min(anchor_free_reps, trigger_retrigger_free_reps)

            rabi._configure_external_trigger(sdg, trigger_period_s, margin=trigger_margin)
            rabi.setup_awg_sequences(
                awg, tau_mw_us, n_reps, laser_us, pre_us, post_us,
                sequence_name_ch1="calib_ch1", sequence_name_ch2="calib_ch2",
                ch1_vpp=ch1_vpp, ch2_vpp=ch2_vpp, ch2_offset_v=ch2_offset_v,
                anchor_free_reps=anchor_free_reps,
            )
            if extra_settle_s > 0:
                time.sleep(extra_settle_s)

            # Discards the reference_unlock/overload latch caused by
            # setup_awg_sequences() itself -- see rabi.py's cmd_
            # calibrate_phase() docstring/notes.md for the full history
            # of why this and the two discard reads below are needed.
            def _print_status(label):
                s = lia.read_overload_status()
                print(f"[pulsed_odmr] diagnostic ({label}): reference_unlock="
                      f"{s['reference_unlock']}, input_overload={s['input']}, "
                      f"filter_overload={s['filter']}, output_overload={s['output']}")
                return s

            _print_status("right after setup_awg_sequences (discard)")

            print(f"[pulsed_odmr] settling {settle_s * 1e3:.0f} ms, then auto-gain + auto-phase")
            time.sleep(settle_s)
            _print_status("after 1st settle_s, before auto_gain")
            lia.auto_gain()
            time.sleep(settle_s)
            status = _print_status("after auto_gain + 2nd settle_s, before auto_phase")

            # AGAN only steps sensitivity ONE range per call -- rescale-
            # retry loop instead of trusting a single AGAN call.
            for attempt in range(max_rescale_attempts):
                if not status["any"]:
                    break
                old_v = lia.get_sensitivity_v()
                new_v = rabi._step_sensitivity_coarser(lia)
                lia.read_overload_status()  # discard the range-switch transient
                print(f"[pulsed_odmr] OVERLOAD after auto_gain: rescaling sensitivity "
                      f"{old_v:.3e} V -> {new_v:.3e} V full scale, re-checking "
                      f"(attempt {attempt + 1}/{max_rescale_attempts})")
                time.sleep(settle_s)
                status = _print_status(f"after rescale attempt {attempt + 1}")
            else:
                print(f"[pulsed_odmr] WARNING: still overloading after "
                      f"{max_rescale_attempts} rescale attempts -- phase_deg "
                      f"below may still be corrupted")

            lia.auto_phase()
            # APHS steps the reference phase, which transiently
            # redistributes signal between X/Y -- discard so the final
            # check below reflects the actual settled state.
            lia.read_overload_status()
            time.sleep(settle_s)
            phase_deg = lia.get_phase_deg()
            x, y = lia.read_xy()

            status = _print_status("after auto_phase + 3rd settle_s (final)")
            if status["reference_unlock"] or status["any"]:
                print("[pulsed_odmr] WARNING: reference unlock or overload flagged "
                      "right at the auto-phase reading -- this phase_deg may be "
                      "corrupted, consider re-running before trusting it")

            # R relative to the active sensitivity's full scale -- catches
            # the TOO-WEAK failure mode overload alone can't (see rabi.py's
            # cmd_calibrate_phase() notes.md entry for how this was found).
            r = (x ** 2 + y ** 2) ** 0.5
            active_sensitivity_v = lia.get_sensitivity_v()
            fraction_of_full_scale = r / active_sensitivity_v
            print(f"[pulsed_odmr] signal check: R={r:.3e} V is "
                  f"{fraction_of_full_scale * 100:.1f}% of the active "
                  f"{active_sensitivity_v:.3e} V full scale")
            if fraction_of_full_scale < 0.05:
                print("[pulsed_odmr] WARNING: R is under 5% of full scale -- "
                      "auto_phase()'s null may be noise-dominated rather than "
                      "signal-dominated, giving an inconsistent phase_deg "
                      "run-to-run; pick a freq_hz closer to resonance or a finer "
                      "sensitivity range before trusting this")

            print(f"[pulsed_odmr] auto-phase result: phase_deg={phase_deg:.3f} deg "
                  f"(X={x:.6e} V, Y={y:.6e} V after nulling)")
            print(f"[pulsed_odmr] use this with cmd_run() via phase_deg={phase_deg:.3f}")

            with open(f"{run_path}/{file_name}_calibrate_phase.txt", "w") as fh:
                fh.write(f"freq_hz={freq_hz}\n")
                fh.write(f"drive_power_dbm={drive_power_dbm}\n")
                fh.write(f"tau_mw_us={tau_mw_us}\n")
                fh.write(f"n_reps={n_reps}\n")
                fh.write(f"phase_deg={phase_deg}\n")
                fh.write(f"x_after_null={x}\n")
                fh.write(f"y_after_null={y}\n")
        finally:
            print("[pulsed_odmr] shutting down")
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
            try:
                lia.go_to_local()
            except Exception:
                pass
            lia.close()
            try:
                awg.write("OUTPUT1 OFF")
                awg.write("OUTPUT2 OFF")
            except Exception:
                pass
            try:
                awg.close()
            except Exception:
                pass
            try:
                sdg.write("C1:OUTP OFF")
                sdg.close()
            except Exception:
                pass

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
    elif command == "run-repeat":
        cmd_run_repeat(file_name, **extra)
    elif command == "calibrate-phase":
        cmd_calibrate_phase(file_name, **extra)
    else:
        raise SystemExit(f"unknown command {command!r} (expected 'run', "
                          f"'run-repeat', or 'calibrate-phase')")


if __name__ == "__main__":
    main()
