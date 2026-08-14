"""nRF24L01+ PA/LNA receiver on the Pi's SPI0 CE0.

Role in this system
-------------------
This is the transport for a **remote** sensor node -- an ESP32 that is not
plugged into the Pi.  With USB available, use the serial link instead: it is
faster, lossless, and adds nothing to the airwaves.

The nRF24 shares the 2.4 GHz band with the WiFi being sensed, so the channel
must sit clear of the AP in use -- a co-located +20 dBm PA transmitting on top
of the measurement is self-defeating.  The default is channel **80 (2480 MHz)**:
inside the licence-free ISM band and clear of WiFi channel 7 (2432-2452).

Do not chase further separation by going above channel 83.  Past 2483.5 MHz you
leave ISM, and the PA/LNA module's antenna match and front-end filter are tuned
for 2.4-2.48 GHz.  An earlier default of channel 108 (2508 MHz) delivered only
25-33% of packets regardless of rate for exactly that reason -- flat,
rate-independent loss is the signature of bad RF rather than buffer overflow.

Wiring (see PROJECT.txt for the full table)::

    module   BCM   physical
    CE       25    22
    CSN       8    24        (CE0 -> /dev/spidev0.0)
    SCK      11    23
    MOSI     10    19
    MISO      9    21
    IRQ      24    18        (optional)
    VCC      --     17       3.3 V ONLY
    GND      --     20

Power is the usual failure: the PA/LNA variant draws ~115 mA peaks and the
Pi's 3.3 V rail sags. Fit a 10-100 uF electrolytic plus 100 nF ceramic
directly across the module's own VCC/GND pins, or feed it from a separate
regulator. Without that it works for a few packets and then goes silent.
"""

from __future__ import annotations

import time

from .base import Link
from .link_crypto import LinkCrypto
from .radio_common import Reassembler, trim_to_terminator

PAYLOAD_SIZE = 32
DEFAULT_ADDRESS = b"CSI01"


class NRF24Link(Link):
    """Receives fragmented CSI frames over an nRF24L01+."""

    name = "nrf24"

    def __init__(
        self,
        *,
        bus: int = 0,
        device: int = 0,
        ce_pin: int = 25,
        channel: int = 80,
        address: bytes = DEFAULT_ADDRESS,
        key: bytes | str | None = None,
    ) -> None:
        super().__init__()
        self.bus = bus
        self.device = device
        self.ce_pin = ce_pin
        self.channel = channel
        self.address = address
        self.radio = None
        self.reasm = Reassembler()
        # Decrypts what the node sealed; None means the link is expected
        # to be plaintext (only useful while debugging).
        self.crypto = LinkCrypto(key) if key else None

    def _open(self):
        from pyrf24 import RF24, RF24_2MBPS, RF24_CRC_16, RF24_PA_HIGH

        # pyrf24 encodes the SPI device as bus*10 + device, so /dev/spidev0.0
        # is 0 and /dev/spidev0.1 is 1.
        radio = RF24(self.ce_pin, self.bus * 10 + self.device)
        if not radio.begin():
            raise RuntimeError(
                "nRF24 did not respond. Check wiring, and check power first -- "
                "a missing decoupling capacitor is the usual cause."
            )
        radio.channel = self.channel
        radio.data_rate = RF24_2MBPS
        radio.pa_level = RF24_PA_HIGH
        radio.crc_length = RF24_CRC_16
        radio.payload_size = PAYLOAD_SIZE
        # Auto-ack ON, matching the transmitter.  A lost fragment otherwise
        # discards its entire frame with no recovery; measured ~4.7% packet
        # loss compounded to ~25% frame loss.  The receiver must agree with the
        # sender here -- if one end acks and the other does not, nothing gets
        # through at all.
        radio.set_auto_ack(True)
        radio.open_rx_pipe(1, self.address)
        radio.listen = True
        return radio

    def run(self) -> None:
        while not self._stop.is_set():
            try:
                self.radio = self._open()
            except Exception as exc:  # noqa: BLE001 - surfaced in the UI
                self.last_error = f"{type(exc).__name__}: {exc}"
                self.connected = False
                self._stop.wait(5.0)
                continue

            self.connected = True
            self.last_error = None
            self.decoder.reset()

            try:
                while not self._stop.is_set():
                    if not self.radio.available():
                        # 2 Mbit/s means a packet can land every ~150 us; poll
                        # fast enough not to overflow the 3-deep RX FIFO, but
                        # not so fast that we spin a core.
                        time.sleep(0.0005)
                        continue

                    # Drain the FIFO completely before sleeping again.  Reading
                    # a single packet per poll cannot keep up: one CSI frame is
                    # five fragments, so 100 Hz is 500 packets/s, and the FIFO
                    # is only three deep.  Any packet that arrives while we are
                    # away is lost outright -- auto-ack is off, so there are no
                    # retries -- and losing one fragment discards the whole
                    # frame.  Measured: one-per-poll gave 19% packet loss, which
                    # compounded to 66% frame loss.
                    for _ in range(24):
                        packet = bytes(self.radio.read(PAYLOAD_SIZE))
                        frame = self.reasm.push(packet)
                        if frame is not None and self.crypto is not None:
                            frame = self.crypto.decrypt(frame)
                        if frame:
                            # Drop the fixed-payload padding before framing.
                            frame = trim_to_terminator(frame)
                            now = time.monotonic()
                            for decoded in self.decoder.feed(frame):
                                if hasattr(decoded, "host_time"):
                                    decoded.host_time = now
                                self.emit(decoded)
                        if not self.radio.available():
                            break
            except Exception as exc:  # noqa: BLE001
                self.last_error = f"read: {exc}"
            finally:
                self.connected = False
                self.close()

            if not self._stop.is_set():
                self._stop.wait(3.0)

    def close(self) -> None:
        if self.radio is not None:
            try:
                self.radio.listen = False
                self.radio.power = False
            except Exception:
                pass
            self.radio = None

    def info(self) -> dict:
        base = super().info()
        base["radio"] = {
            "channel": self.channel,
            "frequency_mhz": 2400 + self.channel,
            "reassembly": self.reasm.stats(),
            "crypto": self.crypto.stats() if self.crypto else "off",
        }
        return base
