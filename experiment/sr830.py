"""
sr830.py -- driver for the Stanford Research Systems SR830 DSP Lock-In
Amplifier.

Command mnemonics below are taken directly from the SR830 manual's
"Abridged Command List" (Chapter 1, page 1-7/1-8) -- see
https://www.thinksrs.com/products/sr830.html for the full manual. NOT YET
TESTED against real hardware.

Two things worth confirming empirically before relying on this:
  - reset() uses "*RST"/"*CLS" (standard IEEE-488.2 common commands, which
    most GPIB instruments including this one implement) -- the manual's own
    abridged command list happens to print the reset command as plain "RST"
    (no asterisk); if "*RST" doesn't behave as expected, try "RST" instead
    with debug=True to see the SR830's own error reporting.
  - The SENSITIVITY_V / TIME_CONSTANT_S range tables below follow the
    standard 1-2-5 / 1-3-10 sequences described in the manual's "Gain and
    Time Constant" command section (page 5-6) -- double check a couple of
    codes against a live front-panel read before trusting set_sensitivity_v()
    /set_time_constant_s()'s auto-selected values for anything safety-critical.

SAFETY (from the manual): the front-end amp is easily damaged by a
photomultiplier used improperly -- an unterminated PMT output cable can
charge up to several hundred volts in a short time, and connecting that
charged cable to the SR830's input can damage the front-end op-amps.
Always discharge the cable BEFORE connecting the PMT output to the SR830
input, and connect the PMT output to the SR830 before powering the PMT on.
"""
import pyvisa

from visa_retry import call_with_reconnect


class SR830:
    """Stanford Research Systems SR830 DSP Lock-In Amplifier."""

    # Sensitivity range codes (SENS) -> full-scale value in volts (or amps,
    # for the current-input modes) -- standard 1-2-5 sequence: code 0 = 2nV
    # through code 26 = 1V full scale (manual page 5-6).
    SENSITIVITY_V = [
        2e-9, 5e-9, 10e-9, 20e-9, 50e-9, 100e-9, 200e-9, 500e-9,
        1e-6, 2e-6, 5e-6, 10e-6, 20e-6, 50e-6, 100e-6, 200e-6, 500e-6,
        1e-3, 2e-3, 5e-3, 10e-3, 20e-3, 50e-3, 100e-3, 200e-3, 500e-3, 1.0,
    ]

    # Time constant range codes (OFLT) -> seconds -- standard 1-3-10
    # sequence: code 0 = 10us through code 19 = 30ks (manual page 5-6).
    TIME_CONSTANT_S = [
        10e-6, 30e-6, 100e-6, 300e-6, 1e-3, 3e-3, 10e-3, 30e-3, 100e-3,
        300e-3, 1.0, 3.0, 10.0, 30.0, 100.0, 300.0, 1e3, 3e3, 10e3, 30e3,
    ]

    # SNAP? parameter codes (manual page 5-15/5-16).
    _SNAP_CODES = {
        "x": 1, "y": 2, "r": 3, "theta": 4,
        "aux1": 5, "aux2": 6, "aux3": 7, "aux4": 8,
        "freq": 9, "ch1": 10, "ch2": 11,
    }

    def __init__(self, resource, timeout=5000, debug=False):
        self.resource = resource
        self.timeout = timeout
        self.write_termination = "\n"
        self.read_termination = "\n"

        self.rm = pyvisa.ResourceManager()
        self.inst = self.rm.open_resource(resource)
        self.inst.timeout = timeout
        self.inst.write_termination = self.write_termination
        self.inst.read_termination = self.read_termination
        self.debug = debug

        # Tell the instrument to send query responses over GPIB, not
        # RS232 -- without this, query() would hang waiting for a response
        # sent out the other port instead. OUTX 1 = GPIB.
        self.write("OUTX 1")
        print("SR830: connected")

    def close(self):
        if self.inst is not None:
            self.inst.close()

    def go_to_local(self):
        """Return the instrument to front-panel (local) control."""
        self.write("LOCL 0")

    def write(self, cmd):
        def _do():
            self.inst.write(cmd)
            if self.debug:
                err = self.inst.query("ERRS?").strip()
                if err != "0":
                    print(f"{cmd} => ERRS {err}")
        call_with_reconnect(self, _do)

    def query(self, cmd):
        return call_with_reconnect(self, lambda: self.inst.query(cmd).strip())

    def reset(self):
        self.write("*RST")
        self.write("*CLS")

    def idn(self):
        return self.query("*IDN?")

    # --- Reference and phase ---

    def set_phase_deg(self, phase_deg):
        self.write(f"PHAS {phase_deg}")

    def get_phase_deg(self):
        return float(self.query("PHAS?"))

    def set_reference_external(self, slope="sine"):
        """Lock to an external reference input. slope: 'sine' (trigger on
        the sine zero-crossing), 'ttl_rising', or 'ttl_falling'."""
        slope_codes = {"sine": 0, "ttl_rising": 1, "ttl_falling": 2}
        self.write("FMOD 0")
        self.write(f"RSLP {slope_codes[slope]}")

    def set_reference_internal(self, freq_hz):
        """Use the SR830's own internal oscillator as the reference --
        drive your chopper/switch from its sine output (SLVL sets the
        amplitude) or sync output."""
        self.write("FMOD 1")
        self.write(f"FREQ {freq_hz}")

    def get_frequency_hz(self):
        return float(self.query("FREQ?"))

    def set_harmonic(self, n):
        """Detect at the n-th harmonic of the reference (n=1 for the
        fundamental). n * reference_freq_hz must stay <= 102 kHz."""
        self.write(f"HARM {n}")

    def set_sine_output_vrms(self, vrms):
        """Internal reference's sine output amplitude, 0.004-5.000 Vrms."""
        self.write(f"SLVL {vrms}")

    # --- Input and filter ---

    def set_input_config(self, mode="a"):
        """mode: 'a' (single-ended voltage), 'a-b' (differential voltage),
        'i_1M' (current input, 1 Mohm conversion gain), or 'i_100M'
        (current input, 100 Mohm conversion gain)."""
        codes = {"a": 0, "a-b": 1, "i_1M": 2, "i_100M": 3}
        self.write(f"ISRC {codes[mode]}")

    def set_input_coupling(self, ac=True):
        self.write(f"ICPL {0 if ac else 1}")

    def set_input_grounding(self, grounded=True):
        self.write(f"IGND {1 if grounded else 0}")

    def set_line_notch_filters(self, mode="out"):
        """mode: 'out' (disabled), 'line' (notch at line frequency),
        '2xline' (notch at 2x line frequency), or 'both'."""
        codes = {"out": 0, "line": 1, "2xline": 2, "both": 3}
        self.write(f"ILIN {codes[mode]}")

    # --- Gain and time constant ---

    def set_sensitivity_v(self, target_v):
        """Set sensitivity to the smallest available full-scale range that
        is still >= target_v (the most sensitive range that shouldn't
        overload for a signal around that size). Returns the actual
        full-scale value selected, in volts (or amps, for current-input
        modes)."""
        for i, v in enumerate(self.SENSITIVITY_V):
            if v >= target_v:
                self.write(f"SENS {i}")
                return v
        self.write(f"SENS {len(self.SENSITIVITY_V) - 1}")
        return self.SENSITIVITY_V[-1]

    def set_dynamic_reserve(self, mode="normal"):
        """mode: 'high_reserve', 'normal', or 'low_noise'."""
        codes = {"high_reserve": 0, "normal": 1, "low_noise": 2}
        self.write(f"RMOD {codes[mode]}")

    def set_time_constant_s(self, target_s):
        """Set the time constant to the smallest available value that is
        still >= target_s, from TIME_CONSTANT_S. Returns the actual value
        selected, in seconds. Longer time constant = narrower detection
        bandwidth = better noise rejection, at the cost of slower response
        -- see the module this is used from for how it trades against your
        measurement's total acquisition time."""
        for i, t in enumerate(self.TIME_CONSTANT_S):
            if t >= target_s:
                self.write(f"OFLT {i}")
                return t
        self.write(f"OFLT {len(self.TIME_CONSTANT_S) - 1}")
        return self.TIME_CONSTANT_S[-1]

    def set_filter_slope_db_oct(self, slope=24):
        """slope: 6, 12, 18, or 24 dB/octave."""
        codes = {6: 0, 12: 1, 18: 2, 24: 3}
        self.write(f"OFSL {codes[slope]}")

    def set_synchronous_filter(self, enabled):
        """Only meaningful below 200 Hz reference frequency -- helps reject
        harmonics of the reference that OFLT's own rolloff alone doesn't
        fully suppress at low frequencies."""
        self.write(f"SYNC {1 if enabled else 0}")

    # --- Reading output values ---

    def read_xy(self):
        return self.snap("x", "y")

    def read_r_theta(self):
        return self.snap("r", "theta")

    def snap(self, *params):
        """
        Read 2-6 parameters simultaneously, all from the same internal
        sample (unlike separate OUTP? calls, which could straddle two
        different samples) -- params are any of 'x', 'y', 'r', 'theta',
        'aux1'-'aux4', 'freq', 'ch1', 'ch2'. Returns a tuple of floats in
        the order requested.
        """
        if not 2 <= len(params) <= 6:
            raise ValueError(f"snap() takes 2-6 parameters, got {len(params)}")
        arg = ",".join(str(self._SNAP_CODES[p]) for p in params)
        result = self.query(f"SNAP? {arg}")
        return tuple(float(v) for v in result.split(","))

    def get_sensitivity_v(self):
        """Read back the currently-selected sensitivity range, in volts (or
        amps for current-input modes) -- use this after auto_gain() to find
        out what it actually picked."""
        code = int(self.query("SENS?"))
        return self.SENSITIVITY_V[code]

    def read_overload_status(self):
        """
        Query the LIA status byte (LIAS?) and decode the overload bits, per
        the SR830 manual's status byte definition: bit 0 = input/reserve
        overload, bit 1 = filter (time constant) overload, bit 2 = output
        (X/Y/R) overload. NOT YET VERIFIED against the front-panel OVLD LED
        on this specific unit -- cross-check once on real hardware (force
        an overload deliberately, e.g. with sensitivity set far too
        sensitive for the current signal, and confirm this matches the LED)
        before trusting it unattended for anything safety-critical.

        Returns a dict: {"input": bool, "filter": bool, "output": bool,
        "any": bool} -- "any" is True if any of the three bits are set.
        """
        status = int(self.query("LIAS?"))
        input_overload = bool(status & 0x01)
        filter_overload = bool(status & 0x02)
        output_overload = bool(status & 0x04)
        return {
            "input": input_overload,
            "filter": filter_overload,
            "output": output_overload,
            "any": input_overload or filter_overload or output_overload,
        }

    def auto_gain(self):
        self.write("AGAN")

    def auto_reserve(self):
        self.write("ARSV")

    def auto_phase(self):
        self.write("APHS")
