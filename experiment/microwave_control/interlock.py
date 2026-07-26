"""
Safety interlock for continuous CW operation at a fixed (resonance) frequency.

Intended use: once a resonance has been located and the generator is left
running continuously at that frequency (with an amplifier downstream),
this script watches reflected power at that exact frequency and
immediately kills RF output if either:

  (a) the spectrum analyzer can't be reached (GPIB dropout, powered off,
      disconnected, etc.) -- if we can't verify reflected power is safe,
      assume it isn't, and shut down.
  (b) reflected power at the operating frequency exceeds a threshold --
      e.g. the resonance has drifted away from the operating frequency,
      so what used to be a safe dip is now closer to full reflection.

This script owns turning RF on/off once monitoring starts -- don't drive
RF on/off from anywhere else while it's running.
"""
import os
import sys
import time
import argparse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from hp8673h import HP8673H
from e4403b import E4403B


def try_connect_analyzer(resource):
    """Attempt to connect to the spectrum analyzer and confirm it actually
    responds (not just that the resource opened). Returns an E4403B
    instance, or None if the analyzer can't be reached."""
    try:
        sa = E4403B(resource)
        sa.query("*IDN?")
        return sa
    except Exception as e:
        print(f"[interlock] cannot reach spectrum analyzer: {e}")
        return None


def read_reflected_power_dbm(sa, freq_hz, span_hz=10e6):
    """Read reflected power at freq_hz. Returns None if the read fails for
    any reason -- treated as 'analyzer unreachable' by the caller."""
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


def trip_interlock(gen, reason):
    """Immediately kill RF output and report why."""
    print(f"[interlock] TRIPPED: {reason}")
    try:
        gen.rf_off()
        print("[interlock] RF output OFF")
    except Exception as e:
        print(f"[interlock] WARNING: failed to turn off RF output cleanly: {e}")


def monitor(gen, sa_resource, freq_hz, threshold_dbm, poll_interval_s=1.0,
            max_missed_reads=1):
    """
    Continuously monitor reflected power at freq_hz. Trips (shuts off RF
    and returns a reason string) if:
      (a) the analyzer can't be reached (at startup, or for
          max_missed_reads consecutive polls), or
      (b) reflected power exceeds threshold_dbm.
    """
    print(f"[interlock] monitoring {freq_hz/1e9:.4f} GHz, "
          f"threshold {threshold_dbm} dBm, polling every {poll_interval_s}s")

    sa = try_connect_analyzer(sa_resource)
    if sa is None:
        trip_interlock(gen, "spectrum analyzer not reachable at startup")
        return "analyzer_unreachable"

    missed = 0
    try:
        while True:
            power_dbm = read_reflected_power_dbm(sa, freq_hz)

            if power_dbm is None:
                missed += 1
                print(f"[interlock] analyzer read failed ({missed}/{max_missed_reads})")
                if missed >= max_missed_reads:
                    trip_interlock(gen, "spectrum analyzer unreachable")
                    return "analyzer_unreachable"
                time.sleep(poll_interval_s)
                continue

            missed = 0
            print(f"[interlock] reflected power: {power_dbm:.2f} dBm")

            if power_dbm > threshold_dbm:
                trip_interlock(
                    gen,
                    f"reflected power {power_dbm:.2f} dBm exceeds threshold {threshold_dbm} dBm",
                )
                return "overpower"

            time.sleep(poll_interval_s)
    finally:
        sa.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--gen-resource", default="GPIB1::19::INSTR")
    parser.add_argument("--sa-resource", default="GPIB0::18::INSTR")
    parser.add_argument("--freq-hz", type=float, default=2.51e9,
                         help="operating (resonance) frequency in Hz")
    parser.add_argument("--power-dbm", type=float, default=0.0,
                         help="generator output power in dBm")
    parser.add_argument("--threshold-dbm", type=float, default=-40.0,
                         help="reflected power trip threshold in dBm")
    parser.add_argument("--poll-interval-s", type=float, default=1.0)
    args = parser.parse_args()

    gen = HP8673H(args.gen_resource)
    gen.preset()
    gen.set_frequency_hz(args.freq_hz)
    gen.set_power_dbm(args.power_dbm)
    gen.rf_on()
    time.sleep(1.0)  # let the initial frequency/level settle before monitoring

    try:
        reason = monitor(
            gen, args.sa_resource, args.freq_hz, args.threshold_dbm,
            poll_interval_s=args.poll_interval_s,
        )
        print(f"[interlock] stopped: {reason}")
    finally:
        gen.close()


if __name__ == "__main__":
    main()
