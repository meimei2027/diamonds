import pyvisa
import numpy as np
import os
from datetime import datetime
import time


class RTB2004:
    def __init__(self, resource, segment_buffer_size=100, timeout=100000):
        self.rm = pyvisa.ResourceManager()
        self.inst = self.rm.open_resource(resource)

        self.inst.timeout = timeout
        self.inst.chunk_size = 10 * 1024 * 1024  # 10 MB chunks
        self.debug = True


    def reset(self):
        self.write("*RST")
        self.write("*CLS")
    
    def close(self):
        if self.inst is not None:
            self.inst.close()
    
    def write(self, cmd):
        self.inst.write(cmd)
        if self.debug: 
            print(cmd + " => " + self.inst.query("SYST:ERR?"))

    def query(self, cmd):
        return self.inst.query(cmd).strip()

    def setup_segmented_mode(self, segments=1000):
        self.write("*RST")
        self.write("*CLS")

        self.write("ACQuire:MEMory MANual")
        self.write("ACQuire:POINts 10000")
        self.write(f"ACQuire:NSINgle:COUNt {segments}")
        self.write("ACQuire:SEGMented:STATe ON")

        self.write("SINGle")
        time.sleep(1)

        self.write("STOP")
        print(self.get_segment_count())

        # self.write("CHANnel1:HISTory:PALL ON")
        # self.write("CHANnel1:HISTory:REPLay ON")

        # self.write("CHANnel1:HISTory:STARt -5")
        # self.write("CHANnel1:HISTory:STOP 0")

        for i in range(1, segments):
            self.read_segment(i)
            


    def get_timetable(self, save=False, path="/USB_FRONT/DATA", ch=1):
        print(self.query(f"CHANnel{ch}:HISTory:TSRelative:ALL?"))
        if save: 
            self.write(f"CHANnel{ch}:HISTory:EXPort:NAME \"{path}\"")
            self.write(f"CHANnel{ch}:HISTory:EXPort:SAVE")

    def set_timebase(self, scale_seconds):
        self.write(f"TIMebase:SCALe {scale_seconds}")


    def set_trigger_edge(self, level=0.0):
        self.write("TRIGger:A:TYPE EDGE")
        self.write(f"TRIGger:A:SOURce EXT")
        self.write(f"TRIGger:A:LEVel {level}")
        self.write("TRIGger:A:MODE NORM")


    def wait_for_acquisition(self, timeout=60):
        pass
        # start = time.time()

        # while True:
        #     opc = self.inst.query("*OPC?").strip()
        #     if opc == "1":
        #         return
        #     if time.time() - start > timeout:
        #         raise TimeoutError("Acquisition timeout")
        #     time.sleep(0.1)


    def get_segment_count(self):
        return int(self.query("ACQuire:AVAilable?"))

    def read_segment(self, segment_index=1, ch=1):
        self.write(f"CHANnel1:HISTory:CURRent {segment_index}")
        self.write(f"EXPort:WAVeform:SOURce CH{ch}")
        self.write("FORMAT CSV")
        self.write("EXPort:WFMSave:DEST \"/USB_FRONT/DATA\"")
        self.write(f"CHANnel{ch}:DATA:POINts MAX")
        self.write(f"EXPort:WAVeform:NAME \"/USB_FRONT/DATA/WFMNEW0{segment_index}\"")
        self.write("EXPort:WAVeform:SAVE")


        # self.write("WAVeform:FORMat BYTE")
        # raw = self.inst.query_binary_values(
        #     "WAVeform:DATA?",
        #     datatype='B',
        #     container=np.array
        # )
        # y_origin = float(self.query("WAVeform:YORigin?"))
        # y_ref = float(self.query("WAVeform:YREFerence?"))
        # y_inc = float(self.query("WAVeform:YINCrement?"))

        # voltage = (raw - y_ref) * y_inc + y_origin

        # return voltage


    def run(self, segments=1000, ch=1):
        self.setup_segmented_mode(
            segments=segments
        )