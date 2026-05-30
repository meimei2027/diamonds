import ks33600a
import rtb2004
import numpy as np

# generator = ks33600a.KS33600A("USB0::0x0957::0x5707::MY53800810::INSTR")
# generator.upload_csv("./waveforms/test_scope_100.csv")
# generator.play_continuously(sample_rate=500e6, channel_list=[1, 2])
# generator.close()


# # generator.upload_csv("./waveforms/test.csv")
# # generator.play_continuously(sample_rate=1e9, channel_list=[1, 2], vpp=1)


# load INF
# 1V p-p 
# channel 1: 900 mV
# channel 2: excluding ringing, about 500 mV (20 mV low)
# scaled from 0 to 1 instead of -1 to 1


scope = rtb2004.RTB2004("USB0::0x0AAD::0x01D6::108904::INSTR", timeout=10000, debug=True)
scope.run(segments=100)
scope.close()