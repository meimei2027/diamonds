"""
t1_test.py -- all-optical T1 measurement.

Sequence per trigger (played on AWG channel 1, arb waveforms "polarize",
"dark", "readout"):

    polarize (10us)  ->  dark x N (1us each)  ->  readout (300ns)
                                                       |
                                          (dark-time signal: fluorescence
                                           after evolving in the dark for
                                           N * 1us, weaker for larger N as
                                           population relaxes out of ms=0)

    -> polarize (10us) -> readout (300ns)
                              |
                 (calibration/reference: fluorescence right after a fresh
                  polarization, i.e. at ~zero dark time -- the "fully
                  bright" reference to normalize the dark-time signal
                  against)

Both readouts are captured within the same scope trace/segment. Sweeps N
(the "dark" arb's repeat count -- total dark time = N * 1us, the dark arb's
fixed atomic duration) log-spaced from 1us to 100us, saving each point as its
own `data/<N>us_calib.npy` (+ _timetable.npy, _metadata.txt from
RTB2004.run()) for 5000 segments. Fit an exponential to the dark-time signal
(normalized by the calibration reading, segment by segment) vs. dark time to
get T1.
"""
import sys
import time

import numpy as np

import ks33600a
import rtb2004
import generate_arb

AWG_RESOURCE = "USB0::0x0957::0x5707::MY53800810::INSTR"
SCOPE_RESOURCE = "USB0::0x0AAD::0x01D6::108904::INSTR"
DATA_DIR = "D:\\t1"
SEGMENTS = 5000
SCOPE_START_S = -0.5e-6  # passed through to RTB2004.run()'s start_s arg --
                          # where the acquired segment starts, relative to
                          # the trigger. Set from the diagnostic measurement
                          # (t1_test.py diagnostic / t1_test.ipynb): the
                          # readout pulse rises at ~0.39us after the trigger
                          # and peaks at ~0.6us -- starting at +1e-6 (the old
                          # value) opened the window AFTER the peak, missing
                          # the rise entirely. -0.5e-6 opens a bit before the
                          # trigger for a baseline reference, then covers the
                          # rise/peak/decay within the 4us window that
                          # follows. Re-check with the diagnostic if the
                          # AOM/PMT delay changes (different alignment,
                          # different AOM, etc.).
DARK_UNIT_S = 1e-6  # the "dark" arb waveform's fixed duration -- total dark
                     # time = repeat_count * DARK_UNIT_S, so only integer
                     # microsecond dark times are actually achievable

# Diagnostic-mode-only settings -- see run_delay_diagnostic().
DIAGNOSTIC_START_S = -5e-6   # look before the trigger too, to see baseline
DIAGNOSTIC_SCALE_S = 2e-6    # ~20x coarser than the normal 1e-7, to widen
                              # the captured window well beyond the readout
                              # pulse's own 300ns, at the cost of resolution
DIAGNOSTIC_SEGMENTS = 100    # just enough to average out shot noise for a
                              # quick look -- not a real measurement


def build_block_descriptor(sequence_name, segments):
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


def dark_time_sweep_us(start_us=1, stop_us=100, num=14):
    """
    Log-spaced dark times (in us) between start_us and stop_us, rounded to
    the nearest achievable integer microsecond (see DARK_UNIT_S) with
    duplicates from rounding removed -- so the returned array can have
    fewer than num points, especially near the low end where log spacing
    puts points closer together than 1us apart. That's expected, not a bug.
    """
    raw = np.geomspace(start_us, stop_us, num)
    rounded = np.round(raw).astype(int)
    return np.unique(rounded)


def setup_awg_waveforms(awg):
    """
    Upload the polarization/readout/dark arb waveforms once -- these don't
    depend on dark time, only the sequence's "dark" repeat_count does (set
    per sweep point by upload_sequence()).
    """
    fs = 1e9

    def rf(freq, duration):
        n = int(duration * fs)
        t = np.arange(n) / fs
        return t, np.sin(2 * np.pi * freq * t).astype(np.float32)

    def zeros(duration):
        n = int(duration * fs)
        t = np.arange(n) / fs
        return t, np.zeros(n, dtype=np.float32)

    t_polarization, ch_polarization = rf(77e6, 10e-6)
    t_readout, ch_readout = rf(77e6, 300e-9)
    t_dark, ch_dark = zeros(DARK_UNIT_S)

    generate_arb.write_csv("waveforms/polarization.csv", t_polarization, ch_polarization)
    generate_arb.write_csv("waveforms/readout.csv", t_readout, ch_readout)
    generate_arb.write_csv("waveforms/dark.csv", t_dark, ch_dark)

    awg.upload_csv("waveforms/polarization.csv", sample_rate=fs, ch2_exists=False,
                    arb_name_1="polarize")
    awg.upload_csv("waveforms/dark.csv", sample_rate=fs, ch2_exists=False,
                    arb_name_1="dark")
    awg.upload_csv("waveforms/readout.csv", sample_rate=fs, ch2_exists=False,
                    arb_name_1="readout")


def upload_sequence(awg, dark_repeat_count, sequence_name="test"):
    """
    Build and upload a sequence with the given dark-segment repeat count --
    total dark time = dark_repeat_count * DARK_UNIT_S.

    sequence_name must be UNIQUE per distinct dark_repeat_count used within
    a single AWG session/connection -- re-uploading DATA:SEQ under a name
    that already exists does not appear to actually replace it (confirmed
    for real: a sweep across 1/2/3/4us dark times, all uploaded as "test",
    produced IDENTICAL inter-trigger timing in every file's timetable,
    matching only the first (1us) upload -- later "DATA:SEQ" calls silently
    had no effect. See notes.md. run_sweep() passes a name derived from the
    dark time to avoid this.

    Both readout segments use the same "highAtStart" marker, so the scope's
    EXT TRIG can't tell them apart -- fine for run_sweep() (both readouts
    are captured, whichever it triggers on, and are told apart afterward by
    their position within each segment's trace), but NOT fine for the
    single-readout delay diagnostic -- see upload_diagnostic_sequence().
    """
    block = build_block_descriptor(sequence_name, [
        ["polarize", "1", "once", "lowAtStart", 10],
        ["dark", str(dark_repeat_count), "repeat", "lowAtStart", 10],
        ["readout", "1", "once", "highAtStart", 10],
        ["polarize", "1", "once", "lowAtStart", 10],  # calibration measurement
        ["readout", "1", "once", "highAtStart", 10],
    ])
    awg.write(f"DATA:SEQ {block}")


def upload_diagnostic_sequence(awg, dark_repeat_count, sequence_name="test_diagnostic"):
    """
    Simplified sequence for run_delay_diagnostic() only: polarize -> dark ->
    readout, with NO second polarize+readout ("calibration") stage.

    upload_sequence()'s full 5-part sequence has two readout events per
    loop, both marked "highAtStart" -- the scope's EXT TRIG can't
    distinguish which one it's capturing, and a real diagnostic capture's
    timetable showed a perfectly uniform inter-trigger spacing that doesn't
    match either "always the same readout" or "evenly alternating between
    both" (see notes.md), so it's genuinely unclear which pulse(s) that data
    represented. This sequence has only one readout, so every captured
    segment is unambiguously that same pulse -- there's nothing else it
    could be.

    See upload_sequence()'s docstring re: sequence_name needing to be
    unique per distinct dark_repeat_count -- not an issue for the
    diagnostic today since it's only ever called once per AWG connection,
    but named explicitly here to avoid colliding with run_sweep()'s "test".
    """
    block = build_block_descriptor(sequence_name, [
        ["polarize", "1", "once", "lowAtStart", 10],
        ["dark", str(dark_repeat_count), "repeat", "lowAtStart", 10],
        ["readout", "1", "once", "highAtStart", 10],
    ])
    awg.write(f"DATA:SEQ {block}")


def setup_awg_output(awg, sequence_name="test"):
    awg.write("OUTP1:LOAD 50")
    awg.write("SOUR1:FUNC:ARB:PTP 0.632")
    awg.write("SOUR1:FUNC:ARB:SRAT 1e9")
    awg.write(f'SOUR1:FUNC:ARB "{sequence_name}"')
    awg.write("SOUR1:FUNC ARB")
    awg.write("OUTPUT1 ON")
    awg.write("TRIG1:SOUR IMM")


def run_sweep(dark_times_us, segments=SEGMENTS):
    print(f"[t1_test] dark time sweep: {list(dark_times_us)} us "
          f"({len(dark_times_us)} points, {segments} segments each)")

    print("[t1_test] configuring AWG")
    awg = ks33600a.KS33600A(AWG_RESOURCE, debug=True)
    setup_awg_waveforms(awg)

    print("[t1_test] connecting to scope")
    scope = rtb2004.RTB2004(SCOPE_RESOURCE, timeout=100000, debug=True)

    try:
        for i, dark_time_us in enumerate(dark_times_us):
            label = f"{dark_time_us}us_calib"
            sequence_name = f"test_{dark_time_us}us"
            print(f"[t1_test] point {i + 1}/{len(dark_times_us)}: "
                  f"dark time = {dark_time_us} us -- saving as '{label}'")

            upload_sequence(awg, dark_repeat_count=int(dark_time_us), sequence_name=sequence_name)
            setup_awg_output(awg, sequence_name=sequence_name)
            time.sleep(1.0)  # let the new sequence/output settle before acquiring

            scope.run(segments=segments, path=DATA_DIR, name=label,
                      start_s=SCOPE_START_S)
            print(f"[t1_test] point {i + 1}/{len(dark_times_us)} done: saved "
                  f"{DATA_DIR}/{label}.npy, {label}_timetable.npy, {label}_metadata.txt")
    finally:
        scope.close()
        awg.close()

    print("[t1_test] sweep done")


def run_delay_diagnostic():
    """
    Capture a WIDE window around the readout-pulse trigger (t=0 = the AWG
    marker edge at the very start of the 300ns RF burst that drives the
    AOM) to see how long after that edge the actual fluorescence shows up
    at the PMT -- i.e. measure the combined AOM-turn-on + acoustic-transit +
    PMT-response + cabling delay directly from data, instead of guessing
    from datasheets (see the conversation this was added for).

    Uses a fixed, short dark time (1us) -- dark time doesn't matter for
    this measurement, we're only locating where the readout pulse's optical
    response lands relative to its own trigger edge, not measuring T1.

    Uses upload_diagnostic_sequence() (polarize -> dark -> readout, no
    calibration stage) rather than the normal upload_sequence() -- the
    normal sequence has two identically-marked readout events per loop, so
    the scope's EXT TRIG can't tell which one it's capturing on any given
    segment (confirmed for real: a capture using the normal sequence showed
    a perfectly uniform inter-trigger spacing that doesn't match either
    "always the same readout" or "evenly alternating between both" -- see
    notes.md). With only one readout in the sequence, every captured
    segment is unambiguously that same pulse.

    Saves data/diagnostic_delay.npy (+ _timetable.npy, _metadata.txt), and
    prints the exact window captured (start/end relative to the trigger).
    Look at the averaged trace (mean across segments) afterward to find
    where the fluorescence actually rises -- that's the real delay. Use it
    to set SCOPE_START_S so run_sweep()'s much narrower (4us) window
    actually contains the pulse instead of missing it.
    """
    print("[t1_test] DIAGNOSTIC MODE: wide capture around the readout trigger, "
          "to measure AOM/PMT delay -- not a T1 measurement")

    print("[t1_test] configuring AWG")
    awg = ks33600a.KS33600A(AWG_RESOURCE, debug=True)
    setup_awg_waveforms(awg)
    upload_diagnostic_sequence(awg, dark_repeat_count=1)
    setup_awg_output(awg)
    time.sleep(1.0)

    print("[t1_test] connecting to scope")
    scope = rtb2004.RTB2004(SCOPE_RESOURCE, timeout=100000, debug=True)

    try:
        sample_rate_hz, window_start_s, window_end_s = scope.run(
            segments=DIAGNOSTIC_SEGMENTS, path=DATA_DIR, name="diagnostic_delay",
            start_s=DIAGNOSTIC_START_S, scale_s=DIAGNOSTIC_SCALE_S,
        )
        print(f"[t1_test] captured {DATA_DIR}/diagnostic_delay.npy: window "
              f"{window_start_s*1e6:+.2f} us to {window_end_s*1e6:+.2f} us "
              f"relative to the readout trigger "
              f"({(window_end_s - window_start_s)*1e6:.2f} us wide, "
              f"{sample_rate_hz/1e9:.4f} GSa/s, {DIAGNOSTIC_SEGMENTS} segments)")
        print("[t1_test] next: average the segments and find where the "
              "fluorescence actually rises -- that offset (relative to t=0) "
              "is the real AOM/PMT delay. Set SCOPE_START_S to something a "
              "bit before that so run_sweep()'s 4us window contains it.")
    finally:
        scope.close()
        awg.close()

    print("[t1_test] diagnostic done")


def main():
    if len(sys.argv) > 1 and sys.argv[1] == "diagnostic":
        run_delay_diagnostic()
    else:
        # Optional segment-count override, e.g. `python t1_test.py 100` for
        # a quick end-to-end check across all dark times before committing
        # to the full SEGMENTS=5000 sweep.
        segments = int(sys.argv[1]) if len(sys.argv) > 1 else SEGMENTS
        run_sweep(dark_time_sweep_us(1, 100, num=14), segments=segments)


if __name__ == "__main__":
    main()
