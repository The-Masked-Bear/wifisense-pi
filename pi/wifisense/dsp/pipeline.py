"""The end-to-end CSI processing pipeline.

Raw frames in one side, a UI-ready snapshot out the other.  This is the only
place that knows the order of operations, so the link layer stays ignorant of
signal processing and the web layer stays ignorant of both.
"""

from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass, field

import numpy as np

from ..protocol import CsiFrame, EnvFrame, LogFrame, StatusFrame
from .breathing import BreathingDetector
from .csi import SanitizedCsi, sanitize
from .motion import MotionDetector, MotionState

# The waterfall is drawn ~40 px tall on a phone and ~120 px on a desktop, and
# a 52-wide vector is already finer than either.  Binning down to 32 halves the
# bytes on the wire for no visible loss.
WATERFALL_BINS = 32

# Waterfall column rate.  Appending one column per CSI frame means 100 columns
# a second, which forces 100 full-canvas blits in the browser and scrolls a
# 1400 px panel in 14 seconds -- faster than anything being displayed actually
# changes.  25 Hz keeps roughly a minute of history on screen and cuts the
# browser's per-frame work fourfold.
WATERFALL_HZ = 25.0


# Environment history: 600 samples at one every 3 s is half an hour, which is
# the shortest span over which a room's CO2 and humidity visibly respond to
# someone walking in.  A minute of it would just look like noise.
ENV_TRACE_LEN = 600

# Outdoor background CO2, matching MQ135_ATMO_PPM in the firmware -- the
# concentration the sensor's baseline is anchored to during calibration.
ATMO_PPM = 420.0

# Below this the reading is not merely clean, it is IMPOSSIBLE: indoor air is
# always at or above outdoor background, because the outdoor supply is the
# floor and people only add to it.  A reading underneath it therefore says
# nothing about the air and everything about the baseline -- Rs has drifted
# above the R0 it was calibrated against.
#
# That happens constantly with a new sensor, whose element resistance climbs
# for the first day or two of continuous power. Reporting a confident green
# "0 - Good" in that state would be the sensor's least trustworthy moment
# dressed up as its most reassuring, so it is called out instead.
#
# No margin: anything strictly below outdoor background is provably wrong.
# A well-ventilated room legitimately reads AT outdoor, which maps to AQI 0
# without triggering this flag.  The old 10% margin (378 ppm) let readings
# from 378-420 -- still physically impossible indoors -- display as a
# confident green "Good", which is exactly the misleading state this flag
# exists to prevent.
STALE_BASELINE_PPM = ATMO_PPM

# Air Quality Index, computed the way a real AQI is computed: piecewise-linear
# interpolation between breakpoints onto the standard 0-500 scale, with the
# familiar category boundaries at 50 / 100 / 150 / 200 / 300.
#
# What is NOT standard is the pollutant.  A regulatory AQI (US EPA, or India's
# CPCB) is the worst of several sub-indices, and in practice PM2.5 dominates
# it.  The MQ135 cannot measure particulates at all -- it is one resistance
# responding to a mix of reducing gases -- so no honest PM2.5 sub-index exists
# here, and an index that ignores PM2.5 will read "good" in a room full of
# smoke that a real monitor would score 200.
#
# So these breakpoints map the sensor's CO2-equivalent reading onto the AQI
# scale using the established indoor-air ventilation thresholds: ~420 ppm is
# outdoor air, 1000 is the conventional ceiling for occupied rooms, 2000 is
# where measurable cognitive effects are documented, and 5000 is the
# occupational exposure limit.  The number is directly comparable to itself
# over time and roughly comparable to a real AQI in feel; it is not the same
# measurement, and the UI says so rather than implying otherwise.
#
# The lowest band starts at 420, not a round 400, because 420 is the outdoor
# background the firmware anchors its calibration to.  Anchor and scale floor
# have to be the same number: with the scale starting at 400, a perfectly
# calibrated sensor sitting in genuinely outdoor air reported AQI 5 rather than
# 0, so the best reading the instrument can physically produce was not the
# bottom of its own scale.
#
#                  ppm_lo   ppm_hi   aqi_lo  aqi_hi  category
AQI_BREAKPOINTS = (
    (ATMO_PPM, 600.0, 0, 50, "good"),
    (600.0, 1000.0, 51, 100, "moderate"),
    (1000.0, 1500.0, 101, 150, "sensitive"),
    (1500.0, 2000.0, 151, 200, "unhealthy"),
    (2000.0, 5000.0, 201, 300, "very-unhealthy"),
    (5000.0, 10000.0, 301, 500, "hazardous"),
)

AQI_LABELS = {
    "good": "Good",
    "moderate": "Moderate",
    "sensitive": "Unhealthy for sensitive groups",
    "unhealthy": "Unhealthy",
    "very-unhealthy": "Very unhealthy",
    "hazardous": "Hazardous",
}


def air_quality_index(ppm: float | None) -> tuple[int | None, str | None]:
    """CO2-equivalent ppm to an AQI value and its category.

    Returns (None, None) for a missing or uncalibrated reading, so the caller
    can distinguish "no data" from a genuine index of 0.
    """
    if ppm is None:
        return None, None
    # Cleaner than the cleanest breakpoint still means the best category; the
    # scale has no room below zero.
    if ppm <= AQI_BREAKPOINTS[0][0]:
        return 0, AQI_BREAKPOINTS[0][4]
    for lo, hi, i_lo, i_hi, name in AQI_BREAKPOINTS:
        if ppm <= hi:
            return int(round((i_hi - i_lo) / (hi - lo) * (ppm - lo) + i_lo)), name
    # The scale is defined to stop at 500; beyond it the sensor is far outside
    # anything its curve was fitted for anyway.
    return 500, AQI_BREAKPOINTS[-1][4]

@dataclass
class PipelineStats:
    frames_in: int = 0
    frames_used: int = 0
    frames_rejected: int = 0
    dropped_by_seq: int = 0
    last_frame_time: float = 0.0
    started: float = field(default_factory=time.monotonic)
    # Rolling (count, timestamp) marks for true throughput.
    _mark_count: int = 0
    _mark_time: float = 0.0
    _throughput: float = 0.0

    def throughput(self) -> float:
        """Frames actually delivered per second.

        Distinct from the detector's sample-rate estimate, which is the median
        gap between *received* frames.  That median is deliberately robust to
        outliers so one stall cannot skew the frequency axis -- but the same
        property makes it blind to loss: over a link dropping 17% of frames it
        still reported a confident 100 Hz while only 75 arrived.  For anything
        a human reads, that is the wrong number.
        """
        now = time.monotonic()
        if self._mark_time == 0.0:
            self._mark_time, self._mark_count = now, self.frames_used
            return 0.0
        dt = now - self._mark_time
        if dt >= 2.0:
            self._throughput = (self.frames_used - self._mark_count) / dt
            self._mark_time, self._mark_count = now, self.frames_used
        return self._throughput

    def as_dict(self) -> dict:
        now = time.monotonic()
        return {
            "throughput_hz": round(self.throughput(), 1),
            "frames_in": self.frames_in,
            "frames_used": self.frames_used,
            "frames_rejected": self.frames_rejected,
            "dropped_by_seq": self.dropped_by_seq,
            "uptime": round(now - self.started, 1),
            "stale": round(now - self.last_frame_time, 2) if self.last_frame_time else None,
        }


class Pipeline:
    """Owns the detectors and the rolling display buffers."""

    def __init__(
        self,
        *,
        nominal_rate: float = 50.0,
        history_seconds: float = 60.0,
        breathing_window: float = 45.0,
        trim_guard: bool = True,
        sea_level_hpa: float = 1013.25,
    ) -> None:
        self.nominal_rate = nominal_rate
        self.history_seconds = history_seconds
        self.breathing_window = breathing_window
        self.trim_guard = trim_guard
        self.sea_level_pa = sea_level_hpa * 100.0

        self._motion: MotionDetector | None = None
        self._breathing: BreathingDetector | None = None
        self._n_sub = 0

        self.stats = PipelineStats()
        self.last_status: StatusFrame | None = None
        self.last_env: EnvFrame | None = None
        self.env_repeats = 0
        # (host_time, temp_c, humidity, pressure_hpa, gas_ppm); any element may
        # be None when that particular sensor did not answer.
        self.env_trace: deque[tuple] = deque(maxlen=ENV_TRACE_LEN)
        self.logs: deque[str] = deque(maxlen=50)

        # Display series, decoupled from the detector's internal buffers so the
        # UI can be served at its own cadence without touching the DSP state.
        hist = int(history_seconds * nominal_rate)
        self.motion_trace: deque[tuple[float, float, float]] = deque(maxlen=hist)
        # 1500 columns at 25 Hz is a minute of history -- enough to backfill a
        # desktop-width canvas the instant a browser connects, instead of
        # leaving it half empty for the first 40 seconds.
        self.waterfall: deque[list[int]] = deque(maxlen=1500)
        self.rssi_trace: deque[tuple[float, int]] = deque(maxlen=hist)

        # Monotonic count of every waterfall row ever produced.  Clients track
        # how many they have seen so each receives only the new ones rather
        # than the whole window on every update.
        self.waterfall_total = 0
        self._last_seq: int | None = None
        self._amp_lo = 0.0
        self._amp_hi = 1.0
        self._bin_edges = None
        self._bin_counts = None
        self._wf_decim = max(1, int(round(nominal_rate / WATERFALL_HZ)))
        self._wf_count = 0

    # ------------------------------------------------------------------ setup

    def _ensure(self, n_sub: int) -> None:
        if self._motion is not None and n_sub == self._n_sub:
            return
        self._n_sub = n_sub
        self._motion = MotionDetector(
            n_sub, history_seconds=self.history_seconds, nominal_rate=self.nominal_rate
        )
        self._breathing = BreathingDetector(
            n_sub, nominal_rate=self.nominal_rate, window_seconds=self.breathing_window
        )

    # ------------------------------------------------------------------ input

    def handle(self, frame) -> None:
        """Accept any decoded frame type from a link."""
        if isinstance(frame, CsiFrame):
            self._handle_csi(frame)
        elif isinstance(frame, StatusFrame):
            self.last_status = frame
        elif isinstance(frame, EnvFrame):
            self._handle_env(frame)
        elif isinstance(frame, LogFrame):
            self.logs.append(frame.text)

    def _handle_env(self, frame: EnvFrame) -> None:
        # The node retransmits each reading about three times, a second apart,
        # because a single environment frame is easily lost in the CSI traffic
        # sharing the radio.  node_ms is the node's own sample timestamp, so
        # repeats of a reading already held are dropped here rather than
        # tripling every point in the history.
        #
        # Dropping them outright -- rather than refreshing host_time -- is what
        # keeps the displayed age honest: a repeat carries no newer information,
        # so the reading really is as old as when it was taken. If every copy of
        # a sample is lost, the age climbs and the UI flags it, which is exactly
        # what should happen.
        if self.last_env is not None and frame.node_ms == self.last_env.node_ms:
            self.env_repeats += 1
            return
        if not frame.host_time:
            frame.host_time = time.monotonic()
        self.last_env = frame
        self.env_trace.append(
            (
                frame.host_time,
                frame.temperature_c,
                frame.humidity_pct if frame.dht_ok else None,
                frame.pressure_pa / 100.0 if frame.bmp_ok else None,
                frame.gas_ppm if frame.gas_calibrated else None,
            )
        )

    def _handle_csi(self, frame: CsiFrame) -> None:
        self.stats.frames_in += 1
        if not frame.host_time:
            frame.host_time = time.monotonic()
        self.stats.last_frame_time = frame.host_time

        # Sequence gaps tell us the link is losing frames, which matters for
        # breathing (it assumes a near-uniform window) and is worth surfacing.
        if self._last_seq is not None:
            gap = (frame.seq - self._last_seq) & 0xFFFF
            if 1 < gap < 1000:
                self.stats.dropped_by_seq += gap - 1
        self._last_seq = frame.seq

        s = sanitize(frame, trim_guard=self.trim_guard)
        if s is None:
            self.stats.frames_rejected += 1
            return
        self.stats.frames_used += 1

        self._ensure(s.amplitude.size)
        assert self._motion is not None and self._breathing is not None

        st = self._motion.push(s)
        self._breathing.push(s)

        # dB, not the 0-1 display scaling: the UI plots this against the
        # detection thresholds, which are expressed in dB over the noise floor.
        self.motion_trace.append((s.host_time, st.motion_db, st.vital_db))
        self.rssi_trace.append((s.host_time, s.rssi))
        self._wf_count += 1
        if self._wf_count >= self._wf_decim:
            self._wf_count = 0
            self.waterfall.append(self._bin_amplitude(s.amplitude))
            self.waterfall_total += 1

    def _bin_amplitude(self, amp: np.ndarray) -> list[int]:
        """Bin and quantise one amplitude vector to 0-255 for the waterfall.

        The colour scale is auto-ranged from a slowly-adapting percentile pair
        rather than the instantaneous min/max, so the display does not flash on
        every outlier but still follows genuine changes in the room.
        """
        if amp.size >= WATERFALL_BINS:
            edges = self._bin_edges
            if edges is None or edges[-1] != amp.size:
                edges = np.linspace(0, amp.size, WATERFALL_BINS + 1).astype(int)
                self._bin_edges = edges
                self._bin_counts = np.maximum(np.diff(edges), 1)
            # One reduceat instead of 32 separate slice-and-mean calls.  At
            # 100 Hz the loop version was half of this process's entire CPU
            # budget -- 3200 numpy calls a second to average 52 numbers.
            binned = np.add.reduceat(amp, edges[:-1]) / self._bin_counts
        else:
            binned = np.interp(
                np.linspace(0, amp.size - 1, WATERFALL_BINS), np.arange(amp.size), amp
            )

        # Sorting 32 values costs far less than two np.percentile calls, which
        # carry a large fixed overhead relative to an array this small.
        srt = np.sort(binned)
        last = srt.size - 1
        lo = float(srt[int(0.05 * last)])
        hi = float(srt[int(0.95 * last)])
        a = 0.02
        self._amp_lo = (1 - a) * self._amp_lo + a * lo if self._amp_lo else lo
        self._amp_hi = (1 - a) * self._amp_hi + a * hi if self._amp_hi else hi
        span = max(self._amp_hi - self._amp_lo, 1e-6)
        scaled = np.clip((binned - self._amp_lo) / span, 0.0, 1.0)
        return [int(v) for v in (scaled * 255).astype(np.uint8)]

    def _env_snapshot(self, trace_points: int = 120) -> dict | None:
        """The environment panel's payload, or None if no frame has arrived.

        Returning None rather than an empty dict is deliberate: the UI hides
        the whole panel in that case, so a build with no sensors attached shows
        a dashboard that looks finished rather than one full of dashes.
        """
        e = self.last_env
        if e is None:
            return None

        temp = e.temperature_c
        alt = e.altitude_m(self.sea_level_pa)
        dew = e.dew_point_c()

        aqi, category = air_quality_index(e.gas_ppm if e.gas_calibrated else None)
        stale_baseline = e.gas_calibrated and e.gas_ppm < STALE_BASELINE_PPM

        series = []
        if self.env_trace:
            trace = list(self.env_trace)
            step = max(1, len(trace) // trace_points)
            trace = trace[::step][-trace_points:]
            t0 = trace[-1][0]
            series = [
                {
                    "t": round(t - t0, 1),
                    "tc": round(tc, 2) if tc is not None else None,
                    "rh": round(rh, 1) if rh is not None else None,
                    "hpa": round(hpa, 2) if hpa is not None else None,
                    "ppm": round(ppm) if ppm is not None else None,
                    # The sparkline plots the index, not the raw ppm, so the
                    # curve and the big number it sits under move together.
                    # A reading below outdoor background is stale (baseline
                    # drift, not clean air), so its AQI is suppressed rather
                    # than painted as a confident green zero.
                    "aqi": air_quality_index(ppm)[0] if ppm is None or ppm >= STALE_BASELINE_PPM else None,
                }
                for t, tc, rh, hpa, ppm in trace
            ]

        return {
            "age": round(max(0.0, time.monotonic() - e.host_time), 1),
            "temp_c": round(temp, 2) if temp is not None else None,
            "temp_source": "bmp280" if e.bmp_ok else ("dht22" if e.dht_ok else None),
            "bmp_temp_c": round(e.bmp_temp_c, 2) if e.bmp_ok else None,
            "dht_temp_c": round(e.dht_temp_c, 2) if e.dht_ok else None,
            "humidity": round(e.humidity_pct, 1) if e.dht_ok else None,
            "dew_point_c": round(dew, 1) if dew is not None else None,
            "pressure_hpa": round(e.pressure_pa / 100.0, 2) if e.bmp_ok else None,
            "altitude_m": round(alt, 1) if alt is not None else None,
            "gas_ppm": round(e.gas_ppm) if e.gas_calibrated else None,
            "gas_ratio": round(e.gas_ratio, 3) if e.gas_calibrated else None,
            "gas_mv": e.gas_mv,
            "gas_rs": e.gas_rs_ohms,
            # The baseline the whole index rests on, and how many times the
            # node has auto-corrected it. Surfaced because the alternative was
            # a serial cable -- and opening that port resets the board.
            "gas_r0": e.gas_r0_ohms,
            "gas_abc": e.gas_abc,
            "aqi": aqi,
            "aqi_category": category,
            "aqi_label": AQI_LABELS.get(category) if category else None,
            # True when the reading has fallen below outdoor background, which
            # means the baseline has drifted rather than the air having
            # improved.  The UI shows this instead of a reassuring green zero.
            "baseline_stale": stale_baseline,
            "sensors": {
                "bmp280": e.bmp_ok,
                "dht22": e.dht_ok,
                "mq135": e.gas_ok,
                "mq135_calibrated": e.gas_calibrated,
            },
            "dht_fail": e.dht_fail,
            "series": series,
        }

    # ----------------------------------------------------------------- output

    @property
    def motion_state(self) -> MotionState:
        return self._motion.state if self._motion else MotionState()

    def observation(self) -> tuple[dict, dict, dict | None]:
        """Scalar-only state for the archive: no waveform, spectrum or series.

        Deliberately not ``snapshot()``.  That builds a waterfall delta, a
        180-point motion trace and two respiration arrays -- several kilobytes
        of work per call, all of it for a display that is not being drawn.

        Calling this is close to free even at 1 Hz: ``BreathingDetector.compute``
        rate-limits itself to one real evaluation a second and returns its
        cached state otherwise, so the archive shares whatever the UI already
        paid for rather than duplicating the FFT.
        """
        st = self.motion_state
        breathing: dict = {}
        if self._breathing is not None:
            b = self._breathing.compute(
                moving=st.moving, occupied=st.occupied,
                motion_db=st.motion_db, vital_db=st.vital_db,
            )
            breathing = {"bpm": b.bpm, "confidence": b.confidence, "valid": b.valid}

        env = None
        e = self.last_env
        if e is not None:
            ppm = e.gas_ppm if e.gas_calibrated else None
            # Same AQI function and the same staleness rule the live panel uses,
            # so a night in the archive can never disagree with what was on
            # screen at the time.
            aqi, _ = air_quality_index(ppm)
            env = {
                "temp_c": e.temperature_c,
                "humidity": e.humidity_pct if e.dht_ok else None,
                "pressure_hpa": e.pressure_pa / 100.0 if e.bmp_ok else None,
                "gas_ppm": ppm,
                "aqi": aqi,
                "baseline_stale": bool(ppm is not None and ppm < STALE_BASELINE_PPM),
            }
        return st.as_dict(), breathing, env

    def snapshot(self, *, waterfall_rows: int = 0, trace_points: int = 180, full: bool = True) -> dict:
        """A UI update.

        ``waterfall_rows`` is how many *new* rows the caller has not seen yet,
        so each client receives only its delta.

        ``full`` controls the slow-changing fields -- the activity trace, the
        respiration waveform and spectrum, and the node status.  Those are
        several kilobytes and none of them changes meaningfully faster than a
        couple of Hz, so re-sending them at the full UI rate was costing ~1
        Mbit/s and a large share of a Pi 4 core for no visible benefit.  The
        server asks for them a few times a second and sends only the state and
        the waterfall delta in between.
        """
        st = self.motion_state
        breathing = {}
        if self._breathing is not None:
            b = self._breathing.compute(moving=st.moving, occupied=st.occupied,
                                        motion_db=st.motion_db, vital_db=st.vital_db)
            breathing = b.as_dict()
            if not full:
                # The scalar readings are cheap; the two arrays are not.
                breathing = {k: v for k, v in breathing.items()
                             if k not in ("waveform", "spectrum", "spectrum_bpm")}

        motion_series = []
        if full and self.motion_trace:
            # islice over the deque rather than list() of the whole thing: the
            # trace holds up to 6000 samples and copying it all to keep 180 was
            # pure waste at every update.
            trace = list(self.motion_trace)
            step = max(1, len(trace) // trace_points)
            trace = trace[::step][-trace_points:]
            t0 = trace[-1][0]
            motion_series = [
                {"t": round(t - t0, 1), "m": round(m, 2), "p": round(p, 2)} for t, m, p in trace
            ]

        rows: list[list[int]] = []
        if waterfall_rows > 0:
            rows = list(self.waterfall)[-waterfall_rows:]

        status = None
        if self.last_status is not None:
            s = self.last_status
            status = {
                "uptime_ms": s.uptime_ms,
                "csi_count": s.csi_count,
                "dropped": s.dropped,
                "free_heap": s.free_heap,
                "rssi": s.rssi,
                "channel": s.channel,
                "sample_rate": s.sample_rate,
                "link_mode": "sta" if s.link_mode == 1 else "sniffer",
                "ip": s.ip_str,
                "associated": s.associated,
                "csi_enabled": s.csi_enabled,
            }

        return {
            "t": time.time(),
            "motion": st.as_dict(),
            "breathing": breathing,
            "series": motion_series,
            "waterfall": rows,
            "waterfall_bins": WATERFALL_BINS,
            "stats": self.stats.as_dict(),
            "node": status,
            # Only on full updates.  These readings change over minutes, and
            # the trace behind them is a couple of kilobytes -- re-sending it
            # twelve times a second alongside the waterfall was exactly the
            # waste the fast/slow split exists to avoid.
            "env": self._env_snapshot() if full else None,
            "logs": list(self.logs)[-5:],
        }

    @property
    def waterfall_len(self) -> int:
        return len(self.waterfall)

    def reset(self) -> None:
        if self._motion:
            self._motion.reset()
        if self._breathing:
            self._breathing.reset()
        self.motion_trace.clear()
        self.waterfall.clear()
        self.rssi_trace.clear()
        # Deliberately NOT cleared: last_env and env_trace.  Reset exists to
        # re-baseline the CSI detectors, and throwing away half an hour of
        # temperature history to do that would be a surprising side effect --
        # the environment sensors have no baseline to re-establish.
        self.stats = PipelineStats()
        self.waterfall_total = 0
        self._last_seq = None
