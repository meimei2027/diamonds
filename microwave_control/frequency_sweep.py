import time
import numpy as np


def frequency_sweep(gen, sa, start_hz, stop_hz, step_hz, power_dbm=-20, settle_s=0.0,
                     initial_settle_s=1.0):
    """
    Step the HP8673H's CW frequency from start_hz to stop_hz (inclusive) in
    step_hz increments, reading the E4403B's marker amplitude at each point.

    Assumes the generator output is fed into the analyzer input (e.g. via a
    coupler) and that the analyzer's span already covers [start_hz, stop_hz].

    initial_settle_s is a one-time delay after jumping to the sweep's start
    frequency, before any measurement is taken. The HP8673H's manual notes
    that AUTO PEAK re-leveling triggers on any frequency change > 50 MHz --
    the initial jump from wherever the generator was previously set to
    start_hz is almost always >50 MHz, and needs much more time to settle
    than the small step-to-step increments during the sweep itself. Without
    this, the first few points of a sweep can show a spurious dip that has
    nothing to do with whatever is connected to the generator.

    Returns (freqs_hz, power_dbm_array).
    """
    freqs_hz = np.arange(start_hz, stop_hz + step_hz / 2, step_hz)

    gen.preset()
    gen.set_power_dbm(power_dbm)
    gen.set_frequency_hz(start_hz)
    gen.rf_on()
    time.sleep(initial_settle_s)

    sa.write("TRAC1:MODE WRITE")
    sa.write("AVER:STATE OFF")
    sa.write("INIT:CONT OFF")
    sa.write("CALC:MARK1:MODE POS")

    readings_dbm = np.empty(len(freqs_hz))

    for i, f in enumerate(freqs_hz):
        gen.set_frequency_hz(f)
        if settle_s:
            time.sleep(settle_s)
        sa.write("INIT:IMM")
        sa.query("*OPC?")
        sa.write(f"CALC:MARK1:X {f}")
        readings_dbm[i] = float(sa.query("CALC:MARK1:Y?"))

    gen.rf_off()

    return freqs_hz, readings_dbm


def frequency_sweep_full_trace(gen, sa, start_hz, stop_hz, step_hz,
                                power_dbm=-20, rbw_hz=None):
    """
    Step the HP8673H's CW frequency in step_hz increments from start_hz to
    stop_hz (inclusive). At each step, retune the E4403B to center on that
    frequency (span = step_hz, so adjacent windows tile with no gaps) and
    capture its FULL trace, rather than a single marker reading.

    Useful for seeing more than just the programmed tone -- spurs,
    harmonics, and the analyzer's own noise floor away from the peak.

    Returns a list of (freqs_hz, power_dbm) tuples, one per step.
    """
    centers_hz = np.arange(start_hz, stop_hz + step_hz / 2, step_hz)

    gen.preset()
    gen.set_power_dbm(power_dbm)
    gen.rf_on()

    sa.write("TRAC1:MODE WRITE")
    sa.write("AVER:STATE OFF")
    if rbw_hz is not None:
        sa.write(f"BAND {rbw_hz}")
    sa.write("INIT:CONT OFF")

    traces = []
    for center in centers_hz:
        gen.set_frequency_hz(center)
        sa.set_center_span(center, step_hz)
        sa.write("INIT:IMM")
        sa.query("*OPC?")
        traces.append(sa.get_trace(1))

    gen.rf_off()

    return traces


def concatenate_traces(traces):
    """Flatten a list of (freqs_hz, power_dbm) tuples from adjacent,
    tiled analyzer windows into single sorted arrays."""
    freqs_hz = np.concatenate([f for f, _ in traces])
    power_dbm = np.concatenate([p for _, p in traces])
    order = np.argsort(freqs_hz)
    return freqs_hz[order], power_dbm[order]
