import pyvisa
import numpy as np
import os
from datetime import datetime
import time
import h5py

class RTB2004:
    def __init__(self, resource, timeout=100000, debug=False):
        self.rm = pyvisa.ResourceManager()
        self.inst = self.rm.open_resource(resource)

        self.inst.timeout = timeout
        self.inst.chunk_size = 10 * 1024 * 1024  # 10 MB chunks
        self.debug = debug
        self.reset()
        print("RTB2004: connected")

    def reset(self):
        self.write("*RST")
        self.write("*CLS")
    
    def close(self):
        if self.inst is not None:
            self.inst.close()
    
    def write(self, cmd):
        self.inst.write(cmd)
        if self.debug: 
            message = self.inst.query("SYST:ERR?")
            if message[0] != "0":
                print(cmd + " => " + message)

    def query(self, cmd):
        return self.inst.query(cmd).strip()

    def setup_segmented_mode(self, segments=1000):
        self.write("ACQuire:MEMory MANual")
        self.write("ACQuire:POINts 10000")
        self.write(f"ACQuire:NSINgle:COUNt {segments}")
        self.write("ACQuire:SEGMented:STATe ON")
            

    def get_timetable(self, save=False, path="/USB_FRONT/data", ch=1):
        return self.query(f"CHANnel{ch}:HISTory:TSRelative:ALL?")
        if save: 
            self.write(f"CHANnel{ch}:HISTory:EXPort:NAME \"{path}\"")
            self.write(f"CHANnel{ch}:HISTory:EXPort:SAVE")

    def set_timebase(self, scale_seconds):
        self.write(f"TIMebase:SCALe {scale_seconds}")
        return self.query("ACQuire:SRATe?")


    def set_trigger_edge(self, level=0.0, ch=1):
        self.write(f"CHANnel{ch}:SCALe 500e-3")
        self.write("TRIGger:A:TYPE EDGE")
        self.write("TRIGger:A:SOURce EXT")
        self.write("TRIGger:A:EDGE:SLOPe POS")
        self.write(f"TRIGger:A:LEVel5 {level}")
        self.write("TRIGger:A:MODE NORM")
        # self.write(f"CHANnel{ch}:COUPling ACLimit")


    def wait_for_acquisition(self, segments, timeout=60):
        start = time.time()
        while True:
            opc = self.inst.query("*OPC?").strip()
            num_of_segments = self.get_segment_count()
            if opc == "1" and segments == num_of_segments:
                return
            if time.time() - start > timeout:
                print(f"got {num_of_segments} segments")
                raise TimeoutError("Acquisition timeout")
            time.sleep(0.5)


    def get_segment_count(self):
        return int(self.query("ACQuire:AVAilable?"))

    def read_segment(self, segment_index=1, ch=1):
        self.write(f"CHANnel{ch}:HISTory:CURRent {segment_index}")
        raw = self.inst.query_binary_values(
            f"CHANnel{ch}:DATA?",
            datatype='f',
            container=np.array,
            is_big_endian=True
        )
        return raw


    def run(self, segments=1000, ch=1, path="./data", name="waveform"):
        sample_rate = self.set_timebase(1e-7)
        print("sample rate", sample_rate)
        self.setup_segmented_mode(
            segments=segments
        )
        self.set_trigger_edge(level=100e-3)

        self.write("SINGle")
        self.wait_for_acquisition(segments)

        self.write("STOP")
        print("acquired segments", self.get_segment_count())
        # if segment count not what is expected, rerun?
        self.save_segments(segments, ch, path=path, name=name)

    
    def save_segments(self, segments, ch, path, name):
        self.write(f"EXPort:WAVeform:SOURce CH{ch}")
        self.write("FORMat REAL")
        self.write(f"CHANnel{ch}:DATA:POINts MAX")

        NUM_SAMPLES = 10000

        t0 = time.perf_counter()
        all_segments = np.empty((segments, NUM_SAMPLES), dtype=np.float32)

        for i in range(segments):
            if i == 1:
                time0 = time.perf_counter()
            if i == 2:
                time1 = time.perf_counter()
                print("ETA:", (time1 - time0) * segments, "s")        
            all_segments[i] = self.read_segment(i + 1)
            if i % 100 == 0:
                print(i, "segments done")

        t1 = time.perf_counter()
        np.save(path + f"/{name}.npy", all_segments)
        t2 = time.perf_counter()

        print("time acquire", t1 - t0, "s")
        print("time save", t2 - t1, "s")