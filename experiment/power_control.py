import numpy as np
import sys
import socket
import time
import pandas as pd
from scipy import stats

import pyvisa
import time # for sleep
import binascii

class PowerSupply:
    def __init__(self, resource, debug=True):
        self.ip = resource
        self.rm = pyvisa.ResourceManager()
        self.inst = self.rm.open_resource(resource)
        self.debug = debug
        print("PowerSupply: connected")

    def write(self, cmd):
        self.inst.write(cmd)
        if self.debug: 
            message = self.inst.query("SYST:ERR?")
            if message[0] != "0":
                print(cmd + " => " + message)
    def query(self, cmd):
        return self.inst.query(cmd).strip()
    
    def close(self):
        if self.inst is not None:
            self.inst.close()
    def power_on(self):
        self.write('OUTP CH1,ON')
    def power_off(self):
        self.write('OUTP CH1,OFF')

    def gauss_settings(self, g, direction):
        filenames = ['./coil_data/X.csv', './coil_data/Y.csv', './coil_data/Z.csv']
        regressions_ig = []
        regression_iv = []
        for i in filenames:
            a, g, v = np.loadtxt(i, delimiter=',', skiprows=1, unpack=True)
            regressions_ig.append(stats.linregress(a, g))
            regression_iv.append(stats.linregress(a, v))
        if direction == 'x':
            return (g - regressions_ig[0].intercept) / regressions_ig[0].slope, (g - regression_iv[0].intercept) / regression_iv[0].slope
        elif direction == 'y':
            return (g - regressions_ig[1].intercept) / regressions_ig[1].slope, (g - regression_iv[1].intercept) / regression_iv[1].slope
        elif direction == 'z':
            return (g - regressions_ig[2].intercept) / regressions_ig[2].slope, (g - regression_iv[2].intercept) / regression_iv[2].slope
        else:
            raise ValueError("Invalid direction. Must be 'x', 'y', or 'z'.")

    def run(self, g, dir):
        self.write_termination='\n' 
        self.read_termination='\n'
        print (self.rm.list_resources()) 
        time.sleep(0.04) 
        self.write('INST CH 1')
        print("Overcurrent protect value: ", str(self.query('OCP?')))
        a, v = self.gauss_settings(g, dir)

        self.write(f'VOLT {v+0.1}') # add 0.1 V to ensure we don't underestimate the voltage needed
        self.write(f'CURR {a}')
        time.sleep(2)       

        self.power_on()
        print("Voltage:", self.query("MEAS:VOLT?"))
        print("Current:", self.query("MEAS:CURR?"))
        time.sleep(10) 
        self.power_off() 
        self.write('*IDN?')
        time.sleep(1)
        qStr = self.query('*IDN?') 
        print (str(qStr)) 
        self.close() 
    

# Comment this out when you're using this as a module, this is just for testing
if __name__ == "__main__":
    # ip = 'USB0::0xF4EC::0x1410::SPD13DCD7R1877::INSTR'
    ip2 = 'ASRL4::INSTR'
    g = 15 # Gauss
    direction = 'x' # 'x', 'y', or 'z'
    ps = PowerSupply(ip2)
    ps.run(g, direction)