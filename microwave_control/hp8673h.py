import pyvisa
import time

from visa_retry import call_with_reconnect


class HP8673H:
    """
    HP 8673H Synthesized Signal Generator.

    Pre-SCPI HP-IB instrument: commands are short ASCII mnemonics
    (e.g. "FR3GZ" sets CW frequency to 3 GHz), not SCPI strings.
    Program codes are from the HP 8673C/D Operating and Service Manual,
    Table 3-7 (same command set used across the whole 8673A/B/C/D/E/G/H family).
    """

    def __init__(self, resource="GPIB0::19::INSTR", timeout=5000, debug=False):
        self.resource = resource
        self.timeout = timeout
        self.rm = pyvisa.ResourceManager()
        self.inst = self.rm.open_resource(resource)

        self.inst.timeout = timeout
        self.debug = debug
        print("HP8673H: connected")

    def close(self):
        if self.inst is not None:
            self.inst.close()

    def write(self, cmd):
        def _do():
            self.inst.write(cmd)
            if self.debug:
                print(cmd)
        call_with_reconnect(self, _do)

    def read_active_parameter(self):
        """Address the generator to talk and read back its currently
        displayed parameter (e.g. the CW frequency in MHz)."""
        self.write("OA")
        return call_with_reconnect(self, lambda: self.inst.read().strip())

    def preset(self):
        """IP: Instrument Preset (RCL 0 equivalent)."""
        self.write("IP")

    def rf_on(self):
        self.write("RF1")

    def rf_off(self):
        self.write("RF0")

    def auto_peak(self, state=True):
        self.write("K1" if state else "K0")

    def set_power_dbm(self, power_dbm):
        self.write(f"PL{power_dbm:g}DB")

    def set_frequency_hz(self, freq_hz):
        """FR: set CW frequency."""
        self.write(f"FR{freq_hz:.0f}HZ")

    def set_freq_increment_hz(self, step_hz):
        """FI: set the FREQ INCR step size used by manual tune/step keys."""
        self.write(f"FI{step_hz:.0f}HZ")

    def setup_sweep(self, start_hz, stop_hz, num_steps=100, dwell_ms=20):
        """
        Configure a start/stop sweep using an explicit number of steps.
        Does not start the sweep -- call start_auto_sweep() /
        start_single_sweep() afterwards.
        """
        self.write(f"FA{start_hz:.0f}HZ")   # START sweep frequency
        self.write(f"FB{stop_hz:.0f}HZ")    # STOP sweep frequency
        self.write(f"{num_steps:d}SS")      # number of steps
        self.write(f"{dwell_ms:d}DWMS")     # dwell time per step

    def start_auto_sweep(self):
        """W2: repetitive AUTO sweep."""
        self.write("W2")

    def start_single_sweep(self):
        """W6: arm and begin one SINGLE sweep."""
        self.write("W6")

    def stop_sweep(self):
        """W0: disable sweep, return to CW."""
        self.write("W0")

    def go_to_local(self):
        self.inst.control_ren(pyvisa.constants.VI_GPIB_REN_ADDRESS_GTL)
