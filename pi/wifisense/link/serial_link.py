"""Serial transport: the ESP32-S3 over its CP2102 USB-UART bridge.

This is the primary link.  It carries full-rate raw CSI with headroom to
spare, needs no radio configuration, and -- unlike either sub-GHz option --
adds nothing to the 2.4 GHz band we are trying to measure.
"""

from __future__ import annotations

import glob
import time

import serial

from .base import Link

# Ordered by how likely each is to be the sensor.  ttyUSB* covers the CP2102
# and CH340 bridges; ttyACM* covers boards wired to the S3's native USB.
PORT_GLOBS = ("/dev/ttyUSB*", "/dev/ttyACM*")


def find_port() -> str | None:
    for pattern in PORT_GLOBS:
        matches = sorted(glob.glob(pattern))
        if matches:
            return matches[0]
    return None


class SerialLink(Link):
    """Reads COBS-framed CSI from a serial port, reconnecting as needed."""

    name = "serial"

    def __init__(
        self,
        port: str | None = None,
        baud: int = 921600,
        *,
        reconnect_delay: float = 2.0,
    ) -> None:
        super().__init__()
        self.port = port
        self.baud = baud
        self.reconnect_delay = reconnect_delay
        self._ser: serial.Serial | None = None

    def run(self) -> None:
        while not self._stop.is_set():
            port = self.port or find_port()
            if port is None:
                self.last_error = "no serial device found"
                self.connected = False
                self._stop.wait(self.reconnect_delay)
                continue

            try:
                # A short timeout keeps the read loop responsive to shutdown
                # without spinning the CPU when the board goes quiet.
                self._ser = serial.Serial(port, self.baud, timeout=0.1)
            except (serial.SerialException, OSError) as exc:
                self.last_error = f"open {port}: {exc}"
                self.connected = False
                self._stop.wait(self.reconnect_delay)
                continue

            self.connected = True
            self.last_error = None
            self.decoder.reset()

            try:
                while not self._stop.is_set():
                    # Read whatever is buffered in one call rather than byte by
                    # byte: at 100 Hz this is ~15 kB/s, and per-byte reads
                    # would burn measurable CPU on a Pi 4 for no benefit.
                    waiting = self._ser.in_waiting
                    data = self._ser.read(max(waiting, 1))
                    if not data:
                        continue
                    now = time.monotonic()
                    for frame in self.decoder.feed(data):
                        # Stamp on arrival.  The ESP32's own timestamp is in
                        # the frame and is better for measuring intervals, but
                        # the host clock is what the rest of the system uses.
                        if hasattr(frame, "host_time"):
                            frame.host_time = now
                        self.emit(frame)
            except (serial.SerialException, OSError) as exc:
                self.last_error = f"read: {exc}"
            finally:
                self.connected = False
                self.close()

            if not self._stop.is_set():
                self._stop.wait(self.reconnect_delay)

    def send(self, data: bytes) -> bool:
        """Write a command frame back to the node."""
        if self._ser is None or not self._ser.is_open:
            return False
        try:
            self._ser.write(data)
            return True
        except (serial.SerialException, OSError):
            return False

    def close(self) -> None:
        if self._ser is not None:
            try:
                self._ser.close()
            except Exception:
                pass
            self._ser = None
