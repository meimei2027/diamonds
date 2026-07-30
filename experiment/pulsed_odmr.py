"""
pulsed_odmr.py -- pulsed ODMR: sweeps microwave frequency while gating the
microwave into a short pulse (via a fast RF switch) inside the dark period
of a polarize -> dark -> readout laser cycle, instead of leaving the
microwave continuously on throughout the cycle the way cw_odmr.py's
run_spectrum() does. Pulsing the microwave (rather than driving it
continuously) avoids continuous-drive effects and can resolve a narrower
ODMR line than the CW measurement -- see the conversation this was written
for (cw_odmr.py's spectrum scans weren't showing a resolvable dip; pulsed
drive is one way to test whether that's a linewidth/resolution issue).

NOT YET TESTED against real hardware -- in particular, verify the two OPEN
ITEMs flagged below (channel synchronization drift, and the actual
switching truth table on your specific unit) before trusting data from
this.

Hardware
--------
New component: Mini-Circuits ZYSWA-2-50DR+, a 50-ohm SPDT ABSORPTIVE RF
switch (DC2-5000 MHz, +-5V dual supply, TTL-compatible control input,
~20ns switching time / ~5-6ns rise-fall -- negligible next to any pulse
duration used here, which will be >=100s of ns). Datasheet truth table:

    control LOW  (0-0.7V)   : RF IN (port 3) -> RF1 (port 2)
    control HIGH (2.1-5V)   : RF IN (port 3) -> RF2 (port 1)

Wiring:
  RF IN   (3) <- HP8673H microwave output (through the amplifier chain),
                run CONTINUOUSLY at a fixed CW frequency/power for each
                sweep point. The SWITCH does the pulsing here, not the
                generator -- toggling the generator's own RF output over
                GPIB takes on the order of seconds, far too slow for
                microsecond-scale gating.
  RF1     (2) -> a 50-ohm dummy load (the "dump" path). Absorptive, so the
                generator always sees a clean 50-ohm termination regardless
                of switch state -- unlike a reflective switch, there's no
                risk of reflecting unwanted power back at the amplifier
                while the pulse is off.
  RF2     (1) -> onward to the resonator/coupler (the "sample" path) --
                where the MW goes DURING the pulse.
  Control (4) <- AWG CH2 (see "AWG channel layout" below).
  +5V/-5V     <- a SEPARATE external dual-rail bench supply -- NOT derived
                from the AWG or the generator. Power this up (and confirm
                a clean +-5V) before applying RF or the control signal.
  Max RF input power: +31 dBm (500-5000 MHz). The amplifier's own measured
                compression point at 0 dBm drive was ~27.4 dBm output
                (see new_amplifier_gain.ipynb) -- ~3.6 dB of margin, but
                confirm the ACTUAL power reaching RF IN before connecting,
                especially if drive_power_dbm is raised from the default.

OPEN ITEM: the truth table above is this switch's datasheet default;
double check it against the label/silkscreen on the physical unit and a
quick continuity/RF check before relying on it -- getting it backwards
would mean the MW pulse goes to the dump and the "off" period goes to the
sample, silently inverting the whole measurement.

AWG channel layout
-------------------
CH1: laser/AOM control -- polarize -> dark -> readout, same building blocks
     as t1_test.py (10us bright polarize, dark_us of dark, 300ns bright
     readout), self-triggered (TRIG1:SOUR IMM) and looped forever. The
     "readout" segment carries the marker that triggers the scope, exactly
     as in t1_test.py. Unlike t1_test.py, dark_us is FIXED for the whole
     frequency sweep (only the generator's frequency changes per point), so
     there's no per-point re-upload here.
CH2: MW gate -- a SEPARATE DATA:SEQ sequence built to have EXACTLY the same
     total duration as CH1's, idling LOW (MW routed to the dump) except for
     a pulse_us-long HIGH pulse (MW routed to the sample) starting
     pulse_start_us into the dark period. Also self-triggered
     (TRIG2:SOUR IMM) and looped forever. Output configured for a 0-5V
     swing into a high-impedance load (OUTP2:LOAD INF), comfortably inside
     the datasheet's control-voltage LOW/HIGH thresholds.

Synchronization: OUTPUT1 ON and OUTPUT2 ON are two separate SCPI writes, so
there's no guarantee the two channels' free-running sequences start on the
exact same sample clock edge -- left alone, the MW pulse could drift
relative to the dark window over many loops (the scan can run for tens of
minutes). Keysight's Trueform series SCPI has
[SOURce[1|2]:]PHASe:SYNChronize specifically for this: sent once both
channels are configured and running, it establishes a common sample-clock
reference point between the two channels of ONE instrument (each channel
keeps its own phase/offset -- only the alignment between them changes).
Called once here, right after both outputs are turned on, before the
frequency sweep starts.

OPEN ITEM (verify before trusting data): confirm PHASe:SYNChronize actually
keeps the two sequences locked over the FULL scan duration on this specific
instrument/firmware -- e.g. scope CH1's marker/sync output against CH2's
gate output on two scope channels simultaneously at the start and end of a
long scan and check the pulse hasn't drifted relative to the dark window.
If it has, re-sending PHASe:SYNChronize periodically (e.g. once per sweep
point, right after each frequency change) would be a low-cost mitigation.
Also unverified: whether "SOUR2:DATA:SEQ" (used below to target channel 2's
sequence -- t1_test.py's proven CH1 usage omits any "SOUR1:" prefix, since
that appears to be this instrument's default target) is actually the
correct way to address channel 2 for this specific command on this
firmware; confirm with debug=True (see ks33600a.py's write(), which now
raises on any SCPI error) before assuming it silently worked.

Usage:
    python pulsed_odmr.py run <file_name> [key=value ...]
        Sweeps microwave frequency across [freq_start_hz, freq_stop_hz],
        gating a short MW pulse into the dark period each cycle, and
        records the readout-window signal at each frequency -- same
        interlock/safety design as cw_odmr.py's run_spectrum (inline
        reflected-power check per point; generator kept at a fixed CW
        power/frequency between checks, only the frequency changes).
        Saves data/<file_name>/<file_name>_pulsed_spectrum.npy (shape
        (num_freq_points, segments_per_point, 10000)),
        _pulsed_spectrum_freqs_hz.npy, _pulsed_spectrum_reflected_dbm.npy,
        _pulsed_spectrum_metadata.txt.

        Recognized key=value overrides (all optional):
          freq_start_hz=2.82e9    sweep start frequency
          freq_stop_hz=2.92e9     sweep stop frequency
          freq_step_hz=200e3      sweep step
          drive_power_dbm=0.0     generator CW power for the whole scan
          threshold_dbm=-10.0     interlock trip threshold, in dBm
          segments_per_point=100  scope segments captured at each frequency
          dark_us=10.0            total dark period length, in us
          pulse_start_us=4.0      MW pulse start, us into the dark period
          pulse_us=4.0            MW pulse duration (tau_MW), in us --
                                   pulse_start_us + pulse_us must not
                                   exceed dark_us
          settle_s=0.05           settle time after each frequency change
                                   -- NEVER 0, see hp8673h.py's
                                   frequency_sweep() docstring for why

Example:
    python pulsed_odmr.py run pulsed1 freq_start_hz=2.82e9 freq_stop_hz=2.92e9
"""
import sys
import time

import numpy as np

import ks33600a
import rtb2004
import generate_arb
from hp8673h import HP8673H
from e4403b import E4403B
from cw_odmr import parse_kv_args, _tee_stdout

AWG_RESOURCE = "USB0::0x0957::0x5707::MY53800810::INSTR"
SCOPE_RESOURCE = "USB0::0x0AAD::0x01D6::108904::INSTR"
# See notes.md -- GPIB bus numbering isn't stable, confirm with
# pyvisa.ResourceManager().list_resources() if these don't match.
GEN_RESOURCE = "GPIB1::19::INSTR"
SA_RESOURCE = "GPIB0::18::INSTR"

DATA_DIR = "D:\\pulsed_odmr"

POLARIZE_US = 10.0  # same convention as t1_test.py
READOUT_US = 0.3
SCOPE_START_S = -0.5e-6  # see t1_test.py's SCOPE_START_S for why -- same
                          # AOM/PMT delay applies here, same readout arb


def build_block_descriptor(sequence_name, segments):
    """Same DATA:SEQ block-descriptor builder as t1_test.py."""
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


def setup_awg_waveforms(awg, dark_us, pulse_start_us, pulse_us):
    """
    Upload CH1's laser arbs (polarize/dark/readout, same as t1_test.py) and
    CH2's MW-gate arbs (gate_pre/gate_high/gate_post spanning the same
    dark_us window, plus gate segments matching polarize/readout so CH2's
    total sequence duration exactly matches CH1's).
    """
    fs = 1e9

    def us_to_samples(duration_us):
        # round(), not int()/truncation -- duration_us * 1e-6 * fs can land
        # a hair under the intended integer (e.g. 3.9999999999999996 instead
        # of 4.0) purely from float representation error, and truncating
        # that silently drops a sample. Matters a lot here specifically:
        # dark_samples MUST equal pre_samples + high_samples + post_samples
        # exactly, or CH1's dark segment and CH2's pre/high/post segments
        # drift out of step by one sample -- confirmed for real by an
        # offline test before this was fixed (10/4/2us -> 20300 vs 20299
        # total samples between the two channels).
        return max(1, round(duration_us * 1e-6 * fs))

    def rf(freq, n_samples):
        t = np.arange(n_samples) / fs
        return t, np.sin(2 * np.pi * freq * t).astype(np.float32)

    def const(n_samples, value):
        return np.full(n_samples, value, dtype=np.float32)

    dark_samples = us_to_samples(dark_us)
    pulse_pre_samples = us_to_samples(pulse_start_us)
    pulse_high_samples = us_to_samples(pulse_us)
    pulse_post_samples = dark_samples - pulse_pre_samples - pulse_high_samples
    if pulse_post_samples < 0:
        raise ValueError(
            f"pulse_start_us + pulse_us ({pulse_start_us + pulse_us}) exceeds "
            f"dark_us ({dark_us}) -- the MW pulse must fit inside the dark period"
        )

    # CH1: laser/AOM control -- same building blocks/upload path as
    # t1_test.py (through a CSV file, arb_name_1 targets channel 1).
    t_pol, ch_pol = rf(80e6, us_to_samples(POLARIZE_US))
    t_ro, ch_ro = rf(80e6, us_to_samples(READOUT_US))
    ch_dark = const(dark_samples, 0.0)
    t_dark = np.arange(len(ch_dark)) / fs

    generate_arb.write_csv("waveforms/pulsed_polarize.csv", t_pol, ch_pol)
    generate_arb.write_csv("waveforms/pulsed_dark.csv", t_dark, ch_dark)
    generate_arb.write_csv("waveforms/pulsed_readout.csv", t_ro, ch_ro)
    awg.upload_csv("waveforms/pulsed_polarize.csv", sample_rate=fs, ch2_exists=False,
                    arb_name_1="polarize")
    awg.upload_csv("waveforms/pulsed_dark.csv", sample_rate=fs, ch2_exists=False,
                    arb_name_1="dark")
    awg.upload_csv("waveforms/pulsed_readout.csv", sample_rate=fs, ch2_exists=False,
                    arb_name_1="readout")

    # CH2: MW gate -- +1 normalized = HIGH (control routes RF IN -> RF2,
    # the sample path), -1 normalized = LOW (control routes RF IN -> RF1,
    # the dump path) -- see setup_awg_output() for how these map to real
    # 0-5V. Uploaded directly via upload_waveform() (bypassing the CSV
    # round-trip, and upload_csv()'s hardcoded ch=1 for the single-column
    # case) so each arb lands on channel 2.
    awg.upload_waveform(const(us_to_samples(POLARIZE_US), -1.0), arb_name="gate_pol",
                         ch=2, sample_rate=fs)
    awg.upload_waveform(const(pulse_pre_samples, -1.0), arb_name="gate_pre",
                         ch=2, sample_rate=fs)
    awg.upload_waveform(const(pulse_high_samples, 1.0), arb_name="gate_high",
                         ch=2, sample_rate=fs)
    awg.upload_waveform(const(pulse_post_samples, -1.0), arb_name="gate_post",
                         ch=2, sample_rate=fs)
    awg.upload_waveform(const(us_to_samples(READOUT_US), -1.0), arb_name="gate_ro",
                         ch=2, sample_rate=fs)


def upload_sequences(awg, sequence_name_ch1="pulsed", sequence_name_ch2="pulsed_gate"):
    block1 = build_block_descriptor(sequence_name_ch1, [
        ["polarize", "1", "once", "lowAtStart", 10],
        ["dark", "1", "once", "lowAtStart", 10],
        ["readout", "1", "once", "highAtStart", 10],
    ])
    awg.write(f"DATA:SEQ {block1}")  # unprefixed -> channel 1, matching
                                       # t1_test.py's proven convention

    block2 = build_block_descriptor(sequence_name_ch2, [
        ["gate_pol", "1", "once", "lowAtStart", 10],
        ["gate_pre", "1", "once", "lowAtStart", 10],
        ["gate_high", "1", "once", "lowAtStart", 10],
        ["gate_post", "1", "once", "lowAtStart", 10],
        ["gate_ro", "1", "once", "lowAtStart", 10],
    ])
    awg.write(f"SOUR2:DATA:SEQ {block2}")  # explicit channel-2 prefix --
                                             # see module docstring's OPEN
                                             # ITEM about this being unverified


def setup_awg_output(awg, sequence_name_ch1="pulsed", sequence_name_ch2="pulsed_gate"):
    # CH1: laser/AOM drive, same convention as t1_test.py.
    awg.write("OUTP1:LOAD 50")
    awg.write("SOUR1:FUNC:ARB:PTP 0.632")
    awg.write("SOUR1:FUNC:ARB:SRAT 1e9")
    awg.write(f'SOUR1:FUNC:ARB "{sequence_name_ch1}"')
    awg.write("SOUR1:FUNC ARB")
    awg.write("OUTPUT1 ON")
    awg.write("TRIG1:SOUR IMM")

    # CH2: MW gate -- 0-5V swing into a high-impedance load (the switch's
    # CMOS control input draws very little current, per the datasheet),
    # comfortably inside the control-voltage LOW (<=0.7V) / HIGH (>=2.1V)
    # thresholds.
    awg.write("OUTP2:LOAD INF")
    awg.write("SOUR2:FUNC:ARB:PTP 5.0")
    awg.write("SOUR2:VOLT:OFFS 2.5")
    awg.write("SOUR2:FUNC:ARB:SRAT 1e9")
    awg.write(f'SOUR2:FUNC:ARB "{sequence_name_ch2}"')
    awg.write("SOUR2:FUNC ARB")
    awg.write("OUTPUT2 ON")
    awg.write("TRIG2:SOUR IMM")

    # Align the two channels' sample-clock zero points now that both are
    # running -- see the module docstring's "AWG channel synchronization"
    # section (including its OPEN ITEM: this has NOT been verified to hold
    # over a full scan on real hardware yet).
    awg.write("PHASe:SYNChronize")


def cmd_run(file_name, **kw):
    freq_start_hz = float(kw.get("freq_start_hz", 2.82e9))
    freq_stop_hz = float(kw.get("freq_stop_hz", 2.92e9))
    freq_step_hz = float(kw.get("freq_step_hz", 200e3))
    drive_power_dbm = float(kw.get("drive_power_dbm", 0.0))
    threshold_dbm = float(kw.get("threshold_dbm", -10.0))
    segments_per_point = int(kw.get("segments_per_point", 100))
    dark_us = float(kw.get("dark_us", 10.0))
    pulse_start_us = float(kw.get("pulse_start_us", 4.0))
    pulse_us = float(kw.get("pulse_us", 4.0))  # tau_MW
    settle_s = float(kw.get("settle_s", 0.05))  # NEVER 0 -- see
                                                  # hp8673h.py's
                                                  # frequency_sweep()
                                                  # docstring

    run_path = f"{DATA_DIR}/{file_name}"
    import os
    os.makedirs(run_path, exist_ok=True)
    log_dir = f"{DATA_DIR}/logs"
    os.makedirs(log_dir, exist_ok=True)
    log_path = f"{log_dir}/{file_name}_pulsed_spectrum.txt"

    with _tee_stdout(log_path):
        print("[pulsed_odmr] step 1/4: configuring AWG (laser sequence + MW gate)")
        awg = ks33600a.KS33600A(AWG_RESOURCE, debug=True)
        setup_awg_waveforms(awg, dark_us, pulse_start_us, pulse_us)
        upload_sequences(awg)
        setup_awg_output(awg)
        awg.close()
        print(f"[pulsed_odmr] step 1/4 done: {dark_us}us dark period, "
              f"{pulse_us}us MW pulse starting {pulse_start_us}us into it")

        print("[pulsed_odmr] step 2/4: connecting to HP8673H + E4403B")
        gen = HP8673H(GEN_RESOURCE)
        ilock_sa = None
        scope = None
        try:
            gen.preset()
            gen.set_power_dbm(drive_power_dbm)
            gen.set_frequency_hz(freq_start_hz)
            gen.rf_on()
            time.sleep(1.0)  # let the initial frequency/level settle

            ilock_sa = HP8673H.try_connect_analyzer(SA_RESOURCE)
            if ilock_sa is None:
                gen.trip_interlock("spectrum analyzer not reachable at startup")
                return
            print("[pulsed_odmr] step 2/4 done")

            freqs_hz = np.arange(freq_start_hz, freq_stop_hz + freq_step_hz / 2,
                                  freq_step_hz)
            est_bytes = len(freqs_hz) * segments_per_point * rtb2004.RTB2004.NUM_SAMPLES * 4
            print(f"[pulsed_odmr] step 3/4: pulsed spectrum scan "
                  f"{freqs_hz[0]/1e9:.5f}-{freqs_hz[-1]/1e9:.5f} GHz "
                  f"({len(freqs_hz)} points, {freq_step_hz/1e3:.1f} kHz step), "
                  f"{segments_per_point} segments/point, drive {drive_power_dbm} dBm, "
                  f"threshold {threshold_dbm} dBm, estimated size {est_bytes/1e6:.0f} MB")

            scope = rtb2004.RTB2004(SCOPE_RESOURCE, timeout=100000)
            combined = np.empty((len(freqs_hz), segments_per_point,
                                  rtb2004.RTB2004.NUM_SAMPLES), dtype=np.float32)
            reflected_dbm_arr = np.full(len(freqs_hz), np.nan)
            sample_rate_hz = None
            t0_s = None
            n_completed = 0

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

                segs, sample_rate_hz, t0_s = scope.acquire_segments_to_memory(
                    segments=segments_per_point, ch=1, start_s=SCOPE_START_S,
                )
                combined[i] = segs
                n_completed = i + 1
                print(f"[pulsed_odmr] point {n_completed}/{len(freqs_hz)}: "
                      f"f={f/1e9:.5f} GHz, reflected={reflected_dbm:.2f} dBm")

            tripped = n_completed < len(freqs_hz)
            combined = combined[:n_completed]
            freqs_hz = freqs_hz[:n_completed]
            reflected_dbm_arr = reflected_dbm_arr[:n_completed]

            if n_completed == 0:
                print("[pulsed_odmr] step 3/4 FAILED: no points completed -- nothing to save")
            else:
                np.save(f"{run_path}/{file_name}_pulsed_spectrum.npy", combined)
                np.save(f"{run_path}/{file_name}_pulsed_spectrum_freqs_hz.npy", freqs_hz)
                np.save(f"{run_path}/{file_name}_pulsed_spectrum_reflected_dbm.npy",
                        reflected_dbm_arr)
                with open(f"{run_path}/{file_name}_pulsed_spectrum_metadata.txt", "w") as fh:
                    fh.write(f"sample_rate_hz={sample_rate_hz}\n")
                    fh.write(f"t0_s={t0_s}\n")
                    fh.write(f"num_points={rtb2004.RTB2004.NUM_SAMPLES}\n")
                    fh.write(f"dark_us={dark_us}\n")
                    fh.write(f"pulse_start_us={pulse_start_us}\n")
                    fh.write(f"pulse_us={pulse_us}\n")
                print(f"[pulsed_odmr] step 3/4 done"
                      f"{' (PARTIAL -- interlock tripped)' if tripped else ''}: "
                      f"saved {run_path}/{file_name}_pulsed_spectrum.npy "
                      f"({n_completed} points), _pulsed_spectrum_freqs_hz.npy, "
                      f"_pulsed_spectrum_reflected_dbm.npy, _pulsed_spectrum_metadata.txt")
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
            if ilock_sa is not None:
                ilock_sa.close()
            if scope is not None:
                scope.close()

    print("[pulsed_odmr] done")


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
