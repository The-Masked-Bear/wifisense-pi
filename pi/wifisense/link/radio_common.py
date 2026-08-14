"""Fragmentation and reassembly shared by the CC1101 and nRF24 links.

Both radios carry payloads far smaller than a CSI frame: the nRF24L01+ is
capped at 32 bytes per packet, the CC1101 at a 64-byte FIFO, while one CSI
frame is ~149 bytes.  Frames therefore have to be split and put back together.

Wire format of each radio packet::

    +--------+--------+------------------+
    | seq    | frag   | payload          |
    | u8     | u8     | 0..N bytes       |
    +--------+--------+------------------+

``seq`` identifies the frame, and increments per frame (wrapping at 256).
``frag`` is the fragment index in the low 7 bits; bit 7 marks the last
fragment.  A fragment whose ``seq`` does not match the one being assembled
abandons the partial frame -- over a lossy radio, resynchronising quickly
matters far more than salvaging a frame that is already missing a piece.

The reassembled bytes are ordinary COBS frames, so they decode through exactly
the same path as the serial link.
"""

from __future__ import annotations

LAST_FLAG = 0x80


def fragment(payload: bytes, seq: int, max_packet: int) -> list[bytes]:
    """Split ``payload`` into radio packets of at most ``max_packet`` bytes."""
    body = max_packet - 2
    if body < 1:
        raise ValueError("max_packet must exceed the 2-byte header")
    out: list[bytes] = []
    total = (len(payload) + body - 1) // body or 1
    for i in range(total):
        chunk = payload[i * body : (i + 1) * body]
        flag = LAST_FLAG if i == total - 1 else 0
        out.append(bytes([seq & 0xFF, (i & 0x7F) | flag]) + chunk)
    return out


class Reassembler:
    """Rebuilds frames from radio fragments, tolerating loss."""

    def __init__(self, max_frame: int = 2048) -> None:
        self.max_frame = max_frame
        self._seq: int | None = None
        self._next = 0
        self._buf = bytearray()
        self.completed = 0
        self.dropped = 0
        self.duplicates = 0

    def push(self, packet: bytes) -> bytes | None:
        """Feed one radio packet.  Returns a complete frame, or None."""
        if len(packet) < 2:
            return None
        seq, frag = packet[0], packet[1]
        index = frag & 0x7F
        last = bool(frag & LAST_FLAG)

        # Duplicate fragment: same frame, and an index we have already taken.
        # Auto-ack retransmits whenever the acknowledgement is lost rather than
        # the data, so the receiver legitimately sees the same fragment twice.
        # Treating that as an error abandoned a frame that was in fact arriving
        # perfectly well -- which showed up as far more "lost" frames than were
        # ever sent.  Ignore it and keep going.
        if self._seq == seq and self._buf and index < self._next:
            self.duplicates += 1
            return None

        if self._seq != seq or index != self._next:
            # Either a new frame started, or we missed a fragment of this one.
            if self._buf:
                self.dropped += 1
            if index != 0:
                # Joined mid-frame; wait for a fresh start rather than emit a
                # frame with a hole in it, which would fail CRC anyway.
                self._seq = None
                self._buf.clear()
                return None
            self._seq = seq
            self._buf.clear()
            self._next = 0

        self._buf.extend(packet[2:])
        self._next = index + 1

        if len(self._buf) > self.max_frame:
            self._buf.clear()
            self._seq = None
            self.dropped += 1
            return None

        if last:
            frame = bytes(self._buf)
            self._buf.clear()
            self._seq = None
            self._next = 0
            self.completed += 1
            return frame
        return None

    def stats(self) -> dict:
        return {"completed": self.completed, "dropped": self.dropped,
                "duplicates": self.duplicates}


def trim_to_terminator(frame: bytes) -> bytes:
    """Cut a reassembled payload back to its COBS terminator.

    The nRF24 sends fixed-size payloads, so the final fragment of every frame
    carries padding.  Unencrypted that padding is zeros, which the framer
    happily skips as empty chunks.  Encrypted, the very same padding decrypts
    to *random* bytes: they get appended to the decoder's buffer, merge with
    the front of the next frame, and corrupt it -- so the link works in the
    clear and fails the moment encryption is switched on, which is a
    thoroughly misleading symptom.

    A COBS frame contains no interior zero, so the first zero byte is always
    the real terminator and everything after it is padding.
    """
    end = frame.find(0)
    return frame if end < 0 else frame[: end + 1]
