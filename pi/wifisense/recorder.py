"""Recording and replay of raw CSI sessions.

Recording captures the *encoded* frames rather than decoded objects, so a
replay exercises the entire decode path -- framing, CRC, sanitisation, DSP --
exactly as a live link would.  A bug that only shows up on real data can then
be reproduced deterministically instead of waiting for the room to misbehave
again.

File format, repeated until EOF::

    f64  host_time   monotonic seconds at arrival
    u16  length      bytes of frame that follow
    ...  frame       the COBS-encoded frame including its 0x00 terminator

A 16-byte header identifies the file and records the wall-clock start so a
session can be dated.
"""

from __future__ import annotations

import struct
import time
from pathlib import Path

MAGIC = b"CSIREC01"
HEADER = struct.Struct("<8sd")
RECORD = struct.Struct("<dH")


class Recorder:
    """Appends raw frames to a session file."""

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._fh = open(self.path, "wb")
        self._fh.write(HEADER.pack(MAGIC, time.time()))
        self.frames = 0
        self.bytes = 0

    def write(self, raw: bytes, host_time: float | None = None) -> None:
        if len(raw) > 0xFFFF:
            return
        self._fh.write(RECORD.pack(host_time or time.monotonic(), len(raw)))
        self._fh.write(raw)
        self.frames += 1
        self.bytes += len(raw)

    def close(self) -> None:
        try:
            self._fh.flush()
            self._fh.close()
        except (OSError, ValueError):
            pass

    def __enter__(self) -> "Recorder":
        return self

    def __exit__(self, *exc) -> None:
        self.close()


def read_session(path: Path | str):
    """Yield ``(host_time, raw_frame)`` pairs from a recording."""
    p = Path(path)
    with open(p, "rb") as fh:
        head = fh.read(HEADER.size)
        if len(head) < HEADER.size:
            return
        magic, _started = HEADER.unpack(head)
        if magic != MAGIC:
            raise ValueError(f"{p} is not a CSI recording")
        while True:
            rec = fh.read(RECORD.size)
            if len(rec) < RECORD.size:
                return
            t, length = RECORD.unpack(rec)
            payload = fh.read(length)
            if len(payload) < length:
                return
            yield t, payload


def session_info(path: Path | str) -> dict:
    p = Path(path)
    frames = 0
    first = last = None
    for t, _raw in read_session(p):
        frames += 1
        if first is None:
            first = t
        last = t
    duration = (last - first) if (first is not None and last is not None) else 0.0
    return {
        "path": str(p),
        "frames": frames,
        "duration": round(duration, 2),
        "rate": round(frames / duration, 1) if duration > 0 else 0.0,
        "size": p.stat().st_size if p.exists() else 0,
    }
