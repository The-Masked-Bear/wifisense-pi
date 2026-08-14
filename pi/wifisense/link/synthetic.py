"""A physically-motivated CSI simulator.

This exists for two reasons.  It lets the DSP and the UI be built and verified
before the ESP32 is flashed, and -- more importantly -- it is the only way to
test the detectors against *known* ground truth.  When a real room reports
14 breaths/min there is no way to check that number; here there is.

The model is a small multipath sum rather than filtered noise, because the
distinguishing feature of real CSI is that sub-carriers are *correlated in a
frequency-dependent way*: a path at delay tau contributes a term rotating at
exp(-j*2*pi*f*tau) across the band, so a moving reflector produces a
characteristic ripple, not independent per-bin jitter.  A detector tuned on
white noise falls over immediately on real data; one tuned on this does not.
"""

from __future__ import annotations

import math
import time

import numpy as np

from ..protocol import LLTF_SUBCARRIERS, build_csi_frame
from .base import Link

SUBCARRIER_SPACING_HZ = 312_500.0  # 802.11 OFDM, 20 MHz


class Scenario:
    """Named ground-truth conditions the simulator can be driven into."""

    EMPTY = "empty"
    STILL = "still"  # present, breathing, otherwise motionless
    WALKING = "walking"
    RESTLESS = "restless"  # present, fidgeting, breathing irregular


class SyntheticLink(Link):
    """Generates CSI frames that behave like a real 2.4 GHz indoor channel."""

    name = "synthetic"

    def __init__(
        self,
        *,
        rate_hz: float = 50.0,
        scenario: str = Scenario.STILL,
        breathing_bpm: float = 14.0,
        seed: int = 0,
        n_subcarriers: int = LLTF_SUBCARRIERS,
        noise: float = 0.7,
        auto_cycle: float = 0.0,
    ) -> None:
        super().__init__()
        self.rate_hz = rate_hz
        self.scenario = scenario
        self.breathing_bpm = breathing_bpm
        self.n_subcarriers = n_subcarriers
        self.noise = noise
        self.auto_cycle = auto_cycle
        self.rng = np.random.default_rng(seed)

        # Static multipath: the room itself.  A direct path plus a handful of
        # reflections off walls and furniture, with delays in the 0-60 ns range
        # that a domestic room produces.
        self.n_static = 6
        self.static_gain = self.rng.uniform(0.25, 1.0, self.n_static)
        self.static_gain[0] = 1.4  # dominant line-of-sight component
        self.static_delay = self.rng.uniform(0, 60e-9, self.n_static)
        self.static_delay[0] = 0.0
        self.static_phase = self.rng.uniform(0, 2 * np.pi, self.n_static)

        # The body: one reflector whose path length changes with chest motion
        # and with gross movement.
        self.body_gain = 0.45
        self.body_delay = 22e-9

        self._t0 = time.monotonic()
        self._seq = 0
        self._walk_phase = self.rng.uniform(0, 2 * np.pi)

        k = np.arange(n_subcarriers) - n_subcarriers // 2
        self._freqs = k * SUBCARRIER_SPACING_HZ

    # ------------------------------------------------------------ generation

    def _body_path_delta(self, t: float) -> float:
        """Extra path length (metres) contributed by the body at time ``t``."""
        if self.scenario == Scenario.EMPTY:
            return 0.0

        # Respiration: a 6 mm chest excursion, which the radio sees as a ~12 mm
        # round-trip path change.
        f_breath = self.breathing_bpm / 60.0
        breath = 0.006 * math.sin(2 * math.pi * f_breath * t)

        if self.scenario == Scenario.STILL:
            # Even a still person drifts a few millimetres over tens of seconds.
            drift = 0.004 * math.sin(2 * math.pi * 0.012 * t + self._walk_phase)
            return 2.0 * breath + drift

        if self.scenario == Scenario.RESTLESS:
            fidget = 0.05 * math.sin(2 * math.pi * 0.35 * t + self._walk_phase)
            fidget += 0.02 * math.sin(2 * math.pi * 1.1 * t)
            return 2.0 * breath + fidget

        if self.scenario == Scenario.WALKING:
            # ~1 m/s gait with the characteristic torso bob on top.
            walk = 0.5 * math.sin(2 * math.pi * 0.25 * t + self._walk_phase)
            bob = 0.03 * math.sin(2 * math.pi * 1.9 * t)
            return walk + bob

        return 0.0

    def _frame(self, t: float) -> np.ndarray:
        c = 299_792_458.0
        f_carrier = 2.437e9  # channel 6

        h = np.zeros(self.n_subcarriers, dtype=np.complex128)
        for g, d, ph in zip(self.static_gain, self.static_delay, self.static_phase):
            h += g * np.exp(1j * (ph - 2 * np.pi * self._freqs * d))

        if self.scenario != Scenario.EMPTY:
            extra = self._body_path_delta(t)
            delay = self.body_delay + extra / c
            # The carrier-frequency term is what makes millimetre motion
            # visible: a 12 mm path change is ~0.1 of a wavelength at 2.4 GHz,
            # a phase rotation of ~35 degrees, which is easily measurable.
            carrier_rot = -2 * np.pi * f_carrier * (extra / c)
            h += self.body_gain * np.exp(1j * (carrier_rot - 2 * np.pi * self._freqs * delay))

        # Receiver noise, then the AGC gain step that makes raw CSI amplitude
        # untrustworthy on real hardware -- the pipeline has to survive it.
        h += (self.rng.normal(0, self.noise, self.n_subcarriers)
              + 1j * self.rng.normal(0, self.noise, self.n_subcarriers)) * 0.05
        agc = self.rng.choice([1.0, 1.0, 1.0, 0.79, 1.26])
        h *= agc

        # Guard bands and DC are reported as zero by the radio.
        h[0] = 0
        h[self.n_subcarriers // 2] = 0
        h[27:38] = 0

        scale = 40.0 / (np.abs(h).max() or 1.0)
        iq = np.empty(self.n_subcarriers * 2, dtype=np.int8)
        iq[0::2] = np.clip(np.round(h.imag * scale), -128, 127)  # imaginary first
        iq[1::2] = np.clip(np.round(h.real * scale), -128, 127)
        return iq

    # ------------------------------------------------------------------- run

    def run(self) -> None:
        self.connected = True
        period = 1.0 / self.rate_hz
        next_at = time.monotonic()
        cycle_at = time.monotonic() + self.auto_cycle
        order = [Scenario.EMPTY, Scenario.STILL, Scenario.WALKING, Scenario.RESTLESS]

        while not self._stop.is_set():
            now = time.monotonic()
            if now < next_at:
                time.sleep(min(next_at - now, 0.05))
                continue

            if self.auto_cycle > 0 and now >= cycle_at:
                self.scenario = order[(order.index(self.scenario) + 1) % len(order)]
                cycle_at = now + self.auto_cycle

            t = now - self._t0
            iq = self._frame(t)
            rssi = int(-42 - 8 * abs(math.sin(t * 0.05)) + self.rng.normal(0, 1.2))

            raw = build_csi_frame(
                int(t * 1e6),
                iq,
                rssi=rssi,
                channel=6,
                seq=self._seq,
            )
            self._seq = (self._seq + 1) & 0xFFFF
            self.feed_bytes(raw)

            # Real links jitter; a perfectly regular simulator would let a
            # resampling bug pass unnoticed.
            next_at += period * float(self.rng.uniform(0.85, 1.15))
            if next_at < now - 0.5:
                next_at = now + period

        self.connected = False

    def set_scenario(self, scenario: str) -> None:
        self.scenario = scenario
