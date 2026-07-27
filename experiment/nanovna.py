"""
NanoVNA-F V3 vector network analyzer driver.

Not a VISA instrument -- it's a plain USB-CDC serial port with a line-based
ASCII command shell (type "help" at a terminal emulator to see the full
command list), not SCPI. Write "<command>\r", then read lines until the
"ch> " shell prompt reappears; the shell echoes the command back as the
first line.

The command protocol here (drain-before-write, echo suppression, prompt
framing, "sweep"/"frequencies"/"data" commands) is adapted from
NanoVNA-Saver (GPLv3), which has already done the work of reverse-engineering
this across firmware forks:
  https://github.com/NanoVNA-Saver/nanovna-saver
  src/NanoVNASaver/Hardware/{Serial,VNA,NanoVNA,NanoVNA_F_V3}.py
This driver only implements what we need for scripted sweeps (no GUI, no
screenshot capture, no calibration-file handling).
"""
import time

import numpy as np
import serial
from serial.tools import list_ports

# Matches NanoVNA-Saver's Hardware.py:USBDEVICETYPES entry for the STM32
# virtual COM port that classic NanoVNA/NanoVNA-F firmware enumerates as.
# Confirm against Device Manager / `nanovna.find_port()` if your unit shows
# up under a different VID:PID (e.g. a CH340/CP210x USB-serial bridge).
DEFAULT_VID_PID = (0x0483, 0x5740)


def find_port(vid_pid=DEFAULT_VID_PID):
    """Look for a NanoVNA-family USB-CDC port by VID:PID. Returns the first
    matching device path (e.g. "COM5"), or None if not found."""
    vid, pid = vid_pid
    for p in list_ports.comports():
        if p.vid == vid and p.pid == pid:
            return p.device
    return None


def drain_serial(ser, timeout=0.05):
    """Drain any stale bytes sitting in the input buffer before starting a
    new command (adapted from NanoVNA-Saver's Serial.drain_serial)."""
    old_timeout = ser.timeout
    ser.timeout = timeout
    while ser.read(128):
        pass
    ser.timeout = old_timeout


class NanoVNAF3:
    """
    NanoVNA-F V3 vector network analyzer (800x480 screen, GD32F303-based).

    sweep_points range and max frequency below match NanoVNA-Saver's
    NanoVNA_F_V3 class.
    """

    SWEEP_POINTS_MIN = 11
    SWEEP_POINTS_MAX = 801
    SWEEP_MAX_FREQ_HZ = 6.3e9

    def __init__(self, port=None, baudrate=115200, timeout=1.0, debug=False):
        self.port = port or find_port()
        if self.port is None:
            raise RuntimeError(
                "NanoVNA not found on any USB-CDC port -- pass port= "
                "explicitly (e.g. NanoVNAF3(port='COM5'))"
            )
        self.baudrate = baudrate
        self.timeout = timeout
        self.inst = serial.Serial(self.port, baudrate=baudrate, timeout=timeout)
        self.debug = debug
        self.datapoints = 101
        drain_serial(self.inst)
        print(f"NanoVNA-F V3: connected on {self.port}")

    def close(self):
        if self.inst is not None:
            self.inst.close()

    def reconnect(self, wait_s=1.0):
        """Close and reopen the serial port. Useful after a command that
        may have reset the device's USB peripheral (see load_calibration()
        and notes.md) -- the old pyserial handle stays wedged even after
        the device comes back, so a fresh handle is needed."""
        try:
            self.inst.close()
        except Exception:
            pass
        time.sleep(wait_s)
        self.inst = serial.Serial(self.port, baudrate=self.baudrate, timeout=self.timeout)
        drain_serial(self.inst)

    def exec_command(self, command, wait=0.05, max_retries=200):
        """
        Write a command and return its response lines, having read until the
        "ch> " prompt reappears. Suppresses the command's own echo.
        """
        drain_serial(self.inst)
        self.inst.write(f"{command}\r".encode("ascii"))
        if self.debug:
            print(command)
        time.sleep(wait)

        lines = []
        retries = 0
        while True:
            line = self.inst.readline().decode("ascii", errors="replace").strip()
            if not line:
                retries += 1
                if retries > max_retries:
                    raise IOError(f"NanoVNA: too many retries waiting for {command!r}")
                time.sleep(wait)
                continue
            if line == command:  # suppress echo
                continue
            if line.startswith("ch>"):
                break
            lines.append(line)
        return lines

    def read_version(self):
        return self.exec_command("version")[0]

    def read_info(self):
        return "\n".join(self.exec_command("info"))

    def set_bandwidth(self, bandwidth_hz):
        """Set IF bandwidth in Hz. Ignores any error text the firmware
        returns (bandwidth argument units/valid set vary across forks --
        check self.get_bandwidths() or notes.md for what your unit accepts)."""
        self.exec_command(f"bandwidth {bandwidth_hz:.0f}")

    def get_bandwidths(self):
        result = " ".join(self.exec_command("bandwidth"))
        try:
            result = result.split(" {")[1].strip("}")
            return sorted(int(i) for i in result.split("|"))
        except IndexError:
            return []

    def load_calibration(self, slot=0, settle_s=3.0):
        """
        Apply a previously saved calibration -- and the rest of the
        instrument state it was saved with (frequency plan, display
        settings) -- from state slot `slot`, via the firmware's
        `recall {slot}` command.

        WARNING -- crash risk: on our NanoVNA-F V3 (firmware 0.6.0, build
        2025-06-18), sending `recall` reliably crashed the USB-CDC
        connection. The device echoed the command and then stopped
        responding to any read/write entirely; Windows reported "a device
        attached to the system is not functioning" on the port. A full
        power cycle (not just a USB replug) was required to recover, and
        this happened twice in a row under identical conditions. See
        notes.md for details. Root cause not established -- don't call
        this from an unattended script without someone present to power-
        cycle the unit if it hangs, and check notes.md for whether this has
        since been root-caused before relying on it.

        Unlike exec_command(), this does NOT loop waiting for a "ch> "
        prompt, since on our unit that prompt never reappeared -- it sends
        the command, waits settle_s for the recall (and possible USB
        reset) to finish, then drains whatever showed up. If the port
        wedges, call reconnect() and re-check with read_version() before
        continuing.
        """
        drain_serial(self.inst)
        self.inst.write(f"recall {slot}\r".encode("ascii"))
        if self.debug:
            print(f"recall {slot}")
        time.sleep(settle_s)
        drain_serial(self.inst)

    def set_sweep(self, start_hz, stop_hz, points=101):
        if not (self.SWEEP_POINTS_MIN <= points <= self.SWEEP_POINTS_MAX):
            raise ValueError(
                f"points must be in [{self.SWEEP_POINTS_MIN}, {self.SWEEP_POINTS_MAX}]"
            )
        self.datapoints = points
        self.exec_command(f"sweep {start_hz:.0f} {stop_hz:.0f} {points}")

    def read_frequencies(self):
        return np.array([float(line) for line in self.exec_command("frequencies")])

    def read_values(self, channel=0):
        """Read complex S-parameter data for `channel` (0 = S11, 1 = S21).
        Each response line is "re im"."""
        lines = self.exec_command(f"data {channel}")
        return np.array([complex(*map(float, line.split())) for line in lines])

    def sweep(self, start_hz, stop_hz, points=101, channels=(0, 1)):
        """
        Configure and read one sweep.
        Returns (freqs_hz, {channel: complex_array_for_that_channel}).
        """
        self.set_sweep(start_hz, stop_hz, points)
        time.sleep(0.05)
        freqs_hz = self.read_frequencies()
        data = {ch: self.read_values(ch) for ch in channels}
        return freqs_hz, data

    def save_sweep_npy(self, filepath, freqs_hz, data):
        """
        Save a sweep to a single .npy file: columns are
        [frequency_hz, Re(ch), Im(ch), ...] for each channel in `data`, in
        ascending channel order.
        """
        channels = sorted(data)
        columns = [freqs_hz]
        for ch in channels:
            columns.append(data[ch].real)
            columns.append(data[ch].imag)
        array = np.column_stack(columns)
        np.save(filepath, array)
        print(f"saved {filepath} (columns: frequency_hz, "
              + ", ".join(f"Re(ch{ch}),Im(ch{ch})" for ch in channels) + ")")

    def run(self, start_hz, stop_hz, points=201, channels=(0, 1),
            path="./data", name="nanovna_sweep"):
        """Run one sweep and save it to "{path}/{name}.npy". Returns
        (freqs_hz, data) as from sweep()."""
        freqs_hz, data = self.sweep(start_hz, stop_hz, points, channels)
        self.save_sweep_npy(f"{path}/{name}.npy", freqs_hz, data)
        return freqs_hz, data


if __name__ == "__main__":
    vna = NanoVNAF3(debug=True)
    print(vna.read_version())
    # Uncomment to apply the calibration saved in slot 0 before sweeping --
    # see load_calibration()'s docstring and notes.md first: this crashed
    # the USB connection on our unit and needed a power cycle to recover.
    # vna.load_calibration(0)
    print(vna.read_info())
    vna.run(start_hz=1e6, stop_hz=900e6, points=401, path="data")
    vna.close()
