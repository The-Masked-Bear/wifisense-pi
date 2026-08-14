"""Link abstraction.

Every transport -- USB CDC, UART, CC1101, nRF24, a recorded file, or the
synthetic generator -- presents the same interface: start it, and it pushes
decoded protocol frames into a queue.  The pipeline never learns which one it
is talking to, which is what makes the four transports interchangeable at
runtime.
"""

from __future__ import annotations

import abc
import threading
import time
from queue import Empty, Full, Queue

from ..protocol import DecodeStats, FrameDecoder


class Link(abc.ABC):
    """Base class for a frame source."""

    name = "link"

    def __init__(self, queue_size: int = 4096) -> None:
        self.queue: Queue = Queue(maxsize=queue_size)
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self.decoder = FrameDecoder()
        self.connected = False
        self.last_error: str | None = None
        self.dropped_full = 0
        self.started_at = 0.0

    # ------------------------------------------------------- lifecycle

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self.started_at = time.monotonic()
        self._thread = threading.Thread(target=self._run_guarded, name=f"link-{self.name}", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2.0)
        self.close()
        self.connected = False

    def _run_guarded(self) -> None:
        try:
            self.run()
        except Exception as exc:  # noqa: BLE001 - a link death must not be silent
            self.last_error = f"{type(exc).__name__}: {exc}"
            self.connected = False

    @abc.abstractmethod
    def run(self) -> None:
        """Read from the transport until :attr:`_stop` is set."""

    def close(self) -> None:
        """Release the transport.  Override when there is something to free."""

    # --------------------------------------------------------- plumbing

    def emit(self, frame) -> None:
        """Hand a decoded frame to the consumer.

        When the consumer falls behind, the *oldest* frame is discarded rather
        than the newest.  For a real-time display, stale data has no value --
        showing the room as it was two seconds ago is worse than skipping.
        """
        try:
            self.queue.put_nowait(frame)
        except Full:
            self.dropped_full += 1
            try:
                self.queue.get_nowait()
                self.queue.put_nowait(frame)
            except (Empty, Full):
                pass

    def feed_bytes(self, data: bytes) -> None:
        """Push raw stream bytes through the framer and emit what comes out."""
        for frame in self.decoder.feed(data):
            self.emit(frame)

    def drain(self, limit: int = 512) -> list:
        """Pop up to ``limit`` frames.  Non-blocking."""
        out = []
        for _ in range(limit):
            try:
                out.append(self.queue.get_nowait())
            except Empty:
                break
        return out

    @property
    def stats(self) -> DecodeStats:
        return self.decoder.stats

    def info(self) -> dict:
        return {
            "name": self.name,
            "connected": self.connected,
            "error": self.last_error,
            "queued": self.queue.qsize(),
            "dropped_full": self.dropped_full,
            "decode": self.decoder.stats.as_dict(),
        }
