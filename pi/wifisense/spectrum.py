"""Sub-GHz spectrum monitor using the Pi's CC1101.

The CC1101 sits idle whenever the CSI transport is the nRF24 or the USB cable,
so it can spend that time listening.  Sweeping the 433 MHz band costs nothing
and adds a genuinely independent channel to the system: at 433 MHz the
wavelength is 69 cm against 12.5 cm for WiFi, so it penetrates walls better and
responds to a different physical scale.

Practically it answers two questions:

* **What is transmitting near me?**  Cheap sensors, doorbells, car remotes and
  tyre-pressure monitors all live in this band, and they are close enough that
  even a poorly matched antenna hears them.
* **Is something interfering?**  A new persistent emitter next to the band is
  worth knowing about, both on its own and because it may affect the WiFi
  sensing.

Receiving works even when the *link* does not.  Hearing an emitter only
requires that something out there is loud enough; a link additionally requires
one specific weak transmitter to reach one specific receiver, which is a far
harder budget.
"""

from __future__ import annotations

import threading
import time

import numpy as np

# The licence-free segment is 433.05-434.79 MHz.  Sweeping a little either side
# catches neighbours that sit just outside it.
DEFAULT_START_MHZ = 430.0
DEFAULT_STOP_MHZ = 435.0
DEFAULT_STEPS = 32

# Display range for the waterfall.  -110 dBm is below any real noise floor and
# -55 is a very strong local emitter, so this spans everything of interest.
DISPLAY_MIN_DBM = -110.0
DISPLAY_MAX_DBM = -55.0

# A channel counts as active when it rises this far above its own learned
# quiet level.  8 dB is comfortably beyond the ~2 dB the noise floor wanders.
EVENT_THRESHOLD_DB = 8.0


def _rssi_dbm(raw: int) -> float:
    """Convert the CC1101's RSSI register to dBm (datasheet section 17.3)."""
    return (raw - 256) / 2 - 74 if raw >= 128 else raw / 2 - 74


class SubGHzScanner:
    """Sweeps the 433 MHz band and reports levels and activity."""

    def __init__(
        self,
        *,
        bus: int = 0,
        device: int = 1,
        start_mhz: float = DEFAULT_START_MHZ,
        stop_mhz: float = DEFAULT_STOP_MHZ,
        steps: int = DEFAULT_STEPS,
        history: int = 400,
        samples_per_channel: int = 8,
    ) -> None:
        self.bus = bus
        self.device = device
        self.freqs = np.linspace(start_mhz, stop_mhz, steps)
        self.samples_per_channel = samples_per_channel

        self.rows: list[list[int]] = []  # quantised 0-255, for the waterfall
        self.total_rows = 0
        self.latest_dbm = np.full(steps, DISPLAY_MIN_DBM)
        self._history = np.full((history, steps), np.nan)
        self._hist_n = 0
        self._hist_head = 0

        self.available = False
        self.last_error: str | None = None
        self.sweeps = 0
        self.enabled = True

        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._lock = threading.Lock()

    # ------------------------------------------------------------- lifecycle

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="spectrum", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=3.0)

    def pause(self) -> None:
        """Yield the radio, e.g. when the CC1101 becomes the CSI transport."""
        self.enabled = False

    def resume(self) -> None:
        self.enabled = True

    # ------------------------------------------------------------------ scan

    def _run(self) -> None:
        while not self._stop.is_set():
            if not self.enabled:
                self._stop.wait(1.0)
                continue
            try:
                import cc1101
            except ImportError as exc:
                self.last_error = f"cc1101 package missing: {exc}"
                self.available = False
                self._stop.wait(30.0)
                continue

            try:
                self._sweep_forever(cc1101)
            except Exception as exc:  # noqa: BLE001 - surfaced in the UI
                self.last_error = f"{type(exc).__name__}: {exc}"
                self.available = False
                if not self._stop.is_set():
                    self._stop.wait(10.0)

    def _sweep_forever(self, cc1101) -> None:
        with cc1101.CC1101(spi_bus=self.bus, spi_chip_select=self.device) as radio:
            # Open at the library's slow default, then raise the clock -- it
            # probes PARTNUM straight after its reset strobe and reads garbage
            # if the bus is already fast.
            radio._spi.max_speed_hz = 5_000_000

            R = cc1101.ConfigurationRegisterAddress
            # Wide receive bandwidth (203 kHz) so each step covers ground, and
            # AGC settings that let the RSSI reading track a wide dynamic range.
            for name, value in (
                ("MDMCFG4", 0x8C), ("MDMCFG3", 0x22), ("MDMCFG2", 0x02),
                ("AGCTRL2", 0x43), ("AGCTRL1", 0x40), ("AGCTRL0", 0x91),
                ("MCSM0", 0x18), ("FSCTRL1", 0x06), ("FSCTRL0", 0x00),
            ):
                reg = getattr(R, name, None)
                if reg is not None:
                    radio._write_burst(start_register=reg, values=[value])

            self.available = True
            self.last_error = None

            while not self._stop.is_set() and self.enabled:
                row = np.empty(self.freqs.size)
                for i, mhz in enumerate(self.freqs):
                    if self._stop.is_set() or not self.enabled:
                        return
                    radio._command_strobe(cc1101.StrobeAddress.SIDLE)
                    radio.set_base_frequency_hertz(float(mhz) * 1e6)
                    # Recalibrate on every hop: the synthesiser must relock, and
                    # reading RSSI before it has is how you get a plausible
                    # looking spectrum made entirely of noise.
                    radio._command_strobe(cc1101.StrobeAddress.SCAL)
                    time.sleep(0.004)
                    radio._command_strobe(cc1101.StrobeAddress.SRX)
                    time.sleep(0.006)
                    vals = []
                    for _ in range(self.samples_per_channel):
                        vals.append(
                            _rssi_dbm(
                                radio._read_status_register(
                                    cc1101.StatusRegisterAddress.RSSI
                                )
                            )
                        )
                        time.sleep(0.0015)
                    # Peak, not mean: a burst transmission occupies the channel
                    # for a few milliseconds, and averaging buries it in the
                    # surrounding silence.
                    row[i] = max(vals)
                self._commit(row)

    # ---------------------------------------------------------------- output

    def _commit(self, row: np.ndarray) -> None:
        with self._lock:
            self.latest_dbm = row
            self._history[self._hist_head] = row
            self._hist_head = (self._hist_head + 1) % self._history.shape[0]
            self._hist_n = min(self._hist_n + 1, self._history.shape[0])
            self.sweeps += 1

            scaled = np.clip(
                (row - DISPLAY_MIN_DBM) / (DISPLAY_MAX_DBM - DISPLAY_MIN_DBM), 0.0, 1.0
            )
            self.rows.append([int(v) for v in (scaled * 255).astype(np.uint8)])
            if len(self.rows) > 600:
                del self.rows[: len(self.rows) - 600]
            self.total_rows += 1

    def baseline(self) -> np.ndarray:
        """Per-channel quiet level: a low quantile of recent history.

        A low quantile rather than a mean, because a channel that is busy half
        the time would otherwise learn its own traffic as the floor and stop
        reporting it.
        """
        with self._lock:
            if self._hist_n < 8:
                return np.full(self.freqs.size, DISPLAY_MIN_DBM)
            hist = self._history[: self._hist_n]
        return np.nanquantile(hist, 0.20, axis=0)

    def events(self) -> list[dict]:
        """Channels currently sitting well above their own quiet level."""
        base = self.baseline()
        with self._lock:
            cur = self.latest_dbm.copy()
        if self._hist_n < 8:
            return []
        over = cur - base
        out = []
        for i in np.argsort(over)[::-1]:
            if over[i] < EVENT_THRESHOLD_DB:
                break
            out.append(
                {
                    "mhz": round(float(self.freqs[i]), 2),
                    "dbm": round(float(cur[i]), 1),
                    "over": round(float(over[i]), 1),
                }
            )
        return out[:6]

    def snapshot(self, new_rows: int = 0) -> dict:
        with self._lock:
            rows = self.rows[-new_rows:] if new_rows > 0 else []
            latest = self.latest_dbm.copy()
            total = self.total_rows
        return {
            "available": self.available,
            "error": self.last_error,
            "freqs_mhz": [round(float(f), 2) for f in self.freqs],
            "latest_dbm": [round(float(v), 1) for v in latest],
            "baseline_dbm": [round(float(v), 1) for v in self.baseline()],
            "rows": rows,
            "total_rows": total,
            "sweeps": self.sweeps,
            "events": self.events(),
            "bins": int(self.freqs.size),
        }
