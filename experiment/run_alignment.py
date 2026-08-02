"""
run_alignment.py -- AWG-driven amplitude-modulated tone for optical/RF
alignment.

CH1 outputs an 80 MHz, 632 mVpp sine wave (the usual alignment tone level
-- see ks33600a.py's own KS33600A.run_alignment() method, which this
supersedes for cases where you want CH2 live as its own real output too,
rather than just the AWG's internal-only AM oscillator). CH2 outputs a
10 Hz square wave and ALSO serves as CH1's AM modulation source, via the
AWG's internal channel-to-channel routing (SOUR1:AM:SOUR CH2) -- no
external loopback cable between CH2's output and CH1's EXT MOD input
needed. Both channels are simultaneously live, independent outputs.

NOT YET VERIFIED against real hardware -- "CH1"/"CH2" as literal AM:SOURce
tokens (selecting the other channel as an internal modulation source,
distinct from INTernal/EXTernal) is a documented Trueform/33600-series
feature, but hasn't been confirmed on this specific unit yet. ks33600a.py's
write() raises immediately on any SCPI error, so a syntax problem here will
surface clearly rather than silently failing.

Usage:
    python run_alignment.py
"""
import ks33600a

AWG_RESOURCE = "USB0::0x0957::0x5707::MY53800810::INSTR"


def run_alignment(carrier_freq_hz=80e6, carrier_vpp=0.632,
                   mod_freq_hz=10.0, mod_vpp=5.0, mod_offset_v=2.5,
                   am_depth_pct=100.0):
    awg = ks33600a.KS33600A(AWG_RESOURCE, debug=True)
    try:
        awg.write("TRIG:SOUR IMM")

        # CH1: carrier tone, into 50 ohm (matches KS33600A.run_alignment()).
        awg.write("OUTP1:LOAD 50")
        awg.write("SOUR1:FUNC SIN")
        awg.write(f"SOUR1:FREQ {carrier_freq_hz}")
        awg.write("SOUR1:VOLT:UNIT VPP")
        awg.write(f"SOUR1:VOLT {carrier_vpp}")

        # CH2: its own live square-wave output (usable standalone, e.g. as
        # a scope trigger reference during alignment) -- TTL-ish levels,
        # same convention as cw_odmr_lock_in.py's chop square wave.
        awg.write("OUTP2:LOAD INF")
        awg.write("SOUR2:FUNC SQU")
        awg.write(f"SOUR2:FREQ {mod_freq_hz}")
        awg.write("SOUR2:VOLT:UNIT VPP")
        awg.write(f"SOUR2:VOLT {mod_vpp}")
        awg.write(f"SOUR2:VOLT:OFFS {mod_offset_v}")

        # CH1 AM, sourced from CH2's own waveform internally -- no external
        # loopback cable from CH2's output to CH1's EXT MOD input needed.
        awg.write("SOUR1:AM:STAT OFF")
        awg.write("SOUR1:AM:SOUR CH2")
        awg.write(f"SOUR1:AM:DEPT {am_depth_pct}")
        awg.write("SOUR1:AM:STAT ON")

        awg.write("OUTPUT1 ON")
        awg.write("OUTPUT2 ON")

        print(f"[run_alignment] CH1: {carrier_freq_hz/1e6:.1f} MHz sine, "
              f"{carrier_vpp*1e3:.0f} mVpp, AM depth {am_depth_pct:.0f}%, "
              f"modulation sourced from CH2 (no external cable)")
        print(f"[run_alignment] CH2: {mod_freq_hz:.1f} Hz square wave, "
              f"{mod_vpp:.1f} Vpp / {mod_offset_v:.1f} V offset")
        print("[run_alignment] both channels ON")
    finally:
        awg.close()


if __name__ == "__main__":
    run_alignment()
