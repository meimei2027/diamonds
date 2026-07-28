import pyvisa
import numpy as np
import os
from datetime import datetime
import time


def make_t(sample_rate_hz, num_points, t0_s=0.0):
    """
    Build a time axis for a waveform of num_points samples acquired at
    sample_rate_hz, starting at t0_s seconds (relative to the trigger).

    sample_rate_hz is used directly, with no correction factor. An earlier
    version of this function divided the reported rate by 4, which happened
    to be right for whatever acquisition settings it was written against,
    but is wrong under this driver's actual settings (ACQuire:MEMory MANual
    + ACQuire:POINts + segmented mode): verified against a known 80 MHz
    reference signal, where using the reported rate directly (no /4) gave
    the correct frequency. ACQuire:SRATe?'s relationship to the real
    per-sample spacing appears to depend on acquisition mode/point
    count/channel count, so don't assume a fixed correction factor applies
    across different settings -- see notes.md.

    This does NOT center the axis on t=0 by default -- an earlier version
    did (`np.arange(-N/2, N/2) * dt`), which assumed the acquisition window
    is centered on the trigger point. It isn't: set_timebase() configures a
    nonzero TIMebase:POSition (currently 3e-6), and the acquired buffer
    (ACQuire:MEMory MANual + ACQuire:POINts) is wider than what's shown on
    the display, so TIMebase:RANGe (the display width) is not the buffer's
    width either. The real relationship, verified against 5 independent
    (points, position, scale) configurations on this unit:

        t0_s = TIMebase:POSition - num_points / (2 * sample_rate_hz)

    i.e. TIMebase:POSition is the time (relative to the trigger) of the
    *center* of the full acquired buffer, not of the display window --
    TIMebase:RANGe/TIMebase:REFerence describe a narrower sub-window
    centered on that same point, for display/panning purposes only. Get
    t0_s from RTB2004.get_time_origin(ch), which queries
    CHANnel<n>:DATA:XORigin? directly (the authoritative source -- prefer
    it over recomputing this formula yourself). See notes.md.
    """
    dt = 1 / sample_rate_hz
    return t0_s + np.arange(num_points) * dt


def parse_metadata(metadata_path):
    """
    Parse a `{name}_metadata.txt` file written by RTB2004.save_metadata()
    into a dict: {"sample_rate_hz": float, "t0_s": float, "num_points": int}.
    """
    values = {}
    with open(metadata_path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            key, _, value = line.partition("=")
            values[key] = float(value)
    values["num_points"] = int(values["num_points"])
    return values


def make_t_from_metadata(metadata_path):
    """
    Read a `{name}_metadata.txt` file written by RTB2004.save_metadata()
    and return the corresponding time axis via make_t() -- so later
    analysis of `{name}.npy` only needs that .npy plus its metadata file,
    not a live connection to the scope.
    """
    meta = parse_metadata(metadata_path)
    return make_t(meta["sample_rate_hz"], meta["num_points"], t0_s=meta["t0_s"])


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

    def set_timebase(self, scale_seconds, position):
        self.write(f"TIMebase:SCALe {scale_seconds}")
        self.write(f"TIMebase:POSition {position}")
        return self.query("ACQuire:SRATe?")


    def set_trigger_edge(self, level=0.0, ch=1):
        self.write(f"CHANnel{ch}:SCALe 500e-3")
        self.write("TRIGger:A:TYPE EDGE")
        self.write("TRIGger:A:SOURce EXT")
        self.write("TRIGger:A:EDGE:SLOPe POS")
        self.write(f"TRIGger:A:LEVel5 {level}")
        self.write("TRIGger:A:MODE NORM")
        # self.write(f"CHANnel{ch}:COUPling ACLimit")


    def wait_for_acquisition(self, segments, timeout=60, on_poll=None):
        """
        on_poll(num_of_segments), if given, is called once per poll with the
        current segment count -- lets a caller monitor progress (e.g. to
        detect an external event like an interlock trip mid-acquisition)
        without needing its own polling loop.
        """
        start = time.time()
        while True:
            opc = self.inst.query("*OPC?").strip()
            num_of_segments = self.get_segment_count()
            if on_poll is not None:
                on_poll(num_of_segments)
            if opc == "1" and segments == num_of_segments:
                return
            if time.time() - start > timeout:
                print(f"got {num_of_segments} segments")
                raise TimeoutError("Acquisition timeout")
            time.sleep(0.5)


    def get_segment_count(self):
        return int(self.query("ACQuire:AVAilable?"))

    def read_segment_old(self, segment_index=1, ch=1):
        self.write(f"CHANnel{ch}:HISTory:CURRent {segment_index}")
        self.write(f"EXPort:WAVeform:SOURce CH{ch}")
        self.write("FORMAT CSV")
        self.write("EXPort:WFMSave:DEST \"/USB_FRONT/data\"")
        self.write(f"CHANnel{ch}:DATA:POINts MAX")
        self.write(f"EXPort:WAVeform:NAME \"/USB_FRONT/data/TEST{segment_index}\"")
        self.write("EXPort:WAVeform:SAVE")

    def read_segment(self, segment_index=1, ch=1):
        self.write(f"CHANnel{ch}:HISTory:CURRent {segment_index}")
        raw = self.inst.query_binary_values(
            f"CHANnel{ch}:DATA?",
            datatype='f',
            container=np.array,
            is_big_endian=True
        )
        return raw

    def get_time_origin(self, ch=1):
        """
        Query the instrument directly for the true time axis of the most
        recently read-out waveform on channel ch: (t0_s, dt_s) -- the time
        of the first sample relative to the trigger event, and the sample
        interval. Use with make_t(). Equivalent to
        TIMebase:POSition - num_points / (2 * sample_rate) (see make_t()'s
        docstring and notes.md), but queries the instrument directly rather
        than recomputing it, since that's one less place to get out of sync
        if settings change.
        """
        t0_s = float(self.query(f"CHANnel{ch}:DATA:XORigin?"))
        dt_s = float(self.query(f"CHANnel{ch}:DATA:XINCrement?"))
        return t0_s, dt_s


    def run(self, segments=1000, ch=1, path="./data", name="waveform", on_poll=None):
        sample_rate_hz = float(self.set_timebase(1e-7, 3e-6))
        print("sample rate", sample_rate_hz)
        self.setup_segmented_mode(
            segments=segments
        )
        self.set_trigger_edge(level=100e-3)

        self.write("SINGle")
        self.wait_for_acquisition(segments, on_poll=on_poll)

        self.write("STOP")
        print("acquired segments", self.get_segment_count())
        # if segment count not what is expected, rerun?
        self.save_segments(segments, ch, path=path, name=name)
        self.save_timetable(ch, path, name)
        self.save_metadata(ch, path, name, sample_rate_hz)

    def save_available_segments(self, ch=1, path="./data", name="waveform", max_segments=None):
        """
        Read out and save whatever segments are currently sitting in the
        scope's history buffer right now, rather than waiting for an exact
        target count -- for salvaging partial data after an acquisition was
        interrupted (e.g. wait_for_acquisition() raised a VisaIOError
        partway through, on a *different*, now-dead connection -- call this
        on a fresh RTB2004 connection instead). Sends STOP first to freeze
        the count so it doesn't keep changing under us.

        max_segments, if given, caps how many are read out (e.g. the
        originally requested count, in case the scope kept triggering past
        that while communication was down).

        Returns the number of segments actually read out and saved (0 if
        none were available).
        """
        self.write("STOP")
        available = self.get_segment_count()
        if max_segments is not None:
            available = min(available, max_segments)
        if available <= 0:
            print(f"[rtb2004] no segments available to salvage")
            return 0

        self.save_segments(available, ch, path=path, name=name)
        self.save_timetable(ch, path, name)

        # ACQuire:SRATe? reflects the *current* live instrument setting --
        # __init__()'s *RST (this is a fresh connection, reconnected after
        # the original one died) resets that to some default, NOT the rate
        # the buffered data was actually captured at. CHANnel<n>:DATA:
        # XINCrement? (via get_time_origin(), queried after save_segments()
        # above has read real data) is frozen to the actual captured
        # waveform instead, and is what we want here. Confirmed this bug
        # for real: a salvaged run's metadata once claimed 109 MSa/s when
        # the true rate (and the one baked into the raw samples) was
        # 2.5 GSa/s -- see notes.md.
        _t0_s, dt_s = self.get_time_origin(ch)
        sample_rate_hz = 1 / dt_s
        self.save_metadata(ch, path, name, sample_rate_hz)
        return available

    def save_timetable(self, ch, path, name):
        raw = self.get_timetable(ch=ch)
        timestamps_s = np.array([float(x) for x in raw.split(",")])
        np.save(f"{path}/{name}_timetable.npy", timestamps_s)
        return timestamps_s

    def save_metadata(self, ch, path, name, sample_rate_hz):
        """
        Write `{path}/{name}_metadata.txt` with everything needed to
        reconstruct the time axis for `{path}/{name}.npy` later via
        make_t_from_metadata() -- sample_rate_hz, the true trigger-relative
        start time (t0_s, queried directly via CHANnel<n>:DATA:XORigin? --
        see make_t()'s docstring and notes.md for why this can't just be
        computed from TIMebase:POSition alone), and the number of points
        per segment (from CHANnel<n>:DATA:HEADer?, the actual returned
        record length -- see notes.md, this can differ from what
        ACQuire:POINts was set to).
        """
        t0_s, _dt_s = self.get_time_origin(ch)
        header = self.query(f"CHANnel{ch}:DATA:HEADer?")
        num_points = int(header.split(",")[2])

        metadata_path = f"{path}/{name}_metadata.txt"
        with open(metadata_path, "w") as f:
            f.write(f"sample_rate_hz={sample_rate_hz}\n")
            f.write(f"t0_s={t0_s}\n")
            f.write(f"num_points={num_points}\n")
        return metadata_path


    def save_segments(self, segments, ch, path, name):
        read_segment_old = False
        if read_segment_old: 
            self.read_segment_old(1)
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