"""
t1_test.py -- all-optical T1 measurement.

Sequence per trigger (played on AWG channel 1, arb waveforms "polarize",
"dark", "readout"):

    polarize (10us)  ->  dark x N (1us each)  ->  readout (300ns)
                                                       |
                                          (fluorescence after evolving in
                                           the dark for N * 1us)

Sweeps N (the "dark" arb's repeat count -- total dark time = N * 1us, the
dark arb's fixed atomic duration) log-spaced from 1us to 1ms, saving each
point as its own `data/<N>us.npy` (+ _timetable.npy, _metadata.txt from
RTB2004.run()) for SEGMENTS segments. Fit an exponential to fluorescence vs.
dark time to get T1.

An earlier version added a second polarize+readout ("calibration") stage
after the main readout, meant as a fresh-polarization "fully bright"
reference to normalize the dark-time signal against. Removed -- turned out
not to be a useful reference: the calibration readout fires ~10.3us after
the main readout, by which point the laser has already been continuously on
for a while (through the rest of the main readout and all of the second
polarize segment), so it was actually measuring saturated steady-state
fluorescence, not a comparable fresh bright transient (confirmed for real:
the calibration trace showed no detectable pulse in the same window the
main readout's pulse was clearly visible in -- see notes.md). Since it
wasn't a valid reference anyway, each dark time is now just its own
single-readout measurement -- equivalent to what run_sweep() used to get
from only half its segments (the other half went to the now-removed
calibration reads), hence SEGMENTS dropping from 5000 to 2500 to keep the
same real per-point sample count.
"""
import sys
import time

import numpy as np

import ks33600a
import rtb2004
import generate_arb
from cw_odmr import parse_kv_args

AWG_RESOURCE = "USB0::0x0957::0x5707::MY53800810::INSTR"
SCOPE_RESOURCE = "USB0::0x0AAD::0x01D6::108904::INSTR"
DATA_DIR = "D:\\t1"
SEGMENTS = 2500
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

# Falloff-diagnostic-mode-only settings -- see run_falloff_diagnostic(). A
# constant turn-ON delay (measured by run_delay_diagnostic()) doesn't by
# itself limit how short a meaningful dark time can be -- it's just where to
# look for the signal. What actually matters is how fast the AOM stops
# diffracting light once told to turn OFF, which is a different quantity
# (same underlying acoustic-transit mechanism, but not necessarily the same
# number). This diagnostic measures that instead.
FALLOFF_DIAGNOSTIC_START_S = -2e-6  # a bit before turn-off, to see the
                                     # steady bright level for reference
FALLOFF_DIAGNOSTIC_OFF_US = 80      # total off-period length (repeat_count,
                                     # in us) -- must comfortably exceed the
                                     # ~32us window DIAGNOSTIC_SCALE_S gives,
                                     # or the sequence loops back to
                                     # "polarize" (turns back on) before the
                                     # decay is fully captured


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
    Build and upload a polarize -> dark -> readout sequence with the given
    dark-segment repeat count -- total dark time = dark_repeat_count *
    DARK_UNIT_S. Used by both run_sweep() and run_delay_diagnostic() --
    there's no separate calibration stage to make them differ (see the
    module docstring for why that was removed), so a single readout per
    loop means every captured segment is unambiguously that one pulse, no
    classification needed.

    sequence_name must be UNIQUE per distinct dark_repeat_count used within
    a single AWG session/connection -- re-uploading DATA:SEQ under a name
    that already exists does not appear to actually replace it (confirmed
    for real: a sweep across 1/2/3/4us dark times, all uploaded as "test",
    produced IDENTICAL inter-trigger timing in every file's timetable,
    matching only the first (1us) upload -- later "DATA:SEQ" calls silently
    had no effect. See notes.md. run_sweep() passes a name derived from the
    dark time to avoid this.
    """
    block = build_block_descriptor(sequence_name, [
        ["polarize", "1", "once", "lowAtStart", 10],
        ["dark", str(dark_repeat_count), "repeat", "lowAtStart", 10],
        ["readout", "1", "once", "highAtStart", 10],
    ])
    awg.write(f"DATA:SEQ {block}")


def upload_falloff_diagnostic_sequence(awg, off_duration_us, sequence_name="test_falloff"):
    """
    Build and upload a polarize -> dark sequence for run_falloff_diagnostic()
    -- polarize (bright, 10us) then dark (off_duration_us total, OFF), with
    the scope's trigger marker on the polarize->dark TRANSITION (t=0 = the
    AOM/RF drive told to turn off) rather than on a readout. Measures how
    long fluorescence actually takes to decay to baseline after turn-off --
    see FALLOFF_DIAGNOSTIC_START_S's comment / the conversation this was
    added for.

    The first 1us of the off period gets its own "once" segment carrying
    the "highAtStart" marker (mirrors how "readout" carries the marker in
    upload_sequence()) so the trigger fires exactly once per loop, right at
    turn-off; the rest of the off period is additional "repeat" dark
    segments with no marker, just extending how long the AOM stays off so
    the sequence doesn't loop back to "polarize" (turn back on) before the
    decay is fully captured.
    """
    off_duration_us = int(off_duration_us)
    block = build_block_descriptor(sequence_name, [
        ["polarize", "1", "once", "lowAtStart", 10],
        ["dark", "1", "once", "highAtStart", 10],
        ["dark", str(max(off_duration_us - 1, 1)), "repeat", "lowAtStart", 10],
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
            label = f"{dark_time_us}us"
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

    Uses the same upload_sequence() as run_sweep() (a single readout per
    loop, no calibration stage -- see the module docstring), so every
    captured segment is unambiguously that one pulse.

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
    upload_sequence(awg, dark_repeat_count=1, sequence_name="test_diagnostic")
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


def run_falloff_diagnostic():
    """
    Capture a WIDE window around the polarize->dark TRANSITION (t=0 = the
    AWG marker edge at the moment the RF drive to the AOM stops) to see how
    long the fluorescence signal actually takes to decay to baseline
    afterward -- the complementary measurement to run_delay_diagnostic()'s
    turn-ON delay.

    Why this, and not just the turn-on delay: a constant turn-on delay
    doesn't by itself limit how short a meaningful dark time can be, it just
    says where to look for the signal. What actually limits the shortest
    meaningful dark time is how fast the AOM stops diffracting light once
    told to turn off -- a dark period shorter than that turn-off transient
    wouldn't actually be dark, regardless of anything about the readout
    timing (see the conversation this was added for / notes.md).

    Uses upload_falloff_diagnostic_sequence() -- polarize (bright) -> dark
    (long enough to not loop back to polarize before the decay is fully
    captured), triggered on the polarize->dark transition itself rather
    than on a readout.

    Saves data/diagnostic_falloff.npy (+ _timetable.npy, _metadata.txt), and
    prints the exact window captured (start/end relative to the
    transition). Look at the averaged trace afterward to find how long
    after t=0 the signal actually settles back to (dark) baseline -- that's
    the real minimum meaningful dark time, not anything derived from the
    turn-on delay.
    """
    print("[t1_test] FALLOFF DIAGNOSTIC MODE: wide capture around the turn-off "
          "transition, to measure how fast fluorescence decays after the AOM "
          "is told to turn off -- not a T1 measurement")

    print("[t1_test] configuring AWG")
    awg = ks33600a.KS33600A(AWG_RESOURCE, debug=True)
    setup_awg_waveforms(awg)
    upload_falloff_diagnostic_sequence(awg, off_duration_us=FALLOFF_DIAGNOSTIC_OFF_US)
    setup_awg_output(awg, sequence_name="test_falloff")
    time.sleep(1.0)

    print("[t1_test] connecting to scope")
    scope = rtb2004.RTB2004(SCOPE_RESOURCE, timeout=100000, debug=True)

    try:
        sample_rate_hz, window_start_s, window_end_s = scope.run(
            segments=DIAGNOSTIC_SEGMENTS, path=DATA_DIR, name="diagnostic_falloff",
            start_s=FALLOFF_DIAGNOSTIC_START_S, scale_s=DIAGNOSTIC_SCALE_S,
        )
        print(f"[t1_test] captured {DATA_DIR}/diagnostic_falloff.npy: window "
              f"{window_start_s*1e6:+.2f} us to {window_end_s*1e6:+.2f} us "
              f"relative to the turn-off transition "
              f"({(window_end_s - window_start_s)*1e6:.2f} us wide, "
              f"{sample_rate_hz/1e9:.4f} GSa/s, {DIAGNOSTIC_SEGMENTS} segments)")
        print("[t1_test] next: average the segments and find how long after "
              "t=0 the signal actually settles back to (dark) baseline -- "
              "that's the shortest dark time that's still meaningfully dark.")
    finally:
        scope.close()
        awg.close()

    print("[t1_test] falloff diagnostic done")


def main():
    if len(sys.argv) > 1 and sys.argv[1] == "diagnostic":
        run_delay_diagnostic()
    elif len(sys.argv) > 1 and sys.argv[1] == "falloff":
        run_falloff_diagnostic()
    else:
        # e.g. `python t1_test.py segments=100` for a quick end-to-end check
        # before committing to the full SEGMENTS=2500 sweep, or
        # `python t1_test.py start_after_us=8` to resume a sweep partway
        # through (skips every dark time <= 8us -- useful if earlier points
        # already completed and you don't want to redo them).
        kv = parse_kv_args(sys.argv[1:])
        segments = int(kv.get("segments", SEGMENTS))
        start_after_us = float(kv.get("start_after_us", 0))

        dark_times_us = dark_time_sweep_us(1, 1000, num=30)
        dark_times_us = dark_times_us[dark_times_us > start_after_us]
        if len(dark_times_us) == 0:
            raise SystemExit(f"start_after_us={start_after_us} excludes every dark time "
                              f"in the sweep -- nothing to do")

        run_sweep(dark_times_us, segments=segments)


if __name__ == "__main__":
    main()
