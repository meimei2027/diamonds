"""
spd1305x.py -- driver for the Siglent SPD1305X single-channel programmable
DC power supply, used to power the RF amplifier during CW-ODMR
measurements.

NOT YET FULLY VERIFIED against real hardware. output_on()/output_off() use
"OUTP CH1,{ON|OFF}" -- even though this is a single-channel model, Siglent's
SPD1000X series shares firmware/command parsing with the multi-channel
SPD3303X, which requires a channel argument specifically for the OUTPut
command (VOLT/CURR setpoints do not need it, since there's only one level
to set on this model). Confirmed empirically that the plain "OUTP ON" form
(no channel argument) silently does nothing on this unit -- write() now
checks SYST:ERR? after every command and raises on any error, so a wrong
command shows up as a loud RuntimeError instead of a silent no-op like
that one did.
"""
import pyvisa

from visa_retry import call_with_reconnect


class SPD1305X:
    """Siglent SPD1305X single-channel programmable DC power supply."""

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
        print("SPD1305X: connected")

    def close(self):
        if self.inst is not None:
            self.inst.close()

    def go_to_local(self):
        """Return the instrument to front-panel (local) control."""
        self.write("SYST:LOCAL")

    def write(self, cmd):
        def _do():
            self.inst.write(cmd)
            err = self.inst.query("SYST:ERR?").strip()
            if self.debug:
                print(cmd + " => " + err)
            if not (err.startswith("0") or err.startswith("+0")):
                raise RuntimeError(f"SPD1305X error after {cmd!r}: {err}")
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
        voltage/current setting before these are applied.
        """
        self.set_voltage(voltage_v)
        self.set_current_limit(current_limit_a)
        self.output_on()
        print(f"SPD1305X: output ON at {voltage_v} V, {current_limit_a} A limit")

    def turn_off(self):
        self.output_off()
        print("SPD1305X: output OFF")
