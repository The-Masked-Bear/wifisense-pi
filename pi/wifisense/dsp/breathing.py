"""Respiration estimation from CSI.

A breathing chest wall moves roughly 4-12 mm.  At 2.4 GHz one wavelength is
125 mm, so that displacement is a few percent of a wavelength -- small, but it
modulates the multipath sum periodically, and periodic is exactly what an FFT
is good at digging out of noise.  Integrating over a 45-second window buys
back the SNR that the tiny displacement costs.

This only works on a **still** subject.  Walking changes the channel by orders
of magnitude more than breathing does, burying the respiration component
completely.  That is a physical limit, not an implementation shortcut, so the
detector reports low confidence during movement rather than emitting a number
that happens to be in the plausible range.

Frequency band: 0.13-0.60 Hz, i.e. 8-36 breaths/min, which spans deep sleep
through mild exertion with margin on both ends.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

import numpy as np

from .csi import SanitizedCsi, principal_component, subcarrier_sensitivity
from .filters import BandpassBank, RingBuffer, ScalarRing, resample_uniform

BREATH_LOW_HZ = 0.13  # 7.8 breaths/min
BREATH_HIGH_HZ = 0.60  # 36 breaths/min
RESAMPLE_HZ = 20.0  # comfortably above Nyquist for 0.6 Hz
WINDOW_SECONDS = 45.0
MIN_SECONDS = 20.0  # below ~4 breath cycles the peak is not trustworthy

# --- rate tracking ---------------------------------------------------------
# How far a candidate peak may sit from the tracked rate and still count as the
# same breath.  Respiration drifts by a breath or two per minute, not ten.
TRACK_TOL_BPM = 2.5
# A peak near the tracked rate keeps the lock if it is at least this fraction
# as strong as the strongest peak in the band.
RIVAL_FRAC = 0.45
# Windows that must agree before the reading is published.  The analysis window
# is 45 s and recomputes every second, so consecutive windows overlap heavily:
# genuine respiration agrees immediately, one-off artefacts do not.
AGREE_NEEDED = 3
# Confidence hysteresis.  A single threshold made the valid flag chatter on and
# off every few seconds as confidence hovered around it -- the motion detector
# has hysteresis for exactly this reason, and this one never got any.
VALID_ON = 0.42
VALID_OFF = 0.22
# Above this much gross motion the tracker will confirm an existing rate but
# will not adopt a new one: at that level a limb swing produces a perfectly
# convincing periodic peak that has nothing to do with breathing.
RELOCK_MOTION_MAX_DB = 10.0
# Harmonics summed when choosing the fundamental.  Seven covers the range where
# a real chest still puts measurable energy without reaching so far up that
# unrelated motion starts contributing.
HARMONICS = 7
# Confidence smoothing.  The per-window prominence measure is legitimately
# noisy -- a small shift in the spectrum swings it a lot -- and on a subject
# sitting perfectly still with a rock-steady rate it was seen collapsing from
# 0.36 to 0.06 and back within seconds.  What matters is whether the peak has
# been convincing *lately*, not in this one window, so it is smoothed.  Rising
# faster than it falls, so a genuine lock is published promptly but a brief
# glitch does not tear it down.
CONF_ATTACK = 0.45
CONF_DECAY = 0.12


@dataclass(slots=True)
class BreathingState:
    bpm: float = 0.0
    confidence: float = 0.0
    valid: bool = False
    window_fill: float = 0.0
    waveform: list[float] | None = None  # normalised, for the UI trace
    spectrum: list[float] | None = None  # in-band PSD, for the UI
    spectrum_bpm: list[float] | None = None
    reason: str = "warming up"

    def as_dict(self) -> dict:
        return {
            "bpm": round(self.bpm, 1),
            "confidence": round(self.confidence, 3),
            "valid": self.valid,
            "window_fill": round(self.window_fill, 3),
            "waveform": self.waveform or [],
            "spectrum": self.spectrum or [],
            "spectrum_bpm": self.spectrum_bpm or [],
            "reason": self.reason,
        }


class BreathingDetector:
    """Sliding-window respiration estimator."""

    def __init__(
        self,
        n_subcarriers: int,
        *,
        nominal_rate: float = 50.0,
        window_seconds: float = WINDOW_SECONDS,
    ) -> None:
        self.window_seconds = window_seconds
        cap = max(128, int(window_seconds * nominal_rate * 1.2))
        self.buf = RingBuffer(cap, n_subcarriers)
        self.times = ScalarRing(cap)
        self.n_subcarriers = n_subcarriers
        self.bank = BandpassBank()
        self.state = BreathingState()
        self._last_compute = 0.0
        self._recent_bpm: list[float] = []
        # Tracked rate, and how many consecutive windows have agreed with it.
        self._locked: float | None = None
        self._agree = 0
        self._valid = False
        self._conf = 0.0

    def push(self, s: SanitizedCsi) -> None:
        if s.amplitude.size != self.n_subcarriers:
            cap = self.buf.capacity
            self.n_subcarriers = s.amplitude.size
            self.buf = RingBuffer(cap, self.n_subcarriers)
            self.times.clear()
            return
        self.buf.push(s.amplitude)
        self.times.push(s.host_time or time.monotonic())

    def compute(
        self,
        *,
        moving: bool,
        occupied: bool,
        motion_db: float = 0.0,
        vital_db: float = 0.0,
        min_interval: float = 1.0,
    ) -> BreathingState:
        """Recompute the estimate.  Cheap to call often; it rate-limits itself."""
        now = time.monotonic()
        if now - self._last_compute < min_interval:
            return self.state
        self._last_compute = now

        st = self.state
        t = self.times.data()
        st.window_fill = min(1.0, (t[-1] - t[0]) / self.window_seconds) if t.size > 1 else 0.0

        if not occupied:
            st.valid, st.confidence, st.bpm, st.reason = False, 0.0, 0.0, "room empty"
            self._recent_bpm.clear()
            self._locked, self._agree, self._valid = None, 0, False
            self._conf = 0.0
            return st
        # Veto only when gross motion is actually strong enough to bury the
        # respiration signal -- not merely because the motion flag is set.
        #
        # Gating on the flag alone was far too strict in practice: a person
        # seated at a desk, typing and shifting, measures 10-14 dB of motion
        # while their breathing sits at 30+ dB, and respiration was refused the
        # entire time despite being 20+ dB clear of the interference. Since a
        # seated subject is the main case this feature exists for, that made it
        # close to useless.
        #
        # Two conditions genuinely warrant refusing:
        #   * motion strong in absolute terms (walking about), or
        #   * motion within 6 dB of the respiration signal, where the estimate
        #     really would be dominated by movement rather than the chest.
        swamped = motion_db > 18.0 or motion_db > vital_db - 6.0
        if moving and swamped:
            st.valid, st.confidence, st.reason = False, 0.0, "subject moving"
            self._valid = False
            # Keep the tracked rate: a few seconds of movement should not throw
            # away a lock that took several windows to establish, and the
            # subject's breathing rate has not changed just because they moved.
            self._agree = max(0, self._agree - 1)
            return st
        if t.size < 32 or (t[-1] - t[0]) < MIN_SECONDS:
            st.valid, st.confidence, st.reason = False, 0.0, "collecting window"
            return st

        window = self.buf.data()[-t.size :]

        # Resample onto a uniform grid before any spectral work.  CSI arrival
        # jitter would otherwise smear the respiration line across bins.
        grid, uniform = resample_uniform(t, window.astype(np.float64), RESAMPLE_HZ, self.window_seconds)
        if uniform.shape[0] < int(MIN_SECONDS * RESAMPLE_HZ):
            st.valid, st.confidence, st.reason = False, 0.0, "collecting window"
            return st

        # Concentrate on the sub-carriers that are actually responding.  Ones
        # sitting in a fade null contribute noise and nothing else.
        weights = subcarrier_sensitivity(uniform)
        keep = weights > np.quantile(weights, 0.5)
        if keep.sum() >= 4:
            uniform = uniform[:, keep]

        # Collapse the correlated sub-carriers onto their dominant varying
        # direction -- signal adds coherently, noise does not.
        comp = principal_component(uniform, n_components=1)[:, 0]

        filtered = self.bank.apply(comp, RESAMPLE_HZ, BREATH_LOW_HZ, BREATH_HIGH_HZ, order=4)
        if not np.any(filtered):
            st.valid, st.confidence, st.reason = False, 0.0, "no signal in band"
            return st

        bpm, conf, freqs, psd = self._spectral_peak(
            filtered, RESAMPLE_HZ, prefer=self._locked
        )

        # --- track the rate rather than re-deciding it every window ---------
        if self._locked is None:
            self._locked = bpm
            self._agree = 1
        elif abs(bpm - self._locked) <= TRACK_TOL_BPM:
            # Agrees with the lock: fold it in slowly.  A slow EMA lets the rate
            # follow a real drift while refusing to be yanked by one window.
            self._locked += 0.25 * (bpm - self._locked)
            self._agree = min(self._agree + 1, AGREE_NEEDED + 3)
        else:
            # Disagrees.  Give up the lock only after repeated disagreement, and
            # never while the subject is moving enough to fake a rhythm.
            self._agree -= 1
            if self._agree <= 0:
                if motion_db <= RELOCK_MOTION_MAX_DB:
                    self._locked = bpm
                    self._agree = 1
                else:
                    self._agree = 0
                    conf = 0.0

        settled = self._agree >= AGREE_NEEDED
        if not settled:
            conf *= 0.5
        # Movement that was not strong enough to veto still degrades the
        # estimate; reflect that in the confidence rather than hiding it.
        if moving:
            conf *= 0.75

        # Smooth before deciding.  Asymmetric: quick to rise, slow to fall.
        alpha = CONF_ATTACK if conf > self._conf else CONF_DECAY
        self._conf += alpha * (conf - self._conf)
        conf = self._conf

        # --- hysteresis on the published flag -------------------------------
        if self._valid:
            self._valid = conf >= VALID_OFF and settled
        else:
            self._valid = conf >= VALID_ON and settled

        self._recent_bpm.append(bpm)
        if len(self._recent_bpm) > 8:
            self._recent_bpm.pop(0)

        st.bpm = float(self._locked) if self._locked is not None else bpm
        st.confidence = conf
        st.valid = self._valid
        st.reason = "ok" if st.valid else ("settling" if not settled else "weak periodicity")

        # Downsample the waveform for transport: the UI draws ~300 px wide.
        norm = filtered / (np.max(np.abs(filtered)) or 1.0)
        step = max(1, norm.size // 300)
        st.waveform = [round(float(v), 4) for v in norm[::step][-300:]]

        band = (freqs >= BREATH_LOW_HZ) & (freqs <= BREATH_HIGH_HZ)
        bpsd = psd[band]
        bfreq = freqs[band]
        if bpsd.size:
            bpsd = bpsd / (bpsd.max() or 1.0)
            step = max(1, bpsd.size // 120)
            st.spectrum = [round(float(v), 4) for v in bpsd[::step]]
            st.spectrum_bpm = [round(float(f * 60.0), 2) for f in bfreq[::step]]
        return st

    # ------------------------------------------------------------- internals

    def _spectral_peak(
        self, x: np.ndarray, fs: float, prefer: float | None = None
    ) -> tuple[float, float, np.ndarray, np.ndarray]:
        """Find the dominant in-band frequency and score how convincing it is.

        A Hann taper suppresses the spectral leakage that would otherwise let
        the (much larger) out-of-band residue bleed into the breathing band.
        Zero-padding 4x does not add information but interpolates the peak
        location, which turns a coarse 1.3 BPM bin spacing into a usable
        reading.
        """
        n = x.size
        win = np.hanning(n)
        xw = (x - x.mean()) * win
        nfft = int(2 ** np.ceil(np.log2(n * 4)))
        spec = np.fft.rfft(xw, n=nfft)
        psd = (np.abs(spec) ** 2).astype(np.float64)
        freqs = np.fft.rfftfreq(nfft, 1.0 / fs)

        band = (freqs >= BREATH_LOW_HZ) & (freqs <= BREATH_HIGH_HZ)
        if not band.any():
            return 0.0, 0.0, freqs, psd

        bpsd = psd[band]
        bfreq = freqs[band]

        # Pick the fundamental by HARMONIC SUMMATION, not by raw argmax.
        #
        # Chest motion does not modulate the channel sinusoidally: at close
        # range the phase swing is large enough to leave the small-angle regime,
        # so the response is rich in harmonics. Measured on a real subject the
        # 7th harmonic came in 2.4 dB ABOVE the fundamental -- and a plain
        # argmax duly reported a rate seven times too fast.
        #
        # Scoring each candidate by the energy at f0, 2*f0, 3*f0 ... makes the
        # true fundamental win, because only the true fundamental has all of its
        # multiples lit up. A harmonic scores poorly: its own multiples land on
        # empty spectrum. This is the standard pitch-detection trick, and
        # respiration rate is the same problem wearing different clothes.
        #
        # Harmonics are weighted down by 1/n so a low candidate cannot win
        # merely by having more multiples inside the analysis range.
        score = np.zeros_like(bpsd)
        for i, f0 in enumerate(bfreq):
            if f0 <= 0:
                continue
            total = 0.0
            for n in range(1, HARMONICS + 1):
                hz = f0 * n
                if hz > fs / 2 * 0.95:
                    break
                total += psd[int(np.argmin(np.abs(freqs - hz)))] / n
            score[i] = total
        pk = int(np.argmax(score)) if np.any(score > 0) else int(np.argmax(bpsd))

        # Continuity bias.  A plain argmax picks a fresh winner every window, so
        # whenever two peaks are comparable -- which is normal for a weak or
        # slightly contaminated respiration signal -- the estimate flips between
        # them.  Measured on a live subject it jumped 8.8 -> 25.5 -> 10.5 bpm in
        # under a minute, which no lung does.
        #
        # So if a peak already close to the tracked rate is at least RIVAL_FRAC
        # as strong as the global maximum, keep it.  Real respiration drifts
        # slowly; a genuinely new rate has to clearly out-argue the incumbent
        # rather than merely edge past it on one noisy window.
        if prefer is not None:
            near = np.abs(bfreq * 60.0 - prefer) <= TRACK_TOL_BPM
            if near.any():
                cand = int(np.argmax(np.where(near, score, -np.inf)))
                if score[cand] >= RIVAL_FRAC * score[pk]:
                    pk = cand

        peak_f = float(bfreq[pk])
        peak_p = float(bpsd[pk])

        # Confidence = how far the peak stands above the rest of the band.
        # The median is a robust stand-in for "the noise floor here"; a clean
        # respiration line runs 10-100x above it, a spurious one barely 2-3x.
        floor = float(np.median(bpsd)) or 1e-12
        ratio = peak_p / floor
        conf = float(np.clip(np.log10(max(ratio, 1.0)) / 1.7, 0.0, 1.0))

        # Fraction of band energy inside a narrow window around the peak: a
        # genuine breath is narrowband, broadband wobble is not.
        near = np.abs(bfreq - peak_f) < 0.04
        share = float(bpsd[near].sum() / (bpsd.sum() or 1e-12))
        conf *= float(np.clip(share * 3.0, 0.3, 1.0))

        return peak_f * 60.0, float(np.clip(conf, 0.0, 1.0)), freqs, psd

    def reset(self) -> None:
        self.buf.clear()
        self.times.clear()
        self.state = BreathingState()
        self._recent_bpm.clear()
        self._locked, self._agree, self._valid = None, 0, False
