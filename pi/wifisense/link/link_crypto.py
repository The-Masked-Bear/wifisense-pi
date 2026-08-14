"""AES-128-CTR for the radio uplink.  Mirrors firmware/src/link_crypto.cpp.

CSI is not innocuous telemetry: it reveals when a room is occupied, when
people move, and when they sleep.  Transmitted in the clear, anyone in range
with a matching radio on the same channel reads all of it.  Encrypting the
payload makes the stream useless to a passive listener without the key.

Wire format of an encrypted radio frame, before fragmentation::

    +-----------+-----------+---------------------------+
    | session   | counter   | ciphertext                |
    | u32 LE    | u32 LE    | N bytes                   |
    +-----------+-----------+---------------------------+

The 8-byte header is in the clear because the receiver needs it to rebuild the
counter block; it leaks only a random session id and a frame counter.  The CTR
nonce is ``session || counter || 8 zero bytes``.

Counter-mode security rests entirely on never reusing a (key, nonce) pair.  The
counter is 32 bits -- 497 days at 100 Hz -- and the sender draws a fresh random
session id at every boot, so a reset starts a new nonce space instead of
replaying the old one.

This is CONFIDENTIALITY, not authentication.  The CRC16 inside the encrypted
payload means a random forgery survives with probability 1/65536, which is
proportionate for a home sensor link, but it is not a MAC.
"""

from __future__ import annotations

import struct

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

HEADER = struct.Struct("<II")
HEADER_LEN = HEADER.size  # 8


class LinkCrypto:
    """Decrypts frames produced by the firmware's crypto_encrypt()."""

    def __init__(self, key: bytes | str) -> None:
        if isinstance(key, str):
            key = bytes.fromhex(key.strip().replace(":", ""))
        if len(key) != 16:
            raise ValueError(f"radio key must be 16 bytes (32 hex chars), got {len(key)}")
        self.key = key
        self.decrypted = 0
        self.short = 0
        self._seen: dict[int, int] = {}
        self.replays = 0
        self.resyncs = 0
        self._consecutive = 0
        # Consecutive rejections before assuming the sender restarted.
        self.resync_after = 50

    def decrypt(self, blob: bytes) -> bytes | None:
        """Strip the header and decrypt.  Returns None if the frame is unusable."""
        if len(blob) <= HEADER_LEN:
            self.short += 1
            return None
        session, counter = HEADER.unpack_from(blob, 0)

        # Reject a counter we have already accepted for this session.  Over a
        # one-way radio link a replayed frame is otherwise indistinguishable
        # from a fresh one, and stale CSI injected into the pipeline would read
        # as real motion.
        last = self._seen.get(session)
        if last is not None and counter <= last:
            self.replays += 1
            self._consecutive += 1
            # A genuine replay is occasional.  A long unbroken run of them means
            # the sender restarted its counter under a session id we have seen
            # before -- which should not happen, but did when the node's RNG was
            # unseeded at boot.  Resynchronise rather than rejecting every frame
            # forever, and say so, because silently refusing all traffic is a
            # far worse failure than accepting a stale frame.
            if self._consecutive >= self.resync_after:
                self._seen[session] = counter
                self._consecutive = 0
                self.resyncs += 1
                nonce = HEADER.pack(session, counter) + b"\x00" * 8
                cipher = Cipher(algorithms.AES(self.key), modes.CTR(nonce)).decryptor()
                self.decrypted += 1
                return cipher.update(blob[HEADER_LEN:]) + cipher.finalize()
            return None
        self._consecutive = 0
        self._seen[session] = counter
        if len(self._seen) > 8:
            # A handful of sessions is normal across node reboots; unbounded
            # growth is not.
            oldest = min(self._seen, key=lambda s: self._seen[s])
            self._seen.pop(oldest, None)

        nonce = HEADER.pack(session, counter) + b"\x00" * 8
        cipher = Cipher(algorithms.AES(self.key), modes.CTR(nonce)).decryptor()
        out = cipher.update(blob[HEADER_LEN:]) + cipher.finalize()
        self.decrypted += 1
        return out

    def encrypt(self, payload: bytes, session: int, counter: int) -> bytes:
        """Inverse of :meth:`decrypt`.  Used by the tests."""
        nonce = HEADER.pack(session, counter) + b"\x00" * 8
        cipher = Cipher(algorithms.AES(self.key), modes.CTR(nonce)).encryptor()
        return HEADER.pack(session, counter) + cipher.update(payload) + cipher.finalize()

    def stats(self) -> dict:
        return {
            "decrypted": self.decrypted,
            "short": self.short,
            "replays": self.replays,
            "resyncs": self.resyncs,
            "sessions": len(self._seen),
        }
