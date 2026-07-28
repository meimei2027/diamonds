"""
cw_odmr.py -- CLI entry point for the CW-ODMR pipeline built up in
cw_odmr.ipynb. See notes.md for the calibrated values (resonance frequency,
amplifier power, interlock threshold) this script's defaults are based on.

Usage:
    python cw_odmr.py vna <file_name>
        Take one NanoVNA sweep (2-4 GHz) and save it to
        data/<file_name>_vna.npy.

    python cw_odmr.py run <file_name> [key=value ...]
        Configures the AWG first (before anything else, so it has the whole
        multi-minute resonance sweep to settle), sweeps for the resonance
        (coarse+fine, HP8673H + E4403B), starts the reflected-power
        interlock at that frequency, then acquires segments on the RTB2004
        while the interlock runs concurrently in the background. Saves
        data/<file_name>.npy, _timetable.npy, _metadata.txt (same layout as
        RTB2004.run()), plus data/<file_name>_resonance_coarse.csv / _fine.csv
        from the sweep.

        Recognized key=value overrides (all optional):
          segments=1000          number of scope segments to acquire
          res_start_hz=2e9       resonance sweep start frequency
          res_stop_hz=3e9        resonance sweep stop frequency
          coarse_step_hz=6.7e6   coarse sweep step (track the expected FWHM -- notes.md)
          fine_span_hz=20e6      fine sweep span around the coarse dip
          fine_step_hz=50e3      fine sweep step
          res_power_dbm=-40.0    drive power during the resonance sweep
          drive_power_dbm=0.0    drive power during acquisition (interlock-protected)
          threshold_dbm=-10.0    interlock trip threshold, in dBm
          poll_interval_s=1.0    interlock polling interval, in seconds
          awg_carrier_freq_hz=80e6  AWG CH1 carrier frequency (into scope Channel 1)
          awg_carrier_vpp=0.632     AWG CH1 amplitude
          awg_trigger_freq_hz=1.0  AWG CH2 trigger frequency (into scope EXT TRIG)
          awg_trigger_vpp=2.0      AWG CH2 amplitude

    python cw_odmr.py run_background <file_name> [segments=1000]
        Background/baseline measurement: no resonance sweep, no microwave at
        all -- explicitly forces RF off first, then just acquires segments
        on the RTB2004 (no interlock needed, since there's no RF to protect
        against). Still configures the AWG first, same as `run` (the scope's
        segmented acquisition needs the CH2 trigger regardless of RF state).
        Saves data/<file_name>.npy, _timetable.npy, _metadata.txt, same
        layout as `run`, for background-subtracting against a `run` dataset
        taken under the same trigger/timebase settings. Same awg_* overrides
        as `run` are recognized here too.

Example:
    python cw_odmr.py vna baseline
    python cw_odmr.py run trial1 segments=1000
    python cw_odmr.py run_background trial1_bg segments=1000
"""
import os
import sys
import time
import threading

import pyvisa

from hp8673h import HP8673H
from e4403b import E4403B
from rtb2004 import RTB2004
from nanovna import NanoVNAF3

# See notes.md -- GPIB bus numbering isn't stable, confirm with
# pyvisa.ResourceManager().list_resources() if these don't match.
GEN_RESOURCE = "GPIB1::19::INSTR"
SA_RESOURCE = "GPIB0::18::INSTR"
SCOPE_RESOURCE = "USB0::0x0AAD::0x01D6::108904::INSTR"
AWG_RESOURCE = "USB0::0x0957::0x5707::MY53800810::INSTR"

DATA_DIR = "data"


def setup_awg(carrier_freq_hz=80e6, carrier_vpp=0.632, trigger_freq_hz=1.0, trigger_vpp=2.0):
    """
    Configure the AWG's CH1 (continuous carrier -- currently standing in for
    the real laser/PL signal during dry-run testing, straight into scope
    Channel 1) and CH2 (external trigger for the scope's segmented
    acquisition) fresh, then disconnect -- the AWG keeps outputting after we
    close the connection, so it doesn't need to stay open for the rest of
    the run.

    Called first thing, before the (5-20 minute) resonance sweep, so the
    AWG's output has plenty of time to settle before anything actually
    depends on it (the interlock's trigger timing, the scope's acquisition).

    KS33600A.__init__() sends *RST on connect, wiping any previously
    configured output, so this always reconfigures both channels from
    scratch rather than assuming prior state.
    """
    from ks33600a import KS33600A

    print(f"[cw_odmr] configuring AWG: CH1 {carrier_freq_hz/1e6:.1f} MHz sine "
          f"@ {carrier_vpp} Vpp, CH2 {trigger_freq_hz:.1f} Hz square @ {trigger_vpp} Vpp")
    awg = KS33600A(AWG_RESOURCE)
    try:
        awg.write("SOUR1:FUNC SIN")
        awg.write(f"SOUR1:FREQ {carrier_freq_hz}")
        awg.write("OUTP1:LOAD 50")  # CH1 -> scope Channel 1, a real 50 Ohm input
        awg.write("SOUR1:VOLT:UNIT VPP")
        awg.write(f"SOUR1:VOLT {carrier_vpp}")
        awg.write("TRIG1:SOUR IMM")
        awg.write("OUTP1 ON")

        awg.write("SOUR2:FUNC SQU")
        awg.write(f"SOUR2:FREQ {trigger_freq_hz}")
        awg.write("OUTP2:LOAD INF")  # CH2 -> scope EXT TRIG, a high-Z input
        awg.write("SOUR2:VOLT:UNIT VPP")
        awg.write(f"SOUR2:VOLT {trigger_vpp}")
        awg.write("SOUR2:VOLT:OFFS 0")
        awg.write("TRIG2:SOUR IMM")
        awg.write("OUTP2 ON")
    finally:
        awg.close()
    print("[cw_odmr] AWG configured")


def parse_kv_args(argv):
    """Parse ["key=value", ...] into a dict, coercing each value to int or
    float where possible and leaving it as a string otherwise."""
    kv = {}
    for arg in argv:
        if "=" not in arg:
            raise SystemExit(f"unrecognized argument {arg!r} (expected key=value)")
        key, _, value = arg.partition("=")
        try:
            value = int(value)
        except ValueError:
            try:
                value = float(value)
            except ValueError:
                pass
        kv[key] = value
    return kv


def acquire_segments(file_name, segments, on_poll=None, max_attempts=3, retry_wait_s=45.0):
    """
    Run a segmented scope acquisition (RTB2004.run()), saving
    data/<file_name>.npy, _timetable.npy, _metadata.txt.

    The scope has occasionally gone unresponsive (VisaIOError) right around
    an interlock trip -- not root-caused, but confirmed that a fresh
    connection immediately afterward does NOT help (hit the same error again
    on the very next attempt); it needs actual wall-clock time to recover (a
    plain aliveness check succeeds again within a couple minutes).

    On VisaIOError: wait retry_wait_s, then try to SALVAGE whatever segments
    the scope actually captured before communication dropped (its history
    buffer is independent of our now-dead connection -- reconnecting fresh
    and reading it out is safe) via save_available_segments(). If that
    recovers any segments at all, treat it as done (with fewer than
    `segments` requested, saved as-is) rather than discarding them and
    restarting from scratch. Only if salvage comes back empty (or fails too)
    do we retry the full acquisition from zero. See notes.md.
    """
    last_known = {"count": 0}

    def _wrapped_on_poll(num_segments):
        last_known["count"] = num_segments
        if on_poll is not None:
            on_poll(num_segments)

    for attempt in range(1, max_attempts + 1):
        scope = RTB2004(SCOPE_RESOURCE, timeout=100000)
        try:
            scope.run(segments=segments, ch=1, path=DATA_DIR, name=file_name,
                      on_poll=_wrapped_on_poll)
            scope.close()
            return
        except pyvisa.errors.VisaIOError as e:
            print(f"[cw_odmr] scope error on attempt {attempt}/{max_attempts} ({e})")
            try:
                scope.close()
            except Exception:
                pass

            print(f"[cw_odmr] waiting {retry_wait_s:.0f}s, then trying to salvage "
                  f"partial data (last known: {last_known['count']} segments)")
            time.sleep(retry_wait_s)

            try:
                salvage_scope = RTB2004(SCOPE_RESOURCE, timeout=100000)
                try:
                    saved = salvage_scope.save_available_segments(
                        ch=1, path=DATA_DIR, name=file_name, max_segments=segments,
                    )
                finally:
                    salvage_scope.close()
                if saved > 0:
                    print(f"[cw_odmr] salvaged {saved} of {segments} requested segments "
                          f"-- saved as {DATA_DIR}/{file_name}.npy (PARTIAL)")
                    return
                print("[cw_odmr] nothing to salvage")
            except Exception as salvage_e:
                print(f"[cw_odmr] salvage attempt also failed ({salvage_e})")

            if attempt == max_attempts:
                raise
            print(f"[cw_odmr] retrying full acquisition from scratch "
                  f"(attempt {attempt + 1}/{max_attempts})")


def cmd_vna(file_name):
    os.makedirs(DATA_DIR, exist_ok=True)

    print("[cw_odmr] connecting to NanoVNA...")
    vna = NanoVNAF3(debug=True)
    try:
        print(f"[cw_odmr] connected: {vna.read_version()}")

        print("[cw_odmr] sweeping 2-4 GHz...")
        vna.run(2e9, 4e9, 801, channels=(0,), path=DATA_DIR, name=f"{file_name}_vna")
        print(f"[cw_odmr] done: saved {DATA_DIR}/{file_name}_vna.npy")
    finally:
        vna.close()


def cmd_run(file_name, **kw):
    segments = int(kw.get("segments", 1000))
    res_start_hz = float(kw.get("res_start_hz", 2.0e9))
    res_stop_hz = float(kw.get("res_stop_hz", 3.0e9))
    coarse_step_hz = float(kw.get("coarse_step_hz", 6.7e6))
    fine_span_hz = float(kw.get("fine_span_hz", 20e6))
    fine_step_hz = float(kw.get("fine_step_hz", 50e3))
    res_power_dbm = float(kw.get("res_power_dbm", -40.0))
    drive_power_dbm = float(kw.get("drive_power_dbm", 0.0))
    threshold_dbm = float(kw.get("threshold_dbm", -10.0))
    poll_interval_s = float(kw.get("poll_interval_s", 1.0))
    awg_carrier_freq_hz = float(kw.get("awg_carrier_freq_hz", 80e6))
    awg_carrier_vpp = float(kw.get("awg_carrier_vpp", 0.632))
    awg_trigger_freq_hz = float(kw.get("awg_trigger_freq_hz", 1.0))
    awg_trigger_vpp = float(kw.get("awg_trigger_vpp", 2.0))

    os.makedirs(DATA_DIR, exist_ok=True)

    print("[cw_odmr] step 1/6: configuring AWG")
    setup_awg(awg_carrier_freq_hz, awg_carrier_vpp, awg_trigger_freq_hz, awg_trigger_vpp)
    print("[cw_odmr] step 1/6 done")

    print("[cw_odmr] step 2/6: connecting to HP8673H + E4403B")
    gen = HP8673H(GEN_RESOURCE)
    sa = None
    stop_event = None
    thread = None
    try:
        sa = E4403B(SA_RESOURCE)
        print("[cw_odmr] step 2/6 done")

        print(f"[cw_odmr] step 3/6: sweeping for resonance "
              f"({res_start_hz/1e9:.4f}-{res_stop_hz/1e9:.4f} GHz, {res_power_dbm} dBm)")
        result = gen.resonance_sweep(
            sa, res_start_hz, res_stop_hz, coarse_step_hz, fine_span_hz, fine_step_hz,
            res_power_dbm, output_prefix=f"{DATA_DIR}/{file_name}_resonance",
        )
        f0_hz = result["f0_hz"]
        depth_db = result["baseline_dbm"] - result["dip_dbm"]
        print(f"[cw_odmr] step 3/6 done: f0 = {f0_hz/1e9:.5f} GHz, Q ~= {result['Q']:.0f}, "
              f"depth = {depth_db:.1f} dB")

        # monitor_interlock() opens its own E4403B connection on SA_RESOURCE --
        # release this one first so the two don't fight over the same GPIB address.
        sa.go_to_local()
        sa.close()
        sa = None

        print(f"[cw_odmr] step 4/6: starting interlock at {f0_hz/1e9:.5f} GHz "
              f"(drive {drive_power_dbm} dBm, threshold {threshold_dbm} dBm)")
        gen.preset()
        gen.set_frequency_hz(f0_hz)
        gen.set_power_dbm(drive_power_dbm)
        gen.rf_on()
        time.sleep(1.0)  # let the initial frequency/level settle before monitoring

        stop_event = threading.Event()
        interlock_result = {}

        def _interlock_thread():
            interlock_result["reason"] = gen.monitor_interlock(
                SA_RESOURCE, f0_hz, threshold_dbm,
                poll_interval_s=poll_interval_s, stop_event=stop_event,
            )

        thread = threading.Thread(target=_interlock_thread, daemon=True)
        thread.start()
        time.sleep(poll_interval_s + 0.5)  # give it time past the startup connectivity check

        if not thread.is_alive():
            # It already returned -- almost certainly tripped (or couldn't reach
            # the analyzer) before we ever got to the scope. Don't proceed.
            print(f"[cw_odmr] step 4/6 FAILED: interlock stopped immediately "
                  f"({interlock_result.get('reason', 'unknown')}) -- aborting")
            return
        print("[cw_odmr] step 4/6 done: interlock running")

        trip_segment = {"count": None}

        def _on_poll(num_segments):
            # Fires once per scope poll (~2/s); records the segment count the
            # first time we notice the interlock thread has died, so we know
            # how many segments were captured before vs. after the trip.
            if trip_segment["count"] is None and not thread.is_alive():
                trip_segment["count"] = num_segments
                print(f"[cw_odmr] interlock tripped after segment {num_segments} "
                      f"(of {segments} requested)")

        print(f"[cw_odmr] step 5/6: acquiring {segments} segments on the scope")
        acquire_segments(file_name, segments, on_poll=_on_poll)
        tripped = trip_segment["count"] is not None
        stop_event.set()
        thread.join(timeout=poll_interval_s + 5)

        if tripped:
            print(f"[cw_odmr] step 5/6: INTERLOCK TRIPPED DURING ACQUISITION "
                  f"({interlock_result.get('reason', 'unknown')}) at segment "
                  f"{trip_segment['count']} of {segments} -- data from that "
                  f"segment onward may be invalid")
        print(f"[cw_odmr] step 5/6 done: saved {DATA_DIR}/{file_name}.npy, "
              f"{file_name}_timetable.npy, {file_name}_metadata.txt")
    finally:
        # Always release every instrument, no matter what happened above
        # (scope error, interlock trip, an exception partway through setup,
        # etc.) -- never leave RF live or a connection dangling.
        print("[cw_odmr] step 6/6: shutting down")
        if stop_event is not None:
            stop_event.set()
        try:
            gen.rf_off()
        except Exception as e:
            print(f"[cw_odmr] WARNING: failed to turn off RF cleanly ({e})")
        try:
            gen.go_to_local()
        except Exception:
            pass
        gen.close()
        if sa is not None:
            try:
                sa.go_to_local()
            except Exception:
                pass
            sa.close()
        if thread is not None:
            thread.join(timeout=poll_interval_s + 5)

    print("[cw_odmr] done")


def cmd_run_background(file_name, **kw):
    segments = int(kw.get("segments", 1000))
    awg_carrier_freq_hz = float(kw.get("awg_carrier_freq_hz", 80e6))
    awg_carrier_vpp = float(kw.get("awg_carrier_vpp", 0.632))
    awg_trigger_freq_hz = float(kw.get("awg_trigger_freq_hz", 1.0))
    awg_trigger_vpp = float(kw.get("awg_trigger_vpp", 2.0))

    os.makedirs(DATA_DIR, exist_ok=True)

    # Still needed even with RF off -- the scope's segmented acquisition
    # requires the AWG's CH2 trigger to collect anything at all.
    print("[cw_odmr] step 1/4: configuring AWG")
    setup_awg(awg_carrier_freq_hz, awg_carrier_vpp, awg_trigger_freq_hz, awg_trigger_vpp)
    print("[cw_odmr] step 1/4 done")

    print("[cw_odmr] step 2/4: ensuring RF is off")
    gen = HP8673H(GEN_RESOURCE)
    try:
        gen.rf_off()
        gen.go_to_local()
    finally:
        gen.close()
    print("[cw_odmr] step 2/4 done: RF off")

    print(f"[cw_odmr] step 3/4: acquiring {segments} background segments on the "
          f"scope (RF off -- no interlock needed)")
    acquire_segments(file_name, segments)
    print(f"[cw_odmr] step 3/4 done: saved {DATA_DIR}/{file_name}.npy, "
          f"{file_name}_timetable.npy, {file_name}_metadata.txt")

    print("[cw_odmr] step 4/4: done")


def main():
    if len(sys.argv) < 3:
        print(__doc__)
        raise SystemExit(1)

    command = sys.argv[1]
    file_name = sys.argv[2]
    extra = parse_kv_args(sys.argv[3:])

    if command == "vna":
        cmd_vna(file_name)
    elif command == "run":
        cmd_run(file_name, **extra)
    elif command == "run_background":
        cmd_run_background(file_name, **extra)
    else:
        raise SystemExit(
            f"unknown command {command!r} (expected 'vna', 'run', or 'run_background')"
        )


if __name__ == "__main__":
    main()
