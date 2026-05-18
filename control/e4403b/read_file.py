import pyvisa
import time
from pathlib import Path
import numpy as np

rm = pyvisa.ResourceManager()
instr = rm.open_resource('GPIB0::18::INSTR')

instr.timeout = 20000
instr.write_termination = '\n'
instr.read_termination = '\n'

instr.write(":FREQ:CENT 77MHz")
instr.write(":FREQ:SPAN 500MHz")

instr.write(":FORM REAL,32")          # binary float transfer
instr.write(":FORM:BORD SWAP")        # little-endian

instr.write(":DISP:WIND:TRAC1:MODE AVER")
instr.write(":AVER:COUNT 20")
instr.write(":INIT:CONT OFF")

for _ in range(20):
    instr.write(":INIT:IMM")
    instr.query("*OPC?")

# instr.write(":DISP:WIND:TRAC1:MODE MAXH")
# for _ in range(20):
#     instr.write(":INIT:IMM")
#     instr.query("*OPC?")

# # Trigger sweep
# instr.write(":INIT:IMM")
# instr.query("*OPC?")                  # wait until sweep completes

trace = instr.query_binary_values(
    ":TRACE:DATA? TRACE1",
    datatype='f',
    is_big_endian=False
)

trace = np.array(trace)

print("Trace points:", len(trace))

center = float(instr.query(":FREQ:CENT?"))
span = float(instr.query(":FREQ:SPAN?"))

start_freq = center - span / 2
stop_freq  = center + span / 2

freqs = np.linspace(start_freq, stop_freq, len(trace))

save_dir = Path("./captures")
save_dir.mkdir(exist_ok=True)

filename = "esa_trace.csv"
filepath = save_dir / filename

data = np.column_stack((freqs, trace))

np.savetxt(
    filepath,
    data,
    delimiter=",",
    header="frequency_hz,amplitude_dbm",
    comments=""
)

print(f"Saved trace to: {filepath}")