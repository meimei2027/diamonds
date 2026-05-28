import numpy as np
import pyvisa


class KS33600A:
    def __init__(self, resource_name, debug=False):
        rm = pyvisa.ResourceManager()
        self.inst = rm.open_resource(resource_name)
        self.inst.timeout = 20000
        self.inst.chunk_size = 1024 * 1024
        self.MAX_SAMPLE_RATE = 1e9
        self.arb_name_prefix = "ARB_CH"
        self.arb_name_ch1 = "ARB_CH1"
        self.arb_name_ch2 = "ARB_CH2"
        self.debug = debug
        print("Keysight 33600A: connected")

    def close(self):
        if self.inst is not None:
            self.inst.close()

    def write(self, cmd):
        self.inst.write(cmd)
        if self.debug: 
            print(cmd + " => " + self.inst.query("SYST:ERR?"))

    def query(self, cmd):
        return self.inst.query(cmd).strip()


    def load_csv(self, csv_file):
        data = np.genfromtxt(csv_file, delimiter=",", skip_header=1)
        ch1 = np.asarray(data[:, 1], dtype=np.float32)
        ch2 = np.asarray(data[:, 2], dtype=np.float32)
        return ch1, ch2


    def upload_waveform(self, waveform, arb_name, ch=1, sample_rate=None):
        if sample_rate is None:
            sample_rate = self.MAX_SAMPLE_RATE

        # clear volatile memory
        self.write(f"SOUR{ch}:DATA:VOL:CLE")
        # binary format
        self.write("FORM:BORD SWAP")

        self.inst.write_binary_values(
            f"SOUR{ch}:DATA:ARB {arb_name},",
            waveform,
            datatype='f',
            is_big_endian=False
        )

        print(f"Keysight 33600A: uploaded arb to channel {ch}")

    def upload_csv(self, csv_file, sample_rate=None):
        ch1, ch2 = self.load_csv(csv_file)
        self.upload_waveform(ch1, arb_name=self.arb_name_ch1, ch=1, sample_rate=sample_rate)
        self.upload_waveform(ch2, arb_name=self.arb_name_ch2, ch=2, sample_rate=sample_rate)


    def run(self, vpp, channel_list=(1, 2), sample_rate=None):
        if sample_rate is None:
            sample_rate = self.MAX_SAMPLE_RATE

        for ch in channel_list:
            # turn on smoothing filter so we can have 1 GSa/s, max sample rate 
            self.write(f"SOUR{ch}:FUNC:ARB:FILT NORM")

            # need to write this before the others
            self.write(f"SOUR{ch}:FUNC:ARBitrary:PTPeak {vpp}")
            self.write(f"SOUR{ch}:FUNC ARB")
            self.write(f"SOUR{ch}:FUNC:ARB {self.arb_name_prefix}{ch}")
            self.write(f"SOUR{ch}:FUNC:ARB:SRAT {sample_rate}")

            self.write(f"TRIG{ch}:SOUR EXT")
            self.write(f"TRIG{ch}:SLOP POS")

            # self.write(f"SOUR{ch}:FUNC:ARB:SRAT {sample_rate}")
            self.write(f"SOUR{ch}:BURST:MODE TRIG")
            self.write(f"SOUR{ch}:BURST:NCYC 1")
            self.write(f"SOUR{ch}:BURST:STAT ON")
            self.write(f"OUTP{ch} ON")

        print("Keysight 33600A: waiting for external trigger")

    def run_alignment(self, vpp=0.632, carrier_freq=77e6, mod_freq=1.0):

        self.write("TRIG:SOUR IMM")
        self.write("OUTP1:LOAD 50")
        self.write("SOUR1:FUNC SIN")
        self.write(f"SOUR1:FREQ {carrier_freq}")
        self.write(f"SOUR1:VOLT {vpp} VPP") # 0 dBm

        self.write("SOUR1:AM:STAT OFF")
        self.write("SOUR1:AM:SOUR INT")
        self.write("SOUR1:AM:INT:FUNC SQU")
        self.write(f"SOUR1:AM:INT:FREQ {mod_freq}")
        self.write("SOUR1:AM:DEPT 100")
        self.write("SOUR1:AM:STAT ON")
        self.write("OUTP1 ON")

    def play_continuously(self, sample_rate, channel_list=(1, 2), vpp=1):
        for ch in channel_list:
            self.write(f"SOUR{ch}:FUNC:ARB:FILT NORM")
            self.write(f"SOUR{ch}:FUNC:ARB:PTP 1") # doesn't seem to work for channel 2?
            self.write(f"SOUR{ch}:FUNC ARB")
            self.write(f"SOUR{ch}:FUNC:ARB {self.arb_name_prefix}{ch}")
            self.write(f"SOUR{ch}:FUNC:ARB:SRAT {sample_rate}")
            self.write(f"TRIG{ch}:SOUR IMM")
            self.write(f"SOUR{ch}:BURS:STAT OFF")
            self.write(f"OUTP{ch} ON")
            # self.write(f"SOUR1:VOLT {vpp} VPP")