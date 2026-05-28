import ks33600a
import rtb2004
import numpy as np

# generator = ks33600a.KS33600A("USB0::0x0957::0x5707::MY53800810::INSTR")
# generator.upload_csv("./waveforms/test_scope_100.csv")
# generator.play_continuously(sample_rate=100e6)
# generator.close()

scope = rtb2004.RTB2004("USB0::0x0AAD::0x01D6::108904::INSTR", timeout=10000, debug=True)
scope.run(segments=15)
scope.close()


# print(scope.query("*IDN?"))
# print(scope.query("*OPT?"))
