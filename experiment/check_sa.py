"""
check_sa_calibration.py -- verify the E4403B's amplitude calibration against
a known source (HP8673H connected directly to the analyzer, no cable loss
beyond the direct connection).

Steps through a range of generator power levels at a fixed frequency and
compares what the analyzer reads back. A healthy analyzer should show a
small, constant offset across the whole range -- on our unit, consistently
-4.4 to -4.5 dB (ordinary cable loss, not a fault). A large offset, an
offset that changes with power (not constant), or SYST:ERR? reporting
652 "Connect Calibration Output to Input" all indicate the analyzer needs
re-aligning -- see notes.md for the fix (front panel Align Now -> RF, with
AMPTD REF OUT cabled to INPUT 50 Ohm, and the analyzer must be warmed up
~5 minutes first -- aligning cold does not fix it).

Usage:
    python check_sa_calibration.py [freq_hz]

    freq_hz defaults to 2.5e9 if not given. notes.md mentions the fault has
    been worse at some frequencies than others, so it's worth spot-checking
    a few different frequencies, not just one.
"""
import sys
import time

from hp8673h import HP8673H
from e4403b import E4403B

# See notes.md -- GPIB bus numbering isn't stable, confirm with
# pyvisa.ResourceManager().list_resources() if these don't match.
GEN_RESOURCE = "GPIB1::19::INSTR"
SA_RESOURCE = "GPIB0::18::INSTR"

POWERS_DBM = [-40, -30, -20, -10, -5, 0]

# Expected healthy offset (ordinary cable loss for a direct generator-to-
# analyzer connection) -- see notes.md. Flagged if a reading's offset
# strays far from this or if the offsets aren't roughly constant across
# the power range.
EXPECTED_OFFSET_DB = -4.4
OFFSET_TOLERANCE_DB = 2.0


def main():
    freq_hz = float(sys.argv[1]) if len(sys.argv) > 1 else 2.5e9

    gen = HP8673H(GEN_RESOURCE, debug=True)
    sa = E4403B(SA_RESOURCE, debug=True)

    sa.set_center_span(freq_hz, 10e6)
    sa.write("TRAC1:MODE WRITE")
    sa.write("AVER:STATE OFF")
    sa.write("INIT:CONT OFF")
    sa.write("CALC:MARK1:MODE POS")
    sa.write(f"CALC:MARK1:X {freq_hz}")

    gen.preset()
    gen.set_frequency_hz(freq_hz)
    gen.rf_on()

    print(f"\nfrequency: {freq_hz/1e9:.4f} GHz")
    print(f"{'set (dBm)':>10} {'measured (dBm)':>15} {'delta (dB)':>12}")
    deltas = []
    for power_dbm in POWERS_DBM:
        gen.set_power_dbm(power_dbm)
        time.sleep(0.5)
        sa.write("INIT:IMM")
        sa.query("*OPC?")
        measured = float(sa.query("CALC:MARK1:Y?"))
        delta = measured - power_dbm
        deltas.append(delta)
        print(f"{power_dbm:>10.1f} {measured:>15.2f} {delta:>12.2f}")

    gen.rf_off()
    gen.go_to_local()
    gen.close()
    sa.go_to_local()
    sa.close()

    spread = max(deltas) - min(deltas)
    mean_delta = sum(deltas) / len(deltas)
    print(f"\nmean delta: {mean_delta:.2f} dB, spread across power range: {spread:.2f} dB")

    if spread > 2.0:
        print("WARNING: delta is not roughly constant across the power range -- "
              "this looks like a real calibration/overload fault, not just cable "
              "loss. See notes.md for the Align Now -> RF fix (must be warmed up "
              "~5 min first).")
    elif abs(mean_delta - EXPECTED_OFFSET_DB) > OFFSET_TOLERANCE_DB:
        print(f"WARNING: delta is constant but far from the expected healthy "
              f"~{EXPECTED_OFFSET_DB} dB cable-loss figure -- worth double-checking "
              f"the physical setup (cable/connectors) before assuming a cal fault.")
    else:
        print("Looks healthy -- constant offset consistent with normal cable loss.")


if __name__ == "__main__":
    main()
