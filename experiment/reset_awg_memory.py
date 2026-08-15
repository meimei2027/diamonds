"""
Standalone recovery script for the Keysight 33600A AWG's "Arb: Out of
memory" error (SCPI error +880), e.g.:

    RuntimeError: KS33600A error after 'DATA:SEQ #294"pulsed_odmr_ch1",
    "anchor",1,onceWaitTrig,maintain,10,"ch1_combined",800000,repeat,
    maintain,10': +880,"Arb: Out of memory"

KS33600A.__init__() already does this same reset+clear on every fresh
connection (see ks33600a.py) -- so this normally isn't needed. But if a
run dies mid-sequence with the instrument still holding accumulated arb
waveform data (e.g. a crashed process that never got to close() cleanly,
or several waveforms uploaded across manual/REPL experimentation without
reconnecting), the leftover volatile memory persists on the INSTRUMENT
itself, independent of any new script's connection -- run this once to
clear it before retrying.

Usage:
    python reset_awg_memory.py
"""
import sys

import ks33600a
from rabi import AWG_RESOURCE


def main():
    print(f"Connecting to AWG at {AWG_RESOURCE} ...")
    awg = ks33600a.KS33600A(AWG_RESOURCE, debug=True)

    try:
        # KS33600A.__init__() already ran clear_error_queue() + reset() +
        # SOUR{1,2}:DATA:VOL:CLE once on connect -- repeat the volatile-
        # memory clear explicitly here anyway (cheap, and makes this
        # script's intent self-evident rather than relying on a side
        # effect of __init__).
        print("Clearing CH1/CH2 volatile arb waveform memory ...")
        awg.write("SOUR1:DATA:VOL:CLE")
        awg.write("SOUR2:DATA:VOL:CLE")

        # Confirm no arb waveforms remain defined on either channel.
        for ch in (1, 2):
            names = awg.query(f"SOUR{ch}:DATA:VOL:CAT?")
            print(f"CH{ch} volatile catalog after clear: {names}")

        print("AWG memory reset complete -- safe to retry the failed command.")
    finally:
        awg.close()


if __name__ == "__main__":
    sys.exit(main())
