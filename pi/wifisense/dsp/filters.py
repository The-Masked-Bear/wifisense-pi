"""Signal-processing primitives used by the CSI pipeline.

Nothing here knows about WiFi -- these are the generic pieces (outlier
rejection, band-pass design, ring buffers, uniform resampling) that the
motion and breathing detectors are built from.
"""

from __future__ import annotations

import numpy as np
from scipy import signal


# ------------------------------------------------------------ outlier removal


def hampel(x: np.ndarray, window: int = 7, n_sigmas: float = 3.0) -> np.ndarray:
    """Hampel filter: replace outliers with the local median.

    CSI amplitude is riddled with single-sample spikes caused by AGC steps and
    the odd corrupted symbol.  A plain low-pass smears those spikes across
    neighbouring samples; the Hampel filter removes them outright before any
    smoothing happens.

    A point is an outlier when it sits more than ``n_sigmas`` robust standard
    deviations from the median of the window centred on it, where the robust
    deviation is 1.4826 * MAD (the constant makes MAD a consistent estimator
    of sigma for Gaussian data).
    """
    x = np.asarray(x, dtype=np.float64)
    if x.size < 3:
        return x.copy()
    k = max(1, window // 2)
    padded = np.pad(x, k, mode="edge")
    # Sliding windows without a Python loop: (N, 2k+1)
    windows = np.lib.stride_tricks.sliding_window_view(padded, 2 * k + 1)
    med = np.median(windows, axis=-1)
    mad = np.median(np.abs(windows - med[:, None]), axis=-1)
    sigma = 1.4826 * mad
    out = x.copy()
    # Where sigma is 0 the window is constant, so nothing can be an outlier.
    bad = (sigma > 0) & (np.abs(x - med) > n_sigmas * sigma)
    out[bad] = med[bad]
    return out


def hampel_columns(mat: np.ndarray, window: int = 7, n_sigmas: float = 3.0) -> np.ndarray:
    """Apply :func:`hampel` down each column of a (time, subcarrier) matrix."""
    mat = np.asarray(mat, dtype=np.float64)
    if mat.ndim == 1:
        return hampel(mat, window, n_sigmas)
    out = np.empty_like(mat)
    for j in range(mat.shape[1]):
        out[:, j] = hampel(mat[:, j], window, n_sigmas)
    return out


# --------------------------------------------------------------- filter banks


class BandpassBank:
    """Cached zero-phase Butterworth band-pass, designed once per (fs, band).

    ``filtfilt`` is used rather than ``lfilter`` because respiration analysis
    cares about the *timing* of peaks; a causal IIR would introduce a
    frequency-dependent group delay and skew the period estimate.  Zero-phase
    filtering is legitimate here since we always work on a completed buffer,
    never sample-by-sample.
    """

    def __init__(self) -> None:
        self._cache: dict[tuple, np.ndarray] = {}

    def sos(self, fs: float, low: float, high: float, order: int = 4) -> np.ndarray:
        key = (round(fs, 3), low, high, order)
        sos = self._cache.get(key)
        if sos is None:
            nyq = fs / 2.0
            lo = max(low / nyq, 1e-6)
            hi = min(high / nyq, 0.999)
            if lo >= hi:
                raise ValueError(f"invalid band {low}-{high} Hz at fs={fs}")
            sos = signal.butter(order, [lo, hi], btype="bandpass", output="sos")
            self._cache[key] = sos
        return sos

    def apply(self, x: np.ndarray, fs: float, low: float, high: float, order: int = 4) -> np.ndarray:
        sos = self.sos(fs, low, high, order)
        # filtfilt needs a run-up of ~3 * the SOS order on each edge.
        if x.shape[0] <= 3 * (order * 2):
            return np.zeros_like(x)
        return signal.sosfiltfilt(sos, x, axis=0)


def lowpass(x: np.ndarray, fs: float, cutoff: float, order: int = 4) -> np.ndarray:
    """Zero-phase Butterworth low-pass along axis 0."""
    nyq = fs / 2.0
    wn = min(cutoff / nyq, 0.999)
    if x.shape[0] <= 3 * order * 2:
        return np.asarray(x, dtype=np.float64)
    sos = signal.butter(order, wn, btype="low", output="sos")
    return signal.sosfiltfilt(sos, x, axis=0)


# ---------------------------------------------------------------- ring buffer


class RingBuffer:
    """Fixed-capacity circular buffer of float rows, newest last on read.

    The pipeline pushes one row per CSI frame at up to 100 Hz and reads the
    whole window several times a second.  Pre-allocating and overwriting keeps
    that allocation-free, which matters on a Pi 4 where the GC pauses show up
    as visible stutter in the waterfall.

    ``dtype`` matters more than it looks.  float32 is right for amplitudes --
    they are small, and halving the memory helps cache behaviour.  It is
    catastrophically wrong for **timestamps**: ``time.monotonic()`` grows with
    uptime, and float32 carries only ~7 significant digits, so at 10^4 seconds
    of uptime the representable step is already ~1.2 ms and by 10^5 seconds it
    is ~7.6 ms -- comparable to an entire sample period at 100 Hz.  That
    quantisation masquerades as sampling jitter and smears the respiration
    peak.  Time series must use float64.
    """

    __slots__ = ("_buf", "_n", "_head", "capacity", "width", "dtype")

    def __init__(self, capacity: int, width: int, dtype=np.float32) -> None:
        self.capacity = int(capacity)
        self.width = int(width)
        self.dtype = dtype
        self._buf = np.zeros((self.capacity, self.width), dtype=dtype)
        self._n = 0
        self._head = 0

    def push(self, row: np.ndarray) -> None:
        self._buf[self._head] = row
        self._head = (self._head + 1) % self.capacity
        if self._n < self.capacity:
            self._n += 1

    def __len__(self) -> int:
        return self._n

    @property
    def full(self) -> bool:
        return self._n >= self.capacity

    def data(self, last: int | None = None) -> np.ndarray:
        """Chronological view, oldest first.  ``last`` limits to N newest rows."""
        if self._n == 0:
            return np.zeros((0, self.width), dtype=self.dtype)
        if self._n < self.capacity:
            out = self._buf[: self._n]
        else:
            out = np.concatenate((self._buf[self._head :], self._buf[: self._head]), axis=0)
        if last is not None and last < len(out):
            out = out[-last:]
        return out

    def latest(self) -> np.ndarray | None:
        if self._n == 0:
            return None
        return self._buf[(self._head - 1) % self.capacity]

    def clear(self) -> None:
        self._n = 0
        self._head = 0


class ScalarRing:
    """RingBuffer specialised to a single scalar series.

    Defaults to float64 because the dominant use is timestamps, where float32
    would quantise away the sample-to-sample spacing (see :class:`RingBuffer`).
    """

    __slots__ = ("_ring",)

    def __init__(self, capacity: int, dtype=np.float64) -> None:
        self._ring = RingBuffer(capacity, 1, dtype=dtype)

    def push(self, value: float) -> None:
        self._ring.push(np.array([value], dtype=self._ring.dtype))

    def data(self, last: int | None = None) -> np.ndarray:
        return self._ring.data(last).ravel()

    def __len__(self) -> int:
        return len(self._ring)

    @property
    def full(self) -> bool:
        return self._ring.full

    def clear(self) -> None:
        self._ring.clear()


# --------------------------------------------------------- adaptive threshold


class AdaptiveBaseline:
    """Tracks the quiet-room level of a positive-valued statistic.

    The absolute scale of every CSI-derived motion metric depends on room
    geometry, furniture, distance and the AP's transmit power, so a fixed
    threshold that works in one room is meaningless in the next.  Instead we
    track a low quantile of the recent history: whatever the room's floor is,
    that quantile sits on it, because a person cannot keep a room in continuous
    motion indefinitely.

    Updates are frozen while the metric reads "active" so that a person who
    stays in the room does not slowly get absorbed into the baseline.  The
    freeze has a timeout, otherwise a permanent environmental change (a fan
    switched on, a door left open) would wedge the detector on forever.
    """

    def __init__(
        self,
        quantile: float = 0.15,
        history: int = 1200,
        min_samples: int = 60,
        freeze_timeout_s: float = 120.0,
    ) -> None:
        self.quantile = quantile
        self.history = ScalarRing(history)
        self.min_samples = min_samples
        self.freeze_timeout_s = freeze_timeout_s
        self._frozen_since: float | None = None
        self._level = 0.0
        self._scale = 1.0

    def update(self, value: float, now: float, active: bool) -> None:
        if active:
            if self._frozen_since is None:
                self._frozen_since = now
            elif now - self._frozen_since > self.freeze_timeout_s:
                # Held active too long to be a person; let the room re-baseline.
                self._frozen_since = None
                self.history.push(value)
            return

        self._frozen_since = None
        self.history.push(value)
        if len(self.history) >= self.min_samples:
            hist = self.history.data()
            self._level = float(np.quantile(hist, self.quantile))
            # Spread between the floor and the typical quiet value gives a
            # natural unit for "how far above the floor is significant".
            upper = float(np.quantile(hist, 0.9))
            self._scale = max(upper - self._level, 1e-9)

    @property
    def level(self) -> float:
        return self._level

    @property
    def ready(self) -> bool:
        return len(self.history) >= self.min_samples

    def normalise(self, value: float) -> float:
        """Express ``value`` in units of "how far above the quiet floor"."""
        if not self.ready:
            return 0.0
        return (value - self._level) / self._scale

    def clear(self) -> None:
        self.history.clear()
        self._frozen_since = None
        self._level = 0.0
        self._scale = 1.0


class Hysteresis:
    """Two-threshold latch with a minimum dwell time.

    A single threshold on a noisy metric produces a state that chatters many
    times a second, which looks broken in the UI and is useless for triggering
    anything.  Requiring the signal to cross a higher bar to switch on than to
    switch off, and to then hold for a minimum time, turns it into a state that
    a human reads as stable.
    """

    def __init__(self, on: float, off: float, min_on_s: float = 1.5, min_off_s: float = 3.0) -> None:
        if off > on:
            raise ValueError("off threshold must be <= on threshold")
        self.on = on
        self.off = off
        self.min_on_s = min_on_s
        self.min_off_s = min_off_s
        self.state = False
        self._since = 0.0

    def update(self, value: float, now: float) -> bool:
        if self._since == 0.0:
            self._since = now
        held = now - self._since
        if self.state:
            if value < self.off and held >= self.min_on_s:
                self.state = False
                self._since = now
        else:
            if value > self.on and held >= self.min_off_s:
                self.state = True
                self._since = now
        return self.state

    @property
    def held_for(self) -> float:
        return self._since


# ------------------------------------------------------------- time alignment


def resample_uniform(
    t: np.ndarray, x: np.ndarray, fs: float, duration: float | None = None
) -> tuple[np.ndarray, np.ndarray]:
    """Resample an irregularly-sampled series onto a uniform grid.

    CSI frames do not arrive on a metronome -- the AP's response timing, WiFi
    retries and USB scheduling all add jitter, and frames are occasionally
    dropped entirely.  Feeding that directly to an FFT smears the respiration
    peak across neighbouring bins and can invent peaks that are pure sampling
    artefact.  Linear interpolation onto a fixed grid costs a little
    high-frequency fidelity, which is irrelevant in the 0.1-0.6 Hz band we
    care about.

    Returns ``(uniform_times, resampled)``; the resampled array keeps the
    trailing axes of ``x``.
    """
    t = np.asarray(t, dtype=np.float64)
    x = np.asarray(x, dtype=np.float64)
    if t.size < 2:
        return t, x

    span = t[-1] - t[0]
    if duration is not None:
        span = min(span, duration)
    n = int(span * fs)
    if n < 2:
        return t, x

    grid = t[-1] - np.arange(n - 1, -1, -1) / fs
    if x.ndim == 1:
        return grid, np.interp(grid, t, x)

    out = np.empty((n, x.shape[1]), dtype=np.float64)
    for j in range(x.shape[1]):
        out[:, j] = np.interp(grid, t, x[:, j])
    return grid, out


def estimate_rate(times: np.ndarray, default: float = 50.0) -> float:
    """Robust sample-rate estimate from arrival timestamps.

    The median inter-arrival gap is used rather than ``n / elapsed`` so that a
    single stall (USB hiccup, WiFi scan) does not drag the estimate down and
    misplace every subsequent frequency axis.
    """
    t = np.asarray(times, dtype=np.float64)
    if t.size < 8:
        return default
    dt = np.diff(t)
    dt = dt[dt > 0]
    if dt.size < 4:
        return default
    med = float(np.median(dt))
    if med <= 0:
        return default
    return 1.0 / med
