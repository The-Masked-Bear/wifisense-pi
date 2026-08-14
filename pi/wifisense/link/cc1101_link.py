"""CC1101 sub-GHz receiver on the Pi's SPI0 CE1.

Role in this system
-------------------
The CC1101's advantage over every other transport here is that it operates at
433 or 868 MHz -- **completely outside the 2.4 GHz band being sensed**.  It can
therefore run continuously without perturbing the measurement, which is not
true of the nRF24.

Its disadvantage is throughput.  At the configured 100 kBaud a 60-byte fragment
takes ~5.8 ms on the air, so a three-fragment CSI frame caps at ~55 Hz even
with a perfect receiver.  Fast enough for motion and breathing (which needs
~20 Hz), not for the full 100 Hz the USB cable carries.

Wiring (see PROJECT.txt for the full table)::

    module   BCM   physical
    CSN       7    26        (CE1 -> /dev/spidev0.1)
    SCK      11    23
    MOSI     10    19
    MISO      9    21
    GDO0     23    16        (not needed by this driver -- it polls over SPI)
    GDO2     27    13        (not needed by this driver)
    VCC      --     1        3.3 V ONLY -- not 5 V tolerant on any pin
    GND      --     9

Check the silkscreen: several CC1101 breakout layouts exist and their pin
*orders* differ, so match by label rather than by position.  Attach an antenna
matched to the module's band before powering it; transmitting into an open port
can damage the PA.

Receive path notes (the parts that bit us, in case they bite again)
-------------------------------------------------------------------
* The FIFO is parsed as variable-length packets: length byte, that many payload
  bytes, two appended status bytes (RSSI, LQI with the CRC-ok flag in bit 7).
  python-cc1101's ``_get_received_packet`` is NOT usable here: it dumps the
  whole FIFO the moment two bytes are present, which returns a mid-packet
  truncation *including the length byte* and can glue two packets together.
  The reassembler then never sees a clean [seq][frag][...] boundary and the
  link delivers zero frames while looking perfectly healthy on SPI.
* MCSM1.RXOFF_MODE is set to "stay in RX", so the chip keeps listening after
  each packet instead of dropping to IDLE and missing whatever arrives while
  Python is between polls.
* The SPI clock is raised to 5 MHz **after** the library's open handshake.
  At its 55.7 kHz default, reading one 63-byte FIFO takes ~9 ms -- longer than
  the packet's own airtime, so the receiver could never keep up with
  back-to-back fragments.  But entering at 5 MHz fails outright ~70% of the
  time: the library reads PARTNUM immediately after the SRES strobe with no
  settling delay, and at 5 MHz the chip is still resetting (measured on this
  wiring).  So: open slow, verify, then bump.  5 MHz is within the CC1101's
  6.5 MHz burst-access limit and the wiring already carries 10 MHz to the
  nRF24.
* MARCSTATE is read raw, not through the library's enum: the enum raises
  ValueError on the transient calibration states (IFADCON & friends), which are
  exactly what a live radio reports while entering RX.
"""

from __future__ import annotations

import time

from .base import Link
from .link_crypto import LinkCrypto
from .radio_common import Reassembler, trim_to_terminator

# The CC1101's RX FIFO is 64 bytes; the node sends at most a 60-byte packet
# (2-byte fragment header + 58 payload), leaving headroom against overrun.
MAX_PACKET = 60

# MARCSTATE (raw values; the library's IntEnum is incomplete on purpose).
MARCSTATE_RX = 0x0D
MARCSTATE_RXFIFO_OVERFLOW = 0x11

# MCSM1: CCA_MODE=11 (chip default), RXOFF_MODE=11 -> stay in RX after each
# packet, TXOFF_MODE=00 (unused; this end never transmits).
MCSM1_STAY_IN_RX = 0x3C

# 5 MHz: fast enough that draining a full 63-byte FIFO (~100 us) is trivially
# quicker than a packet's ~5.8 ms airtime, and inside the chip's 6.5 MHz
# burst-access limit.
SPI_HZ = 5_000_000


class CC1101Link(Link):
    """Receives fragmented frames over a CC1101."""

    name = "cc1101"

    def __init__(
        self,
        *,
        bus: int = 0,
        device: int = 1,
        frequency_mhz: float = 433.92,
        baud: float = 38_400.0,
        sync_word: bytes = b"\xd3\x91",
        key: bytes | str | None = None,
    ) -> None:
        super().__init__()
        self.bus = bus
        self.device = device
        self.frequency_mhz = frequency_mhz
        self.baud = baud
        self.sync_word = sync_word
        self.reasm = Reassembler()
        # Decrypts what the node sealed; None means the link is expected
        # to be plaintext (only useful while debugging).
        self.crypto = LinkCrypto(key) if key else None
        self._radio = None
        self.packets = 0
        self.crc_bad = 0
        self.length_bad = 0
        self.truncated = 0
        self.rearms = 0
        self.overflows = 0

    def _read_pending(self, radio, cc1101) -> list[bytes]:
        """Drain every complete packet in the RX FIFO, payloads only.

        Variable-length mode layout per packet: [len][len bytes][RSSI][LQI],
        with the CRC-ok flag in bit 7 of LQI.  CRC-bad packets are dropped
        here, before they can poison fragment reassembly.
        """
        strobe = radio._command_strobe
        read_burst = radio._read_burst
        rxbytes = radio._read_status_register

        out: list[bytes] = []
        n = rxbytes(cc1101.StatusRegisterAddress.RXBYTES)
        if n & 0x80:  # overflow latched: FIFO contents unusable until flushed
            self.overflows += 1
            strobe(cc1101.StrobeAddress.SIDLE)
            strobe(cc1101.StrobeAddress.SFRX)
            strobe(cc1101.StrobeAddress.SRX)
            return out
        n &= 0x7F

        while n > 0:
            length = read_burst(start_register=cc1101.FIFORegisterAddress.RX, length=1)[0]
            if length < 2 or length > MAX_PACKET:
                # Noise false-sync; the rest of the FIFO is unsalvageable.
                self.length_bad += 1
                strobe(cc1101.StrobeAddress.SIDLE)
                strobe(cc1101.StrobeAddress.SFRX)
                strobe(cc1101.StrobeAddress.SRX)
                return out

            # The rest of the packet may still be arriving over the air
            # (~80 us/byte at 100 kBaud).  Wait for it, bounded.
            deadline = time.monotonic() + 0.05
            while (rxbytes(cc1101.StatusRegisterAddress.RXBYTES) & 0x7F) < length + 2:
                if time.monotonic() > deadline:
                    self.truncated += 1
                    strobe(cc1101.StrobeAddress.SIDLE)
                    strobe(cc1101.StrobeAddress.SFRX)
                    strobe(cc1101.StrobeAddress.SRX)
                    return out
                time.sleep(0.0005)

            buf = read_burst(
                start_register=cc1101.FIFORegisterAddress.RX, length=length + 2
            )
            if buf[length + 1] & 0x80:  # CRC ok
                out.append(bytes(buf[:length]))
            else:
                self.crc_bad += 1
            n = rxbytes(cc1101.StatusRegisterAddress.RXBYTES) & 0x7F
        return out

    def run(self) -> None:
        while not self._stop.is_set():
            try:
                import cc1101
            except ImportError as exc:
                self.last_error = f"cc1101 package missing: {exc}"
                self.connected = False
                self._stop.wait(10.0)
                continue

            # Reset the chip before the library opens it.  python-cc1101
            # probes PARTNUM on __enter__, and a chip left mid-RX or with a
            # latched FIFO overflow by a previous run answers that probe with
            # garbage (0xFF / 0xE1), so the open raises and the link only comes
            # up on a later retry.  A bare SRES strobe puts it back in a known
            # state first.
            try:
                import spidev

                _raw = spidev.SpiDev()
                _raw.open(self.bus, self.device)
                _raw.max_speed_hz = 500_000
                _raw.mode = 0
                _raw.xfer2([0x30])  # SRES
                _raw.close()
                time.sleep(0.05)
            except Exception:
                pass

            try:
                # Open at the library's slow default.  It reads PARTNUM
                # immediately after its SRES strobe with no settling delay, and
                # at 5 MHz the chip is still resetting -- entering fast fails
                # roughly 70% of the time on this wiring.  Open slow, let it
                # verify, then raise the clock for the FIFO reads that actually
                # need the bandwidth.
                with cc1101.CC1101(
                    spi_bus=self.bus,
                    spi_chip_select=self.device,
                ) as radio:
                    radio._spi.max_speed_hz = SPI_HZ
                    self._radio = radio
                    # EVERY register below must match the transmitter exactly.
                    # Left on chip defaults the receiver sits on 800 MHz doing
                    # OOK at 115 kBaud while the node transmits 2-FSK at
                    # 433.92 MHz -- a different band and a different
                    # modulation.  It initialises perfectly and hears nothing.
                    #
                    # These are the same values firmware/src/cc1101_driver.cpp
                    # writes, in the same order, so the two ends are provably
                    # identical rather than "configured similarly".
                    R = cc1101.ConfigurationRegisterAddress
                    for name, value in (
                        ("FREQ2", 0x10), ("FREQ1", 0xB0), ("FREQ0", 0x71),
                        ("MDMCFG4", 0xCA), ("MDMCFG3", 0x83), ("MDMCFG2", 0x02),
                        ("MDMCFG1", 0x42), ("MDMCFG0", 0xF8), ("DEVIATN", 0x35),
                        ("SYNC1", 0xD3), ("SYNC0", 0x91),
                        ("PKTLEN", 0x3D), ("PKTCTRL1", 0x04), ("PKTCTRL0", 0x05),
                        ("ADDR", 0x00), ("CHANNR", 0x00),
                        ("FSCTRL1", 0x06), ("FSCTRL0", 0x00),
                        ("MCSM0", 0x18), ("MCSM1", MCSM1_STAY_IN_RX),
                        ("FOCCFG", 0x16), ("BSCFG", 0x6C),
                        # python-cc1101 spells these AGCTRL* (one C); accept either.
                        ("AGCTRL2", 0x43), ("AGCTRL1", 0x40), ("AGCTRL0", 0x91),
                        ("FREND1", 0x56), ("FREND0", 0x10),
                        ("FSCAL3", 0xE9), ("FSCAL2", 0x2A),
                        ("FSCAL1", 0x00), ("FSCAL0", 0x1F),
                        ("TEST2", 0x81), ("TEST1", 0x35), ("TEST0", 0x09),
                        ("FIFOTHR", 0x07),
                    ):
                        reg = getattr(R, name, None)
                        if reg is not None:
                            radio._write_burst(start_register=reg, values=[value])

                    def rearm() -> None:
                        """Flush the RX FIFO and (re-)enter receive mode.

                        The CC1101 does not stay in RX by itself: it drops to
                        IDLE after a packet, and on a FIFO overflow it latches
                        into RXFIFO_OVERFLOW and stops receiving permanently
                        until explicitly flushed.
                        """
                        radio._command_strobe(cc1101.StrobeAddress.SIDLE)
                        radio._command_strobe(cc1101.StrobeAddress.SFRX)
                        radio._command_strobe(cc1101.StrobeAddress.SRX)

                    self.connected = True
                    self.last_error = None
                    self.decoder.reset()
                    # Enter receive mode.  Configuring the registers does not do
                    # this -- without the strobe the radio sits in IDLE and
                    # hears nothing at all, however correct the settings are.
                    rearm()
                    last_check = time.monotonic()

                    while not self._stop.is_set():
                        for payload in self._read_pending(radio, cc1101):
                            self.packets += 1
                            frame = self.reasm.push(payload)
                            if frame is not None and self.crypto is not None:
                                frame = self.crypto.decrypt(frame)
                            if frame:
                                # Drop any fixed-payload padding before framing.
                                frame = trim_to_terminator(frame)
                                now = time.monotonic()
                                for decoded in self.decoder.feed(frame):
                                    if hasattr(decoded, "host_time"):
                                        decoded.host_time = now
                                    self.emit(decoded)

                        # Periodically confirm the radio is still listening.
                        # Cheap at 5 Hz, and it is the difference between a
                        # link that recovers and one that silently dies.
                        now = time.monotonic()
                        if now - last_check > 0.2:
                            last_check = now
                            state = radio._read_status_register(
                                cc1101.StatusRegisterAddress.MARCSTATE
                            ) & 0x1F
                            if state != MARCSTATE_RX:
                                if state == MARCSTATE_RXFIFO_OVERFLOW:
                                    self.overflows += 1
                                rearm()
                                self.rearms += 1
                        time.sleep(0.001)
            except Exception as exc:  # noqa: BLE001 - surfaced in the UI
                self.last_error = f"{type(exc).__name__}: {exc}"
            finally:
                self.connected = False
                self._radio = None

            if not self._stop.is_set():
                self._stop.wait(5.0)

    def info(self) -> dict:
        base = super().info()
        base["radio"] = {
            "frequency_mhz": self.frequency_mhz,
            "baud": self.baud,
            "reassembly": self.reasm.stats(),
            "crypto": self.crypto.stats() if self.crypto else "off",
            "packets": self.packets,
            "crc_bad": self.crc_bad,
            "length_bad": self.length_bad,
            "truncated": self.truncated,
            "rearms": self.rearms,
            "overflows": self.overflows,
        }
        return base
