import ks33600a
import rtb2004_new
import numpy as np

# generator = ks33600a.KS33600A("USB0::0x0957::0x5707::MY53800810::INSTR")
# generator.upload_csv("./waveforms/test_scope.csv")
# generator.play_continuously(sample_rate=50e6)
# generator.close()

scope = rtb2004_new.RTB2004("USB0::0x0AAD::0x01D6::108904::INSTR")
scope.run(segments=10)
scope.close()


# print(scope.query("*IDN?"))
# print(scope.query("*OPT?"))
