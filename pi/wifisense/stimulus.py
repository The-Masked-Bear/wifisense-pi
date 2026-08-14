"""Packet stimulus: the Pi drives the CSI sample rate.

CSI only exists when a packet is *received*, so something must keep the air
busy at a known rate.  Three strategies were measured on this hardware:

======================================  ==========================
strategy                                measured CSI rate
======================================  ==========================
passive sniffing (ambient beacons)      ~1 Hz, irregular
node transmits, capture from ACKs       ~1 Hz (ACK CSI never
(``dump_ack_en = true``)                materialised)
**Pi sends UDP to the node**            **1:1 up to 200 Hz**
======================================  ==========================

The third wins outright, and for a structural reason: the packets arrive as
ordinary data frames addressed to the node, which is the case the CSI path is
designed around.  It also puts the rate under the control of the machine doing
the analysis, so the DSP can ask for the rate it needs rather than discovering
what the network happened to provide.

The node's own address arrives in every status frame, so no configuration is
needed and a DHCP lease change is followed automatically.

Nothing here requires root -- unlike ``ping -i 0.01``, which needs privileges
for sub-200 ms intervals.
"""

from __future__ import annotations

import socket
import threading
import time

# RFC 863 discard.  Nothing listens, and nothing should: the packet's arrival
# at the radio is the entire point, and a reply would only add air traffic.
DISCARD_PORT = 9

# 32 bytes is comfortably above the minimum frame size while staying small
# enough that 200 Hz is negligible load (~51 kbit/s of payload).
_PAYLOAD = b"\xc5\x11" + bytes(30)


class Stimulator:
    """Sends UDP datagrams to the sensor node at a steady rate."""

    def __init__(self, target: str | None = None, rate_hz: float = 100.0) -> None:
        self.target = target
        self.rate_hz = rate_hz
        self.sent = 0
        self.errors = 0
        self._sock: socket.socket | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._lock = threading.Lock()

    # ------------------------------------------------------------- lifecycle

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._sock.setblocking(False)
        self._thread = threading.Thread(target=self._run, name="stimulus", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2.0)
        if self._sock:
            try:
                self._sock.close()
            except OSError:
                pass
            self._sock = None

    def set_target(self, target: str | None) -> None:
        """Point at a (possibly new) node address.  Safe to call repeatedly."""
        with self._lock:
            if target and target != self.target and target != "0.0.0.0":
                self.target = target

    def set_rate(self, rate_hz: float) -> None:
        with self._lock:
            self.rate_hz = max(0.0, min(rate_hz, 500.0))

    # ------------------------------------------------------------------- run

    def _run(self) -> None:
        next_at = time.perf_counter()
        while not self._stop.is_set():
            with self._lock:
                target = self.target
                rate = self.rate_hz

            if not target or rate <= 0:
                self._stop.wait(0.2)
                next_at = time.perf_counter()
                continue

            now = time.perf_counter()
            if now < next_at:
                # Sleep in small slices: a single long sleep would overshoot
                # whenever the rate or target changes mid-wait.
                time.sleep(min(next_at - now, 0.002))
                continue

            try:
                if self._sock is not None:
                    self._sock.sendto(_PAYLOAD, (target, DISCARD_PORT))
                    self.sent += 1
            except OSError:
                # ENETUNREACH while the node re-associates, EAGAIN when the
                # socket buffer is briefly full.  Both are transient and
                # self-correcting; dropping one stimulus packet costs one CSI
                # sample.
                self.errors += 1

            next_at += 1.0 / rate
            # If we have fallen more than a fraction of a second behind (the
            # process was descheduled, or the rate was just raised), resync
            # rather than emitting a catch-up burst -- a burst would put a
            # cluster of samples at one instant and distort the spectrum.
            if next_at < now - 0.2:
                next_at = now + 1.0 / rate

    # ---------------------------------------------------------------- output

    def info(self) -> dict:
        return {
            "target": self.target,
            "rate_hz": self.rate_hz,
            "sent": self.sent,
            "errors": self.errors,
            "running": bool(self._thread and self._thread.is_alive()),
        }
