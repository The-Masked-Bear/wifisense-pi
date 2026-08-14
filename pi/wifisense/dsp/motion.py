"""Motion and presence detection from sanitized CSI.

The central problem is picking a reference to measure against.  An obvious
first design normalises each statistic against its own recent distribution --
and it fails completely, because a room that is occupied the whole time trains
the reference to treat "occupied" as the floor and then reports empty.  Any
self-referential baseline has this defect: it can only see *changes* in
activity, never activity itself.

So the reference here is the **receiver noise floor**, which is a physical
constant of the hardware rather than a property of the room.  The separation
that makes this work:

* Thermal noise is **spatially white**: independent on every sub-carrier,
  because it is generated in the receiver rather than in the room.
* Everything the room does is **spatially correlated**: a moving reflector
  changes the whole frequency response coherently, so channel variation lives
  in a handful of dimensions out of the ~52 available.

Decomposing the sub-carrier covariance therefore separates the two cleanly.
The leading principal component carries the channel; the trailing components
contain essentially only receiver noise, and their power sets the reference.
Band power in the leading component, divided by that reference, is an SNR in
dB that depends on the room only through what is actually happening in it --
not on its size, the distance to the node, or the transmit power.  No learned
baseline, no drift.

That ratio is *not* zero for an empty room on its own, though.  Projecting
finite noisy data onto its own principal axes concentrates noise into the
leading component, so the largest eigenvalue exceeds the median of the noise
subspace even with no signal present at all -- about 7 dB for the window shapes
used here.  :func:`_noise_bias_db` measures that offset for the exact geometry
in use and subtracts it, which is what actually pins an empty room to 0 dB.

A tempting simpler reference -- power in a high frequency band, on the theory
that nothing physiological is fast -- is **wrong**, and wrong in a way that
inverts the result.  The frequency of CSI variation is not the frequency of
the body's motion; it is the Doppler shift ``v/lambda``.  At 2.4 GHz lambda is
12.5 cm, so a 1 m/s walk writes energy at ~8 Hz and a brisk arm swing reaches
past 15 Hz.  A "quiet" band up there is not quiet during exactly the events
worth detecting: walking contaminates the reference, deflating its own score
below that of a person sitting still.  The spatial split has no such failure
mode, because it does not assume anything about rates at all.

The two bands answer genuinely different questions:

``motion_db``
    Energy at 1.5-9 Hz.  Gross movement -- walking, gesturing, sitting down.

``vital_db``
    Energy at 0.13-0.7 Hz.  What a *still* body still emits: the chest wall.
    This is what separates "empty" from "asleep in the chair", which motion
    detection alone gets wrong every time.

A person holding perfectly still and holding their breath is genuinely
invisible to this, and to any other CSI system.  That is physics, not a gap in
the implementation.
"""

from __future__ import annotations

import time
from functools import lru_cache
from dataclasses import dataclass

import numpy as np
from scipy import signal as sps

from .csi import SanitizedCsi, principal_component
from .filters import Hysteresis, RingBuffer, ScalarRing, resample_uniform

# Analysis grid.  20 Hz is comfortably above Nyquist for everything a body
# does, and resampling to a fixed rate means the frequency axis does not move
# when the link rate drifts.
ANALYSIS_HZ = 20.0

# Doppler bands, not displacement bands.  Walking at 0.4-1.5 m/s maps to
# 3-12 Hz at 2.4 GHz, and a moving hand at 0.3 m/s to ~2.4 Hz.
#
# The lower edge is 1.5 Hz rather than 0.5 for a specific reason: respiration
# is not a pure tone.  A chest excursion of ~6 mm swings the carrier phase by
# ~0.6 rad, which is well outside the small-angle regime, so the amplitude
# modulation carries harmonics.  With a fundamental up to 0.7 Hz the second
# harmonic reaches 1.4 Hz, and because the fundamental itself can sit 35 dB
# over the floor, those harmonics are strong enough to read as gross movement:
# at a 0.5 Hz edge a *motionless* breathing subject measured +10.4 dB here and
# was classified as walking.  Starting at 1.5 Hz excludes the second harmonic
# entirely and costs only the ability to see very slow torso drift -- which is
# not movement worth reporting, and which vital_db detects anyway.
MOTION_BAND = (1.5, 9.0)
# Respiration is the exception that really is slow: the chest moves millimetres,
# so its Doppler stays inside 0.13-0.70 Hz (8-42 breaths/min).
VITAL_BAND = (0.13, 0.70)

MOTION_WINDOW_S = 6.0  # short: the "someone just walked in" latency
VITAL_WINDOW_S = 30.0  # long: respiration needs many cycles to resolve


def _raw_band_snr(centred: np.ndarray, band: tuple[float, float]) -> tuple[float, float]:
    """Band power of the leading component over the noise-subspace level, in dB.

    Uncorrected -- see :func:`_noise_bias_db` for why the result must have a
    bias removed before it means anything absolute.
    """
    try:
        u, sv, _ = np.linalg.svd(centred, full_matrices=False)
    except np.linalg.LinAlgError:
        return 0.0, 0.0
    if sv.size < 8 or sv[0] <= 0:
        return 0.0, 0.0

    # Component time-series.  Column 0 carries the channel; the tail is noise.
    comps = u * sv

    # Blackman-Harris rather than the default Hann: respiration can sit 35 dB
    # above the floor, and Hann's -31 dB sidelobes leak enough of that into the
    # motion band to make a sleeping person read as active.  Blackman-Harris
    # reaches -92 dB, putting the leakage below the noise floor where it
    # belongs.
    nperseg = min(comps.shape[0], max(64, comps.shape[0] // 2))
    kw = dict(fs=ANALYSIS_HZ, nperseg=nperseg, noverlap=nperseg // 2,
              window="blackmanharris", detrend="linear")

    freqs, sig_psd = sps.welch(comps[:, 0], **kw)

    # Reference: the bottom half of the component spectrum, which channel
    # variation cannot reach because it is low-rank.  One vectorised welch over
    # the whole tail -- the per-column loop cost an order of magnitude more for
    # identical output.
    n_comp = comps.shape[1]
    lo = max(4, n_comp // 2)
    tail = comps[:, lo : lo + 12]
    if tail.shape[1] == 0:
        return 0.0, 0.0
    _, tail_psd = sps.welch(tail, axis=0, **kw)
    noise = float(np.median(tail_psd))
    if noise <= 0 or not np.isfinite(noise):
        return 0.0, 0.0

    band_sel = (freqs >= band[0]) & (freqs <= min(band[1], ANALYSIS_HZ / 2 * 0.95))
    if not band_sel.any():
        return 0.0, 0.0

    bandpower = float(np.mean(sig_psd[band_sel]))
    return 10.0 * np.log10(max(bandpower / noise, 1e-12)), noise


@lru_cache(maxsize=64)
def _noise_bias_db(n_samples: int, n_channels: int, band: tuple[float, float]) -> float:
    """What :func:`_raw_band_snr` reports for pure white noise.

    The estimator compares the *largest* principal component against the
    *median* of the noise subspace, and for a finite random matrix those are
    not equal even when there is no signal at all.  Marchenko-Pastur puts the
    eigenvalues of a (T, K) white matrix between sigma^2*(1 +/- sqrt(K/T))^2 --
    for the shapes used here that spread alone is ~7 dB, so an empty room would
    report a confident +7 dB of "motion".

    Rather than derive the correction analytically (it depends on T, K, the
    band edges, the window and the Welch segmentation), it is measured: run the
    identical estimator on synthetic white noise of the same shape and keep
    what it says.  Cached per shape, so this costs a few milliseconds once.

    The median of several trials is used because the largest eigenvalue of a
    random matrix has appreciable variance; a single draw would bake that
    draw's luck into the calibration.
    """
    rng = np.random.default_rng(0xC51)
    trials = []
    for _ in range(7):
        noise = rng.standard_normal((n_samples, n_channels))
        noise -= noise.mean(axis=0, keepdims=True)
        db, level = _raw_band_snr(noise, band)
        if level > 0:
            trials.append(db)
    return float(np.median(trials)) if trials else 0.0


@dataclass(slots=True)
class MotionState:
    motion_db: float = 0.0
    vital_db: float = 0.0
    motion: float = 0.0  # 0-1 display scaling of motion_db
    presence: float = 0.0  # 0-1 display scaling of the stronger evidence
    occupied: bool = False
    moving: bool = False
    activity: str = "empty"
    confidence: float = 0.0
    calibrating: bool = True
    calibration_progress: float = 0.0
    rssi: int = 0
    sample_rate: float = 0.0
    noise_floor: float = 0.0

    def as_dict(self) -> dict:
        return {
            "motion_db": round(self.motion_db, 2),
            "vital_db": round(self.vital_db, 2),
            "motion": round(self.motion, 4),
            "presence": round(self.presence, 4),
            "occupied": self.occupied,
            "moving": self.moving,
            "activity": self.activity,
            "confidence": round(self.confidence, 3),
            "calibrating": self.calibrating,
            "calibration_progress": round(self.calibration_progress, 3),
            "rssi": self.rssi,
            "sample_rate": round(self.sample_rate, 1),
        }


class MotionDetector:
    """Streaming motion + presence detector, referenced to the noise floor."""

    def __init__(
        self,
        n_subcarriers: int,
        *,
        history_seconds: float = 60.0,
        nominal_rate: float = 50.0,
        update_hz: float = 5.0,
        motion_on_db: float = 6.0,
        motion_off_db: float = 3.5,
        presence_on_db: float = 4.5,
        presence_off_db: float = 2.5,
    ) -> None:
        self.n_subcarriers = n_subcarriers
        self.nominal_rate = nominal_rate
        self.history_seconds = history_seconds

        cap = max(128, int(max(history_seconds, VITAL_WINDOW_S + 5) * nominal_rate))
        self.window = RingBuffer(cap, n_subcarriers)
        self.times = ScalarRing(cap)
        self.motion_history = ScalarRing(cap)
        self.vital_history = ScalarRing(cap)

        self.motion_gate = Hysteresis(motion_on_db, motion_off_db, min_on_s=0.6, min_off_s=2.0)
        self.presence_gate = Hysteresis(presence_on_db, presence_off_db, min_on_s=2.0, min_off_s=10.0)

        self.state = MotionState()
        self._update_period = 1.0 / update_hz
        self._last_analysis = 0.0
        self._rate_est = nominal_rate

    # ------------------------------------------------------------------ input

    def push(self, s: SanitizedCsi) -> MotionState:
        if s.amplitude.size != self.n_subcarriers:
            self._rebuild(s.amplitude.size)

        now = s.host_time or time.monotonic()
        self.window.push(s.amplitude.astype(np.float32))
        self.times.push(now)

        self.state.rssi = s.rssi

        # The spectral analysis is far too expensive to run per frame at
        # 100 Hz, and nothing in it changes meaningfully faster than ~5 Hz.
        if now - self._last_analysis >= self._update_period:
            self._last_analysis = now
            self._analyze(now)
        return self.state

    # -------------------------------------------------------------- analysis

    def _analyze(self, now: float) -> None:
        t = self.times.data()
        if t.size < 32:
            self.state.calibrating = True
            return

        self._rate_est = self._estimate_rate(t)
        elapsed = float(t[-1] - t[0])

        st = self.state
        st.sample_rate = self._rate_est
        st.calibration_progress = min(1.0, elapsed / MOTION_WINDOW_S)

        if elapsed < MOTION_WINDOW_S:
            st.calibrating = True
            return
        st.calibrating = False

        rows = self.window.data()[-t.size :]

        # --- short window: gross motion + the noise-floor reference ---------
        motion_db, noise_psd = self._band_snr(
            t, rows, MOTION_WINDOW_S, MOTION_BAND, return_noise=True
        )

        # --- long window: respiration-band micro-motion ---------------------
        if elapsed >= VITAL_WINDOW_S * 0.6:
            vital_db, _ = self._band_snr(t, rows, VITAL_WINDOW_S, VITAL_BAND, return_noise=True)
        else:
            vital_db = 0.0

        st.motion_db = motion_db
        st.vital_db = vital_db
        st.noise_floor = float(noise_psd)

        self.motion_history.push(motion_db)
        self.vital_history.push(vital_db)

        moving = self.motion_gate.update(motion_db, now)
        # Either kind of evidence proves occupancy; a walking person need not
        # wait for the slow respiration window to agree.
        occupied = self.presence_gate.update(max(motion_db, vital_db), now) or moving

        st.moving = moving
        st.occupied = occupied
        st.activity = self._classify(motion_db, vital_db, occupied, moving)

        # Display scalings: ~20 dB is a strong, unambiguous signal.
        st.motion = float(np.clip(motion_db / 20.0, 0.0, 1.0))
        st.presence = float(np.clip(max(motion_db, vital_db) / 20.0, 0.0, 1.0))
        st.confidence = float(np.clip((max(motion_db, vital_db) - 2.0) / 12.0, 0.0, 1.0))

    def _band_snr(
        self,
        t: np.ndarray,
        rows: np.ndarray,
        window_s: float,
        band: tuple[float, float],
        *,
        return_noise: bool = False,
    ) -> tuple[float, float]:
        """Power in ``band`` relative to the white-noise floor, in dB.

        Both bands are averaged *per unit bandwidth* so the comparison is a
        true PSD ratio and does not reward whichever band happens to be wider.
        """
        cutoff = t[-1] - window_s
        sel = t >= cutoff
        if sel.sum() < 32:
            return 0.0, 0.0
        tw, rw = t[sel], rows[sel]

        # Uniform grid first: arrival jitter smears energy across the spectrum
        # and would corrupt both the band power and the reference.
        grid, uni = resample_uniform(tw, rw.astype(np.float64), ANALYSIS_HZ, window_s)
        if uni.shape[0] < 64 or uni.shape[1] < 8:
            return 0.0, 0.0

        centred = uni - uni.mean(axis=0, keepdims=True)
        raw, noise = _raw_band_snr(centred, band)
        if noise <= 0:
            return 0.0, 0.0

        # Subtract the bias this estimator has on pure noise (see
        # :func:`_noise_bias_db`), so an empty room reads 0 dB rather than the
        # ~7 dB that the eigenvalue spread alone produces.
        bias = _noise_bias_db(centred.shape[0], centred.shape[1], band)
        return float(np.clip(raw - bias, -20.0, 60.0)), noise

    # ------------------------------------------------------------- internals

    def _estimate_rate(self, t: np.ndarray) -> float:
        if t.size < 16:
            return self.nominal_rate
        dt = np.diff(t[-256:])
        dt = dt[dt > 0]
        if dt.size < 8:
            return self.nominal_rate
        med = float(np.median(dt))
        return 1.0 / med if med > 0 else self.nominal_rate

    def _classify(self, motion_db: float, vital_db: float, occupied: bool, moving: bool) -> str:
        if not occupied:
            return "empty"
        if not moving:
            return "still"
        if motion_db < 10.0:
            return "subtle"
        if motion_db < 18.0:
            return "active"
        return "vigorous"

    def _rebuild(self, n: int) -> None:
        cap = self.window.capacity
        self.n_subcarriers = n
        self.window = RingBuffer(cap, n)
        self.times.clear()

    # ---------------------------------------------------------------- output

    def spectrum_window(self, seconds: float | None = None):
        if seconds is None:
            return self.times.data(), self.window.data()
        n = int(seconds * max(self._rate_est, 1.0))
        return self.times.data(last=n), self.window.data(last=n)

    def reset(self) -> None:
        self.window.clear()
        self.times.clear()
        self.motion_history.clear()
        self.vital_history.clear()
        self.state = MotionState()
        self._last_analysis = 0.0
