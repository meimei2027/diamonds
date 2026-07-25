import pyvisa
import numpy as np

from visa_retry import call_with_reconnect


class E4403B:
    """
    HP/Agilent E4403B ESA-E Series Spectrum Analyzer.

    Unlike the 8673H, this is a full SCPI instrument.
    """

    def __init__(self, resource="GPIB0::18::INSTR", timeout=20000, debug=False):
        self.resource = resource
        self.timeout = timeout
        self.write_termination = '\n'
        self.read_termination = '\n'

        self.rm = pyvisa.ResourceManager()
        self.inst = self.rm.open_resource(resource)

        self.inst.timeout = timeout
        self.inst.write_termination = self.write_termination
        self.inst.read_termination = self.read_termination
        self.debug = debug

        self.write("FORM REAL,32")     # binary float trace transfer
        self.write("FORM:BORD SWAP")   # little-endian
        print("E4403B: connected")

    def close(self):
        if self.inst is not None:
            self.inst.close()

    def write(self, cmd):
        def _do():
            self.inst.write(cmd)
            if self.debug:
                message = self.inst.query("SYST:ERR?")
                if message[0] != "0":
                    print(cmd + " => " + message)
        call_with_reconnect(self, _do)

    def query(self, cmd):
        return call_with_reconnect(self, lambda: self.inst.query(cmd).strip())

    def reset(self):
        self.write("*RST")
        self.write("*CLS")

    def set_center_span(self, center_hz, span_hz):
        self.write(f"FREQ:CENT {center_hz}")
        self.write(f"FREQ:SPAN {span_hz}")

    def set_start_stop(self, start_hz, stop_hz):
        self.write(f"FREQ:STAR {start_hz}")
        self.write(f"FREQ:STOP {stop_hz}")

    def get_freq_span(self):
        center = float(self.query("FREQ:CENT?"))
        span = float(self.query("FREQ:SPAN?"))
        return center, span

    def setup_averaging(self, count=20):
        self.write("AVER:STATE ON")
        self.write(f"AVER:COUNT {count}")

    def clear_averaging(self):
        self.write("AVER:STATE OFF")

    def single_sweep(self, n=1):
        """Trigger n sweeps (e.g. n=averaging count) and block until done."""
        self.write("INIT:CONT OFF")
        for _ in range(n):
            self.write("INIT:IMM")
            self.query("*OPC?")

    def get_freq_axis(self, npoints):
        center, span = self.get_freq_span()
        start_freq = center - span / 2
        stop_freq = center + span / 2
        return np.linspace(start_freq, stop_freq, npoints)

    def get_trace(self, trace=1):
        raw = call_with_reconnect(self, lambda: self.inst.query_binary_values(
            f"TRACE:DATA? TRACE{trace}",
            datatype='f',
            is_big_endian=False,
        ))
        trace_data = np.array(raw)
        freqs = self.get_freq_axis(len(trace_data))
        return freqs, trace_data

    def save_trace(self, freqs, trace_data, filepath):
        data = np.column_stack((freqs, trace_data))
        np.savetxt(
            filepath,
            data,
            delimiter=",",
            header="frequency_hz,amplitude_dbm",
            comments="",
        )
