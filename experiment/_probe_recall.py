import time
import serial
from serial.tools import list_ports


def find_port():
    for p in list_ports.comports():
        if p.vid == 0x0483 and p.pid == 0x5740:
            return p.device
    return None


port = find_port()
print("found port:", port)

ser = serial.Serial(port, baudrate=115200, timeout=0.2)
ser.reset_input_buffer()

print("sending 'recall 0'")
ser.write(b"recall 0\r")

t0 = time.time()
last_present = True
while time.time() - t0 < 25:
    present = find_port() is not None
    if present != last_present:
        print(f"  t={time.time()-t0:5.2f}s: port presence changed -> {present}")
        last_present = present
    # Also try a non-blocking-ish read to see if anything comes back on the
    # ORIGINAL handle while we're at it (only meaningful if port stayed open).
    try:
        chunk = ser.read(256)
        if chunk:
            print(f"  t={time.time()-t0:5.2f}s: got bytes on original handle: {chunk!r}")
    except Exception as e:
        print(f"  t={time.time()-t0:5.2f}s: read on original handle failed: {e}")
    time.sleep(0.2)

print(f"final port presence: {find_port() is not None}")

try:
    ser.close()
except Exception:
    pass

# Now try opening a FRESH handle and see if the device responds to "version".
print("\nattempting fresh reconnect + 'version'...")
port2 = find_port()
print("port for reconnect:", port2)
if port2:
    try:
        ser2 = serial.Serial(port2, baudrate=115200, timeout=2.0)
        ser2.reset_input_buffer()
        ser2.write(b"version\r")
        time.sleep(0.3)
        resp = ser2.read(200)
        print("response:", resp)
        ser2.close()
    except Exception as e:
        print("reconnect failed:", e)
