import ks33600a
import rtb2004
import numpy as np

# generator = ks33600a.KS33600A("USB0::0x0957::0x5707::MY53800810::INSTR")
# generator.upload_csv("./waveforms/test_scope_100.csv")
# generator.play_continuously(sample_rate=500e6, channel_list=[1, 2])
# generator.close()

scope = rtb2004.RTB2004("USB0::0x0AAD::0x01D6::108904::INSTR", timeout=100000, debug=True)
scope.run(segments=6553, path="D:/data_test", name="first_ten") #
timetable = scope.get_timetable()

scope.close()

def str_to_arr(str):
    return np.fromstring(str, sep=',')

def check_timetable(arr, expected_diff, tol=1e-6):
    arr = np.asarray(arr)
    diffs = np.diff(arr)
    return np.all(np.isclose(diffs, expected_diff, atol=tol, rtol=0))

print(check_timetable(str_to_arr(timetable), 20e-6))

# turn stuff off during data collection