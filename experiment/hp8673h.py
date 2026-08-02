"""
HP 8673H Synthesized Signal Generator driver, plus the sweep and safety
routines built on top of it (frequency/resonance sweeps against an E4403B,
and the reflected-power interlock for continuous CW operation).

See notes.md for the settling-time, GPIB-bus-numbering, and other gotchas
these routines' defaults account for.
"""
import os
import sys
import time
import argparse

import pyvisa
import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from visa_retry import call_with_reconnect


class HP8673H:
    """
    HP 8673H Synthesized Signal Generator.

    Pre-SCPI HP-IB instrument: commands are short ASCII mnemonics
    (e.g. "FR3GZ" sets CW frequency to 3 GHz), not SCPI strings.
    Program codes are from the HP 8673C/D Operating and Service Manual,
    Table 3-7 (same command set used across the whole 8673A/B/C/D/E/G/H family).
    """

    def __init__(self, resource="GPIB0::19::INSTR", timeout=5000, debug=False):
        self.resource = resource
        self.timeout = timeout
        self.rm = pyvisa.ResourceManager()
        self.inst = self.rm.open_resource(resource)

        self.inst.timeout = timeout
        self.debug = debug
        print("HP8673H: connected")

    def close(self):
        if self.inst is not None:
            self.inst.close()

    def write(self, cmd):
        def _do():
            self.inst.write(cmd)
            if self.debug:
                print(cmd)
        call_with_reconnect(self, _do)

    def read_active_parameter(self):
        """Address the generator to talk and read back its currently
        displayed parameter (e.g. the CW frequency in MHz)."""
        self.write("OA")
        return call_with_reconnect(self, lambda: self.inst.read().strip())

    def preset(self):
        """IP: Instrument Preset (RCL 0 equivalent)."""
        self.write("IP")

    def rf_on(self):
        self.write("RF1")

    def rf_off(self):
        self.write("RF0")

    def auto_peak(self, state=True):
        self.write("K1" if state else "K0")

    def set_power_dbm(self, power_dbm):
        self.write(f"PL{power_dbm:g}DB")

    def set_frequency_hz(self, freq_hz):
        """FR: set CW frequency."""
        self.write(f"FR{freq_hz:.0f}HZ")

    def set_freq_increment_hz(self, step_hz):
        """FI: set the FREQ INCR step size used by manual tune/step keys."""
        self.write(f"FI{step_hz:.0f}HZ")

    def setup_sweep(self, start_hz, stop_hz, num_steps=100, dwell_ms=20):
        """
        Configure a start/stop sweep using an explicit number of steps.
        Does not start the sweep -- call start_auto_sweep() /
        start_single_sweep() afterwards.
        """
        self.write(f"FA{start_hz:.0f}HZ")   # START sweep frequency
        self.write(f"FB{stop_hz:.0f}HZ")    # STOP sweep frequency
        self.write(f"{num_steps:d}SS")      # number of steps
        self.write(f"{dwell_ms:d}DWMS")     # dwell time per step

    def start_auto_sweep(self):
        """W2: repetitive AUTO sweep."""
        self.write("W2")

    def start_single_sweep(self):
        """W6: arm and begin one SINGLE sweep."""
        self.write("W6")

    def stop_sweep(self):
        """W0: disable sweep, return to CW."""
        self.write("W0")

    def go_to_local(self):
        """Return the instrument to front-panel (local) control."""
        self.inst.control_ren(pyvisa.constants.VI_GPIB_REN_ADDRESS_GTL)

    # -- Frequency sweeps (against an E4403B on the analyzer side) ----------

    def frequency_sweep(self, sa, start_hz, stop_hz, step_hz, power_dbm=-20,
                         settle_s=0.05, initial_settle_s=1.0):
        """
        Step CW frequency from start_hz to stop_hz (inclusive) in step_hz
        increments, reading the E4403B's marker amplitude at each point.

        Assumes the generator output is fed into the analyzer input (e.g. via
        a coupler) and that the analyzer's span already covers
        [start_hz, stop_hz].

        initial_settle_s is a one-time delay after jumping to the sweep's
        start frequency, before any measurement is taken. AUTO PEAK
        re-leveling triggers on any frequency change > 50 MHz -- the initial
        jump from wherever the generator was previously set to start_hz is
        almost always >50 MHz, and needs much more time to settle than the
        small step-to-step increments during the sweep itself. Without this,
        the first few points of a sweep can show a spurious dip that has
        nothing to do with whatever is connected to the generator.

        settle_s defaults to a small nonzero value -- don't set it to 0.
        With settle_s=0, `if settle_s:` below skips the sleep entirely, and
        the analyzer's INIT:IMM fires essentially immediately after the
        frequency-change command is sent, racing the GPIB bus/generator
        before it's finished processing that command. This produced a real,
        reproducible false "dip" of >13 dB at specific frequencies (~2.10 and
        ~2.30 GHz on our unit) that looked exactly like a resonance or a
        generator hardware defect -- it was neither. Confirmed empirically:
        the same sweep with settle_s=0 shows the false dip every time;
        settle_s as low as 0.02 already fully eliminates it. This is why
        `coarse_sweep()`'s dip-finding used to occasionally lock onto ~2.1 or
        ~2.3 GHz instead of the real resonance -- see notes.md.

        Returns (freqs_hz, power_dbm_array).
        """
        freqs_hz = np.arange(start_hz, stop_hz + step_hz / 2, step_hz)

        self.preset()
        self.set_power_dbm(power_dbm)
        self.set_frequency_hz(start_hz)
        self.rf_on()
        time.sleep(initial_settle_s)

        sa.write("TRAC1:MODE WRITE")
        sa.write("AVER:STATE OFF")
        sa.write("INIT:CONT OFF")
        sa.write("CALC:MARK1:MODE POS")

        readings_dbm = np.empty(len(freqs_hz))

        for i, f in enumerate(freqs_hz):
            self.set_frequency_hz(f)
            if settle_s:
                time.sleep(settle_s)
            sa.write("INIT:IMM")
            sa.query("*OPC?")
            sa.write(f"CALC:MARK1:X {f}")
            readings_dbm[i] = float(sa.query("CALC:MARK1:Y?"))

        self.rf_off()

        return freqs_hz, readings_dbm

    def frequency_sweep_full_trace(self, sa, start_hz, stop_hz, step_hz,
                                    power_dbm=-20, rbw_hz=None):
        """
        Step CW frequency in step_hz increments from start_hz to stop_hz
        (inclusive). At each step, retune the E4403B to center on that
        frequency (span = step_hz, so adjacent windows tile with no gaps) and
        capture its FULL trace, rather than a single marker reading.

        Useful for seeing more than just the programmed tone -- spurs,
        harmonics, and the analyzer's own noise floor away from the peak.

        Returns a list of (freqs_hz, power_dbm) tuples, one per step.
        """
        centers_hz = np.arange(start_hz, stop_hz + step_hz / 2, step_hz)

        self.preset()
        self.set_power_dbm(power_dbm)
        self.rf_on()

        sa.write("TRAC1:MODE WRITE")
        sa.write("AVER:STATE OFF")
        if rbw_hz is not None:
            sa.write(f"BAND {rbw_hz}")
        sa.write("INIT:CONT OFF")

        traces = []
        for center in centers_hz:
            self.set_frequency_hz(center)
            sa.set_center_span(center, step_hz)
            sa.write("INIT:IMM")
            sa.query("*OPC?")
            traces.append(sa.get_trace(1))

        self.rf_off()

        return traces

    @staticmethod
    def concatenate_traces(traces):
        """Flatten a list of (freqs_hz, power_dbm) tuples from adjacent,
        tiled analyzer windows into single sorted arrays."""
        freqs_hz = np.concatenate([f for f, _ in traces])
        power_dbm = np.concatenate([p for _, p in traces])
        order = np.argsort(freqs_hz)
        return freqs_hz[order], power_dbm[order]

    # -- Coarse-then-fine resonance sweep ------------------------------------
    #
    # Locates a resonance dip in reflected power by first doing a coarse
    # sweep across a wide range to find the approximate location, then a
    # fine sweep zoomed in around that dip for a precise frequency/depth/Q
    # measurement. See notes.md: the coarse sweep step size should be no
    # coarser than about FWHM/5 for the resonance you expect, or you risk
    # stepping right over a narrow dip entirely.

    def coarse_sweep(self, sa, start_hz, stop_hz, step_hz, power_dbm):
        """Wide sweep to locate the approximate resonance frequency."""
        sa.set_center_span((start_hz + stop_hz) / 2, stop_hz - start_hz)
        return self.frequency_sweep(
            sa, start_hz=start_hz, stop_hz=stop_hz, step_hz=step_hz,
            power_dbm=power_dbm,
        )

    def fine_sweep(self, sa, center_hz, span_hz, step_hz, power_dbm):
        """Narrow, dense sweep centered on a coarse dip for precise measurement."""
        start_hz = center_hz - span_hz / 2
        stop_hz = center_hz + span_hz / 2
        sa.set_center_span(center_hz, span_hz)
        return self.frequency_sweep(
            sa, start_hz=start_hz, stop_hz=stop_hz, step_hz=step_hz,
            power_dbm=power_dbm, settle_s=0.15,
        )

    @staticmethod
    def find_dip(freqs_hz, power_dbm):
        idx = np.argmin(power_dbm)
        return freqs_hz[idx], power_dbm[idx]

    # -- Open-short-load scalar calibration ----------------------------------
    #
    # IMPORTANT LIMITATION: this is NOT a full vector (3-term) OSL
    # calibration. That requires COMPLEX (magnitude + phase) reflection data
    # to separate out directivity, source match, and reflection tracking
    # error -- i.e. an actual VNA. This setup (generator + directional
    # coupler + E4403B marker read) only measures scalar magnitude (dBm), so
    # there's no phase to work with.
    #
    # What this DOES give you: a scalar "response calibration" -- open and
    # short should both reflect ~100% (|Gamma|~=1) and set the "full
    # reflection" reference at each frequency; a 50-ohm load should reflect
    # ~0% (|Gamma|~=0) and sets the "zero reflection" reference. Normalizing
    # a later measurement against these two curves removes the cable/
    # coupler's own frequency-dependent insertion loss and coupling factor --
    # which is almost certainly what's distorting dip DEPTH and baseline
    # shape across a sweep. It does NOT correct source-match or directivity
    # error (those need phase to separate from the real DUT response), and
    # it doesn't move dip FREQUENCY, which doesn't depend on calibration.

    def calibrate_osl(self, sa, start_hz, stop_hz, step_hz, cal_dir,
                       drive_power_dbm=-40.0, settle_s=0.05, initial_settle_s=1.0):
        """
        Manual open-short-load scalar calibration (see limitation note
        above). Prompts you to connect each of the three standards in turn
        at the reference plane (wherever the resonator/DUT normally
        connects), sweeping frequency and recording reflected power for
        each. Saves {cal_dir}/osl_freqs_hz.npy, osl_open_dbm.npy,
        osl_short_dbm.npy, osl_load_dbm.npy -- load later with
        load_osl_calibration(cal_dir) and pass to resonance_sweep(cal_dir=...)
        or apply_osl_calibration() directly.

        Only needs to be redone if the cable/coupler chain between the
        generator and the reference plane changes -- re-run it if you
        swap/re-route cables.

        SAFETY: open and short both present a near-total reflection
        (|Gamma|~=1) at the reference plane. If that plane is downstream of
        an amplifier (as it likely is, since the whole point is to
        calibrate out the cable segment closest to the DUT), the amp's
        output ends up looking into a full mismatch during this step. Not
        every amplifier tolerates that -- check its datasheet's max VSWR/
        mismatch rating before running this for real. drive_power_dbm
        defaults to -40 dBm (matching resonance_sweep()'s own low-power
        default for exactly this reason) specifically to keep this safe
        regardless of amp tolerance -- don't raise it without confirming
        the amp is rated for open/short loads at whatever power you pick.
        """
        import os
        os.makedirs(cal_dir, exist_ok=True)

        # Set the power level up front, with RF still OFF, so it's visible
        # and confirmable on the front panel before anything is physically
        # connected -- frequency_sweep() below would otherwise only set
        # this (and turn RF on) after you've already been prompted to
        # connect the first standard.
        self.preset()
        self.set_power_dbm(drive_power_dbm)
        print(f"[calibrate_osl] generator power set to {drive_power_dbm} dBm "
              f"(RF still OFF) -- confirm this on the front panel before "
              f"connecting any standard")

        freqs_hz = None
        standard_power_dbm = {}
        for standard in ("open", "short", "load"):
            input(f"[calibrate_osl] connect the {standard.upper()} standard at "
                  f"the reference plane, then press Enter to sweep...")
            freqs_hz, power_dbm = self.frequency_sweep(
                sa, start_hz, stop_hz, step_hz, power_dbm=drive_power_dbm,
                settle_s=settle_s, initial_settle_s=initial_settle_s,
            )
            standard_power_dbm[standard] = power_dbm
            print(f"[calibrate_osl] {standard}: recorded {len(power_dbm)} points")

        np.save(f"{cal_dir}/osl_freqs_hz.npy", freqs_hz)
        np.save(f"{cal_dir}/osl_open_dbm.npy", standard_power_dbm["open"])
        np.save(f"{cal_dir}/osl_short_dbm.npy", standard_power_dbm["short"])
        np.save(f"{cal_dir}/osl_load_dbm.npy", standard_power_dbm["load"])
        print(f"[calibrate_osl] saved calibration to {cal_dir}")

    @staticmethod
    def load_osl_calibration(cal_dir):
        """Load a calibration saved by calibrate_osl(), pre-computing the
        linear-power reference curves used by apply_osl_calibration()."""
        freqs_hz = np.load(f"{cal_dir}/osl_freqs_hz.npy")
        open_dbm = np.load(f"{cal_dir}/osl_open_dbm.npy")
        short_dbm = np.load(f"{cal_dir}/osl_short_dbm.npy")
        load_dbm = np.load(f"{cal_dir}/osl_load_dbm.npy")

        open_lin = 10 ** (open_dbm / 10)
        short_lin = 10 ** (short_dbm / 10)
        load_lin = 10 ** (load_dbm / 10)

        return {
            "freqs_hz": freqs_hz,
            "full_lin": (open_lin + short_lin) / 2,
            "load_lin": load_lin,
        }

    @staticmethod
    def apply_osl_calibration(freqs_hz, power_dbm, cal, floor_db=-50.0):
        """
        Normalize raw reflected-power dBm readings against an open-short-
        load scalar calibration (see calibrate_osl()'s limitation note).
        cal is the dict returned by load_osl_calibration(). Interpolates the
        calibration curves (in linear power) onto freqs_hz, since a
        calibration sweep and a later sweep may use different step sizes.

        Returns 10*log10(|Gamma_cal|^2) in dB -- a calibrated, cable/coupler-
        corrected reflection level, NOT true absolute return loss/S11 (see
        limitation note). Scale is directly comparable to raw reflected dBm:
        ~0 dB = full reflection (matches open/short), very negative = well
        absorbed (matches load) -- deeper dips are still deeper dips.

        floor_db bounds how negative the output can go (default -50 dB).
        Near the load reference, ordinary measurement noise can push a
        point's raw power slightly BELOW the load's own reading -- the
        ratio's numerator goes negative, and log(anything <=0) is
        undefined. Flooring the OUTPUT dB value (rather than clipping the
        linear ratio to some tiny-but-positive epsilon like 1e-12, which
        maps to -120 dB) keeps these noise-driven points from swamping the
        plot with meaningless deep spikes swinging across the full dynamic
        range -- a scalar reflectometer at typical drive powers here
        doesn't actually resolve much beyond a few tens of dB anyway, so
        -50 dB is already past the setup's real noise floor, not an
        arbitrary truncation of genuine signal.
        """
        full_lin = np.interp(freqs_hz, cal["freqs_hz"], cal["full_lin"])
        load_lin = np.interp(freqs_hz, cal["freqs_hz"], cal["load_lin"])
        meas_lin = 10 ** (power_dbm / 10)

        gamma_sq = (meas_lin - load_lin) / (full_lin - load_lin)
        gamma_sq = np.clip(gamma_sq, 10 ** (floor_db / 10), None)
        return 10 * np.log10(gamma_sq)

    @staticmethod
    def estimate_q(freqs_hz, power_dbm, smooth_window=9):
        """
        Loaded-Q estimate from a reflection notch: half-power points found in
        linear power (not simply "3 dB above the dip" in dB), FWHM = width
        between them, Q = f0 / FWHM. A light moving average smooths
        point-to-point jitter at the bottom of the notch before finding the
        crossings.
        """
        kernel = np.ones(smooth_window) / smooth_window
        smoothed = np.convolve(power_dbm, kernel, mode='same')

        dip_idx = np.argmin(smoothed)
        f0 = freqs_hz[dip_idx]
        dip_dbm = smoothed[dip_idx]

        edge_n = max(1, len(freqs_hz) // 10)
        baseline_dbm = np.median(np.concatenate([smoothed[:edge_n], smoothed[-edge_n:]]))

        baseline_lin = 10 ** (baseline_dbm / 10)
        dip_lin = 10 ** (dip_dbm / 10)
        half_lin = (baseline_lin + dip_lin) / 2
        half_dbm = 10 * np.log10(half_lin)

        left = dip_idx
        while left > 0 and smoothed[left] < half_dbm:
            left -= 1
        right = dip_idx
        while right < len(smoothed) - 1 and smoothed[right] < half_dbm:
            right += 1

        fwhm_hz = freqs_hz[right] - freqs_hz[left]
        q = f0 / fwhm_hz if fwhm_hz > 0 else float("inf")

        return {"f0_hz": f0, "dip_dbm": dip_dbm, "baseline_dbm": baseline_dbm,
                "fwhm_hz": fwhm_hz, "Q": q}

    def resonance_sweep(self, sa, start_hz, stop_hz, coarse_step_hz,
                         fine_span_hz, fine_step_hz, power_dbm,
                         output_prefix=None, cal_dir=None):
        """
        Run the coarse-then-fine resonance sweep and return the estimate_q()
        result dict, augmented with the raw coarse/fine (freqs_hz, power_dbm)
        arrays. If output_prefix is given, also writes
        "{output_prefix}_coarse.csv" and "{output_prefix}_fine.csv".

        cal_dir (optional): path to an open-short-load calibration folder
        previously written by calibrate_osl(). If given, dip-finding and the
        Q/depth estimate use the calibration-corrected power (see
        apply_osl_calibration()'s limitation note -- this corrects cable/
        coupler frequency response, not source-match/directivity error). Raw
        (uncalibrated) power is still recorded everywhere alongside it --
        CSV output gets an extra power_dbm_cal column, and the returned dict
        gets coarse_power_dbm_cal/fine_power_dbm_cal keys. With cal_dir=None
        (the default), behavior is unchanged from before calibration
        support existed.

        Does not touch RF state or GPIB local/remote mode when finished --
        that's the caller's responsibility (see run_resonance_sweep() for the
        CLI's one-shot behavior).
        """
        cal = self.load_osl_calibration(cal_dir) if cal_dir else None
        if cal is not None:
            print(f"[resonance_sweep] applying OSL calibration from {cal_dir}")

        print(f"[resonance_sweep] coarse sweep {start_hz/1e9:.4f}-{stop_hz/1e9:.4f} GHz, "
              f"step {coarse_step_hz/1e6:.2f} MHz, {power_dbm} dBm")
        coarse_freqs_hz, coarse_power_dbm = self.coarse_sweep(
            sa, start_hz, stop_hz, coarse_step_hz, power_dbm)
        coarse_power_dbm_cal = (
            self.apply_osl_calibration(coarse_freqs_hz, coarse_power_dbm, cal)
            if cal is not None else None
        )
        if output_prefix:
            columns = [coarse_freqs_hz, coarse_power_dbm]
            header = "frequency_hz,power_dbm"
            if cal is not None:
                columns.append(coarse_power_dbm_cal)
                header += ",power_dbm_cal"
            np.savetxt(
                f"{output_prefix}_coarse.csv",
                np.column_stack(columns),
                delimiter=",", header=header, comments="",
            )

        dip_source = coarse_power_dbm_cal if cal is not None else coarse_power_dbm
        dip_freq_hz, dip_power_dbm = self.find_dip(coarse_freqs_hz, dip_source)
        print(f"[resonance_sweep] coarse dip: {dip_freq_hz/1e9:.4f} GHz, {dip_power_dbm:.1f} dBm")

        print(f"[resonance_sweep] fine sweep around {dip_freq_hz/1e9:.4f} GHz, "
              f"span {fine_span_hz/1e6:.2f} MHz, step {fine_step_hz/1e3:.1f} kHz")
        fine_freqs_hz, fine_power_dbm = self.fine_sweep(
            sa, dip_freq_hz, fine_span_hz, fine_step_hz, power_dbm)
        fine_power_dbm_cal = (
            self.apply_osl_calibration(fine_freqs_hz, fine_power_dbm, cal)
            if cal is not None else None
        )
        if output_prefix:
            columns = [fine_freqs_hz, fine_power_dbm]
            header = "frequency_hz,power_dbm"
            if cal is not None:
                columns.append(fine_power_dbm_cal)
                header += ",power_dbm_cal"
            np.savetxt(
                f"{output_prefix}_fine.csv",
                np.column_stack(columns),
                delimiter=",", header=header, comments="",
            )

        fine_source = fine_power_dbm_cal if cal is not None else fine_power_dbm
        result = self.estimate_q(fine_freqs_hz, fine_source)
        result["coarse_freqs_hz"] = coarse_freqs_hz
        result["coarse_power_dbm"] = coarse_power_dbm
        result["fine_freqs_hz"] = fine_freqs_hz
        result["fine_power_dbm"] = fine_power_dbm
        if cal is not None:
            result["coarse_power_dbm_cal"] = coarse_power_dbm_cal
            result["fine_power_dbm_cal"] = fine_power_dbm_cal

        depth_db = result["baseline_dbm"] - result["dip_dbm"]
        print(f"[resonance_sweep] f0 = {result['f0_hz']/1e9:.5f} GHz, "
              f"depth = {depth_db:.1f} dB, FWHM = {result['fwhm_hz']/1e6:.2f} MHz, "
              f"Q ~= {result['Q']:.0f}"
              f"{' (calibrated)' if cal is not None else ''}")

        return result

    # -- Reflected-power safety interlock -------------------------------
    #
    # Intended use: once a resonance has been located and the generator is
    # left running continuously at that frequency (with an amplifier
    # downstream), monitor_interlock() watches reflected power at that exact
    # frequency and immediately kills RF output if either:
    #
    #   (a) the spectrum analyzer can't be reached (GPIB dropout, powered
    #       off, disconnected, etc.) -- if we can't verify reflected power is
    #       safe, assume it isn't, and shut down.
    #   (b) reflected power at the operating frequency exceeds a threshold --
    #       e.g. the resonance has drifted away from the operating frequency,
    #       so what used to be a safe dip is now closer to full reflection.
    #
    # Once monitor_interlock() starts, it owns turning RF on/off -- don't
    # drive RF on/off from anywhere else while it's running.

    @staticmethod
    def try_connect_analyzer(resource):
        """Attempt to connect to the spectrum analyzer and confirm it
        actually responds (not just that the resource opened). Returns an
        E4403B instance, or None if the analyzer can't be reached."""
        from e4403b import E4403B
        try:
            sa = E4403B(resource)
            sa.query("*IDN?")
            return sa
        except Exception as e:
            print(f"[interlock] cannot reach spectrum analyzer: {e}")
            return None

    @staticmethod
    def read_reflected_power_dbm(sa, freq_hz, span_hz=10e6):
        """Read reflected power at freq_hz. Returns None if the read fails
        for any reason -- treated as 'analyzer unreachable' by the caller."""
        try:
            sa.set_center_span(freq_hz, span_hz)
            sa.write("TRAC1:MODE WRITE")
            sa.write("AVER:STATE OFF")
            sa.write("INIT:CONT OFF")
            sa.write("INIT:IMM")
            sa.query("*OPC?")
            sa.write("CALC:MARK1:MODE POS")
            sa.write(f"CALC:MARK1:X {freq_hz}")
            return float(sa.query("CALC:MARK1:Y?"))
        except Exception as e:
            print(f"[interlock] failed to read reflected power: {e}")
            return None

    def trip_interlock(self, reason):
        """Immediately kill RF output and report why."""
        print(f"[interlock] TRIPPED: {reason}")
        try:
            self.rf_off()
            print("[interlock] RF output OFF")
        except Exception as e:
            print(f"[interlock] WARNING: failed to turn off RF output cleanly: {e}")

    def monitor_interlock(self, sa_resource, freq_hz, threshold_dbm,
                           poll_interval_s=1.0, max_missed_reads=1, stop_event=None):
        """
        Continuously monitor reflected power at freq_hz. Trips (shuts off RF
        and returns a reason string) if:
          (a) the analyzer can't be reached (at startup, or for
              max_missed_reads consecutive polls), or
          (b) reflected power exceeds threshold_dbm.

        stop_event (a threading.Event, optional) allows cooperative shutdown
        from another thread once whatever it's protecting has finished
        normally -- e.g. run alongside a scope acquisition in a background
        thread, then call stop_event.set() when the acquisition completes.
        Checked once per poll (so shutdown latency is up to poll_interval_s),
        does NOT turn off RF (a clean stop is not a fault) -- the caller
        still owns RF state after a clean "stopped" return.
        """
        print(f"[interlock] monitoring {freq_hz/1e9:.4f} GHz, "
              f"threshold {threshold_dbm} dBm, polling every {poll_interval_s}s")

        sa = self.try_connect_analyzer(sa_resource)
        if sa is None:
            self.trip_interlock("spectrum analyzer not reachable at startup")
            return "analyzer_unreachable"

        missed = 0
        try:
            while True:
                if stop_event is not None and stop_event.is_set():
                    print("[interlock] stopped (requested)")
                    return "stopped"

                power_dbm = self.read_reflected_power_dbm(sa, freq_hz)

                if power_dbm is None:
                    missed += 1
                    print(f"[interlock] analyzer read failed ({missed}/{max_missed_reads})")
                    if missed >= max_missed_reads:
                        self.trip_interlock("spectrum analyzer unreachable")
                        return "analyzer_unreachable"
                    time.sleep(poll_interval_s)
                    continue

                missed = 0
                print(f"[interlock] reflected power: {power_dbm:.2f} dBm")

                if power_dbm > threshold_dbm:
                    self.trip_interlock(
                        f"reflected power {power_dbm:.2f} dBm exceeds threshold {threshold_dbm} dBm",
                    )
                    return "overpower"

                time.sleep(poll_interval_s)
        finally:
            sa.close()


# -- CLI entry points ---------------------------------------------------
#
# Each subcommand owns constructing and tearing down its own instrument(s),
# mirroring the standalone scripts these were merged from.

def run_resonance_sweep(gen_resource, sa_resource, start_hz, stop_hz,
                         coarse_step_hz, fine_span_hz, fine_step_hz,
                         power_dbm, output_prefix, cal_dir=None):
    from e4403b import E4403B

    gen = HP8673H(gen_resource)
    sa = E4403B(sa_resource)

    try:
        return gen.resonance_sweep(
            sa, start_hz, stop_hz, coarse_step_hz, fine_span_hz, fine_step_hz,
            power_dbm, output_prefix=output_prefix, cal_dir=cal_dir,
        )
    finally:
        gen.rf_off()
        gen.go_to_local()
        gen.close()
        sa.go_to_local()
        sa.close()


def run_calibrate_osl(gen_resource, sa_resource, start_hz, stop_hz, step_hz,
                       power_dbm, cal_dir):
    from e4403b import E4403B

    gen = HP8673H(gen_resource)
    sa = E4403B(sa_resource)

    try:
        gen.calibrate_osl(sa, start_hz, stop_hz, step_hz, cal_dir,
                           drive_power_dbm=power_dbm)
    finally:
        gen.go_to_local()
        gen.close()
        sa.go_to_local()
        sa.close()


def run_interlock(gen_resource, sa_resource, freq_hz, power_dbm,
                   threshold_dbm, poll_interval_s):
    gen = HP8673H(gen_resource)
    gen.preset()
    gen.set_frequency_hz(freq_hz)
    gen.set_power_dbm(power_dbm)
    gen.rf_on()
    time.sleep(1.0)  # let the initial frequency/level settle before monitoring

    try:
        reason = gen.monitor_interlock(
            sa_resource, freq_hz, threshold_dbm, poll_interval_s=poll_interval_s,
        )
        print(f"[interlock] stopped: {reason}")
        return reason
    finally:
        gen.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    resonance_p = sub.add_parser(
        "resonance", help="coarse-then-fine resonance sweep",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    resonance_p.add_argument("--gen-resource", default="GPIB1::19::INSTR")
    resonance_p.add_argument("--sa-resource", default="GPIB0::18::INSTR")
    resonance_p.add_argument("--start-hz", type=float, default=2.0e9)
    resonance_p.add_argument("--stop-hz", type=float, default=3.0e9)
    resonance_p.add_argument("--coarse-step-hz", type=float, default=6.7e6,
                              help="default matches FWHM/5 for a Q~75 resonance -- "
                                   "tune to your expected Q (see notes.md)")
    resonance_p.add_argument("--fine-span-hz", type=float, default=20e6)
    resonance_p.add_argument("--fine-step-hz", type=float, default=50e3)
    resonance_p.add_argument("--power-dbm", type=float, default=0.0)
    resonance_p.add_argument("--output-prefix", default="data/resonance_sweep")
    resonance_p.add_argument("--cal-dir", default=None,
                              help="path to an open-short-load calibration "
                                   "folder from the 'calibrate-osl' subcommand "
                                   "-- omit to use uncalibrated raw power "
                                   "(the previous default behavior)")

    calibrate_p = sub.add_parser(
        "calibrate-osl", help="manual open-short-load scalar calibration "
                               "(see HP8673H.calibrate_osl's docstring)",
    )
    calibrate_p.add_argument("--gen-resource", default="GPIB1::19::INSTR")
    calibrate_p.add_argument("--sa-resource", default="GPIB0::18::INSTR")
    calibrate_p.add_argument("--start-hz", type=float, default=2.0e9)
    calibrate_p.add_argument("--stop-hz", type=float, default=3.0e9)
    calibrate_p.add_argument("--step-hz", type=float, default=6.7e6)
    calibrate_p.add_argument("--power-dbm", type=float, default=-40.0,
                              help="drive power during calibration -- kept low "
                                   "by default since open/short present a "
                                   "near-total reflection at the reference "
                                   "plane; don't raise this without confirming "
                                   "the amplifier tolerates that mismatch at "
                                   "the power you pick")
    calibrate_p.add_argument("--cal-dir", required=True,
                              help="folder to save osl_freqs_hz.npy / "
                                   "osl_open_dbm.npy / osl_short_dbm.npy / "
                                   "osl_load_dbm.npy into")

    interlock_p = sub.add_parser(
        "interlock", help="reflected-power safety interlock for continuous CW operation",
    )
    interlock_p.add_argument("--gen-resource", default="GPIB1::19::INSTR")
    interlock_p.add_argument("--sa-resource", default="GPIB0::18::INSTR")
    interlock_p.add_argument("--freq-hz", type=float, default=2.68725e9,
                              help="operating (resonance) frequency in Hz -- "
                                   "measured f0 with the amplifier in the path "
                                   "at -40 dBm drive; see notes.md")
    interlock_p.add_argument("--power-dbm", type=float, default=0.0,
                              help="generator output power in dBm -- 0 dBm drive "
                                   "for max amplifier output (~20 dBm)")
    interlock_p.add_argument("--threshold-dbm", type=float, default=-10.0,
                              help="reflected power trip threshold in dBm -- see "
                                   "notes.md for the -15 to -25 dBm expected normal "
                                   "range this margin is based on")
    interlock_p.add_argument("--poll-interval-s", type=float, default=1.0)

    args = parser.parse_args()

    if args.command == "resonance":
        run_resonance_sweep(
            args.gen_resource, args.sa_resource, args.start_hz, args.stop_hz,
            args.coarse_step_hz, args.fine_span_hz, args.fine_step_hz,
            args.power_dbm, args.output_prefix, cal_dir=args.cal_dir,
        )
    elif args.command == "calibrate-osl":
        run_calibrate_osl(
            args.gen_resource, args.sa_resource, args.start_hz, args.stop_hz,
            args.step_hz, args.power_dbm, args.cal_dir,
        )
    elif args.command == "interlock":
        run_interlock(
            args.gen_resource, args.sa_resource, args.freq_hz, args.power_dbm,
            args.threshold_dbm, args.poll_interval_s,
        )


if __name__ == "__main__":
    main()
