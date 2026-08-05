"""
spd1168x.py -- driver for the Siglent SPD1168X single-channel programmable
DC power supply, used to drive the static-field coil (field is proportional
to applied current) for NV alignment/biasing.

Modeled on spd1305x.py -- same Siglent SPD1000X-series firmware/command set
as the SPD1305X, so OUTPut still takes a "CH1" channel argument even though
there's only one channel on this model.
"""
import numpy as np
import pyvisa

from visa_retry import call_with_reconnect

# Calibration data measured on our coil: (voltage_v, current_a, field_g).
# Field grows ~linearly with current (coil is just a resistor, well below
# saturation over this range), so we fit straight lines for
# field-vs-current and voltage-vs-current and invert the first to go from
# a desired field to a target current.
#
# Voltage column updated after swapping the coil cable for one with
# different resistance (was ~0.34 ohm, now ~0.5 ohm) -- field-vs-current is
# a property of the coil itself, not the cable, so the field column is
# unchanged from the original calibration.
_COIL_CALIBRATION = [
    (0.243, 0.5, 7.0),
    (0.484, 1.0, 14.3),
    (0.723, 1.5, 21.3),
    (0.961, 2.0, 28.4),
]
_CAL_VOLTAGE = np.array([v for v, i, g in _COIL_CALIBRATION])
_CAL_CURRENT = np.array([i for v, i, g in _COIL_CALIBRATION])
_CAL_FIELD = np.array([g for v, i, g in _COIL_CALIBRATION])

# field_g = FIELD_PER_AMP * current_a + FIELD_INTERCEPT
FIELD_PER_AMP, FIELD_INTERCEPT = np.polyfit(_CAL_CURRENT, _CAL_FIELD, 1)
# volts = COIL_RESISTANCE * current_a + VOLTAGE_INTERCEPT
COIL_RESISTANCE, VOLTAGE_INTERCEPT = np.polyfit(_CAL_CURRENT, _CAL_VOLTAGE, 1)


def current_for_field(field_g):
    """Invert the field-vs-current calibration fit to get the current
    (in amps) needed to produce the desired field (in gauss)."""
    return (field_g - FIELD_INTERCEPT) / FIELD_PER_AMP


def voltage_for_current(current_a):
    """Expected coil voltage drop (in volts) at the given current, from
    the voltage-vs-current calibration fit."""
    return COIL_RESISTANCE * current_a + VOLTAGE_INTERCEPT


class SPD1168X:
    """Siglent SPD1168X single-channel programmable DC power supply."""

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
        print("SPD1168X: connected")

    def close(self):
        if self.inst is not None:
            self.inst.close()

    def write(self, cmd):
        def _do():
            self.inst.write(cmd)
            err = self.inst.query("SYST:ERR?").strip()
            if self.debug:
                print(cmd + " => " + err)
            if not (err.startswith("0") or err.startswith("+0")):
                raise RuntimeError(f"SPD1168X error after {cmd!r}: {err}")
        call_with_reconnect(self, _do)

    def query(self, cmd):
        return call_with_reconnect(self, lambda: self.inst.query(cmd).strip())

    def idn(self):
        return self.query("*IDN?")

    # --- Setpoints ---

    def set_voltage(self, volts):
        self.write(f"VOLT {volts}")

    def get_voltage_setpoint(self):
        return float(self.query("VOLT?"))

    def set_current_limit(self, amps):
        self.write(f"CURR {amps}")

    def get_current_limit_setpoint(self):
        return float(self.query("CURR?"))

    # --- Measured output (not the setpoint) ---

    def read_voltage(self):
        return float(self.query("MEAS:VOLT?"))

    def read_current(self):
        return float(self.query("MEAS:CURR?"))

    # --- Output enable ---

    def output_on(self):
        self.write("OUTP CH1,ON")

    def output_off(self):
        self.write("OUTP CH1,OFF")

    def turn_on(self, voltage_v, current_limit_a):
        """
        Set voltage and current limit BEFORE enabling output, then enable
        it -- avoids a power-on transient where the output could
        momentarily come up at some other (e.g. a previous session's)
        voltage/current setting before these are applied. Important here
        since the coil field tracks current directly.
        """
        self.set_voltage(voltage_v)
        self.set_current_limit(current_limit_a)
        self.output_on()
        print(f"SPD1168X: output ON at {voltage_v} V, {current_limit_a} A limit")

    def turn_off(self):
        self.output_off()
        print("SPD1168X: output OFF")

    # --- Coil field control ---

    def set_field(self, field_g, voltage_margin=1.2):
        """
        Turn on the coil at the current needed to produce field_g (gauss),
        per the field-vs-current calibration fit above.

        The current limit is set to exactly the target current, and the
        voltage setpoint is set to voltage_margin (default 20%) above the
        calibration's expected voltage drop at that current. This puts the
        supply in constant-current mode: it regulates to the requested
        current (and hence field) and lets the actual voltage float to
        whatever the coil needs, rather than the field being at the mercy
        of a fixed voltage setpoint and the coil's exact resistance.

        Only supports field_g >= 0 -- this is a single supply driving the
        coil in one direction, so it can't produce a negative/reversed
        field.
        """
        if field_g < 0:
            raise ValueError("SPD1168X: set_field() only supports field_g >= 0 "
                              "(coil current can't be reversed by this supply)")
        current_a = current_for_field(field_g)
        voltage_v = voltage_for_current(current_a) * voltage_margin
        self.turn_on(voltage_v, current_a)
        print(f"SPD1168X: field set to {field_g} G (I={current_a:.4f} A, "
              f"V={voltage_v:.4f} V)")
