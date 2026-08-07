"""
rabi.py -- pulsed Rabi oscillation measurement: sweeps the microwave pulse
length (tau_mw) at a FIXED, known ODMR resonance frequency, reading out via
lock-in synchronous detection against a slow block-chopped reference
(n_reps loop iterations with the MW pulse present, then n_reps without),
instead of cw_odmr_lock_in.py's continuous fast chop.

AWG sequence upload (setup_awg_sequences() below) is NOT YET IMPLEMENTED --
tests/rabi_awg_marker_test.ipynb is verifying on a real oscilloscope
whether DATA:SEQ's per-segment marker fires once per repeated block or
once per individual repeat before committing to this scheme (see that
notebook's docstring for the full reasoning). Everything else here --
generator, lock-in, PSUs, interlock, sweep loop, saving -- is wired up and
ready; only setup_awg_sequences() needs filling in once the marker
behavior is confirmed.

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

Example:
    python rabi.py run rabi1 freq_hz=2.8692e9 mw_stop_us=4.0
"""
import sys
import time

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


def setup_awg_sequences(awg, mw_us, n_reps, laser_us, pre_us, post_us,
                         sequence_name_ch1, sequence_name_ch2):
    """
    NOT YET IMPLEMENTED.

    Intended structure, per tests/rabi_awg_marker_test.ipynb (pending
    oscilloscope confirmation of DATA:SEQ marker behavior under "repeat"):

    CH1: one "rep" arb (bright laser pulse of laser_us, then a flat dark
    gap of pre_us + mw_us + post_us -- CH1 doesn't care about the MW
    pulse's internal position, just the total gap length). Sequence:
        [["rep", n_reps, "repeat", "highAtStart", 10],   # mw-on block
         ["rep", n_reps, "repeat", "lowAtStart", 10]]     # mw-off block
    uploaded via awg.write(f"DATA:SEQ {block1}") (unprefixed -> channel 1).

    CH2: two arbs of IDENTICAL sample count to CH1's "rep" -- "gate_on_rep"
    (low for laser_us+pre_us, high for mw_us, low for post_us) and
    "gate_off_rep" (low for the entire rep). Sequence:
        [["gate_on_rep", n_reps, "repeat", "lowAtStart", 10],
         ["gate_off_rep", n_reps, "repeat", "lowAtStart", 10]]
    uploaded via awg.write(f"SOUR2:DATA:SEQ {block2}").

    Then configure output (OUTP1:LOAD 50 / OUTP2:LOAD INF, FUNC:ARB:PTP,
    FUNC:ARB pointing at the sequence name, TRIG:SOUR IMM on both, then
    PHASe:SYNChronize once) -- see the test notebook's "configure-output"
    cell for the exact SCPI sequence already proven to work for this.

    sequence_name_ch1/sequence_name_ch2 must be unique per distinct mw_us
    used within a session -- re-uploading DATA:SEQ under an already-used
    name does not actually replace it (see notes.md / t1_test.py's
    upload_sequence() docstring).
    """
    raise NotImplementedError(
        "AWG sequence upload not yet implemented -- see "
        "tests/rabi_awg_marker_test.ipynb for the pending oscilloscope "
        "verification this depends on (does DATA:SEQ's marker_mode fire "
        "once per repeated block, or once per individual repeat?)"
    )


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

    Recognized key=value overrides (all optional):
      freq_hz=2.87e9          fixed MW frequency -- the known ODMR
                              resonance, NOT swept here
      drive_power_dbm=0.0     fixed generator CW power for the whole sweep
      threshold_dbm=-10.0     interlock trip threshold, in dBm
      mw_start_us=0.02        Rabi sweep start pulse length
      mw_stop_us=5.0          Rabi sweep stop pulse length
      mw_step_us=0.1          Rabi sweep step
      n_reps=50               reps per block (mw-on then mw-off) -- sets
                              both averaging depth and the lock-in
                              reference period; see
                              tests/rabi_awg_marker_test.ipynb
      laser_us=2.0            laser pulse duration per rep
      pre_us=1.0              padding before the MW pulse
      post_us=1.0             padding after the MW pulse before the next
                              laser pulse -- keeps RF and readout from
                              overlapping in time (same rationale as the
                              CW-ODMR RF-pickup fix, see notes.md)
      time_constant_s=0.1     SR830 time constant
      settle_periods=5        full reference periods (2*n_reps*rep_us) to
                              wait after uploading a new tau_mw before
                              reading -- the reference period isn't fixed
                              like cw_odmr_lock_in.py's chop_freq_hz, it
                              depends on n_reps and the current tau_mw
      sensitivity_v=5e-3      SR830 sensitivity target -- ignored unless
                               auto_sensitivity=false
      auto_sensitivity=true   call AGAN once the AWG is running the first
                               sweep point, before the sweep loop starts
      phase_deg=0.0           pre-calibrated SR830 phase (NOT
                              auto-calibrated by this script)
      input_coupling=ac       SR830 input coupling, 'ac' or 'dc'
      auto_rescale_on_overload=true  step sensitivity one range coarser
                              and re-read once if the SR830 reports
                              overload after a point -- see
                              max_rescale_attempts
      max_rescale_attempts=3  rescale-and-reread attempts per point before
                              giving up and saving that point as NaN
      psu_voltage_v=12.0      SPD1168X output voltage for the RF amplifier
                              supply (see spd1168x.py) -- on before the
                              sweep starts, off when it ends
      psu_current_limit_a=1.9  SPD1168X current limit
      coil_current_a=1.5      SPD1305X current setpoint for the static-
                              field coil (see spd1305x.py; chosen over the
                              SPD1168X for its higher current limit) --
                              turned on/off alongside the amplifier supply
      coil_voltage_margin=1.2  voltage headroom above the calibration's
                              expected coil voltage drop at coil_current_a,
                              so the supply regulates in constant-current
                              mode (same convention as spd1305x.py's
                              set_field())
      interlock_check_interval=5  run a reflected-power check every this
                              many sweep points -- see module docstring's
                              MAX HOLD note for why this isn't every point
      interlock_hold_periods=3  full reference periods each interlock
                              check's MAX HOLD window runs for
    """
    freq_hz = float(kw.get("freq_hz", 2.87e9))
    drive_power_dbm = float(kw.get("drive_power_dbm", 0.0))
    threshold_dbm = float(kw.get("threshold_dbm", -10.0))
    mw_start_us = float(kw.get("mw_start_us", 0.02))
    mw_stop_us = float(kw.get("mw_stop_us", 5.0))
    mw_step_us = float(kw.get("mw_step_us", 0.1))
    n_reps = int(kw.get("n_reps", 50))
    laser_us = float(kw.get("laser_us", 2.0))
    pre_us = float(kw.get("pre_us", 1.0))
    post_us = float(kw.get("post_us", 1.0))
    time_constant_s = float(kw.get("time_constant_s", 0.1))
    settle_periods = float(kw.get("settle_periods", 5.0))
    sensitivity_v = float(kw.get("sensitivity_v", 5e-3))
    auto_sensitivity = str(kw.get("auto_sensitivity", "true")).lower() == "true"
    phase_deg = float(kw.get("phase_deg", 0.0))
    input_coupling = str(kw.get("input_coupling", "ac"))
    auto_rescale_on_overload = str(kw.get("auto_rescale_on_overload", "true")).lower() == "true"
    max_rescale_attempts = int(kw.get("max_rescale_attempts", 3))
    psu_voltage_v = float(kw.get("psu_voltage_v", 12.0))
    psu_current_limit_a = float(kw.get("psu_current_limit_a", 1.9))
    coil_current_a = float(kw.get("coil_current_a", 1.5))
    coil_voltage_margin = float(kw.get("coil_voltage_margin", 1.2))
    interlock_check_interval = int(kw.get("interlock_check_interval", 5))
    interlock_hold_periods = float(kw.get("interlock_hold_periods", 3.0))

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

        print("[rabi] step 1/3: configuring AWG + SR830 lock-in")
        awg = ks33600a.KS33600A(AWG_RESOURCE, debug=True)
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

            gen.preset()
            gen.set_power_dbm(drive_power_dbm)
            gen.set_frequency_hz(freq_hz)
            gen.rf_on()
            time.sleep(1.0)  # let the initial frequency/level settle

            ilock_sa = HP8673H.try_connect_analyzer(SA_RESOURCE)
            if ilock_sa is None:
                gen.trip_interlock("spectrum analyzer not reachable at startup")
                return

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

                    setup_awg_sequences(
                        awg, mw_us, n_reps, laser_us, pre_us, post_us,
                        sequence_name_ch1=f"rabi_ch1_{i}",
                        sequence_name_ch2=f"rabi_ch2_{i}",
                    )

                    rep_us = laser_us + pre_us + mw_us + post_us
                    ref_period_s = 2 * n_reps * rep_us * 1e-6
                    settle_s = settle_periods * ref_period_s

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

    print("[rabi] done")


def main():
    if len(sys.argv) < 3:
        print(__doc__)
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
