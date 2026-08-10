"""
debug_repeat_one_point.py -- isolate the background-artifact spike (see
notes.md's "spurious off-resonance/no-MW-near-sample signal" entry) from
the actual MW sweep by repeating the SAME tau_mw indefinitely instead of
advancing through mw_values_us.

Deliberately does NOT touch the microwave generator (HP8673H), either PSU
(SPD1168X amp supply, SPD1305X coil supply), or the interlock spectrum
analyzer -- only the AWG, its SDG1062X external trigger, and the SR830
lock-in are connected. Safe to run with the MW generator/amplifier
physically powered off, since nothing here ever asks them to turn on --
CH2 still drives the ZYSWA switch exactly like a real sweep would, but
with no actual RF behind it, switching on/off drives nothing.

Two modes, dispatched by command:

    python debug_repeat_one_point.py with-reupload <file_name> [key=value ...]
        Every iteration goes through the exact same per-point AWG
        reconfigure real sweeps use -- periodic awg.reset()
        (resequence_interval), setup_awg_sequences() (ABOR, clear,
        reupload, FUNC:ARB:SYNC, re-arm) -- at a FIXED mw_us instead of a
        new one each time. If the spike still shows up here, that's
        confirmation it's caused by the repeated reconfigure/retrigger
        cycle itself (or something inside the SR830 reacting to it), not
        by anything that changes between real sweep points.

    python debug_repeat_one_point.py no-reupload <file_name> [key=value ...]
        setup_awg_sequences() runs ONCE, before the loop -- no more
        awg.reset(), ABOR, or reupload after that. The AWG just keeps
        running the SAME already-armed sequence, retriggering naturally
        off the continuously-running SDG1062X, for every iteration.
        Isolates whether the spike needs a reconfigure/retrigger event at
        all, or happens even on a sequence that's never touched again --
        the direct complement to with-reupload.

Both modes just repeat _wait_settle_discarding_transient_overload() +
read_xy() (plus the reference frequency/overload diagnostics) forever
until Ctrl+C, then save whatever was collected to D:/rabi/<file_name>/ in
the same _rabi_x.npy/_rabi_y.npy/_rabi_ref_freq_hz.npy/
_rabi_reference_unlock.npy convention as rabi.py, plus a new
_rabi_overload.npy (any of input/filter/output).

Key=value overrides (all optional, same meaning as in rabi.py's cmd_run()):
    tau_mw_us (default 1.02), n_reps (250), laser_us (2.0), pre_us (1.0),
    post_us (1.0), time_constant_s (0.1), settle_periods (5.0),
    settle_time_constants (9.0), sensitivity_v (1e-4), phase_deg (0.0),
    input_coupling ("ac"), ch1_vpp (0.632), ch2_vpp (5.0),
    ch2_offset_v (2.5), trigger_margin (3.0), anchor_free_reps (500),
    resequence_interval (uses rabi.RESEQUENCE_INTERVAL if not given --
    with-reupload only, ignored by no-reupload since it never resets),
    extra_settle_s (0.0 -- extra fixed wait added after each reupload/
    re-arm, before the normal settle_s countdown starts; with-reupload
    only, tests whether the rare large rail spikes need more margin after
    a reconfigure event, as opposed to the everyday few-uV bump already
    shown to settle out within one normal settle_s window),
    print_every (1 -- print every Nth iteration instead of all of them,
    for a long unattended run).
"""
import os
import sys
import time

import numpy as np

import ks33600a
import rabi
from sr830 import SR830
from sdg1062x import SDG1062X
from cw_odmr import parse_kv_args, _tee_stdout

DATA_DIR = rabi.DATA_DIR


def _run_repeated(file_name, reupload_each_iteration, kw):
    tau_mw_us = float(kw.get("tau_mw_us", 1.02))
    n_reps = int(kw.get("n_reps", 250))
    laser_us = float(kw.get("laser_us", 2.0))
    pre_us = float(kw.get("pre_us", 1.0))
    post_us = float(kw.get("post_us", 1.0))
    time_constant_s = float(kw.get("time_constant_s", 0.1))
    settle_periods = float(kw.get("settle_periods", 5.0))
    settle_time_constants = float(kw.get("settle_time_constants", 9.0))
    sensitivity_v = float(kw.get("sensitivity_v", 1e-4))
    phase_deg = float(kw.get("phase_deg", 0.0))
    input_coupling = str(kw.get("input_coupling", "ac"))
    ch1_vpp = float(kw.get("ch1_vpp", 0.632))
    ch2_vpp = float(kw.get("ch2_vpp", 5.0))
    ch2_offset_v = float(kw.get("ch2_offset_v", 2.5))
    trigger_margin = float(kw.get("trigger_margin", 3.0))
    anchor_free_reps = int(kw.get("anchor_free_reps", 500))
    resequence_interval = int(kw.get("resequence_interval", rabi.RESEQUENCE_INTERVAL))
    print_every = int(kw.get("print_every", 1))
    # Extra fixed wait added on top of settle_s, AFTER the reupload/re-arm
    # and BEFORE _wait_settle_discarding_transient_overload() starts its
    # own countdown -- tests whether the rare, large (~1.5e-4 V) rail
    # spikes (as opposed to the everyday few-uV bump already shown to
    # settle out within one normal settle_s window) need more margin after
    # a reconfigure event specifically, at a normal repeated-reupload
    # cadence. 0 by default (no behavior change).
    extra_settle_s = float(kw.get("extra_settle_s", 0.0))

    run_path = f"{DATA_DIR}/{file_name}"
    os.makedirs(run_path, exist_ok=True)
    log_dir = f"{DATA_DIR}/logs"
    os.makedirs(log_dir, exist_ok=True)

    with _tee_stdout(f"{log_dir}/{file_name}.log"):
        mode = "WITH reupload every iteration" if reupload_each_iteration else \
               "WITHOUT any reupload after the first (single upload, then repeat)"
        print(f"[debug] repeating tau_mw={tau_mw_us} us indefinitely, {mode} -- "
              f"NOT touching the MW generator, either PSU, or the interlock "
              f"spectrum analyzer. Safe to leave the MW generator/amplifier "
              f"off. Ctrl+C to stop and save.")

        awg = ks33600a.KS33600A(rabi.AWG_RESOURCE, debug=False)
        sdg = SDG1062X(rabi.SDG_RESOURCE, debug=False)
        lia = SR830(rabi.SR830_RESOURCE, debug=True)
        rabi.setup_lock_in(lia, time_constant_s, sensitivity_v, phase_deg,
                            input_coupling, auto_sensitivity=False)

        rep_us = laser_us + pre_us + tau_mw_us + post_us
        ref_period_s = 2 * n_reps * rep_us * 1e-6
        settle_s = max(settle_periods * ref_period_s,
                        settle_time_constants * time_constant_s)
        anchor_period_s = ref_period_s * anchor_free_reps
        fixed_trigger_freq_hz = rabi._configure_external_trigger(
            sdg, anchor_period_s, margin=trigger_margin)
        print(f"[debug] ref_period_s={ref_period_s:.6f}, settle_s={settle_s:.3f}, "
              f"extra_settle_s={extra_settle_s:.3f}, "
              f"anchor_period_s={anchor_period_s:.3f}, "
              f"trigger={fixed_trigger_freq_hz/1e3:.3f} kHz, "
              f"resequence_interval={resequence_interval if reupload_each_iteration else 'n/a'}")

        def upload(i):
            rabi.setup_awg_sequences(
                awg, tau_mw_us, n_reps, laser_us, pre_us, post_us,
                sequence_name_ch1=f"debug_ch1_{i}",
                sequence_name_ch2=f"debug_ch2_{i}",
                ch1_vpp=ch1_vpp, ch2_vpp=ch2_vpp, ch2_offset_v=ch2_offset_v,
                anchor_free_reps=anchor_free_reps,
            )

        if not reupload_each_iteration:
            upload(0)  # once, before the loop -- never touched again

        x_values, y_values = [], []
        ref_freq_hz_values = []
        reference_unlock_values, overload_values = [], []

        try:
            i = 0
            while True:
                if reupload_each_iteration:
                    if i > 0 and i % resequence_interval == 0:
                        print(f"[debug] iter {i}: resetting AWG (every "
                              f"{resequence_interval} iterations)")
                        awg.reset()
                        awg.write("SOUR1:DATA:VOL:CLE")
                        awg.write("SOUR2:DATA:VOL:CLE")
                    upload(i)
                    if extra_settle_s > 0:
                        time.sleep(extra_settle_s)

                lia.read_overload_status()  # discard anything stale, same as cmd_run()

                rabi._wait_settle_discarding_transient_overload(lia, settle_s)

                x, y = lia.read_xy()
                ref_freq_hz = lia.get_frequency_hz()
                status = lia.read_overload_status()

                x_values.append(x)
                y_values.append(y)
                ref_freq_hz_values.append(ref_freq_hz)
                reference_unlock_values.append(status["reference_unlock"])
                overload_values.append(status["any"])

                if i % print_every == 0:
                    expected_ref_freq_hz = 1.0 / ref_period_s
                    print(f"[debug] iter {i}: X={x:.6e} V, Y={y:.6e} V, "
                          f"ref_freq={ref_freq_hz:.6f} Hz "
                          f"(expected {expected_ref_freq_hz:.6f} Hz), "
                          f"reference_unlock={status['reference_unlock']}, "
                          f"input_overload={status['input']}, "
                          f"filter_overload={status['filter']}, "
                          f"output_overload={status['output']}")

                i += 1
        except KeyboardInterrupt:
            print(f"[debug] stopped by user after {i} iterations")
        finally:
            print("[debug] shutting down")
            try:
                awg.write("OUTPUT1 OFF")
                awg.write("OUTPUT2 OFF")
            except Exception as e:
                print(f"[debug] WARNING: failed to turn off AWG outputs cleanly ({e})")
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
                lia.close()
            except Exception:
                pass

            if x_values:
                np.save(f"{run_path}/{file_name}_rabi_x.npy", np.array(x_values))
                np.save(f"{run_path}/{file_name}_rabi_y.npy", np.array(y_values))
                np.save(f"{run_path}/{file_name}_rabi_ref_freq_hz.npy",
                        np.array(ref_freq_hz_values))
                np.save(f"{run_path}/{file_name}_rabi_reference_unlock.npy",
                        np.array(reference_unlock_values))
                np.save(f"{run_path}/{file_name}_rabi_overload.npy",
                        np.array(overload_values))
                print(f"[debug] saved {len(x_values)} points to {run_path}/"
                      f"{file_name}_rabi_*.npy")
            else:
                print("[debug] no points completed -- nothing to save")


def cmd_with_reupload(file_name, **kw):
    _run_repeated(file_name, reupload_each_iteration=True, kw=kw)


def cmd_no_reupload(file_name, **kw):
    _run_repeated(file_name, reupload_each_iteration=False, kw=kw)


def main():
    if len(sys.argv) < 3:
        print(__doc__)
        raise SystemExit(1)

    command = sys.argv[1]
    file_name = sys.argv[2]
    kw = parse_kv_args(sys.argv[3:])

    if command == "with-reupload":
        cmd_with_reupload(file_name, **kw)
    elif command == "no-reupload":
        cmd_no_reupload(file_name, **kw)
    else:
        raise SystemExit(f"unknown command {command!r} "
                          f"(expected 'with-reupload' or 'no-reupload')")


if __name__ == "__main__":
    main()
