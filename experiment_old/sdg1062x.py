import numpy as np
import pyvisa


class SDG1062X:
    def __init__(self, resource_name, debug=False):
        rm = pyvisa.ResourceManager()
        self.inst = rm.open_resource(resource_name)
        self.inst.timeout = 20000
        self.inst.chunk_size = 1024 * 1024

        self.arb_name_ch1 = "ARB1"
        self.debug = debug
        self.write("*RST")
        self.MAX_SAMPLE_RATE = 30e6

        print("Siglent SDG1062X: connected")

    def close(self):
        if self.inst is not None:
            self.inst.close()

    def write(self, cmd):
        self.inst.write(cmd)
        if self.debug:
            print(cmd)

    def query(self, cmd):
        return self.inst.query(cmd).strip()

    def load_csv(self, csv_file):
        data = np.genfromtxt(csv_file, delimiter=",", skip_header=1)
        ch1 = np.asarray(data[:, 1], dtype=np.float32)
        return ch1

    def upload_waveform(self, waveform, arb_name=None, ch=1):
        if arb_name is None:
            arb_name = self.arb_name_ch1

        waveform = waveform * 32767
        self.write(f"C{ch}:WVDT DEL,{arb_name}")
        self.inst.write_binary_values(
            f"C{ch}:WVDT WVNM,{arb_name},WAVEDATA",
            waveform,
            datatype='h',
            is_big_endian=False
        )
        print(f"Siglent SDG1062X: uploaded arb to channel {ch}")

    def upload_csv(self, csv_file):
        waveform = self.load_csv(csv_file)
        self.upload_waveform(waveform, ch=1)

    def run(self, vpp=1.0, sample_rate=None):
        if sample_rate is None:
            sample_rate = self.MAX_SAMPLE_RATE

        ch = 1

        self.write(f"C{ch}:BSWV WVTP,ARB")
        self.write(f"C{ch}:ARWV NAME,{self.arb_name_ch1}")
        self.write(f"C{ch}:BSWV AMP,{vpp/5}")
        self.write(f"C{ch}:BSWV OFST,0")
        
        self.write(f"C{ch}:BTWV STATE,ON")
        self.write(f"C{ch}:BTWV TRSR,EXT")
        self.write(f"C{ch}:BTWV GATE_NCYC,NCYC")
        self.write(f"C{ch}:BTWV TIME,1")
        self.write(f"C{ch}:BTWV EDGE,RISE")
        self.write(f"C{ch}:BTWV PLRT,POS")
        self.write(f"C{ch}:SRATE MODE,TARB,VALUE,{sample_rate}")
        self.write(f"C{ch}:OUTP ON")

        # print(self.query(f"C{ch}:BTWV?"))
        # print(self.query(f"C{ch}:ARWV?"))
        # print(self.query(f"C{ch}:SRATE?"))

        print("Siglent SDG1062X: waiting for external trigger")

    def test(self):
        ch = 1
        self.write("*RST")
        self.write(f"C{ch}:BSWV WVTP,ARB")
        self.write(f"C{ch}:ARWV NAME,{self.arb_name_ch1}")
        self.write(f"C{ch}:BTWV STATE,OFF")
        self.write(f"C{ch}:BSWV AMP,1")
        self.write(f"C{ch}:BSWV OFST,0")
        self.write(f"C{ch}:SRATE MODE,TARB,VALUE,30e6")
        self.write(f"C{ch}:OUTP ON")
        print(self.query(f"C{ch}:SRATE?"))

