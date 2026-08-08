"""
rabi.py -- pulsed Rabi oscillation measurement: sweeps the microwave pulse
length (tau_mw) at a FIXED, known ODMR resonance frequency, reading out via
lock-in synchronous detection against a slow block-chopped reference
(n_reps loop iterations with the MW pulse present, then n_reps without),
instead of cw_odmr_lock_in.py's continuous fast chop.

AWG sequence upload (setup_awg_sequences() below) mirrors what was verified
on real hardware in tests/rabi_awg_marker_test.ipynb -- see notes.md's
"Rabi oscillation" section for the full debugging history. Key points:

- The lock-in reference marker lives on CH2's own DATA:SEQ sequence
  (highAtStart/lowAtStart on its mw-on/mw-off blocks), not CH1's -- there
  is only one physical Sync/Marker BNC, shared between channels and
  switched to CH2 via OUTPut:SYNC:SOURce, so the reference is correct
  regardless of any residual CH1/CH2 timing offset.
- CH1/CH2 relative timing (does the laser pulse land in the intended place
  relative to the MW pulse) is kept aligned using the Keysight manual's
  documented "start a sequence on a trigger" technique (p.181): a brief
  DC "anchor" segment marked "onceWaitTrig" at the front of each channel's
  sequence. Both channels hold at their anchor until a shared external
  trigger edge advances them together. This REQUIRES a second instrument
  (the Siglent SDG1062X, see SDG_RESOURCE) supplying a CONTINUOUS,
  sufficiently fast square wave into the AWG's rear-panel Ext Trig BNC --
  every full sequence loop wraps back to the anchor and needs a fresh
  trigger, so a one-time edge is not enough; the SDG1062X's frequency is
  reconfigured every sweep point since it must stay at or above the
  current point's own block-cycle rate (see _configure_external_trigger()).
  `PHASe:SYNChronize` and plain `TRIG:SOUR`/burst-mode approaches were all
  tried first and confirmed broken/inapplicable on this firmware -- see
  notes.md before reintroducing any of them.

See notes.md's "cw_odmr_lock_in.py operation notes" section for the
lock-in/PSU conventions reused here.

Interlock note: reflected power is checked with the spectrum analyzer in
MAX HOLD mode (HP8673H.read_max_hold_reflected_power_dbm()), not a single
sweep. A single sweep (read_reflected_power_dbm(), used everywhere else in
this codebase) has no way to reliably land inside a microsecond-scale MW
pulse -- GPIB round-trip time plus the analyzer's own sweep time both run
far slower than that. MAX HOLD instead free-runs for several full
reference periods, so it's guaranteed to catch at least one real pulse at
its peak somewhere in that window -- trading timing precision for a
hardware guarantee. Each check takes multiple reference periods to
complete, so it only runs periodically (interlock_check_interval), not on
every sweep point.

Usage:
    python rabi.py run <file_name> [key=value ...]
        Sweeps the MW pulse length across [mw_start_us, mw_stop_us],
        recording the SR830's X/Y at each point. See cmd_run()'s docstring
        for the full list of key=value overrides. Saves
        data/<file_name>/<file_name>_rabi_mw_us.npy, _rabi_x.npy,
        _rabi_y.npy, _rabi_reflected_dbm.npy, _rabi_metadata.txt.

    python rabi.py calibrate-phase <file_name> [key=value ...]
        Runs continuously at a single, fixed tau_mw and calls the SR830's
        auto-phase (APHS) to null Y against the real reference -- phase_deg
        is NOT auto-calibrated by cmd_run() itself, so run this first and
        pass the result into cmd_run() via phase_deg=<value>. See
        cmd_calibrate_phase()'s docstring for key=value overrides.

Example:
    python rabi.py run rabi1 freq_hz=2.8692e9 mw_stop_us=4.0
    python rabi.py calibrate-phase phasecal1 mw_us=1.2
"""
import sys
import time

import numpy as np

import ks33600a
from hp8673h import HP8673H
from sr830 import SR830
from spd1305x import SPD1305X, voltage_for_current
from spd1168x import SPD1168X
from sdg1062x import SDG1062X
from cw_odmr import parse_kv_args, _tee_stdout
from cw_odmr_lock_in import set_switch_static

AWG_RESOURCE = "USB0::0x0957::0x5707::MY53800810::INSTR"
GEN_RESOURCE = "GPIB1::19::INSTR"
SA_RESOURCE = "GPIB0::18::INSTR"
SR830_RESOURCE = "GPIB2::2::INSTR"
# Siglent SDG1062X, driven by sdg1062x.py -- supplies the continuous
# external trigger the AWG's "onceWaitTrig" anchor segments need to keep
# CH1/CH2 aligned (see module docstring / notes.md). NOT used via its own
# run() method (that configures it to be externally triggered itself, the
# opposite of what's needed here) -- configured directly as a plain
# continuous square-wave source by _configure_external_trigger() below.
SDG_RESOURCE = "USB0::0xF4EC::0x1103::SDG1XDDX6R5043::INSTR"
# NOT YET VERIFIED -- confirm with pyvisa.ResourceManager().list_resources()
# before trusting this (see notes.md's GPIB-bus-numbering gotchas).
# SPD1305X driving the static-field coil (see spd1305x.py) -- chosen for
# this role over the SPD1168X for its higher current limit.
COIL_PSU_RESOURCE = "USB0::0xF4EC::0x1410::SPD13DCD7R1877::INSTR"
# SPD1168X driving the RF amplifier supply.
AMP_PSU_RESOURCE = "USB0::0xF4EC::0x1410::SPD13DCQ7R0986::INSTR"

DATA_DIR = "D:\\rabi"

RESEQUENCE_INTERVAL = 20  # reset the AWG this often to clear out accumulated
                           # DATA:SEQ sequences before hitting its "too many
                           # sequences defined" limit -- same lesson as
                           # t1_test.py's RESEQUENCE_INTERVAL


def build_block_descriptor(sequence_name, segments):
    """Same DATA:SEQ block-descriptor builder as t1_test.py/pulsed_odmr.py."""
    parts = [f'"{sequence_name}"']
    for arb_name, repeat_count, play_control, marker_mode, marker_point in segments:
        parts.append(
            f'"{arb_name}",{repeat_count},{play_control},{marker_mode},{marker_point}'
        )
    payload = ",".join(parts)
    payload_bytes = payload.encode("utf-8")
    payload_len = len(payload_bytes)
    n = len(str(payload_len))
    return f"#{n}{payload_len}{payload}"


def _us_to_samples(duration_us, sample_rate_hz):
    return max(1, round(duration_us * 1e-6 * sample_rate_hz))


def _rf_pulse(freq_hz, n_samples, sample_rate_hz):
    t = np.arange(n_samples) / sample_rate_hz
    return np.sin(2 * np.pi * freq_hz * t).astype(np.float32)


def _const(n_samples, value):
    return np.full(n_samples, value, dtype=np.float32)


ANCHOR_SAMPLES = 32  # minimum DATA:SEQ segment length on the 33600 Series


def setup_awg_sequences(awg, mw_us, n_reps, laser_us, pre_us, post_us,
                         sequence_name_ch1, sequence_name_ch2,
                         ch1_vpp=0.632, ch2_vpp=5.0, ch2_offset_v=2.5,
                         sample_rate_hz=1e9):
    """
    Uploads CH1's laser-pulse-train arb and CH2's mw-on/mw-off gate arbs
    for one rep at the current mw_us, then builds and uploads a DATA:SEQ
    sequence per channel, and configures both channels' output. Verified
    on real hardware in tests/rabi_awg_marker_test.ipynb -- see notes.md's
    "Rabi oscillation" section for the debugging history behind every
    choice below.

    CH1: "rep" (bright laser pulse of laser_us, then a flat dark gap of
    pre_us + mw_us + post_us -- CH1 doesn't care about the MW pulse's
    internal position, just the total gap length).

    CH2: "gate_on_rep" (low for laser_us+pre_us, high for mw_us, low for
    post_us) and "gate_off_rep" (low for the entire rep), each with the
    SAME sample count as CH1's "rep" (checked with an assert) so the two
    channels' blocks don't drift out of step.

    Both channels' sequences start with a brief "anchor" segment
    (ANCHOR_SAMPLES, "onceWaitTrig") before the real content -- the
    manual's documented "start a sequence on a trigger" technique
    (p.181). Each channel's anchor sits at THAT channel's own real
    "off"/rest level (0.0 normalized for CH1's 0-centered VOLTage
    convention, -1.0 normalized for CH2's unipolar 0-ch2_vpp mapping) --
    reusing one generic anchor value across both channels produces a
    brief wrong-level glitch on whichever channel doesn't use a 0-centered
    convention (confirmed on real hardware for CH2).

    The lock-in reference marker lives on CH2's sequence (highAtStart on
    the real mw-on block, lowAtStart on mw-off) -- OUTPut:SYNC:SOURce is
    set to CH2 here so the shared Sync/Marker BNC reflects it, independent
    of CH1/CH2 relative timing.

    Clears both channels' volatile arb memory first: DATA:ARBitrary errors
    if an arb name already exists, and upload_waveform() doesn't check for
    that error (see notes.md), so reusing the same arb names across sweep
    points without clearing would silently leave stale data in place.

    sequence_name_ch1/sequence_name_ch2 must be unique per distinct mw_us
    used since the AWG's sequence table was last cleared (see
    RESEQUENCE_INTERVAL) -- re-uploading a DATA:SEQ under an already-used
    name does not actually replace it (see notes.md / t1_test.py's
    upload_sequence() docstring).

    Both channels are set to TRIG:SOUR EXT -- this requires a continuous
    external trigger on the AWG's rear-panel Ext Trig BNC to ever actually
    play (see _configure_external_trigger()/cmd_run()'s SDG1062X setup);
    without it, both channels sit forever at their anchor outputting
    nothing.
    """
    awg.write("SOUR1:DATA:VOL:CLE")
    awg.write("SOUR2:DATA:VOL:CLE")

    laser_samples = _us_to_samples(laser_us, sample_rate_hz)
    pre_samples = _us_to_samples(pre_us, sample_rate_hz)
    mw_samples = _us_to_samples(mw_us, sample_rate_hz)
    post_samples = _us_to_samples(post_us, sample_rate_hz)

    ch1_rep = np.concatenate([
        _rf_pulse(80e6, laser_samples, sample_rate_hz),
        _const(pre_samples + mw_samples + post_samples, 0.0),
    ])
    gate_on_rep = np.concatenate([
        _const(laser_samples + pre_samples, -1.0),
        _const(mw_samples, 1.0),
        _const(post_samples, -1.0),
    ])
    gate_off_rep = _const(laser_samples + pre_samples + mw_samples + post_samples, -1.0)

    assert len(ch1_rep) == len(gate_on_rep) == len(gate_off_rep), (
        "CH1 and CH2 rep arbs must have identical sample counts, or the "
        "two channels' blocks will drift out of step"
    )

    anchor_ch1 = _const(ANCHOR_SAMPLES, 0.0)
    anchor_ch2 = _const(ANCHOR_SAMPLES, -1.0)

    awg.upload_waveform(ch1_rep, arb_name="rep", ch=1, sample_rate=sample_rate_hz)
    awg.upload_waveform(gate_on_rep, arb_name="gate_on_rep", ch=2, sample_rate=sample_rate_hz)
    awg.upload_waveform(gate_off_rep, arb_name="gate_off_rep", ch=2, sample_rate=sample_rate_hz)
    awg.upload_waveform(anchor_ch1, arb_name="anchor", ch=1, sample_rate=sample_rate_hz)
    awg.upload_waveform(anchor_ch2, arb_name="anchor", ch=2, sample_rate=sample_rate_hz)

    block1 = build_block_descriptor(sequence_name_ch1, [
        ["anchor", "1", "onceWaitTrig", "maintain", 10],
        ["rep", str(n_reps), "repeat", "maintain", 10],
        ["rep", str(n_reps), "repeat", "maintain", 10],
    ])
    awg.write(f"DATA:SEQ {block1}")  # unprefixed -> channel 1

    block2 = build_block_descriptor(sequence_name_ch2, [
        ["anchor", "1", "onceWaitTrig", "lowAtStart", 10],
        ["gate_off_rep", str(n_reps), "repeat", "lowAtStart", 10],
        ["gate_on_rep", str(n_reps), "repeat", "highAtStart", 10],
    ])
    awg.write(f"SOUR2:DATA:SEQ {block2}")

    # CH1: laser/AOM drive.
    awg.write("OUTP1:LOAD 50")
    awg.write(f"SOUR1:FUNC:ARB:SRAT {sample_rate_hz}")
    awg.write(f'SOUR1:FUNC:ARB "{sequence_name_ch1}"')
    awg.write("SOUR1:FUNC ARB")
    awg.write(f"SOUR1:VOLT {ch1_vpp}")  # NOT FUNC:ARB:PTPeak -- confirmed on
                                          # real hardware that it doesn't
                                          # actually update the channel's
                                          # real amplitude register here
    awg.write("TRIG1:SOUR EXT")
    awg.write("TRIG1:SLOP POS")
    awg.write("TRIG1:LEV 1.5")
    awg.write("OUTPUT1 ON")

    # CH2: MW gate -> ZYSWA switch control.
    awg.write("OUTP2:LOAD INF")
    awg.write(f"SOUR2:FUNC:ARB:SRAT {sample_rate_hz}")
    awg.write(f'SOUR2:FUNC:ARB "{sequence_name_ch2}"')
    awg.write("SOUR2:FUNC ARB")
    awg.write(f"SOUR2:VOLT {ch2_vpp}")
    awg.write(f"SOUR2:VOLT:OFFS {ch2_offset_v}")
    awg.write("TRIG2:SOUR EXT")
    awg.write("TRIG2:SLOP POS")
    awg.write("TRIG2:LEV 1.5")
    awg.write("OUTPUT2 ON")

    awg.write("OUTPut:SYNC:SOURce CH2")


def _configure_external_trigger(sdg, ref_period_s, margin=100):
    """
    Configures the SDG1062X (sdg1062x.py) as a continuous square wave fast
    enough to keep the AWG's "onceWaitTrig" anchor segments (see
    setup_awg_sequences()) retriggered every loop -- confirmed on real
    hardware that a trigger slower than the sequence's own full cycle
    period (ref_period_s = 2 * n_reps * rep_us) leaves both channels
    frozen at their anchor, holding low, for most of the gap between
    edges instead of running continuously (see notes.md).

    margin is a safety multiplier over the bare minimum rate
    (1 / ref_period_s): this trigger free-runs asynchronously to the
    sequence's own wrap-around moment, so each lap picks up a random extra
    "dead time" of up to one trigger period waiting for the next edge,
    which always lengthens the block's "off"/low portion. A higher margin
    shrinks that residual duty-cycle skew (confirmed on real hardware:
    50x margin left a visible few-percent skew, 100x+ measured 49.18%
    instead of an exact 50%) at the cost of a faster (but still easily
    achievable) SDG1062X frequency.
    """
    freq_hz = margin / ref_period_s
    sdg.write("C1:BSWV WVTP,SQUARE")
    sdg.write(f"C1:BSWV FRQ,{freq_hz}")
    sdg.write("C1:BSWV AMP,5")     # 5 Vpp
    sdg.write("C1:BSWV OFST,2.5")  # 0-5V swing, comfortably above the
                                     # 1.5V TRIG:LEV threshold set in
                                     # setup_awg_sequences()
    sdg.write("C1:OUTP ON")
    return freq_hz


def setup_lock_in(lia, time_constant_s, sensitivity_v, phase_deg, input_coupling,
                   auto_sensitivity=False):
    """Same convention as cw_odmr_lock_in.py's setup_lock_in(), minus the
    chop_freq_hz reference-frequency argument -- there's no fixed chop
    frequency here, the reference comes from CH1's block-level marker at
    whatever period the current mw_us/n_reps happen to give."""
    lia.set_reference_external(slope="ttl_rising")
    lia.set_phase_deg(phase_deg)
    lia.set_input_config("a")  # voltage input -- PMT signal already amplified
    lia.set_input_coupling(ac=(input_coupling == "ac"))
    lia.set_time_constant_s(time_constant_s)
    if not auto_sensitivity:
        lia.set_sensitivity_v(sensitivity_v)
    lia.set_filter_slope_db_oct(24)


def _step_sensitivity_coarser(lia):
    """Same as cw_odmr_lock_in.py's _step_sensitivity_coarser() -- step to
    the next LESS sensitive range, one step at a time, to back out of a
    real-time overload without overshooting via auto_gain()."""
    current_v = lia.get_sensitivity_v()
    idx = SR830.SENSITIVITY_V.index(current_v)
    new_idx = min(idx + 1, len(SR830.SENSITIVITY_V) - 1)
    new_v = SR830.SENSITIVITY_V[new_idx]
    if new_idx != idx:
        lia.set_sensitivity_v(new_v)
    return new_v


def cmd_run(file_name, **kw):
    """
    Sweep the MW pulse length (tau_mw) at a fixed frequency, recording the
    SR830's X/Y at each point against the AWG's block-chopped reference.

    Recognized key=value overrides (all optional): freq_hz, drive_power_dbm,
    threshold_dbm, mw_start_us, mw_stop_us, mw_step_us, n_reps, laser_us,
    pre_us, post_us, time_constant_s, settle_periods, settle_time_constants,
    sensitivity_v, auto_sensitivity, phase_deg, input_coupling,
    auto_rescale_on_overload, max_rescale_attempts, psu_voltage_v,
    psu_current_limit_a, coil_current_a, coil_voltage_margin,
    interlock_check_interval, interlock_hold_periods, ch1_vpp, ch2_vpp,
    ch2_offset_v, trigger_margin, reflected_power_scan, res_span_hz,
    coarse_step_hz, fine_span_hz, fine_step_hz, res_power_dbm, res_cal_dir --
    see the parameter-parsing block below for defaults and notes.md for the
    reasoning behind non-obvious ones.

    If reflected_power_scan=true (default), runs a coarse-then-fine
    reflected-power sweep (HP8673H.resonance_sweep()) centered on freq_hz
    (+/- res_span_hz/2) BEFORE the tau_mw sweep starts, purely as a saved
    diagnostic -- unlike cw_odmr_lock_in.py's use_resonance_sweep, this does
    NOT change freq_hz; it's just a pre-flight check/record of the
    reflected-power profile around the fixed operating frequency, run at a
    low, safe res_power_dbm. Saves {file_name}_resonance_coarse.csv and
    _resonance_fine.csv in the run's data directory.
    """
    freq_hz = float(kw.get("freq_hz", 2.843e9))
    drive_power_dbm = float(kw.get("drive_power_dbm", 0.0))
    threshold_dbm = float(kw.get("threshold_dbm", -10.0))
    mw_start_us = float(kw.get("mw_start_us", 0.02))
    mw_stop_us = float(kw.get("mw_stop_us", 5.0))
    mw_step_us = float(kw.get("mw_step_us", 0.02))
    n_reps = int(kw.get("n_reps", 250))
    laser_us = float(kw.get("laser_us", 2.0))
    pre_us = float(kw.get("pre_us", 1.0))
    post_us = float(kw.get("post_us", 1.0))
    time_constant_s = float(kw.get("time_constant_s", 0.1))
    settle_periods = float(kw.get("settle_periods", 5.0))
    # Raised from 5.0: setup_lock_in() uses a 24 dB/oct (4-pole) filter,
    # and a cascaded n-pole RC filter's step response only reaches
    # ~73.5% settled after 5 time constants (vs. ~99.3% for a single
    # pole) -- 9 TC gets a 4-pole filter to ~97.9% settled instead.
    settle_time_constants = float(kw.get("settle_time_constants", 9.0))
    sensitivity_v = float(kw.get("sensitivity_v", 5e-3))
    auto_sensitivity = str(kw.get("auto_sensitivity", "true")).lower() == "true"
    phase_deg = float(kw.get("phase_deg", 0.0))
    input_coupling = str(kw.get("input_coupling", "ac"))
    auto_rescale_on_overload = str(kw.get("auto_rescale_on_overload", "true")).lower() == "true"
    max_rescale_attempts = int(kw.get("max_rescale_attempts", 3))
    psu_voltage_v = float(kw.get("psu_voltage_v", 12.0))
    psu_current_limit_a = float(kw.get("psu_current_limit_a", 1.9))
    coil_current_a = float(kw.get("coil_current_a", 2.0))
    coil_voltage_margin = float(kw.get("coil_voltage_margin", 1.5))
    interlock_check_interval = int(kw.get("interlock_check_interval", 5))
    interlock_hold_periods = float(kw.get("interlock_hold_periods", 3.0))
    ch1_vpp = float(kw.get("ch1_vpp", 0.632))
    ch2_vpp = float(kw.get("ch2_vpp", 5.0))
    ch2_offset_v = float(kw.get("ch2_offset_v", 2.5))
    trigger_margin = float(kw.get("trigger_margin", 100))
    reflected_power_scan = str(kw.get("reflected_power_scan", "true")).lower() == "true"
    res_span_hz = float(kw.get("res_span_hz", 100e6))
    coarse_step_hz = float(kw.get("coarse_step_hz", 2e6))
    fine_span_hz = float(kw.get("fine_span_hz", 20e6))
    fine_step_hz = float(kw.get("fine_step_hz", 50e3))
    res_power_dbm = float(kw.get("res_power_dbm", -40.0))
    res_cal_dir = kw.get("res_cal_dir", None)

    mw_values_us = np.arange(mw_start_us, mw_stop_us + mw_step_us / 2, mw_step_us)

    run_path = f"{DATA_DIR}/{file_name}"
    import os
    os.makedirs(run_path, exist_ok=True)
    log_dir = f"{DATA_DIR}/logs"
    os.makedirs(log_dir, exist_ok=True)
    log_path = f"{log_dir}/{file_name}_rabi.txt"

    with _tee_stdout(log_path):
        print(f"[rabi] tau_mw sweep: {mw_start_us}-{mw_stop_us} us, "
              f"step {mw_step_us} us ({len(mw_values_us)} points), "
              f"n_reps={n_reps}, fixed frequency {freq_hz/1e9:.5f} GHz, "
              f"{drive_power_dbm} dBm")

        print("[rabi] step 1/3: configuring AWG + SDG1062X (external "
              "trigger) + SR830 lock-in")
        # debug=False here just quiets the per-command "cmd => response"
        # print spam -- KS33600A.write() checks SYST:ERR? and raises
        # unconditionally regardless of this flag, so turning it off
        # doesn't reduce error detection at all. NOT the same for SR830
        # below: its write() only queries ERRS? when debug=True, so THAT
        # one is left on -- turning it off would silently disable the
        # lock-in's own error checking, not just its printouts.
        awg = ks33600a.KS33600A(AWG_RESOURCE, debug=False)
        sdg = SDG1062X(SDG_RESOURCE, debug=False)
        lia = SR830(SR830_RESOURCE, debug=True)
        setup_lock_in(lia, time_constant_s, sensitivity_v, phase_deg, input_coupling,
                      auto_sensitivity=auto_sensitivity)
        print(f"[rabi] step 1/3 done: time constant {time_constant_s*1e3:.1f} ms, "
              f"phase {phase_deg} deg (NOT auto-calibrated -- see module docstring)")

        print("[rabi] step 2/3: connecting to HP8673H + E4403B (interlock) "
              "+ SPD1168X (amplifier supply) + SPD1305X (coil supply)")
        gen = HP8673H(GEN_RESOURCE)
        amp_psu = SPD1168X(AMP_PSU_RESOURCE)
        coil_psu = SPD1305X(COIL_PSU_RESOURCE)
        ilock_sa = None
        try:
            amp_psu.turn_on(psu_voltage_v, psu_current_limit_a)
            coil_voltage_v = voltage_for_current(coil_current_a) * coil_voltage_margin
            coil_psu.turn_on(coil_voltage_v, coil_current_a)

            ilock_sa = HP8673H.try_connect_analyzer(SA_RESOURCE)
            if ilock_sa is None:
                gen.trip_interlock("spectrum analyzer not reachable at startup")
                return

            if reflected_power_scan:
                # Hold CH2 static on the sample path (RF2), not chopping --
                # setup_awg_sequences() hasn't run yet at this point (it's
                # inside the sweep loop below), so without this CH2 is in
                # whatever leftover/default state the AWG connection left
                # it in, not deliberately routed anywhere. Same fix as
                # cw_odmr_lock_in.py's resonance sweep needed (see
                # notes.md's ZYSWA section) -- without it, the analyzer
                # would see reflected power from an uncontrolled/wrong
                # switch state instead of the real sample path.
                set_switch_static(awg, route_to_sample=True)
                print(f"[rabi] pre-flight: reflected-power sweep around "
                      f"{freq_hz/1e9:.5f} GHz (+/- {res_span_hz/2/1e6:.1f} MHz), "
                      f"{res_power_dbm} dBm")
                gen.resonance_sweep(
                    ilock_sa, freq_hz - res_span_hz / 2, freq_hz + res_span_hz / 2,
                    coarse_step_hz, fine_span_hz, fine_step_hz, res_power_dbm,
                    output_prefix=f"{run_path}/{file_name}_resonance",
                    cal_dir=res_cal_dir,
                )
                print(f"[rabi] pre-flight done: saved "
                      f"{run_path}/{file_name}_resonance_coarse.csv, "
                      f"_resonance_fine.csv")

            gen.preset()
            gen.set_power_dbm(drive_power_dbm)
            gen.set_frequency_hz(freq_hz)
            gen.rf_on()
            time.sleep(1.0)  # let the initial frequency/level settle

            print(f"[rabi] step 3/3: sweeping tau_mw, threshold {threshold_dbm} dBm")

            x_values = np.full(len(mw_values_us), np.nan)
            y_values = np.full(len(mw_values_us), np.nan)
            reflected_dbm_arr = np.full(len(mw_values_us), np.nan)
            n_completed = 0
            tripped = False

            try:
                for i, mw_us in enumerate(mw_values_us):
                    if i > 0 and i % RESEQUENCE_INTERVAL == 0:
                        print(f"[rabi] point {i + 1}/{len(mw_values_us)}: resetting AWG "
                              f"to clear its sequence table (every "
                              f"{RESEQUENCE_INTERVAL} points)")
                        awg.reset()
                        awg.write("SOUR1:DATA:VOL:CLE")
                        awg.write("SOUR2:DATA:VOL:CLE")

                    rep_us = laser_us + pre_us + mw_us + post_us
                    ref_period_s = 2 * n_reps * rep_us * 1e-6
                    # The reference period can be much shorter than the
                    # lock-in's own RC filter time constant (e.g. at small
                    # n_reps or short tau_mw) -- settle_periods reference
                    # periods alone isn't enough in that case, since the
                    # filter itself hasn't settled yet. Wait whichever is
                    # longer.
                    settle_s = max(settle_periods * ref_period_s,
                                    settle_time_constants * time_constant_s)

                    # Must be reconfigured every point: the external
                    # trigger frequency has to stay at/above this point's
                    # own block-cycle rate, which changes with mw_us (see
                    # _configure_external_trigger()).
                    _configure_external_trigger(sdg, ref_period_s, margin=trigger_margin)

                    setup_awg_sequences(
                        awg, mw_us, n_reps, laser_us, pre_us, post_us,
                        sequence_name_ch1=f"rabi_ch1_{i}",
                        sequence_name_ch2=f"rabi_ch2_{i}",
                        ch1_vpp=ch1_vpp, ch2_vpp=ch2_vpp, ch2_offset_v=ch2_offset_v,
                    )

                    if i == 0 and auto_sensitivity:
                        # A real signal is present now that the AWG is
                        # running the first point -- let AGAN pick a range,
                        # then wait the same settle margin used between
                        # points before trusting/logging it.
                        time.sleep(settle_s)
                        lia.auto_gain()
                        time.sleep(settle_s)
                        actual_sensitivity_v = lia.get_sensitivity_v()
                        print(f"[rabi] auto_sensitivity: AGAN selected "
                              f"{actual_sensitivity_v:.3e} V full scale")
                        sensitivity_v = actual_sensitivity_v

                    if i % interlock_check_interval == 0:
                        hold_s = interlock_hold_periods * ref_period_s
                        power_dbm = HP8673H.read_max_hold_reflected_power_dbm(
                            ilock_sa, freq_hz, hold_s)
                        reflected_dbm_arr[i] = power_dbm if power_dbm is not None else np.nan

                        if power_dbm is not None:
                            print(f"[rabi] interlock check (point {i + 1}/"
                                  f"{len(mw_values_us)}, tau_mw={mw_us:.3f} us): "
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
                            gen.trip_interlock(f"{reason} at tau_mw={mw_us:.3f} us "
                                                f"(point {i + 1}/{len(mw_values_us)})")
                            tripped = True
                            break

                    time.sleep(settle_s)

                    x, y = lia.read_xy()

                    if auto_rescale_on_overload:
                        for attempt in range(max_rescale_attempts):
                            overload_status = lia.read_overload_status()
                            if not overload_status["any"]:
                                break
                            old_v = lia.get_sensitivity_v()
                            new_v = _step_sensitivity_coarser(lia)
                            print(f"[rabi] OVERLOAD at tau_mw={mw_us:.3f} us "
                                  f"(point {i + 1}/{len(mw_values_us)}): rescaling "
                                  f"sensitivity {old_v:.3e} V -> {new_v:.3e} V full "
                                  f"scale, re-reading (attempt {attempt + 1}/"
                                  f"{max_rescale_attempts})")
                            sensitivity_v = new_v
                            time.sleep(settle_s)
                            x, y = lia.read_xy()
                        else:
                            print(f"[rabi] tau_mw={mw_us:.3f} us: still overloading "
                                  f"after {max_rescale_attempts} rescale attempts -- "
                                  f"saving as-is")

                    x_values[i] = x
                    y_values[i] = y
                    n_completed = i + 1
                    print(f"[rabi] point {n_completed}/{len(mw_values_us)}: "
                          f"tau_mw={mw_us:.3f} us, X={x:.6e} V, Y={y:.6e} V")
            except KeyboardInterrupt:
                print("[rabi] stopped by user (Ctrl+C)")

            mw_values_us_trimmed = mw_values_us[:n_completed]
            x_values = x_values[:n_completed]
            y_values = y_values[:n_completed]
            reflected_dbm_arr = reflected_dbm_arr[:n_completed]

            if n_completed == 0:
                print("[rabi] step 3/3 FAILED: no points completed -- nothing to save")
            else:
                np.save(f"{run_path}/{file_name}_rabi_mw_us.npy", mw_values_us_trimmed)
                np.save(f"{run_path}/{file_name}_rabi_x.npy", x_values)
                np.save(f"{run_path}/{file_name}_rabi_y.npy", y_values)
                np.save(f"{run_path}/{file_name}_rabi_reflected_dbm.npy", reflected_dbm_arr)
                with open(f"{run_path}/{file_name}_rabi_metadata.txt", "w") as fh:
                    fh.write(f"freq_hz={freq_hz}\n")
                    fh.write(f"drive_power_dbm={drive_power_dbm}\n")
                    fh.write(f"n_reps={n_reps}\n")
                    fh.write(f"laser_us={laser_us}\n")
                    fh.write(f"pre_us={pre_us}\n")
                    fh.write(f"post_us={post_us}\n")
                    fh.write(f"time_constant_s={time_constant_s}\n")
                    fh.write(f"settle_periods={settle_periods}\n")
                    fh.write(f"settle_time_constants={settle_time_constants}\n")
                    fh.write(f"sensitivity_v={sensitivity_v}\n")
                    fh.write(f"phase_deg={phase_deg}\n")
                print(f"[rabi] step 3/3 done"
                      f"{' (PARTIAL -- interlock tripped)' if tripped else ''}: "
                      f"saved {run_path}/{file_name}_rabi_mw_us.npy "
                      f"({n_completed} points), _rabi_x.npy, _rabi_y.npy, "
                      f"_rabi_reflected_dbm.npy, _rabi_metadata.txt")
        finally:
            print("[rabi] shutting down")
            try:
                gen.rf_off()
            except Exception as e:
                print(f"[rabi] WARNING: failed to turn off RF cleanly ({e})")
            try:
                gen.go_to_local()
            except Exception:
                pass
            gen.close()
            try:
                amp_psu.turn_off()
            except Exception as e:
                print(f"[rabi] WARNING: failed to turn off amplifier supply "
                      f"cleanly ({e})")
            amp_psu.close()
            try:
                coil_psu.turn_off()
            except Exception as e:
                print(f"[rabi] WARNING: failed to turn off coil supply cleanly ({e})")
            try:
                coil_psu.go_to_local()
            except Exception:
                pass
            coil_psu.close()
            if ilock_sa is not None:
                ilock_sa.close()
            try:
                lia.go_to_local()
            except Exception:
                pass
            lia.close()
            try:
                awg.close()
            except Exception:
                pass
            try:
                sdg.write("C1:OUTP OFF")
            except Exception as e:
                print(f"[rabi] WARNING: failed to turn off SDG1062X output "
                      f"cleanly ({e})")
            try:
                sdg.close()
            except Exception:
                pass

    print("[rabi] done")


def cmd_calibrate_phase(file_name, **kw):
    """
    Runs the AWG/lock-in continuously at a single, fixed tau_mw and calls
    the SR830's built-in auto-phase (APHS) to null Y against the real
    CH2-sourced block reference -- this is the gap flagged in cmd_run()'s
    docstring (phase_deg is NOT auto-calibrated by cmd_run() itself). Per
    notes.md's phase-calibration guidance, auto_phase() needs a real,
    reasonably strong signal to get a stable reading -- pick mw_us to
    maximize contrast (e.g. wherever a previous cmd_run()'s Rabi data
    showed the biggest X/Y swing), not an arbitrary weak point.

    Prints and saves the resulting phase_deg to
    {file_name}_calibrate_phase.txt -- pass it into a real cmd_run() via
    phase_deg=<value>.

    Recognized key=value overrides (all optional): freq_hz, drive_power_dbm,
    mw_us, n_reps, laser_us, pre_us, post_us, time_constant_s,
    settle_time_constants, input_coupling, ch1_vpp, ch2_vpp, ch2_offset_v,
    trigger_margin, psu_voltage_v, psu_current_limit_a, coil_current_a,
    coil_voltage_margin -- same meaning/defaults as cmd_run()'s.
    """
    freq_hz = float(kw.get("freq_hz", 2.843e9))
    drive_power_dbm = float(kw.get("drive_power_dbm", 0.0))
    mw_us = float(kw.get("mw_us", 1.0))
    n_reps = int(kw.get("n_reps", 250))
    laser_us = float(kw.get("laser_us", 2.0))
    pre_us = float(kw.get("pre_us", 1.0))
    post_us = float(kw.get("post_us", 1.0))
    time_constant_s = float(kw.get("time_constant_s", 0.1))
    # See cmd_run()'s settle_time_constants comment -- 9 TC needed for the
    # 24 dB/oct filter to actually settle (~98%), not 5.
    settle_time_constants = float(kw.get("settle_time_constants", 9.0))
    input_coupling = str(kw.get("input_coupling", "ac"))
    ch1_vpp = float(kw.get("ch1_vpp", 0.632))
    ch2_vpp = float(kw.get("ch2_vpp", 5.0))
    ch2_offset_v = float(kw.get("ch2_offset_v", 2.5))
    trigger_margin = float(kw.get("trigger_margin", 100))
    psu_voltage_v = float(kw.get("psu_voltage_v", 12.0))
    psu_current_limit_a = float(kw.get("psu_current_limit_a", 1.9))
    coil_current_a = float(kw.get("coil_current_a", 2.0))
    coil_voltage_margin = float(kw.get("coil_voltage_margin", 1.5))

    run_path = f"{DATA_DIR}/{file_name}"
    import os
    os.makedirs(run_path, exist_ok=True)
    log_dir = f"{DATA_DIR}/logs"
    os.makedirs(log_dir, exist_ok=True)
    log_path = f"{log_dir}/{file_name}_calibrate_phase.txt"

    with _tee_stdout(log_path):
        print(f"[rabi] phase calibration: tau_mw={mw_us} us, n_reps={n_reps}, "
              f"fixed frequency {freq_hz/1e9:.5f} GHz, {drive_power_dbm} dBm")

        awg = ks33600a.KS33600A(AWG_RESOURCE, debug=True)
        sdg = SDG1062X(SDG_RESOURCE, debug=True)
        lia = SR830(SR830_RESOURCE, debug=True)
        # phase_deg=0 as a starting point -- auto_phase() below finds the
        # real value; auto_sensitivity=True so the initial sensitivity_v
        # doesn't matter.
        setup_lock_in(lia, time_constant_s, sensitivity_v=5e-3, phase_deg=0.0,
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

            rep_us = laser_us + pre_us + mw_us + post_us
            ref_period_s = 2 * n_reps * rep_us * 1e-6
            settle_s = max(5 * ref_period_s, settle_time_constants * time_constant_s)

            _configure_external_trigger(sdg, ref_period_s, margin=trigger_margin)
            setup_awg_sequences(
                awg, mw_us, n_reps, laser_us, pre_us, post_us,
                sequence_name_ch1="calib_ch1", sequence_name_ch2="calib_ch2",
                ch1_vpp=ch1_vpp, ch2_vpp=ch2_vpp, ch2_offset_v=ch2_offset_v,
            )

            print(f"[rabi] settling {settle_s * 1e3:.0f} ms, then auto-gain + auto-phase")
            time.sleep(settle_s)
            lia.auto_gain()
            time.sleep(settle_s)

            lia.auto_phase()
            time.sleep(settle_s)
            phase_deg = lia.get_phase_deg()
            x, y = lia.read_xy()

            print(f"[rabi] auto-phase result: phase_deg={phase_deg:.3f} deg "
                  f"(X={x:.6e} V, Y={y:.6e} V after nulling)")
            print(f"[rabi] use this with cmd_run() via phase_deg={phase_deg:.3f}")

            with open(f"{run_path}/{file_name}_calibrate_phase.txt", "w") as fh:
                fh.write(f"freq_hz={freq_hz}\n")
                fh.write(f"drive_power_dbm={drive_power_dbm}\n")
                fh.write(f"mw_us={mw_us}\n")
                fh.write(f"n_reps={n_reps}\n")
                fh.write(f"phase_deg={phase_deg}\n")
                fh.write(f"x_after_null={x}\n")
                fh.write(f"y_after_null={y}\n")
        finally:
            print("[rabi] shutting down")
            try:
                gen.rf_off()
            except Exception as e:
                print(f"[rabi] WARNING: failed to turn off RF cleanly ({e})")
            try:
                gen.go_to_local()
            except Exception:
                pass
            gen.close()
            try:
                amp_psu.turn_off()
            except Exception as e:
                print(f"[rabi] WARNING: failed to turn off amplifier supply "
                      f"cleanly ({e})")
            amp_psu.close()
            try:
                coil_psu.turn_off()
            except Exception as e:
                print(f"[rabi] WARNING: failed to turn off coil supply cleanly ({e})")
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
            except Exception as e:
                print(f"[rabi] WARNING: failed to turn off SDG1062X output "
                      f"cleanly ({e})")
            try:
                sdg.close()
            except Exception:
                pass

    print("[rabi] phase calibration done")


def main():
    if len(sys.argv) < 3:
        print(__doc__)
        raise SystemExit(1)

    command = sys.argv[1]
    file_name = sys.argv[2]
    extra = parse_kv_args(sys.argv[3:])

    if command == "run":
        cmd_run(file_name, **extra)
    elif command == "calibrate-phase":
        cmd_calibrate_phase(file_name, **extra)
    else:
        raise SystemExit(f"unknown command {command!r} "
                          f"(expected 'run' or 'calibrate-phase')")


if __name__ == "__main__":
    main()
