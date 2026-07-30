"""
cw_odmr.py -- CLI entry point for the CW-ODMR pipeline built up in
cw_odmr.ipynb. See notes.md for the calibrated values (resonance frequency,
amplifier power, interlock threshold) this script's defaults are based on.

Usage:
    python cw_odmr.py vna <file_name>
        Take one NanoVNA sweep (2-4 GHz) and save it to
        data/<file_name>/<file_name>_vna.npy.

    python cw_odmr.py run <file_name> [key=value ...]
        Configures the AWG first (before anything else, so it has the whole
        multi-minute resonance sweep to settle), sweeps for the resonance
        (coarse+fine, HP8673H + E4403B), starts the reflected-power
        interlock at that frequency, then acquires segments on the RTB2004
        while the interlock runs concurrently in the background. Saves
        data/<file_name>/<file_name>.npy, _timetable.npy, _metadata.txt
        (same layout as RTB2004.run()), plus
        data/<file_name>/<file_name>_resonance_coarse.csv / _fine.csv from
        the sweep.

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
          awg_trigger_freq_hz=10.0  AWG CH2 trigger frequency (into scope EXT TRIG)
          awg_trigger_vpp=2.0      AWG CH2 amplitude

    python cw_odmr.py run_spectrum <file_name> [key=value ...]
        Like `run`, but instead of acquiring many segments at a single fixed
        resonance frequency, steps across the resonance's FWHM in fine
        frequency increments, collecting a small number of segments at EACH
        frequency (generator power held constant throughout) -- a frequency-
        resolved dataset instead of a long time-domain acquisition at one
        point. Safety is checked inline once per frequency point (reflected
        power at the CURRENT frequency, right before acquiring there) rather
        than via `run`'s background-thread interlock at one fixed frequency,
        since that design doesn't apply when the frequency itself keeps
        changing. Saves data/<file_name>/<file_name>_resonance_coarse.csv /
        _fine.csv (from the resonance sweep, same as `run`), plus
        data/<file_name>/<file_name>_spectrum.npy (shape (num_freq_points,
        segments_per_point, 10000)), _spectrum_freqs_hz.npy,
        _spectrum_reflected_dbm.npy, _spectrum_metadata.txt.

        Recognized key=value overrides (all optional):
          res_start_hz=2e9       resonance sweep start frequency
          res_stop_hz=3e9        resonance sweep stop frequency
          coarse_step_hz=6.7e6   coarse sweep step (track the expected FWHM -- notes.md)
          fine_span_hz=20e6      fine sweep span around the coarse dip
          fine_step_hz=50e3      fine sweep step
          res_power_dbm=-40.0    drive power during the resonance sweep
          drive_power_dbm=0.0    (constant) drive power during the spectrum scan
          threshold_dbm=-10.0    interlock trip threshold, in dBm
          freq_step_hz=10e3      frequency step across the FWHM
          segments_per_point=10  scope segments captured at each frequency
          fwhm_margin=1.0        scan span = FWHM * this margin
          settle_s=0.05          settle time after each frequency change --
                                 NEVER set to 0, see frequency_sweep()'s
                                 docstring in hp8673h.py
          awg_carrier_freq_hz=80e6  AWG CH1 carrier frequency (into scope Channel 1)
          awg_carrier_vpp=0.632     AWG CH1 amplitude
          awg_trigger_freq_hz=10.0  AWG CH2 trigger frequency (into scope EXT TRIG)
          awg_trigger_vpp=2.0      AWG CH2 amplitude

        NOT YET TESTED against real hardware.

    python cw_odmr.py run_background <file_name> [segments=1000]
        Background/baseline measurement: no resonance sweep, no microwave at
        all -- explicitly forces RF off first, then just acquires segments
        on the RTB2004 (no interlock needed, since there's no RF to protect
        against). Still configures the AWG first, same as `run` (the scope's
        segmented acquisition needs the CH2 trigger regardless of RF state).
        Saves data/<file_name>/<file_name>_bg.npy, _bg_timetable.npy,
        _bg_metadata.txt (the `_bg` suffix keeps it from colliding with a
        `run` dataset saved under the same <file_name>), for
        background-subtracting against a `run` dataset taken under the same
        trigger/timebase settings. Same awg_* overrides as `run` are
        recognized here too.

    python cw_odmr.py contrast_check <file_name> [key=value ...]
        Direct RF-on vs. RF-off comparison at a single FIXED frequency
        (default 2.87 GHz, the NV zero-field splitting), with many more
        segments than a `run_spectrum` point gets -- meant to settle whether
        a real ODMR contrast is present at all above the noise/drift floor,
        before trusting a frequency scan to resolve it as a function of
        detuning. Unlike `run`/`run_spectrum`, freq_hz is given directly
        rather than derived from the microwave resonator's own reflection
        dip -- the antenna's resonance is a different physical quantity from
        the NV transition and can drift several MHz between setups, so this
        sidesteps that entirely by just parking at the frequency of
        interest.

        RF-on and RF-off segments are acquired in ALTERNATING blocks
        (block_size each) rather than one long RF-on acquisition followed by
        one long RF-off acquisition, so slow drift affects both conditions
        similarly instead of concentrating in whichever one is measured
        later. Safety uses an inline reflected-power check (like
        `run_spectrum`, not `run`'s background-thread interlock, since RF
        isn't continuously live here) once per RF-on block.

        Saves data/<file_name>/<file_name>.npy (+ _timetable.npy,
        _metadata.txt, RF ON) and data/<file_name>/<file_name>_bg.npy (+
        _bg_timetable.npy, _bg_metadata.txt, RF OFF) -- same naming
        convention as `run`/`run_background`, so existing
        background-subtraction analysis (analyze_data.ipynb) works
        unmodified. Note the timetables here are block-level (one shared
        timestamp per block, elapsed seconds since the run started), not a
        true per-trigger timestamp.

        Recognized key=value overrides (all optional):
          freq_hz=2.87e9         fixed drive frequency
          segments=2000          total scope segments for EACH of RF-on and
                                  RF-off, spread across alternating blocks
          block_size=100         segments per RF-on/RF-off block -- smaller
                                  means tighter interleaving (less drift per
                                  condition) and faster interlock response,
                                  at the cost of more per-block overhead
          drive_power_dbm=0.0    drive power during RF-on blocks
          threshold_dbm=-10.0    interlock trip threshold, in dBm, checked
                                  once per RF-on block (after it settles)
          rf_settle_s=5.0        wait after every RF on/off transition
                                  before checking reflected power or
                                  acquiring -- the generator/amplifier chain
                                  doesn't respond instantaneously
          awg_carrier_freq_hz=80e6  AWG CH1 carrier frequency (unused if CH1
                                     is wired to the real PMT signal, but the
                                     AWG still needs configuring for CH2's
                                     scope trigger)
          awg_carrier_vpp=0.632     AWG CH1 amplitude
          awg_trigger_freq_hz=10.0  AWG CH2 trigger frequency (into scope EXT TRIG)
          awg_trigger_vpp=2.0      AWG CH2 amplitude

Example:
    python cw_odmr.py vna baseline
    python cw_odmr.py run trial1 segments=1000
    python cw_odmr.py run_background trial1 segments=1000
    python cw_odmr.py contrast_check contrast1 freq_hz=2.87e9 segments=2000
"""
import contextlib
import os
import sys
import time
import threading

import numpy as np
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

DATA_DIR = "D:\\cw_odmr"


def setup_awg(carrier_freq_hz=80e6, carrier_vpp=0.632, trigger_freq_hz=10.0, trigger_vpp=2.0):
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


def acquire_segments(path, name, segments, on_poll=None, on_rf=None, max_attempts=3,
                      retry_wait_s=45.0, trigger_freq_hz=10.0):
    """
    Run a segmented scope acquisition (RTB2004.run()), saving
    <path>/<name>.npy, _timetable.npy, _metadata.txt.

    trigger_freq_hz is the AWG CH2 trigger rate actually configured for this
    run (see setup_awg()) -- used only to size the scope's VISA timeout
    (below), not to configure anything itself. The scope needs `segments`
    triggers before its blocking `*OPC?` query (inside
    RTB2004.wait_for_acquisition()) will return, which takes segments /
    trigger_freq_hz seconds at minimum -- if that exceeds the VISA
    connection's own read timeout, pyvisa raises a VisaIOError (VI_ERROR_TMO)
    on that query, not the more informative Python-level TimeoutError
    wait_for_acquisition() would otherwise raise itself. Confirmed for real:
    a hardcoded 100000ms (100s) timeout here was fine for the 1000-segment,
    10Hz-trigger defaults elsewhere, but a 2000-segment run at the same 10Hz
    (needing 200s minimum) hit exactly this VI_ERROR_TMO. `* 2` below is a
    safety margin on top of the raw minimum, plus a fixed floor so short
    acquisitions still get a reasonable timeout for genuine scope hangs.

    The scope has occasionally gone unresponsive (VisaIOError) both right
    around an interlock trip and with no trip at all -- not root-caused
    either way, but confirmed that a fresh connection immediately afterward
    does NOT help (hit the same error again on the very next attempt); it
    needs actual wall-clock time to recover (a plain aliveness check
    succeeds again within a couple minutes).

    on_rf(is_needed), if given, is called with True right before each
    acquisition attempt starts (drive needed) and with False as soon as
    we're done actively triggering and are only reading out already-
    captured history -- whether that's the normal post-acquisition readout
    (via RTB2004.run()'s on_acquired) or the wait-then-salvage sequence
    after a scope error. History readout doesn't need RF on, and reading
    out many segments can take as long as the acquisition itself, so
    leaving it live for that whole window is an unnecessary exposure.

    On VisaIOError: wait retry_wait_s, then try to SALVAGE whatever segments
    the scope actually captured before communication dropped (its history
    buffer is independent of our now-dead connection -- reconnecting fresh
    and reading it out is safe) via save_available_segments(). If that
    recovers any segments at all, treat it as done (with fewer than
    `segments` requested, saved as-is) rather than discarding them and
    restarting from scratch. Only if salvage comes back empty (or fails too)
    do we retry the full acquisition from zero. See notes.md.
    """
    scope_timeout_ms = max(100000, int(segments / trigger_freq_hz * 2 * 1000) + 20000)

    last_known = {"count": 0}

    def _wrapped_on_poll(num_segments):
        last_known["count"] = num_segments
        if on_poll is not None:
            on_poll(num_segments)

    def _set_rf(is_needed):
        if on_rf is not None:
            try:
                on_rf(is_needed)
            except Exception as cb_e:
                print(f"[cw_odmr] WARNING: on_rf callback failed ({cb_e})")

    for attempt in range(1, max_attempts + 1):
        _set_rf(True)
        acquisition_done = {"flag": False}

        def _mark_acquired():
            acquisition_done["flag"] = True
            _set_rf(False)

        scope = RTB2004(SCOPE_RESOURCE, timeout=scope_timeout_ms)
        try:
            scope.run(segments=segments, ch=1, path=path, name=name,
                      on_poll=_wrapped_on_poll, on_acquired=_mark_acquired,
                      acquisition_timeout_s=scope_timeout_ms / 1000)
            scope.close()
            return
        except pyvisa.errors.VisaIOError as e:
            print(f"[cw_odmr] scope error on attempt {attempt}/{max_attempts} ({e})")
            try:
                scope.close()
            except Exception:
                pass

            # Whatever the scope is doing now, it isn't actively collecting
            # new triggers -- no need for RF while we wait/salvage.
            _set_rf(False)

            if acquisition_done["flag"]:
                # Triggering had already finished before this crashed -- all
                # `segments` are already sitting in the scope's history
                # buffer (independent of our now-dead connection), so this
                # is a pure readout failure, not the scope being mid-
                # acquisition. Confirmed live: skipping the wait and
                # salvaging immediately recovered all requested segments.
                # No need for the usual wall-clock recovery window.
                print(f"[cw_odmr] crash happened during history readout "
                      f"(acquisition already complete) -- salvaging immediately")
            else:
                print(f"[cw_odmr] waiting {retry_wait_s:.0f}s, then trying to salvage "
                      f"partial data (last known: {last_known['count']} segments)")
                time.sleep(retry_wait_s)

            try:
                salvage_scope = RTB2004(SCOPE_RESOURCE, timeout=scope_timeout_ms)
                try:
                    saved = salvage_scope.save_available_segments(
                        ch=1, path=path, name=name, max_segments=segments,
                    )
                finally:
                    salvage_scope.close()
                if saved > 0:
                    print(f"[cw_odmr] salvaged {saved} of {segments} requested segments "
                          f"-- saved as {path}/{name}.npy (PARTIAL)")
                    return
                print("[cw_odmr] nothing to salvage")
            except Exception as salvage_e:
                print(f"[cw_odmr] salvage attempt also failed ({salvage_e})")

            if attempt == max_attempts:
                raise
            print(f"[cw_odmr] retrying full acquisition from scratch "
                  f"(attempt {attempt + 1}/{max_attempts})")


class _Tee:
    """Writable stream that forwards writes to multiple underlying streams."""

    def __init__(self, *streams):
        self.streams = streams

    def write(self, data):
        for s in self.streams:
            s.write(data)

    def flush(self):
        for s in self.streams:
            s.flush()


@contextlib.contextmanager
def _tee_stdout(log_path):
    """
    Mirror everything printed while this context is active to log_path, in
    addition to the console -- captures every print() call made during a
    `run`/`run_background`, including ones from the driver modules
    (HP8673H, E4403B, RTB2004, KS33600A) and the interlock thread, with no
    changes needed at each individual print() call site. sys.stdout is a
    single process-wide object shared by all threads, so the interlock
    thread's concurrent prints are captured the same way its output already
    interleaves with the main thread's on the console.
    """
    original_stdout = sys.stdout
    with open(log_path, "w") as log_file:
        sys.stdout = _Tee(original_stdout, log_file)
        try:
            yield
        finally:
            sys.stdout = original_stdout


def cmd_vna(file_name):
    run_path = f"{DATA_DIR}/{file_name}"
    os.makedirs(run_path, exist_ok=True)

    print("[cw_odmr] connecting to NanoVNA...")
    vna = NanoVNAF3(debug=True)
    try:
        print(f"[cw_odmr] connected: {vna.read_version()}")

        print("[cw_odmr] sweeping 2-4 GHz...")
        vna.run(2e9, 4e9, 801, channels=(0,), path=run_path, name=f"{file_name}_vna")
        print(f"[cw_odmr] done: saved {run_path}/{file_name}_vna.npy")
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
    awg_trigger_freq_hz = float(kw.get("awg_trigger_freq_hz", 10.0))
    awg_trigger_vpp = float(kw.get("awg_trigger_vpp", 2.0))

    run_path = f"{DATA_DIR}/{file_name}"
    os.makedirs(run_path, exist_ok=True)
    log_dir = f"{DATA_DIR}/logs"
    os.makedirs(log_dir, exist_ok=True)
    log_path = f"{log_dir}/{file_name}.txt"

    with _tee_stdout(log_path):
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
                res_power_dbm, output_prefix=f"{run_path}/{file_name}_resonance",
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

            def _on_rf(is_needed):
                # We're not actively triggering/collecting while reading out
                # history (normal readout after a full acquisition, or the
                # wait-then-salvage sequence after a scope error) -- no need
                # for RF drive during those windows.
                if is_needed:
                    print("[cw_odmr] resuming RF drive for acquisition")
                    gen.rf_on()
                else:
                    print("[cw_odmr] reading out scope history -- turning RF off "
                          "until the next acquisition attempt")
                    gen.rf_off()

            print(f"[cw_odmr] step 5/6: acquiring {segments} segments on the scope")
            acquire_segments(run_path, file_name, segments, on_poll=_on_poll, on_rf=_on_rf,
                              trigger_freq_hz=awg_trigger_freq_hz)
            tripped = trip_segment["count"] is not None
            stop_event.set()
            thread.join(timeout=poll_interval_s + 5)

            if tripped:
                print(f"[cw_odmr] step 5/6: INTERLOCK TRIPPED DURING ACQUISITION "
                      f"({interlock_result.get('reason', 'unknown')}) at segment "
                      f"{trip_segment['count']} of {segments} -- data from that "
                      f"segment onward may be invalid")
            print(f"[cw_odmr] step 5/6 done: saved {run_path}/{file_name}.npy, "
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


def cmd_run_spectrum(file_name, **kw):
    """
    Like cmd_run(), but instead of acquiring many segments at a single fixed
    resonance frequency, steps across the resonance's FWHM in fine frequency
    increments, collecting a small number of segments at EACH frequency (the
    generator's power held constant throughout) -- builds a frequency-
    resolved dataset instead of a long time-domain acquisition at one point.

    Safety: unlike cmd_run()'s monitor_interlock() (a background thread
    watching reflected power at one FIXED frequency, which doesn't make
    sense here since the operating frequency keeps changing), this reads
    reflected power at the CURRENT frequency inline, once per point, right
    before acquiring there -- reusing the same HP8673H.read_reflected_power_
    dbm()/trip_interlock() primitives monitor_interlock() itself is built
    from. On trip (or if the analyzer can't be reached), stops immediately
    and saves whatever points were completed so far rather than the full
    requested span.

    NOT YET TESTED against real hardware.
    """
    res_start_hz = float(kw.get("res_start_hz", 2.0e9))
    res_stop_hz = float(kw.get("res_stop_hz", 3.0e9))
    coarse_step_hz = float(kw.get("coarse_step_hz", 6.7e6))
    fine_span_hz = float(kw.get("fine_span_hz", 20e6))
    fine_step_hz = float(kw.get("fine_step_hz", 50e3))
    res_power_dbm = float(kw.get("res_power_dbm", -40.0))
    drive_power_dbm = float(kw.get("drive_power_dbm", 0.0))
    threshold_dbm = float(kw.get("threshold_dbm", -10.0))
    freq_step_hz = float(kw.get("freq_step_hz", 10e3))
    segments_per_point = int(kw.get("segments_per_point", 10))
    fwhm_margin = float(kw.get("fwhm_margin", 1.0))
    settle_s = float(kw.get("settle_s", 0.05))  # NEVER 0 -- see frequency_sweep()'s
                                                  # docstring in hp8673h.py for the
                                                  # race-condition bug a 0 default
                                                  # caused there
    awg_carrier_freq_hz = float(kw.get("awg_carrier_freq_hz", 80e6))
    awg_carrier_vpp = float(kw.get("awg_carrier_vpp", 0.632))
    awg_trigger_freq_hz = float(kw.get("awg_trigger_freq_hz", 10.0))
    awg_trigger_vpp = float(kw.get("awg_trigger_vpp", 2.0))

    run_path = f"{DATA_DIR}/{file_name}"
    os.makedirs(run_path, exist_ok=True)
    log_dir = f"{DATA_DIR}/logs"
    os.makedirs(log_dir, exist_ok=True)
    log_path = f"{log_dir}/{file_name}_spectrum.txt"

    with _tee_stdout(log_path):
        print("[cw_odmr] step 1/5: configuring AWG")
        setup_awg(awg_carrier_freq_hz, awg_carrier_vpp, awg_trigger_freq_hz, awg_trigger_vpp)
        print("[cw_odmr] step 1/5 done")

        print("[cw_odmr] step 2/5: connecting to HP8673H + E4403B")
        gen = HP8673H(GEN_RESOURCE)
        sa = None
        scope = None
        try:
            sa = E4403B(SA_RESOURCE)
            print("[cw_odmr] step 2/5 done")

            print(f"[cw_odmr] step 3/5: sweeping for resonance "
                  f"({res_start_hz/1e9:.4f}-{res_stop_hz/1e9:.4f} GHz, {res_power_dbm} dBm)")
            result = gen.resonance_sweep(
                sa, res_start_hz, res_stop_hz, coarse_step_hz, fine_span_hz, fine_step_hz,
                res_power_dbm, output_prefix=f"{run_path}/{file_name}_resonance",
            )
            f0_hz = result["f0_hz"]
            fwhm_hz = result["fwhm_hz"]
            print(f"[cw_odmr] step 3/5 done: f0 = {f0_hz/1e9:.5f} GHz, "
                  f"FWHM = {fwhm_hz/1e6:.3f} MHz, Q ~= {result['Q']:.0f}")

            span_hz = fwhm_hz * fwhm_margin
            freqs_hz = np.arange(f0_hz - span_hz / 2, f0_hz + span_hz / 2 + freq_step_hz / 2,
                                  freq_step_hz)
            est_bytes = len(freqs_hz) * segments_per_point * RTB2004.NUM_SAMPLES * 4
            print(f"[cw_odmr] step 4/5: spectrum scan {freqs_hz[0]/1e9:.5f}-"
                  f"{freqs_hz[-1]/1e9:.5f} GHz ({len(freqs_hz)} points, {freq_step_hz/1e3:.1f} "
                  f"kHz step), {segments_per_point} segments/point, drive {drive_power_dbm} dBm, "
                  f"threshold {threshold_dbm} dBm, estimated size {est_bytes/1e6:.0f} MB")

            # HP8673H.read_reflected_power_dbm() opens its own SA reads on
            # the same resource used here -- release this connection first
            # so they don't fight over the same GPIB address (same reasoning
            # as cmd_run() releasing `sa` before monitor_interlock() starts).
            sa.go_to_local()
            sa.close()
            sa = None

            gen.preset()
            gen.set_power_dbm(drive_power_dbm)
            gen.rf_on()
            time.sleep(1.0)  # let the initial frequency/level settle before scanning

            ilock_sa = HP8673H.try_connect_analyzer(SA_RESOURCE)
            if ilock_sa is None:
                gen.trip_interlock("spectrum analyzer not reachable at startup")
                return

            scope = RTB2004(SCOPE_RESOURCE, timeout=100000)
            combined = np.empty((len(freqs_hz), segments_per_point, RTB2004.NUM_SAMPLES),
                                 dtype=np.float32)
            reflected_dbm_arr = np.full(len(freqs_hz), np.nan)
            sample_rate_hz = None
            t0_s = None
            n_completed = 0
            point_start_time = None

            try:
                for i, f in enumerate(freqs_hz):
                    if i == 0:
                        point_start_time = time.perf_counter()

                    gen.set_frequency_hz(f)
                    time.sleep(settle_s)

                    reflected_dbm = HP8673H.read_reflected_power_dbm(ilock_sa, f)
                    reflected_dbm_arr[i] = reflected_dbm if reflected_dbm is not None else np.nan

                    if reflected_dbm is None or reflected_dbm > threshold_dbm:
                        reason = (
                            "spectrum analyzer unreachable"
                            if reflected_dbm is None else
                            f"reflected power {reflected_dbm:.2f} dBm exceeds threshold "
                            f"{threshold_dbm} dBm"
                        )
                        gen.trip_interlock(f"{reason} at {f/1e9:.5f} GHz "
                                           f"(point {i + 1}/{len(freqs_hz)})")
                        break

                    segs, sample_rate_hz, t0_s = scope.acquire_segments_to_memory(
                        segments=segments_per_point, ch=1,
                    )
                    combined[i] = segs
                    n_completed = i + 1

                    # segments_per_point full triggers are needed per point,
                    # at whatever rate awg_trigger_freq_hz drives CH2 -- e.g.
                    # at the default 10Hz, 10 segments takes >=1s of just
                    # waiting for triggers, on top of per-point SCPI/readout
                    # overhead, so a fine sweep across a wide span can easily
                    # take tens of minutes. Print every point (not just every
                    # N) so this doesn't look hung, plus an ETA after the
                    # first point.
                    if n_completed == 1:
                        eta_s = (time.perf_counter() - point_start_time) * len(freqs_hz)
                        print(f"[cw_odmr] first point took "
                              f"{time.perf_counter() - point_start_time:.2f}s -- "
                              f"ETA for all {len(freqs_hz)} points: {eta_s/60:.1f} min "
                              f"(increase awg_trigger_freq_hz for a faster scan, since "
                              f"segments_per_point full triggers are needed per point)")
                    print(f"[cw_odmr] spectrum point {n_completed}/{len(freqs_hz)}: "
                          f"f={f/1e9:.5f} GHz, reflected={reflected_dbm:.2f} dBm")
            finally:
                ilock_sa.close()

            tripped = n_completed < len(freqs_hz)
            combined = combined[:n_completed]
            freqs_hz = freqs_hz[:n_completed]
            reflected_dbm_arr = reflected_dbm_arr[:n_completed]

            if n_completed == 0:
                print("[cw_odmr] step 4/5 FAILED: no points completed -- nothing to save")
            else:
                np.save(f"{run_path}/{file_name}_spectrum.npy", combined)
                np.save(f"{run_path}/{file_name}_spectrum_freqs_hz.npy", freqs_hz)
                np.save(f"{run_path}/{file_name}_spectrum_reflected_dbm.npy", reflected_dbm_arr)
                scope.save_metadata(ch=1, path=run_path, name=f"{file_name}_spectrum",
                                     sample_rate_hz=sample_rate_hz)
                print(f"[cw_odmr] step 4/5 done{' (PARTIAL -- interlock tripped)' if tripped else ''}: "
                      f"saved {run_path}/{file_name}_spectrum.npy "
                      f"({n_completed} of {len(freqs_hz) + (0 if not tripped else 1)} points), "
                      f"_spectrum_freqs_hz.npy, _spectrum_reflected_dbm.npy, "
                      f"_spectrum_metadata.txt")
        finally:
            print("[cw_odmr] step 5/5: shutting down")
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
            if scope is not None:
                scope.close()

    print("[cw_odmr] done")


def cmd_run_background(file_name, **kw):
    segments = int(kw.get("segments", 1000))
    awg_carrier_freq_hz = float(kw.get("awg_carrier_freq_hz", 80e6))
    awg_carrier_vpp = float(kw.get("awg_carrier_vpp", 0.632))
    awg_trigger_freq_hz = float(kw.get("awg_trigger_freq_hz", 10.0))
    awg_trigger_vpp = float(kw.get("awg_trigger_vpp", 2.0))

    run_path = f"{DATA_DIR}/{file_name}"
    os.makedirs(run_path, exist_ok=True)
    bg_name = f"{file_name}_bg"
    log_dir = f"{DATA_DIR}/logs"
    os.makedirs(log_dir, exist_ok=True)
    log_path = f"{log_dir}/{bg_name}.txt"

    with _tee_stdout(log_path):
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
        acquire_segments(run_path, bg_name, segments, trigger_freq_hz=awg_trigger_freq_hz)
        print(f"[cw_odmr] step 3/4 done: saved {run_path}/{bg_name}.npy, "
              f"{bg_name}_timetable.npy, {bg_name}_metadata.txt")

        print("[cw_odmr] step 4/4: done")


def cmd_contrast_check(file_name, **kw):
    """
    Direct RF-on vs. RF-off comparison at a single FIXED frequency (default
    2.87 GHz) -- see the module docstring's `contrast_check` section for the
    motivation (settling whether a real ODMR contrast exists above the
    noise/drift floor, independent of the microwave resonator's own
    reflection dip, which is a different physical quantity from the NV
    transition frequency).

    RF-on and RF-off segments are acquired in ALTERNATING blocks (default
    100 segments each) rather than one long RF-on acquisition followed by
    one long RF-off acquisition -- the two conditions are then interleaved
    throughout the whole run instead of separated by (potentially many
    minutes of) wall-clock time, so the slow drift already characterized
    elsewhere in this project affects both conditions similarly rather than
    concentrating in whichever one happens to be measured later (which would
    otherwise masquerade as a fake "contrast").

    No resonance sweep -- freq_hz is used directly. Safety here uses the
    same INLINE reflected-power check as run_spectrum() (read power, compare
    to threshold_dbm), not cmd_run()'s background-thread monitor_interlock()
    -- a continuously-running background thread doesn't make sense when RF
    is deliberately toggled off between blocks (there's no forward power to
    reflect while it's off, so a poll landing during an off-block would
    misread as a fault). The check runs AFTER turning RF on and letting it
    settle (rf_settle_s), not before -- checking beforehand would always see
    RF still off from the previous block and never catch a real fault.
    Response latency to a real fault is roughly
    `rf_settle_s + block_size / awg_trigger_freq_hz` seconds (e.g. ~15s at
    the defaults) -- shrink block_size for a tighter margin if needed.
    rf_settle_s is also applied after every RF-off transition, before the
    RF-off block is acquired -- the generator/amplifier chain doesn't
    respond instantaneously to either transition.

    RF-on segments are saved as <file_name>.npy (+ _timetable.npy,
    _metadata.txt) and RF-off segments as <file_name>_bg.npy (+
    _bg_timetable.npy, _bg_metadata.txt) -- same naming convention as
    run()/run_background(), so this can be analyzed with the same
    background-subtraction helpers already written for those (e.g.
    analyze_data.ipynb's _bg_waveform_stats()). The timetables here are
    coarser than those helpers normally see, though: every segment in a
    block shares one timestamp (elapsed seconds since this run started,
    measured in Python right after that block was acquired), not a true
    per-trigger timestamp from the scope -- enough to see drift across the
    whole alternating sequence, not to resolve timing within a block.
    """
    freq_hz = float(kw.get("freq_hz", 2.87e9))
    segments = int(kw.get("segments", 2000))          # total per condition
    block_size = int(kw.get("block_size", 100))        # segments per RF-on/RF-off block
    drive_power_dbm = float(kw.get("drive_power_dbm", 0.0))
    threshold_dbm = float(kw.get("threshold_dbm", -10.0))
    rf_settle_s = float(kw.get("rf_settle_s", 5.0))  # wait after every RF on/off
                                                       # transition before checking
                                                       # reflected power or acquiring --
                                                       # the generator/amplifier chain
                                                       # doesn't respond instantaneously
    awg_carrier_freq_hz = float(kw.get("awg_carrier_freq_hz", 80e6))
    awg_carrier_vpp = float(kw.get("awg_carrier_vpp", 0.632))
    awg_trigger_freq_hz = float(kw.get("awg_trigger_freq_hz", 10.0))
    awg_trigger_vpp = float(kw.get("awg_trigger_vpp", 2.0))

    run_path = f"{DATA_DIR}/{file_name}"
    os.makedirs(run_path, exist_ok=True)
    log_dir = f"{DATA_DIR}/logs"
    os.makedirs(log_dir, exist_ok=True)
    log_path = f"{log_dir}/{file_name}_contrast_check.txt"

    with _tee_stdout(log_path):
        print("[cw_odmr] step 1/4: configuring AWG")
        setup_awg(awg_carrier_freq_hz, awg_carrier_vpp, awg_trigger_freq_hz, awg_trigger_vpp)
        print("[cw_odmr] step 1/4 done")

        print("[cw_odmr] step 2/4: connecting to HP8673H + E4403B")
        gen = HP8673H(GEN_RESOURCE)
        ilock_sa = None
        scope = None
        num_blocks = -(-segments // block_size)  # ceil
        block_timeout_ms = max(100000, int(block_size / awg_trigger_freq_hz * 2 * 1000) + 20000)
        on_blocks = []   # list of (segments_array, elapsed_s)
        off_blocks = []
        sample_rate_hz = None
        t0_s = None
        n_on_done = 0
        n_off_done = 0
        tripped = False
        run_start_time = None

        try:
            gen.preset()
            gen.set_frequency_hz(freq_hz)
            gen.set_power_dbm(drive_power_dbm)

            ilock_sa = HP8673H.try_connect_analyzer(SA_RESOURCE)
            if ilock_sa is None:
                gen.trip_interlock("spectrum analyzer not reachable at startup")
                return
            print("[cw_odmr] step 2/4 done")

            scope = RTB2004(SCOPE_RESOURCE, timeout=block_timeout_ms)
            print(f"[cw_odmr] step 3/4: {num_blocks} alternating blocks of {block_size} "
                  f"segments each ({segments} total per condition) at "
                  f"{freq_hz/1e9:.5f} GHz, drive {drive_power_dbm} dBm, "
                  f"threshold {threshold_dbm} dBm")
            run_start_time = time.perf_counter()

            for block_i in range(num_blocks):
                this_on = min(block_size, segments - n_on_done)
                this_off = min(block_size, segments - n_off_done)
                if this_on <= 0 and this_off <= 0:
                    break

                reflected_dbm = None
                if this_on > 0:
                    # Check AFTER turning RF on (and letting it settle), not
                    # before -- reflected power is only meaningful while
                    # there's forward power to reflect. Checking beforehand
                    # (while RF was still off from the previous block) would
                    # always read near the noise floor and never actually
                    # catch a real fault.
                    gen.rf_on()
                    time.sleep(rf_settle_s)
                    reflected_dbm = HP8673H.read_reflected_power_dbm(ilock_sa, freq_hz)
                    if reflected_dbm is None or reflected_dbm > threshold_dbm:
                        reason = (
                            "spectrum analyzer unreachable"
                            if reflected_dbm is None else
                            f"reflected power {reflected_dbm:.2f} dBm exceeds threshold "
                            f"{threshold_dbm} dBm"
                        )
                        gen.trip_interlock(f"{reason} at block {block_i + 1}/{num_blocks} "
                                            f"(RF-on)")
                        tripped = True
                        break

                    segs_on, sample_rate_hz, t0_s = scope.acquire_segments_to_memory(
                        segments=this_on, ch=1,
                    )
                    on_blocks.append((segs_on, time.perf_counter() - run_start_time))
                    n_on_done += this_on

                gen.rf_off()
                time.sleep(rf_settle_s)
                if this_off > 0:
                    segs_off, sample_rate_hz, t0_s = scope.acquire_segments_to_memory(
                        segments=this_off, ch=1,
                    )
                    off_blocks.append((segs_off, time.perf_counter() - run_start_time))
                    n_off_done += this_off

                reflected_str = f"{reflected_dbm:.2f} dBm" if reflected_dbm is not None else "n/a"
                print(f"[cw_odmr] block {block_i + 1}/{num_blocks}: RF-on "
                      f"{n_on_done}/{segments}, RF-off {n_off_done}/{segments}, "
                      f"reflected={reflected_str}")

            if tripped:
                print(f"[cw_odmr] step 3/4: INTERLOCK TRIPPED -- {n_on_done}/{segments} "
                      f"RF-on and {n_off_done}/{segments} RF-off segments completed")
            print("[cw_odmr] step 3/4 done")
        finally:
            print("[cw_odmr] step 4/4: shutting down")
            try:
                gen.rf_off()
            except Exception as e:
                print(f"[cw_odmr] WARNING: failed to turn off RF cleanly ({e})")
            try:
                gen.go_to_local()
            except Exception:
                pass
            gen.close()
            if ilock_sa is not None:
                ilock_sa.close()
            if scope is not None:
                scope.close()

        if not on_blocks and not off_blocks:
            print("[cw_odmr] nothing acquired -- not saving")
            return

        on_combined = (np.concatenate([b[0] for b in on_blocks])
                       if on_blocks else np.empty((0, RTB2004.NUM_SAMPLES), dtype=np.float32))
        off_combined = (np.concatenate([b[0] for b in off_blocks])
                        if off_blocks else np.empty((0, RTB2004.NUM_SAMPLES), dtype=np.float32))
        on_timetable = (np.concatenate([np.full(len(segs), elapsed_s) for segs, elapsed_s in on_blocks])
                        if on_blocks else np.empty(0))
        off_timetable = (np.concatenate([np.full(len(segs), elapsed_s) for segs, elapsed_s in off_blocks])
                         if off_blocks else np.empty(0))

        np.save(f"{run_path}/{file_name}.npy", on_combined)
        np.save(f"{run_path}/{file_name}_timetable.npy", on_timetable)
        np.save(f"{run_path}/{file_name}_bg.npy", off_combined)
        np.save(f"{run_path}/{file_name}_bg_timetable.npy", off_timetable)
        for suffix in ("", "_bg"):
            with open(f"{run_path}/{file_name}{suffix}_metadata.txt", "w") as f:
                f.write(f"sample_rate_hz={sample_rate_hz}\n")
                f.write(f"t0_s={t0_s}\n")
                f.write(f"num_points={RTB2004.NUM_SAMPLES}\n")

        print(f"[cw_odmr] saved {n_on_done} RF-on segments to {run_path}/{file_name}.npy "
              f"and {n_off_done} RF-off segments to {run_path}/{file_name}_bg.npy "
              f"(interleaved in blocks of {block_size}){' -- PARTIAL' if tripped else ''}")

    print("[cw_odmr] done")


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
    elif command == "run_spectrum":
        cmd_run_spectrum(file_name, **extra)
    elif command == "run_background":
        cmd_run_background(file_name, **extra)
    elif command == "contrast_check":
        cmd_contrast_check(file_name, **extra)
    else:
        raise SystemExit(
            f"unknown command {command!r} (expected 'vna', 'run', 'run_spectrum', "
            f"'run_background', or 'contrast_check')"
        )


if __name__ == "__main__":
    main()
