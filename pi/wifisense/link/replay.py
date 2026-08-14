"""Replay a recorded session as if it were a live link.

Timing is reproduced from the recorded arrival times rather than emitted at a
fixed rate, because the jitter is part of what the DSP has to cope with -- a
perfectly regular replay would hide resampling bugs that real data exposes.
"""

from __future__ import annotations

import time

from ..recorder import read_session
from .base import Link


class ReplayLink(Link):
    """Feeds a recording back through the normal decode path."""

    name = "replay"

    def __init__(self, path: str, *, speed: float = 1.0, loop: bool = True) -> None:
        super().__init__()
        self.path = path
        self.speed = max(speed, 0.01)
        self.loop = loop
        self.position = 0.0
        # Origin of the synthetic timeline handed to the detectors, advanced by
        # one recording length per loop so a looping replay never steps
        # backwards in time (which would corrupt every rolling window).
        self._epoch = 0.0
        self._last_stamp = 0.0

    def run(self) -> None:
        while not self._stop.is_set():
            self.connected = True
            start_wall = time.monotonic()
            start_rec: float | None = None
            count = 0

            try:
                for rec_time, raw in read_session(self.path):
                    if self._stop.is_set():
                        break
                    if start_rec is None:
                        start_rec = rec_time

                    # Sleep until this frame is due, preserving the original
                    # inter-arrival pattern (scaled by `speed`).
                    target = start_wall + (rec_time - start_rec) / self.speed
                    delay = target - time.monotonic()
                    if delay > 0:
                        if self._stop.wait(min(delay, 0.5)):
                            break
                        continue_delay = target - time.monotonic()
                        if continue_delay > 0:
                            time.sleep(min(continue_delay, 0.5))

                    self.position = rec_time - start_rec
                    # Stamp the *recorded* timeline, not the arrival time.  The
                    # detectors size their windows in seconds, so stamping
                    # arrival would make a 2x replay present half as much
                    # history and a 4x replay never fill the analysis window at
                    # all -- the same recording would yield different answers at
                    # different speeds.  Using recorded time makes a replay
                    # reproduce exactly what the live capture produced.
                    stamp = self._epoch + (rec_time - start_rec)
                    for frame in self.decoder.feed(raw):
                        if hasattr(frame, "host_time"):
                            frame.host_time = stamp
                        self.emit(frame)
                    count += 1
                    self._last_stamp = stamp
            except (OSError, ValueError) as exc:
                self.last_error = f"replay: {exc}"
                self.connected = False
                return

            if not self.loop or count == 0:
                break
            # Continue the timeline where this pass ended, plus one sample gap.
            self._epoch = self._last_stamp + 0.01
            self.decoder.reset()

        self.connected = False
