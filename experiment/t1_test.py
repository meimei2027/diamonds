import ks33600a
import rtb2004
import generate_arb
import numpy as np

def build_block_descriptor(sequence_name, segments):
    parts = [f'"{sequence_name}"']
    for arb_name, repeat_count, play_control, marker_mode, marker_point in segments:
        parts.append(
            f'"{arb_name}",{repeat_count},{play_control},{marker_mode},{marker_point}'
        )
    payload = ",".join(parts)
    payload_bytes = payload.encode("utf-8")
    payload_len = len(payload_bytes)
    n = len(str(payload_len))
    return f"#{n}{payload_len}{payload}"

    
generate = False
if generate == True:

    awg = ks33600a.KS33600A("USB0::0x0957::0x5707::MY53800810::INSTR", debug=True)
    FS = 1e9

    def rf(freq, duration):
        n = int(duration * FS)
        t = np.arange(n) / FS
        return t, np.sin(2*np.pi*freq*t).astype(np.float32)

    def zeros(duration):
        n = int(duration * FS)
        t = np.arange(n) / FS
        return t, np.zeros(int(duration * FS), dtype=np.float32)

    t_polarization, ch_polarization = rf(77e6, 10e-6)
    t_readout, ch_readout = rf(77e6, 300e-9)
    t_dark, ch_dark = zeros(10e-6) # x2000 = 20e-3

    generate_arb.write_csv("waveforms/polarization.csv", t_polarization, ch_polarization)
    generate_arb.write_csv("waveforms/readout.csv", t_readout, ch_readout)
    generate_arb.write_csv("waveforms/dark.csv", t_dark, ch_dark)

    awg.upload_csv("waveforms/polarization.csv", sample_rate=1e9, ch2_exists=False, arb_name_1="polarize")
    awg.upload_csv("waveforms/dark.csv", sample_rate=1e9, ch2_exists=False, arb_name_1="dark")
    awg.upload_csv("waveforms/readout.csv", sample_rate=1e9, ch2_exists=False, arb_name_1="readout")


    # print(awg.query("SOURce1:DATA:VOLatile:CATalog?"))
    block = build_block_descriptor("test", [["polarize", "1", "once", "lowAtStart", 10],
                                            ["dark", "2000", "repeat", "lowAtStart", 10],
                                            ["readout", "1", "once", "highAtStart", 10],
                                            ["polarize", "1", "once", "lowAtStart", 10],
                                            ["readout", "1", "once", "highAtStart", 10],
                                            ])

    awg.write(f"DATA:SEQ {block}")


    awg.write(f"OUTP1:LOAD 50")
    awg.write(f"SOUR1:FUNC:ARB:PTP 0.632")
    awg.write(f"SOUR1:FUNC:ARB:SRAT 1e9")

    awg.write('SOUR1:FUNC:ARB "test"')
    awg.write("SOUR1:FUNC ARB")
    awg.write("OUTPUT1 ON")

    awg.write("TRIG1:SOUR IMM")

    # FUNC:ARB:SYNC

def str_to_arr(str):
    return np.fromstring(str, sep=',')

scope = rtb2004.RTB2004("USB0::0x0AAD::0x01D6::108904::INSTR", timeout=100000, debug=True)
scope.run(segments=10, path="./data", name="test") #
timetable = scope.get_timetable()
np.save("./data/timetable.npy", str_to_arr(timetable))
print(timetable)
scope.close()

