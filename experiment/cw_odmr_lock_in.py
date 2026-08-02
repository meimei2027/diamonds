"""
cw_odmr_lock_in.py -- CW-ODMR frequency-resolved spectrum scan, same
resonance-sweep + inline-interlock design as cw_odmr.py's run_spectrum(),
but reading out via lock-in (SR830) synchronous detection instead of scope
segments.

NOT YET TESTED against real hardware.

WARNING -- read before trusting any result from this: chopping the
microwave while the laser stays continuously on (as this script does) is
EXACTLY the condition that produced a real, reproducible, but NOT-genuine-
ODMR artifact in `cw_odmr.py`'s `contrast_check` (see notes.md /
`contrast_check_result.ipynb`) -- a signal synchronized with RF on/off that
turned out to be frequency-independent (same size at a wildly off-resonance
frequency) and required real photocurrent to appear, rather than tracking
the NV transition. Using a lock-in instead of scope-segment comparison does
NOT avoid this: the lock-in demodulates whatever is synchronized with the
chop reference, and it can't distinguish "real fluorescence modulation"
from "any other effect that happens to be synchronized with the same
square wave" any better than the scope comparison could.

UPDATE -- a second, converging pickup-artifact signature was found and
fully localized with this lock-in readout (see notes.md's SR830 section
for the full chain): the effect required real RF power at the resonator,
had nothing to do with light or the PMT tube itself, and was traced to the
SR445A preamp's plastic (unshielded) housing picking up the resonator's
near-field directly -- confirmed by signal dropping with distance and
vanishing entirely once the preamp was replaced with a simple resistor
transimpedance for this signal path. That specific artifact is understood
and fixed. HOWEVER this does not by itself confirm real ODMR contrast is
now present -- before trusting a result from this script, still run the
same two controls used before:
  1. Laser off (no photocurrent) -> a real artifact-of-pickup should vanish.
  2. Far from any plausible NV resonance -> real ODMR contrast should
     vanish/shrink there; the artifact previously did not.
If those controls aren't clean, the safer path is `pulsed_odmr.py` instead,
which confines the MW pulse to the dark period specifically to avoid ever
having RF and photocurrent present at the same time.

How lock-in readout works here (see the conversation this was written for):
unlike a scope segment average, you do NOT get one reading per RF on/off
cycle. The chop reference (AWG CH2's square wave) runs continuously; the
SR830 continuously demodulates the PMT signal against it and low-pass
filters with a settable TIME CONSTANT, integrating over many chop cycles
(e.g. a 100 ms time constant at a 1 kHz chop averages ~100 cycles). So the
per-frequency-point pattern is: set the generator frequency -> wait several
time constants for the lock-in's filter to settle to the new value -> read
one X (and Y) value -> move on. The chop cycle itself is just the internal
clock the lock-in locks to; it never surfaces as a discrete readout.

Hardware
--------
CH2 (AWG) -> split/tee'd to BOTH:
  (a) the ZYSWA-2-50DR+ switch's Control pin (gates the microwave, same
      wiring as rf_switch_test.ipynb / pulsed_odmr.py), and
  (b) the SR830's external reference input, so the lock-in locks onto the
      EXACT same signal driving the switch (see set_reference_external()
      below, TTL rising-edge mode -- CH2's 0-5V square wave is TTL-level
      compatible).
50% duty cycle by default -- maximizes the fundamental-frequency AC content
of the resulting chopped optical signal (same Fourier reasoning as the
sideband work in rf_switch_test.ipynb: a 50% duty square wave puts the most
energy in its fundamental harmonic of any duty cycle).
Laser stays on continuously (external/manual, same as cw_odmr.py's other
CW-ODMR commands) -- only the microwave is chopped.
Generator stays at a FIXED CW power/frequency between interlock checks,
same as run_spectrum() -- only the frequency changes per sweep point.

Phase calibration (see the SR830 phase-calibration discussion in the
conversation this was written for): this script does NOT auto-calibrate
phase -- do it manually once per session (e.g. feed a large, real
modulation at the chop frequency, null Y by adjusting phase, or use
`SR830.auto_phase()` if the signal is already strong enough for it to
converge reliably), then pass the resulting value as `phase_deg`.

Usage:
    python cw_odmr_lock_in.py run <file_name> [key=value ...]
        Sweeps microwave frequency across the resonance's FWHM (same
        coarse-then-fine resonance_sweep() as run_spectrum()), gating the
        microwave with a chopped square wave on AWG CH2 throughout, and
        records the SR830's X/Y lock-in output at each frequency -- same
        inline reflected-power interlock check per point as run_spectrum
        (generator kept at fixed CW power/frequency between checks, only
        the frequency changes). Saves
        data/<file_name>/<file_name>_lockin_spectrum_freqs_hz.npy,
        _lockin_spectrum_x.npy, _lockin_spectrum_y.npy,
        _lockin_spectrum_reflected_dbm.npy, _lockin_spectrum_metadata.txt.

        Recognized key=value overrides (all optional):
          res_start_hz=2e9        resonance sweep start frequency
          res_stop_hz=3e9         resonance sweep stop frequency
          coarse_step_hz=6.7e6    coarse sweep step (track expected FWHM)
          fine_span_hz=20e6       fine sweep span around the coarse dip
          fine_step_hz=50e3       fine sweep step
          res_power_dbm=-40.0     drive power during the resonance sweep
          res_cal_dir=None        path to an open-short-load calibration
                                  folder (see hp8673h.py's calibrate_osl()/
                                  the 'calibrate-osl' CLI subcommand) --
                                  omit to use uncalibrated raw reflected
                                  power for dip-finding/Q, same as before
                                  calibration support existed
          drive_power_dbm=0.0     (constant) drive power during the scan
          threshold_dbm=-10.0     interlock trip threshold, in dBm
          freq_step_hz=10e3       frequency step across the FWHM
          fwhm_margin=1.0         scan span = FWHM * this margin
          settle_s=0.05           settle time after each frequency change
                                  -- NEVER 0, see hp8673h.py's
                                  frequency_sweep() docstring for why
          chop_freq_hz=1000.0     AWG CH2 / lock-in reference frequency
          chop_duty_pct=50.0      AWG CH2 duty cycle
          time_constant_s=0.1     SR830 time constant
          settle_time_constants=5.0  how many time constants to wait after
                                      each frequency change before reading
                                      the lock-in (in addition to settle_s)
          sensitivity_v=5e-3      SR830 sensitivity target (auto-rounds up
                                  to the nearest available range) -- ignored
                                  by default since auto_sensitivity=true;
                                  only takes effect if you explicitly set
                                  auto_sensitivity=false.
          auto_sensitivity=true   call the SR830's own AGAN (auto gain) once
                                  RF/chop is running and a real signal is
                                  present at the input, right before the
                                  frequency sweep starts, instead of trusting
                                  a fixed sensitivity_v -- confirmed on the
                                  bench to actually find a usable range
                                  where a fixed placeholder value did not.
                                  The actual range it picks gets printed and
                                  saved to the metadata file. Set to false to
                                  use sensitivity_v verbatim instead.
          phase_deg=0.0           pre-calibrated SR830 phase (see above --
                                  NOT auto-calibrated by this script)
          input_coupling=ac       SR830 input coupling, 'ac' or 'dc' -- ac
                                  is almost always right here (blocks the
                                  large DC baseline, leaving headroom for
                                  the much smaller AC modulation)

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

    python cw_odmr_lock_in.py calibrate-osl <cal_dir> [key=value ...]
        Open-short-load scalar calibration for the resonance sweep (see
        hp8673h.py's calibrate_osl()). Holds the switch static on RF2 first,
        same as 'run' does before its own resonance sweep. See
        cmd_calibrate_osl()'s docstring for the full list of key=value
        overrides. Saves <cal_dir>/osl_freqs_hz.npy, osl_open_dbm.npy,
        osl_short_dbm.npy, osl_load_dbm.npy -- pass <cal_dir> as
        res_cal_dir=<cal_dir> to 'run' to use it.

Example:
    python cw_odmr_lock_in.py run lockin1 chop_freq_hz=1000 phase_deg=42.3
    python cw_odmr_lock_in.py single calib1 freq_hz=2.87e9 auto_sensitivity=true
    python cw_odmr_lock_in.py calibrate-osl D:\\cw_odmr_lock_in\\osl_cal
"""
import sys
import time

import numpy as np

import ks33600a
from hp8673h import HP8673H
from sr830 import SR830
from cw_odmr import parse_kv_args, _tee_stdout

AWG_RESOURCE = "USB0::0x0957::0x5707::MY53800810::INSTR"
GEN_RESOURCE = "GPIB1::19::INSTR"
SA_RESOURCE = "GPIB0::18::INSTR"
SR830_RESOURCE = "GPIB2::2::INSTR"

DATA_DIR = "D:\\cw_odmr_lock_in"


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


def cmd_run(file_name, **kw):
    res_start_hz = float(kw.get("res_start_hz", 2.0e9))
    res_stop_hz = float(kw.get("res_stop_hz", 3.0e9))
    coarse_step_hz = float(kw.get("coarse_step_hz", 6.7e6))
    fine_span_hz = float(kw.get("fine_span_hz", 20e6))
    fine_step_hz = float(kw.get("fine_step_hz", 50e3))
    res_power_dbm = float(kw.get("res_power_dbm", -40.0))
    res_cal_dir = kw.get("res_cal_dir", None)
    drive_power_dbm = float(kw.get("drive_power_dbm", 0.0))
    threshold_dbm = float(kw.get("threshold_dbm", -10.0))
    freq_step_hz = float(kw.get("freq_step_hz", 10e3))
    fwhm_margin = float(kw.get("fwhm_margin", 1.0))
    settle_s = float(kw.get("settle_s", 0.05))  # NEVER 0 -- see
                                                  # hp8673h.py's
                                                  # frequency_sweep()
                                                  # docstring
    chop_freq_hz = float(kw.get("chop_freq_hz", 1000.0))
    chop_duty_pct = float(kw.get("chop_duty_pct", 50.0))
    time_constant_s = float(kw.get("time_constant_s", 0.1))
    settle_time_constants = float(kw.get("settle_time_constants", 5.0))
    sensitivity_v = float(kw.get("sensitivity_v", 5e-3))
    auto_sensitivity = str(kw.get("auto_sensitivity", "true")).lower() == "true"
    phase_deg = float(kw.get("phase_deg", 0.0))
    input_coupling = str(kw.get("input_coupling", "ac"))

    run_path = f"{DATA_DIR}/{file_name}"
    import os
    os.makedirs(run_path, exist_ok=True)
    log_dir = f"{DATA_DIR}/logs"
    os.makedirs(log_dir, exist_ok=True)
    log_path = f"{log_dir}/{file_name}_lockin_spectrum.txt"

    with _tee_stdout(log_path):
        print("[cw_odmr_lock_in] step 1/4: configuring AWG CH2 (static, not chopping "
              "yet) + SR830 lock-in")
        awg = ks33600a.KS33600A(AWG_RESOURCE, debug=True)
        # Hold the switch parked on the sample path (RF2) instead of chopping
        # for now -- resonance_sweep() below reads the analyzer and needs a
        # clean, unmodulated signal. Chopping only starts once resonance is
        # found, right before the actual lock-in sweep loop.
        set_switch_static(awg, route_to_sample=True)

        lia = SR830(SR830_RESOURCE, debug=True)
        setup_lock_in(lia, chop_freq_hz, time_constant_s, sensitivity_v, phase_deg,
                      input_coupling, auto_sensitivity=auto_sensitivity)
        print(f"[cw_odmr_lock_in] step 1/4 done: switch held static on RF2 (sample path) "
              f"for the resonance sweep; chop @ {chop_freq_hz:.0f} Hz, "
              f"{chop_duty_pct:.0f}% duty starts after resonance is found; "
              f"time constant {time_constant_s*1e3:.1f} ms, phase {phase_deg} deg "
              f"(NOT auto-calibrated -- see module docstring)")

        print("[cw_odmr_lock_in] step 2/4: connecting to HP8673H + E4403B (interlock)")
        gen = HP8673H(GEN_RESOURCE)
        ilock_sa = None
        try:
            print(f"[cw_odmr_lock_in] step 2/4: sweeping for resonance "
                  f"({res_start_hz/1e9:.4f}-{res_stop_hz/1e9:.4f} GHz, {res_power_dbm} dBm)")
            from e4403b import E4403B
            sa = E4403B(SA_RESOURCE)
            result = gen.resonance_sweep(
                sa, res_start_hz, res_stop_hz, coarse_step_hz, fine_span_hz, fine_step_hz,
                res_power_dbm, output_prefix=f"{run_path}/{file_name}_resonance",
                cal_dir=res_cal_dir,
            )
            f0_hz = result["f0_hz"]
            fwhm_hz = result["fwhm_hz"]
            print(f"[cw_odmr_lock_in] step 2/4 done: f0 = {f0_hz/1e9:.5f} GHz, "
                  f"FWHM = {fwhm_hz/1e6:.3f} MHz, Q ~= {result['Q']:.0f}")

            sa.go_to_local()
            sa.close()

            print(f"[cw_odmr_lock_in] enabling switch chop now that resonance is "
                  f"located ({chop_duty_pct:.0f}% duty @ {chop_freq_hz:.0f} Hz)")
            setup_chop(awg, chop_freq_hz, chop_duty_pct)
            awg.close()

            span_hz = fwhm_hz * fwhm_margin
            freqs_hz = np.arange(f0_hz - span_hz / 2, f0_hz + span_hz / 2 + freq_step_hz / 2,
                                  freq_step_hz)
            print(f"[cw_odmr_lock_in] step 3/4: lock-in spectrum scan "
                  f"{freqs_hz[0]/1e9:.5f}-{freqs_hz[-1]/1e9:.5f} GHz "
                  f"({len(freqs_hz)} points, {freq_step_hz/1e3:.1f} kHz step), "
                  f"drive {drive_power_dbm} dBm, threshold {threshold_dbm} dBm")

            gen.preset()
            gen.set_power_dbm(drive_power_dbm)
            gen.set_frequency_hz(freqs_hz[0])
            gen.rf_on()
            time.sleep(1.0)  # let the initial frequency/level settle

            if auto_sensitivity:
                # A real signal (chop + RF) is present now -- let AGAN pick
                # a range, then wait the same settle-time-constants margin
                # used between sweep points before trusting/logging it.
                lia.auto_gain()
                time.sleep(settle_time_constants * time_constant_s)
                actual_sensitivity_v = lia.get_sensitivity_v()
                print(f"[cw_odmr_lock_in] auto_sensitivity: AGAN selected "
                      f"{actual_sensitivity_v:.3e} V full scale")
                sensitivity_v = actual_sensitivity_v

            ilock_sa = HP8673H.try_connect_analyzer(SA_RESOURCE)
            if ilock_sa is None:
                gen.trip_interlock("spectrum analyzer not reachable at startup")
                return

            x_values = np.full(len(freqs_hz), np.nan)
            y_values = np.full(len(freqs_hz), np.nan)
            reflected_dbm_arr = np.full(len(freqs_hz), np.nan)
            n_completed = 0

            settle_after_step_s = settle_time_constants * time_constant_s

            for i, f in enumerate(freqs_hz):
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

                # Let the lock-in's own low-pass filter settle to the new
                # steady-state value before reading -- this is NOT the same
                # thing as settle_s above (which is just the generator's own
                # retuning settle); changing frequency changes what's being
                # demodulated, and the filter needs several time constants
                # to converge to the new reading regardless of how fast the
                # generator itself settled.
                time.sleep(settle_after_step_s)

                x, y = lia.read_xy()
                x_values[i] = x
                y_values[i] = y
                n_completed = i + 1
                print(f"[cw_odmr_lock_in] point {n_completed}/{len(freqs_hz)}: "
                      f"f={f/1e9:.5f} GHz, X={x:.6e} V, Y={y:.6e} V, "
                      f"reflected={reflected_dbm:.2f} dBm")

            tripped = n_completed < len(freqs_hz)
            freqs_hz = freqs_hz[:n_completed]
            x_values = x_values[:n_completed]
            y_values = y_values[:n_completed]
            reflected_dbm_arr = reflected_dbm_arr[:n_completed]

            if n_completed == 0:
                print("[cw_odmr_lock_in] step 3/4 FAILED: no points completed -- nothing to save")
            else:
                np.save(f"{run_path}/{file_name}_lockin_spectrum_freqs_hz.npy", freqs_hz)
                np.save(f"{run_path}/{file_name}_lockin_spectrum_x.npy", x_values)
                np.save(f"{run_path}/{file_name}_lockin_spectrum_y.npy", y_values)
                np.save(f"{run_path}/{file_name}_lockin_spectrum_reflected_dbm.npy",
                        reflected_dbm_arr)
                with open(f"{run_path}/{file_name}_lockin_spectrum_metadata.txt", "w") as fh:
                    fh.write(f"chop_freq_hz={chop_freq_hz}\n")
                    fh.write(f"chop_duty_pct={chop_duty_pct}\n")
                    fh.write(f"time_constant_s={time_constant_s}\n")
                    fh.write(f"settle_time_constants={settle_time_constants}\n")
                    fh.write(f"sensitivity_v={sensitivity_v}\n")
                    fh.write(f"phase_deg={phase_deg}\n")
                    fh.write(f"drive_power_dbm={drive_power_dbm}\n")
                print(f"[cw_odmr_lock_in] step 3/4 done"
                      f"{' (PARTIAL -- interlock tripped)' if tripped else ''}: "
                      f"saved {run_path}/{file_name}_lockin_spectrum_freqs_hz.npy "
                      f"({n_completed} points), _lockin_spectrum_x.npy, "
                      f"_lockin_spectrum_y.npy, _lockin_spectrum_reflected_dbm.npy, "
                      f"_lockin_spectrum_metadata.txt")
        finally:
            print("[cw_odmr_lock_in] step 4/4: shutting down")
            try:
                gen.rf_off()
            except Exception as e:
                print(f"[cw_odmr_lock_in] WARNING: failed to turn off RF cleanly ({e})")
            try:
                gen.go_to_local()
            except Exception:
                pass
            gen.close()
            if ilock_sa is not None:
                ilock_sa.close()
            try:
                lia.go_to_local()
            except Exception:
                pass
            lia.close()
            try:
                awg.close()  # no-op if already closed above once chop started
            except Exception:
                pass

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

    Diagnostic use -- carrier_off=true: isolates whether an observed
    lock-in signal requires the RF carrier to be present at all, or is
    just crosstalk from the AWG CH2 chop control line itself (switch
    control pin transitions, no actual microwave power). With
    carrier_off=true, CH2 still chops the switch and the SR830 still
    demodulates against it exactly as normal, but the generator's RF
    output is kept off throughout (never calls rf_on()) -- if the signal
    disappears, whatever you were seeing needed real RF power (e.g.
    resonator near-field pickup); if it persists, it's coming from the
    control line itself. Reflected-power interlock is still checked each
    sample for safety, but should trivially read very low the whole time
    since there's no RF to reflect.

    Recognized key=value overrides (all optional):
      freq_hz=2.87e9          fixed generator frequency (tuned even with
                               carrier_off=true, in case you flip it back on)
      drive_power_dbm=0.0     generator CW power
      threshold_dbm=-10.0     interlock trip threshold, in dBm
      carrier_off=false       keep RF output off throughout -- see above
      duration_s=0            total run time in seconds; 0 = run until
                               Ctrl+C
      sample_interval_s=1.0   time between lock-in reads (on top of the
                               time-constant settle already built into the
                               lock-in's own filter)
      chop_freq_hz=1000.0     AWG CH2 / lock-in reference frequency
      chop_duty_pct=50.0      AWG CH2 duty cycle
      time_constant_s=0.1     SR830 time constant
      sensitivity_v=5e-3      SR830 sensitivity target -- ignored by default
                               since auto_sensitivity=true; only takes
                               effect if you explicitly set
                               auto_sensitivity=false
      auto_sensitivity=true   call AGAN once RF/chop is running, before the
                               read loop starts (see cmd_run's docstring) --
                               confirmed on the bench to actually find a
                               usable range where a fixed placeholder value
                               did not
      phase_deg=0.0           pre-calibrated SR830 phase
      input_coupling=ac       SR830 input coupling, 'ac' or 'dc'
    """
    freq_hz = float(kw.get("freq_hz", 2.87e9))
    drive_power_dbm = float(kw.get("drive_power_dbm", 0.0))
    threshold_dbm = float(kw.get("threshold_dbm", -10.0))
    carrier_off = str(kw.get("carrier_off", "false")).lower() == "true"
    duration_s = float(kw.get("duration_s", 0.0))  # 0 = run until Ctrl+C
    sample_interval_s = float(kw.get("sample_interval_s", 1.0))
    chop_freq_hz = float(kw.get("chop_freq_hz", 1000.0))
    chop_duty_pct = float(kw.get("chop_duty_pct", 50.0))
    time_constant_s = float(kw.get("time_constant_s", 0.1))
    sensitivity_v = float(kw.get("sensitivity_v", 5e-3))
    auto_sensitivity = str(kw.get("auto_sensitivity", "true")).lower() == "true"
    phase_deg = float(kw.get("phase_deg", 0.0))
    input_coupling = str(kw.get("input_coupling", "ac"))

    run_path = f"{DATA_DIR}/{file_name}"
    import os
    os.makedirs(run_path, exist_ok=True)
    log_dir = f"{DATA_DIR}/logs"
    os.makedirs(log_dir, exist_ok=True)
    log_path = f"{log_dir}/{file_name}_lockin_single.txt"

    with _tee_stdout(log_path):
        print("[cw_odmr_lock_in] step 1/3: configuring AWG CH2 chop + SR830 lock-in")
        awg = ks33600a.KS33600A(AWG_RESOURCE, debug=True)
        setup_chop(awg, chop_freq_hz, chop_duty_pct)
        awg.close()

        lia = SR830(SR830_RESOURCE, debug=True)
        setup_lock_in(lia, chop_freq_hz, time_constant_s, sensitivity_v, phase_deg,
                      input_coupling, auto_sensitivity=auto_sensitivity)
        print(f"[cw_odmr_lock_in] step 1/3 done: {chop_duty_pct:.0f}% duty cycle chop "
              f"@ {chop_freq_hz:.0f} Hz, time constant {time_constant_s*1e3:.1f} ms, "
              f"phase {phase_deg} deg (NOT auto-calibrated -- see module docstring)")

        print(f"[cw_odmr_lock_in] step 2/3: connecting to HP8673H + E4403B (interlock), "
              f"fixed frequency {freq_hz/1e9:.5f} GHz, {drive_power_dbm} dBm"
              f"{' -- CARRIER OFF (control-line-pickup diagnostic)' if carrier_off else ''}")
        gen = HP8673H(GEN_RESOURCE)
        ilock_sa = None
        try:
            gen.preset()
            gen.set_power_dbm(drive_power_dbm)
            gen.set_frequency_hz(freq_hz)
            if carrier_off:
                gen.rf_off()  # explicit, don't rely on preset()'s default state
                print("[cw_odmr_lock_in] CARRIER OFF: RF output intentionally "
                      "disabled -- chop control line and lock-in demodulation "
                      "still running normally, testing for control-line pickup "
                      "with no real RF power present")
            else:
                gen.rf_on()
            time.sleep(1.0)  # let the initial frequency/level settle

            if auto_sensitivity:
                lia.auto_gain()
                time.sleep(5.0 * time_constant_s)
                actual_sensitivity_v = lia.get_sensitivity_v()
                print(f"[cw_odmr_lock_in] auto_sensitivity: AGAN selected "
                      f"{actual_sensitivity_v:.3e} V full scale")
                sensitivity_v = actual_sensitivity_v

            ilock_sa = HP8673H.try_connect_analyzer(SA_RESOURCE)
            if ilock_sa is None:
                gen.trip_interlock("spectrum analyzer not reachable at startup")
                return

            print(f"[cw_odmr_lock_in] step 3/3: reading every {sample_interval_s:.2f}s"
                  f"{f' for {duration_s:.0f}s' if duration_s else ' until Ctrl+C'}, "
                  f"threshold {threshold_dbm} dBm")

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

                    reflected_dbm = HP8673H.read_reflected_power_dbm(ilock_sa, freq_hz)
                    if reflected_dbm is None or reflected_dbm > threshold_dbm:
                        reason = (
                            "spectrum analyzer unreachable"
                            if reflected_dbm is None else
                            f"reflected power {reflected_dbm:.2f} dBm exceeds threshold "
                            f"{threshold_dbm} dBm"
                        )
                        gen.trip_interlock(f"{reason} at {freq_hz/1e9:.5f} GHz "
                                            f"(t={elapsed_s:.1f}s)")
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
                np.save(f"{run_path}/{file_name}_lockin_single_elapsed_s.npy",
                        np.array(elapsed_s_list))
                np.save(f"{run_path}/{file_name}_lockin_single_x.npy", np.array(x_list))
                np.save(f"{run_path}/{file_name}_lockin_single_y.npy", np.array(y_list))
                np.save(f"{run_path}/{file_name}_lockin_single_reflected_dbm.npy",
                        np.array(reflected_dbm_list))
                with open(f"{run_path}/{file_name}_lockin_single_metadata.txt", "w") as fh:
                    fh.write(f"freq_hz={freq_hz}\n")
                    fh.write(f"chop_freq_hz={chop_freq_hz}\n")
                    fh.write(f"chop_duty_pct={chop_duty_pct}\n")
                    fh.write(f"time_constant_s={time_constant_s}\n")
                    fh.write(f"sensitivity_v={sensitivity_v}\n")
                    fh.write(f"phase_deg={phase_deg}\n")
                    fh.write(f"drive_power_dbm={drive_power_dbm}\n")
                    fh.write(f"carrier_off={carrier_off}\n")
                print(f"[cw_odmr_lock_in] step 3/3 done"
                      f"{' (PARTIAL -- interlock tripped)' if tripped else ''}: "
                      f"saved {run_path}/{file_name}_lockin_single_elapsed_s.npy "
                      f"({n_completed} points), _lockin_single_x.npy, "
                      f"_lockin_single_y.npy, _lockin_single_reflected_dbm.npy, "
                      f"_lockin_single_metadata.txt")
        finally:
            print("[cw_odmr_lock_in] shutting down")
            try:
                gen.rf_off()
            except Exception as e:
                print(f"[cw_odmr_lock_in] WARNING: failed to turn off RF cleanly ({e})")
            try:
                gen.go_to_local()
            except Exception:
                pass
            gen.close()
            if ilock_sa is not None:
                ilock_sa.close()
            try:
                lia.go_to_local()
            except Exception:
                pass
            lia.close()

    print("[cw_odmr_lock_in] done")


def cmd_calibrate_osl(cal_dir, **kw):
    """
    Open-short-load scalar calibration for the resonance sweep (see
    hp8673h.py's calibrate_osl()/apply_osl_calibration() for what this does
    and its vector-vs-scalar limitation). Holds the switch static on RF2
    first, same as cmd_run() does before its own resonance sweep -- without
    this, the switch could still be left chopping from a previous run,
    which would corrupt the calibration sweep exactly the way it used to
    corrupt the coarse resonance sweep before that was fixed.

    Recognized key=value overrides (all optional):
      start_hz=2e9            calibration sweep start frequency
      stop_hz=3e9             calibration sweep stop frequency
      step_hz=6.7e6           calibration sweep step -- match whatever
                              coarse_step_hz you'll use for the real
                              resonance sweep
      drive_power_dbm=-40.0   calibration drive power -- kept low by
                              default since open/short present a near-total
                              reflection at the reference plane; don't
                              raise this without confirming the amplifier
                              tolerates that mismatch at the power you pick
    """
    start_hz = float(kw.get("start_hz", 2.0e9))
    stop_hz = float(kw.get("stop_hz", 3.0e9))
    step_hz = float(kw.get("step_hz", 6.7e6))
    drive_power_dbm = float(kw.get("drive_power_dbm", -40.0))

    print("[cw_odmr_lock_in] configuring AWG CH2 (static, not chopping) before calibration")
    awg = ks33600a.KS33600A(AWG_RESOURCE, debug=True)
    set_switch_static(awg, route_to_sample=True)
    awg.close()

    gen = HP8673H(GEN_RESOURCE)
    from e4403b import E4403B
    sa = E4403B(SA_RESOURCE)
    try:
        gen.calibrate_osl(sa, start_hz, stop_hz, step_hz, cal_dir,
                           drive_power_dbm=drive_power_dbm)
    finally:
        gen.go_to_local()
        gen.close()
        sa.go_to_local()
        sa.close()

    print(f"[cw_odmr_lock_in] done: calibration saved to {cal_dir}")


def main():
    if len(sys.argv) < 3:
        print(__doc__)
        raise SystemExit(1)

    command = sys.argv[1]
    file_name = sys.argv[2]
    extra = parse_kv_args(sys.argv[3:])

    if command == "run":
        cmd_run(file_name, **extra)
    elif command == "single":
        cmd_single(file_name, **extra)
    elif command == "calibrate-osl":
        cmd_calibrate_osl(file_name, **extra)
    else:
        raise SystemExit(f"unknown command {command!r} "
                          f"(expected 'run', 'single', or 'calibrate-osl')")


if __name__ == "__main__":
    main()
