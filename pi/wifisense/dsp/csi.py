"""Turning raw ESP32 CSI bytes into something a detector can trust.

Raw CSI off the radio is not a clean measurement of the channel.  Three
distortions dominate, and all three have to go before any statistic computed
downstream means anything:

1. **AGC gain steps.**  The receiver rescales its front end between packets,
   so the *absolute* amplitude jumps by several dB for reasons that have
   nothing to do with the room.  Any variance-based motion metric reads those
   jumps as enormous motion.  Fixed by normalising each frame's amplitude
   vector by its own norm, which discards absolute gain and keeps the *shape*
   of the frequency response -- and shape is exactly what multipath changes.

2. **Phase is nonsense as-received.**  Carrier frequency offset (CFO),
   sampling frequency offset (SFO) and packet detection delay (PDD) each add
   an unknown term to the measured phase, and the PDD term is a different
   random slope on every single packet.  Raw phase is therefore uniformly
   distributed and carries no information.  Fixed by linear de-trending across
   sub-carriers, which removes the slope-and-offset pair that SFO/PDD produce.

3. **Guard bands and the DC bin carry no signal.**  The ESP32 reports all 64
   LLTF sub-carriers, but the outer ones and the DC null are pure noise.
   Including them adds variance that is uncorrelated with anything physical.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from ..protocol import CsiFrame, LLTF_GUARD_TRIMMED, LLTF_SUBCARRIERS, LLTF_USABLE


# Shared empty array for the common case where phase is not computed; avoids
# allocating a throwaway per frame at 100 Hz.
_NO_PHASE = np.empty(0, dtype=np.float32)


@dataclass(slots=True)
class SanitizedCsi:
    """One CSI frame after cleanup."""

    amplitude: np.ndarray  # (K,) float32, AGC-normalised
    phase: np.ndarray  # (K,) float32, linearly de-trended
    raw_amplitude: np.ndarray  # (K,) float32, before normalisation
    gain: float  # the norm that was divided out
    rssi: int
    timestamp_us: int
    host_time: float
    seq: int
    subcarriers: np.ndarray  # (K,) int32, which indices survived selection


def select_subcarriers(n_total: int, trim_guard: bool = True) -> np.ndarray:
    """Which sub-carrier indices are worth keeping.

    For the 64-bin LLTF capture this drops the DC bin and, when ``trim_guard``
    is set, the outer bins that fall in the 802.11n guard band.
    """
    if n_total == LLTF_SUBCARRIERS:
        return LLTF_GUARD_TRIMMED.copy() if trim_guard else LLTF_USABLE.copy()
    if n_total == LLTF_GUARD_TRIMMED.size:
        # Already trimmed at the node (TRIM_SUBCARRIERS): every sub-carrier
        # present carries signal, so keep all of them.  Applying the usual
        # DC-bin rule here would discard a real one.
        return np.arange(n_total, dtype=np.int32)
    # Unknown geometry (HT-LTF included, or a 40 MHz capture): keep everything
    # except an exact-centre DC bin, which is always null.
    idx = np.arange(n_total, dtype=np.int32)
    if n_total % 2 == 0:
        idx = idx[idx != n_total // 2]
    return idx


def detrend_phase(phase: np.ndarray, subcarriers: np.ndarray) -> np.ndarray:
    """Remove the SFO/PDD linear term from an unwrapped phase response.

    The measured phase on sub-carrier *k* is

        phi_measured(k) = phi_true(k) - 2*pi*k*delta_t/N + beta + noise

    where ``delta_t`` is the packet detection delay and ``beta`` collects the
    carrier-offset terms.  Both unknowns are, respectively, a slope in *k* and
    a constant -- so fitting and subtracting a straight line across the
    sub-carriers removes them.  What survives is the part of the phase response
    that actually varies with the channel.

    This is the standard linear-transform sanitisation (Sen et al., and used
    by essentially every CSI paper since).  It cannot recover absolute phase --
    nothing can, from a single antenna -- but the *relative* phase across
    sub-carriers becomes stable and comparable between packets.
    """
    unwrapped = np.unwrap(phase)
    k = subcarriers.astype(np.float64)
    # Least-squares line fit; equivalent to the endpoint-slope formulation but
    # far less sensitive to noise on the two edge sub-carriers.
    a, b = np.polyfit(k, unwrapped, 1)
    return (unwrapped - (a * k + b)).astype(np.float32)


def sanitize(
    frame: CsiFrame,
    *,
    trim_guard: bool = True,
    normalise_gain: bool = True,
    with_phase: bool = False,
) -> SanitizedCsi | None:
    """Clean up one CSI frame.  Returns None if the frame is unusable.

    ``with_phase`` is off by default because phase sanitisation runs a
    least-squares line fit per frame, and at 100 Hz that measured at ~17% of
    total CPU on a Pi 4 -- while none of the motion or respiration detectors
    consume phase at all.  Turn it on when adding a phase-based detector, or
    for offline analysis of a recording.
    """
    n = frame.n_subcarriers
    if n < 8:
        return None

    idx = select_subcarriers(n, trim_guard=trim_guard)
    if idx.size == 0 or idx.max() >= n:
        return None

    csi = frame.complex()[idx]
    amp = np.abs(csi).astype(np.float32)

    # A frame where the radio reported nothing is not a measurement of a quiet
    # room, it is a dropped packet -- and averaging it in would read as a huge
    # change.  Reject rather than propagate.
    gain = float(np.linalg.norm(amp))
    if not np.isfinite(gain) or gain < 1e-6:
        return None

    phase = detrend_phase(np.angle(csi), idx) if with_phase else _NO_PHASE

    norm_amp = (amp / gain * np.sqrt(idx.size)).astype(np.float32) if normalise_gain else amp

    return SanitizedCsi(
        amplitude=norm_amp,
        phase=phase,
        raw_amplitude=amp,
        gain=gain,
        rssi=frame.rssi,
        timestamp_us=frame.timestamp_us,
        host_time=frame.host_time,
        seq=frame.seq,
        subcarriers=idx,
    )


def csi_ratio(csi_a: np.ndarray, csi_b: np.ndarray) -> np.ndarray:
    """Conjugate-multiplication ratio between two antenna streams.

    Dividing the CSI of one antenna by another cancels the CFO and SFO terms
    exactly, because both antennas share one oscillator and one ADC clock --
    the errors are common-mode.  This recovers genuinely usable phase and is
    what makes sub-centimetre respiration sensing possible in the literature.

    The ESP32-S3-WROOM-1 has a single antenna, so this cannot be used with one
    module.  It is kept here because it is the correct upgrade path: a second
    sensor node makes this available and materially improves breathing SNR.
    """
    denom = np.where(np.abs(csi_b) < 1e-9, 1e-9, csi_b)
    return csi_a / denom


def subcarrier_sensitivity(window: np.ndarray) -> np.ndarray:
    """Per-sub-carrier usefulness score over a (time, subcarrier) window.

    Multipath means some sub-carriers sit near a fade null where the response
    barely moves, while others sit on a steep part of the channel and swing
    widely for the same physical displacement.  Weighting by variance
    concentrates the downstream analysis on the sub-carriers that are actually
    responding, which is worth several dB of effective SNR for breathing.
    """
    if window.shape[0] < 4:
        return np.ones(window.shape[1], dtype=np.float64)
    var = np.var(window, axis=0)
    total = var.sum()
    if total <= 0:
        return np.ones(window.shape[1], dtype=np.float64)
    return var / total


def principal_component(window: np.ndarray, n_components: int = 1) -> np.ndarray:
    """Project a (time, subcarrier) window onto its leading principal axes.

    Sub-carriers are heavily correlated -- they are all observing the same
    room -- so most of the interesting variation lives in one or two
    directions, while the noise is spread across all of them.  Taking the
    leading component is a large, cheap SNR win, and it is the standard first
    step in the CSI respiration literature (CARM and descendants).

    The first component is *not* dropped here.  Some papers discard it as
    "static"; that is only correct when the DC term has not already been
    removed.  We mean-centre first, so component 0 is the dominant *varying*
    direction, which is what we want.
    """
    if window.shape[0] < 4:
        return np.zeros((window.shape[0], n_components))
    centred = window - window.mean(axis=0, keepdims=True)
    # Economy SVD: for a (T, K) window with K ~ 52 this is microseconds.
    try:
        _, _, vt = np.linalg.svd(centred, full_matrices=False)
    except np.linalg.LinAlgError:
        return np.zeros((window.shape[0], n_components))
    comps = vt[:n_components].T  # (K, n_components)
    return centred @ comps
