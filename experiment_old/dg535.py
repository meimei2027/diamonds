import pyvisa

class DG535:
    T0 = 1
    A = 2
    B = 3
    C = 5
    D = 6

    def __init__(self, resource_name):
        rm = pyvisa.ResourceManager()
        self.inst = rm.open_resource(resource_name)
        self.inst.timeout = 5000
        self.write(f"RC 0")
        print("DG535: connected")

    def write(self, cmd):
        self.inst.write(cmd)

    def query(self, cmd):
        return self.inst.query(cmd)

    def close(self):
        self.inst.close()

    def set_trigger_internal(self, period_s):
        frequency = 1.0 / period_s
        self.write("TM 0") # internal trigger
        self.write(f"TR 0,{frequency:.9f}") # trigger rate in Hz

    def set_delay(self, channel, reference, delay_s):
        # impedance 50 ohm - mode 0
        self.write(f"TZ {channel},1")
        self.write(f"DT {channel},{reference},{delay_s:.9f}")
    
    # def set_voltage(self, channel, voltage):
        # variable mode
        # self.write(f"OM {channel},1")
        # offset
        # self.write(f'OO {channel},{-voltage/2}')
        # # voltage
        # self.write(f'OA {channel},{voltage}')
        # print("set voltage")

    def configure_sequence(
        self,
        t_cycle,
        t0_to_a=0,
        a_to_b=0,
        b_to_c=0,
        c_to_d=0,
    ):
        
        self.set_trigger_internal(t_cycle)
        tA = t0_to_a
        tB = tA + a_to_b
        tC = tB + b_to_c
        tD = tC + c_to_d

        self.set_delay(self.A, self.T0, tA)
        self.set_delay(self.B, self.T0, tB)
        self.set_delay(self.C, self.T0, tC)
        self.set_delay(self.D, self.T0, tD)

        print("DG535: delays configured")