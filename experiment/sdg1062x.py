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
        data = np.genfromtxt(csv_file, delimiter=",")
        waveform = np.asarray(data, dtype=np.float32)
        return waveform

    def upload_waveform(self, waveform, arb_name=None, ch=1):

        if arb_name is None:
            arb_name = self.arb_name_ch1

        # normalize to [-1, 1]
        waveform = waveform / np.max(np.abs(waveform))

        # convert to int16
        waveform_i16 = np.int16(waveform * 32767)

        # delete old waveform
        self.write(f"C{ch}:WVDT DEL,{arb_name}")

        # upload waveform
        self.inst.write_binary_values(
            f"C{ch}:WVDT WVNM,{arb_name},WAVEDATA,",
            waveform_i16,
            datatype='h',
            is_big_endian=False
        )

        print(f"Siglent SDG1062X: uploaded arb to channel {ch}")

    def upload_csv(self, csv_file):
        waveform = self.load_csv(csv_file)
        self.upload_waveform(waveform, ch=1)

    def run(self, vpp=1.0, sample_rate=1e6):

        ch = 1

        # arbitrary waveform mode
        self.write(f"C{ch}:BSWV WVTP,ARB")
        self.write(f"C{ch}:ARWV NAME,{self.arb_name_ch1}")

        # sample rate
        self.write(f"C{ch}:SRATE MODE,TARB,VALUE,{sample_rate}")

        # amplitude / offset
        self.write(f"C{ch}:BSWV AMP,{vpp}")
        self.write(f"C{ch}:BSWV OFST,0")

        # external trigger burst
        self.write(f"C{ch}:BTWV STATE,ON")
        self.write(f"C{ch}:BTWV TRSR,EXT")
        self.write(f"C{ch}:BTWV GATE_NCYC,NCYC")
        self.write(f"C{ch}:BTWV TIME,1")

        # output on
        self.write(f"C{ch}:OUTP ON")

        print("Siglent SDG1062X: waiting for external trigger")