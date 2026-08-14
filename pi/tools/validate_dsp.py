#!/usr/bin/env python3
"""Validate the DSP chain against synthetic CSI with known ground truth.

Run this before touching hardware, and again after any change to the
detectors.  A real room cannot tell you whether "14 breaths/min" is correct;
the simulator can, so this is the only place the pipeline is actually
falsifiable.

    python tools/validate_dsp.py
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from wifisense.dsp.pipeline import Pipeline
from wifisense.link.synthetic import Scenario, SyntheticLink
from wifisense.protocol import FrameDecoder

RATE = 50.0
GREEN, RED, YEL, DIM, RST = "\033[32m", "\033[31m", "\033[33m", "\033[2m", "\033[0m"

failures: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> bool:
    mark = f"{GREEN}PASS{RST}" if ok else f"{RED}FAIL{RST}"
    print(f"  [{mark}] {label}{('  ' + DIM + detail + RST) if detail else ''}")
    if not ok:
        failures.append(label)
    return ok


def run_scenario(
    scenario: str, seconds: float, bpm: float = 14.0, seed: int = 3, warmup: float = 0.0
) -> tuple[Pipeline, list[dict]]:
    """Drive the pipeline with simulated time -- far faster than real time."""
    gen = SyntheticLink(rate_hz=RATE, scenario=scenario, breathing_bpm=bpm, seed=seed)
    pipe = Pipeline(nominal_rate=RATE, history_seconds=60.0, breathing_window=45.0)
    dec = FrameDecoder()
    dec.feed(b"\x00")

    n = int(seconds * RATE)
    rng = np.random.default_rng(seed + 100)
    t = 0.0
    samples = []
    for i in range(n):
        # Jittered arrival, matching what a real link delivers.
        t += (1.0 / RATE) * float(rng.uniform(0.85, 1.15))
        iq = gen._frame(t)
        raw = gen_build(gen, t, iq, i)
        for frame in dec.feed(raw):
            frame.host_time = t
            pipe.handle(frame)
        if i % int(RATE * 5) == 0 and i > 0:
            samples.append({"t": t, "state": pipe.motion_state.as_dict()})
    return pipe, samples


def gen_build(gen, t, iq, seq):
    from wifisense.protocol import build_csi_frame

    return build_csi_frame(int(t * 1e6), iq, rssi=-45, channel=6, seq=seq)


def breathing_of(pipe: Pipeline, moving: bool, occupied: bool):
    b = pipe._breathing
    b._last_compute = 0.0  # bypass the rate limiter for offline evaluation
    st = pipe.motion_state
    # The detector now tracks the rate across windows before publishing it, so
    # drive it for several windows the way the live service does rather than
    # asking once.
    out = None
    for _ in range(5):
        b._last_compute = 0.0
        out = b.compute(moving=moving, occupied=occupied, motion_db=st.motion_db,
                        vital_db=st.vital_db, min_interval=0.0)
    return out


def main() -> int:
    print(f"\n{'='*68}\n  DSP VALIDATION -- synthetic CSI, known ground truth\n{'='*68}\n")
    t_start = time.monotonic()

    # ---------------------------------------------------------- 1. empty room
    # An empty room must read ~0 dB in both human bands by construction: both
    # the signal bands and the noise band then contain only receiver noise.
    print("1. EMPTY ROOM (90 s) -- both bands must sit at the noise floor")
    pe, _ = run_scenario(Scenario.EMPTY, 90)
    st = pe.motion_state
    check("not occupied", not st.occupied, f"motion={st.motion_db:+.1f}dB vital={st.vital_db:+.1f}dB")
    check("not moving", not st.moving, f"motion={st.motion_db:+.1f}dB")
    check("activity == 'empty'", st.activity == "empty", f"got '{st.activity}'")
    check("motion band near noise floor", abs(st.motion_db) < 4.5, f"{st.motion_db:+.1f} dB")
    check("vital band near noise floor", abs(st.vital_db) < 4.5, f"{st.vital_db:+.1f} dB")
    check("frames survived sanitisation", pe.stats.frames_used > 4000,
          f"{pe.stats.frames_used}/{pe.stats.frames_in}")
    b = breathing_of(pe, st.moving, st.occupied)
    check("no breathing claimed in empty room", not b.valid, f"reason='{b.reason}'")

    # -------------------------------------------------- 2. still + breathing
    print("\n2. STILL PERSON, 14.0 breaths/min (120 s) -- the hard case")
    ps, _ = run_scenario(Scenario.STILL, 120, bpm=14.0)
    st = ps.motion_state
    check("occupied", st.occupied, f"motion={st.motion_db:+.1f}dB vital={st.vital_db:+.1f}dB")
    check("vital band well above noise", st.vital_db > 6.0, f"{st.vital_db:+.1f} dB")
    check("not classified as walking", st.activity in ("still", "subtle"), f"got '{st.activity}'")
    b = breathing_of(ps, st.moving, st.occupied)
    err = abs(b.bpm - 14.0)
    check("breathing detected", b.valid, f"conf={b.confidence:.2f} reason='{b.reason}'")
    check("BPM within +/-1.5 of truth", err < 1.5, f"got {b.bpm:.1f}, truth 14.0, err {err:.2f}")
    check("waveform populated", len(b.waveform or []) > 50, f"{len(b.waveform or [])} pts")
    check("spectrum populated", len(b.spectrum or []) > 20, f"{len(b.spectrum or [])} bins")

    # ------------------------------------------- 3. a different rate, no tuning
    print("\n3. STILL PERSON, 22.0 breaths/min (120 s) -- must not be hard-coded")
    p3, _ = run_scenario(Scenario.STILL, 120, bpm=22.0, seed=11)
    st3 = p3.motion_state
    b = breathing_of(p3, st3.moving, st3.occupied)
    err = abs(b.bpm - 22.0)
    check("breathing detected", b.valid, f"conf={b.confidence:.2f}")
    check("BPM within +/-2.0 of truth", err < 2.0, f"got {b.bpm:.1f}, truth 22.0, err {err:.2f}")

    # ------------------------------------------------------------ 4. walking
    print("\n4. WALKING (90 s) -- must fire motion, must refuse to guess BPM")
    pw, _ = run_scenario(Scenario.WALKING, 90)
    stw = pw.motion_state
    check("occupied", stw.occupied, f"motion={stw.motion_db:+.1f}dB")
    check("moving", stw.moving, f"motion={stw.motion_db:+.1f}dB")
    check("motion band well above noise", stw.motion_db > 8.0, f"{stw.motion_db:+.1f} dB")
    check("activity is subtle/active/vigorous",
          stw.activity in ("active", "vigorous", "subtle"), f"got '{stw.activity}'")
    b = breathing_of(pw, stw.moving, stw.occupied)
    check("breathing suppressed while moving", not b.valid, f"reason='{b.reason}'")

    # ------------------------------------------- 5. separation empty vs occupied
    # These are now absolute dB against a physical reference, so comparing
    # across independent runs is meaningful -- which is the entire point of
    # referencing the noise floor rather than the room's own history.
    print("\n5. DISCRIMINATION -- absolute dB must separate the scenarios")
    m_e, v_e = pe.motion_state.motion_db, pe.motion_state.vital_db
    m_w = pw.motion_state.motion_db
    v_s = ps.motion_state.vital_db
    check("walking motion >> empty motion", m_w > m_e + 8.0,
          f"empty={m_e:+.1f}dB walk={m_w:+.1f}dB  (margin {m_w-m_e:.1f}dB)")
    check("still vitals >> empty vitals", v_s > v_e + 6.0,
          f"empty={v_e:+.1f}dB still={v_s:+.1f}dB  (margin {v_s-v_e:.1f}dB)")
    check("empty room occupied=False while still room occupied=True",
          (not pe.motion_state.occupied) and ps.motion_state.occupied)

    # ------------------------------------------------------ 6. snapshot shape
    print("\n6. SNAPSHOT -- the payload the UI actually consumes")
    snap = ps.snapshot(waterfall_rows=8)
    ok = all(k in snap for k in ("motion", "breathing", "series", "waterfall", "stats"))
    check("all top-level keys present", ok, ", ".join(snap.keys()))
    check("waterfall rows are 32 bins of 0-255",
          len(snap["waterfall"]) == 8 and all(len(r) == 32 for r in snap["waterfall"])
          and all(0 <= v <= 255 for r in snap["waterfall"] for v in r),
          f"{len(snap['waterfall'])} rows")
    check("series has motion+presence", len(snap["series"]) > 10 and "m" in snap["series"][0],
          f"{len(snap['series'])} pts")
    import json
    size = len(json.dumps(snap))
    check("snapshot under 16 KB", size < 16384, f"{size} B")

    # ------------------------------------------------------- 7. sanity checks
    print("\n7. ROBUSTNESS")
    pipe, _ = run_scenario(Scenario.STILL, 30, seed=99)
    check("no NaN/Inf in motion state",
          all(np.isfinite(v) for v in [pipe.motion_state.motion, pipe.motion_state.presence]))
    check("rate estimate near 50 Hz", abs(pipe.motion_state.sample_rate - 50.0) < 6.0,
          f"{pipe.motion_state.sample_rate:.1f} Hz")
    check("zero rejected frames", pipe.stats.frames_rejected == 0,
          f"{pipe.stats.frames_rejected} rejected")

    # ------------------------------------------------------- 8. environment
    # The firmware is the only other implementation of this frame, so without
    # a round trip here the wire format has no test at all -- and a fixed-point
    # scale factor that disagrees between the two sides produces readings that
    # are wrong by exactly 100x while looking entirely well-formed.
    print("\n8. ENVIRONMENT FRAME -- wire format round trip")
    from wifisense.protocol import (
        ENV_BMP_OK,
        ENV_DHT_OK,
        ENV_GAS_CALIBRATED,
        ENV_GAS_OK,
        EnvFrame,
        LEGACY_ENV_STRUCT,
        build_env_frame,
    )

    all_ok = ENV_BMP_OK | ENV_DHT_OK | ENV_GAS_OK | ENV_GAS_CALIBRATED
    raw = build_env_frame(
        90_000,
        pressure_pa=99_850.0,
        gas_rs_ohms=42_000.0,
        gas_r0_ohms=71_143.0,
        gas_abc=2,
        bmp_temp_c=21.34,
        dht_temp_c=22.10,
        humidity_pct=55.6,
        gas_mv=744,
        gas_ratio=1.375,
        gas_ppm=1450,
        flags=all_ok,
    )
    dec = FrameDecoder()
    dec.feed(b"\x00")
    got = list(dec.feed(raw))
    check("decodes to exactly one EnvFrame",
          len(got) == 1 and isinstance(got[0], EnvFrame), f"{len(got)} frame(s)")
    ef = got[0]
    check("temperature survives fixed point", abs(ef.bmp_temp_c - 21.34) < 0.006,
          f"{ef.bmp_temp_c:.3f} vs 21.34")
    check("humidity survives fixed point", abs(ef.humidity_pct - 55.6) < 0.006,
          f"{ef.humidity_pct:.3f} vs 55.6")
    check("gas ratio survives fixed point", abs(ef.gas_ratio - 1.375) < 0.0006,
          f"{ef.gas_ratio:.4f} vs 1.375")
    check("pressure exact", ef.pressure_pa == 99_850.0, f"{ef.pressure_pa}")
    check("baseline R0 and correction count survive the wire",
          ef.gas_r0_ohms == 71_143.0 and ef.gas_abc == 2,
          f"r0={ef.gas_r0_ohms} abc={ef.gas_abc}")
    check("BMP280 preferred as the temperature source",
          ef.temperature_c == ef.bmp_temp_c, f"{ef.temperature_c}")
    dew = ef.dew_point_c()
    check("dew point plausible and below air temperature",
          dew is not None and 10.0 < dew < ef.temperature_c,
          f"{dew:.2f} C at {ef.humidity_pct}% RH")
    alt = ef.altitude_m()
    check("altitude plausible for sub-sea-level pressure",
          alt is not None and 100.0 < alt < 200.0, f"{alt:.1f} m")

    # A truncated payload must be rejected, not silently unpacked from
    # whatever follows it in the buffer.
    from wifisense.protocol import DecodeStats, build_frame, decode_frame

    st8 = DecodeStats()
    check("short env payload rejected",
          decode_frame(build_frame(0x05, b"\x00" * 10)[:-1], st8) is None
          or st8.short_frames > 0)
    legacy_payload = LEGACY_ENV_STRUCT.pack(
        91_000, 99_850, 42_000, 21_34, 22_10, 5_560, 744, 1_375, 1_450, all_ok, 0
    )
    legacy_dec = FrameDecoder()
    legacy_dec.feed(b"\x00")
    legacy = list(legacy_dec.feed(build_frame(0x05, legacy_payload)))
    check("deployed legacy env payload still decodes",
          len(legacy) == 1 and isinstance(legacy[0], EnvFrame)
          and legacy[0].gas_ppm == 1450.0
          and legacy[0].gas_r0_ohms == 0.0 and legacy[0].gas_abc == 0,
          f"{len(legacy)} frame(s)")

    # Per-frame-type tallies back the dashboard's packet panel.  A single total
    # cannot distinguish a link carrying CSI at 100 Hz from one carrying only
    # status frames, so each type is counted separately -- and the sum of the
    # parts must equal the total or the panel silently loses frames.
    from wifisense.protocol import build_csi_frame, build_status_frame

    def _status(**kw):
        args = dict(uptime_ms=1000, csi_count=10, tx_count=0, dropped=0,
                    free_heap=180_000, rssi=-40, channel=6, wifi_state=7,
                    sample_rate=100, link_mode=1)
        args.update(kw)
        return build_status_frame(**args)

    iq = np.zeros(128, dtype=np.int8)
    tally = FrameDecoder()
    tally.feed(b"\x00")
    tally.feed(build_csi_frame(1_000, iq, seq=1))
    tally.feed(build_csi_frame(11_000, iq, seq=2))
    tally.feed(_status())
    tally.feed(build_env_frame(97_000, flags=all_ok, gas_ppm=650, gas_ratio=0.5))
    tally.feed(build_frame(0x04, b"hello"))
    ts = tally.stats
    check("frames are counted per type",
          (ts.csi_frames, ts.status_frames, ts.env_frames, ts.log_frames) == (2, 1, 1, 1),
          f"csi={ts.csi_frames} status={ts.status_frames} "
          f"env={ts.env_frames} log={ts.log_frames}")
    check("per-type counts sum to the total",
          ts.csi_frames + ts.status_frames + ts.env_frames + ts.log_frames == ts.frames_ok,
          f"{ts.csi_frames}+{ts.status_frames}+{ts.env_frames}+{ts.log_frames} "
          f"vs {ts.frames_ok}")
    check("per-type counts reach the UI payload",
          all(k in ts.as_dict() for k in
              ("csi_frames", "status_frames", "env_frames", "log_frames")))
    # A rejected frame must not be tallied as a delivered one of any type.
    bad = FrameDecoder()
    bad.feed(b"\x00")
    corrupt = bytearray(_status())
    corrupt[3] ^= 0xFF
    bad.feed(bytes(corrupt))
    check("a corrupted frame increments no type counter",
          bad.stats.status_frames == 0 and bad.stats.frames_ok == 0
          and (bad.stats.crc_errors + bad.stats.cobs_errors) > 0,
          f"ok={bad.stats.frames_ok} crc={bad.stats.crc_errors} "
          f"cobs={bad.stats.cobs_errors}")

    # Every sensor absent: the frame must still decode, and every derived
    # value must come back None rather than a confident zero.
    empty = list(FrameDecoder().feed(b"\x00" + build_env_frame(1, flags=0)))[0]
    check("absent sensors decode to None, not zero",
          empty.temperature_c is None and empty.dew_point_c() is None
          and empty.altitude_m() is None)

    pe2 = Pipeline(nominal_rate=RATE)
    check("no env panel before any env frame", pe2.snapshot()["env"] is None)
    pe2.handle(ef)
    esnap = pe2.snapshot()["env"]
    check("env snapshot present after one frame", esnap is not None)

    # AQI: the interpolation must be continuous at every breakpoint and
    # monotonic across the whole range, or the number jumps a category for a
    # one-ppm change in the reading.
    from wifisense.dsp.pipeline import AQI_BREAKPOINTS, air_quality_index

    check("AQI at 1450 ppm lands in the sensitive band",
          esnap["aqi"] == air_quality_index(1450.0)[0] and esnap["aqi_category"] == "sensitive",
          f"{esnap['gas_ppm']} ppm -> AQI {esnap['aqi']} '{esnap['aqi_label']}'")
    check("AQI anchors exactly on the category boundaries",
          all(air_quality_index(lo)[0] in (i_lo, i_lo - 1) and air_quality_index(hi)[0] == i_hi
              for lo, hi, i_lo, i_hi, _ in AQI_BREAKPOINTS),
          " ".join(f"{hi:.0f}->{air_quality_index(hi)[0]}" for _, hi, _, _, _ in AQI_BREAKPOINTS))
    seq = [air_quality_index(float(p))[0] for p in range(300, 11000, 17)]
    check("AQI is monotonic in ppm", all(b >= a for a, b in zip(seq, seq[1:])))
    check("AQI clamps to 0..500", seq[0] == 0 and seq[-1] == 500,
          f"{seq[0]}..{seq[-1]}")
    check("uncalibrated gas yields no AQI rather than 0",
          air_quality_index(None) == (None, None))

    # A reading below outdoor background is physically impossible indoors, so
    # it means the baseline drifted -- NOT that the air is pristine.  Painting
    # that green would be the one genuinely misleading state this panel has.
    stale_frame = list(FrameDecoder().feed(
        b"\x00" + build_env_frame(94_000, flags=all_ok, gas_ppm=305, gas_ratio=0.71)))[0]
    p_stale = Pipeline(nominal_rate=RATE)
    p_stale.handle(stale_frame)
    s_stale = p_stale.snapshot()["env"]
    check("sub-outdoor reading is flagged as a drifted baseline",
          s_stale["baseline_stale"] is True and s_stale["aqi"] == 0,
          f"{s_stale['gas_ppm']} ppm -> AQI {s_stale['aqi']}, stale={s_stale['baseline_stale']}")
    ok_frame = list(FrameDecoder().feed(
        b"\x00" + build_env_frame(95_000, flags=all_ok, gas_ppm=650, gas_ratio=0.5)))[0]
    p_ok = Pipeline(nominal_rate=RATE)
    p_ok.handle(ok_frame)
    check("an ordinary indoor reading is NOT flagged",
          p_ok.snapshot()["env"]["baseline_stale"] is False)
    p_warm = Pipeline(nominal_rate=RATE)
    p_warm.handle(list(FrameDecoder().feed(b"\x00" + build_env_frame(96_000, flags=0)))[0])
    check("an uncalibrated sensor is not called stale",
          p_warm.snapshot()["env"]["baseline_stale"] is False)
    check("env snapshot under 8 KB", len(json.dumps(esnap)) < 8192,
          f"{len(json.dumps(esnap))} B")
    # The node retransmits each reading ~3x against radio loss.  Repeats must
    # not triple the history or refresh the displayed age, or a sample whose
    # every copy was lost would still look fresh.
    pts = len(esnap["series"])
    pe2.handle(ef)
    pe2.handle(ef)
    dup = pe2.snapshot()["env"]
    check("retransmitted env frames do not duplicate history",
          len(dup["series"]) == pts and pe2.env_repeats == 2,
          f"{pts} -> {len(dup['series'])} pts, {pe2.env_repeats} repeats dropped")
    newer = list(FrameDecoder().feed(b"\x00" + build_env_frame(93_000, flags=all_ok,
                                                              bmp_temp_c=21.5)))[0]
    pe2.handle(newer)
    check("a genuinely new reading is still accepted",
          len(pe2.snapshot()["env"]["series"]) == pts + 1,
          f"{len(pe2.snapshot()['env']['series'])} pts")

    # reset() re-baselines the CSI detectors; it must not discard the
    # environment history, which has no baseline to re-establish.
    pe2.reset()
    check("reset keeps environment history", pe2.snapshot()["env"] is not None)

    # ------------------------------------------------- 9. archive / sleep
    # The archive is the only place data outlives a restart, and the sleep
    # report is derived entirely from it -- so a silent aggregation bug here
    # would not show up anywhere until someone read a night that was already
    # wrong.  Everything below runs against a throwaway database.
    print("\n9. ARCHIVE AND SLEEP REPORT")
    import tempfile
    from datetime import date as _d
    from datetime import datetime as _dt
    from datetime import timedelta as _td

    from wifisense.archive import AWAKENING_MIN_S, QUIET_DB, Archive

    adb = Path(tempfile.mkdtemp()) / "a.db"
    arc = Archive(adb, interval_s=1.0, env_interval_s=1.0, retain_days=0)
    check("archive opens", arc.open())

    def _obs(motion_db, vital_db, occupied, moving, act, bpm=None, valid=False,
             calib=False, env=None):
        arc.observe(
            {"motion_db": motion_db, "vital_db": vital_db, "occupied": occupied,
             "moving": moving, "activity": act, "calibrating": calib, "rssi": -44},
            {"bpm": bpm or 0.0, "confidence": 0.7 if valid else 0.0, "valid": valid},
            env,
        )

    # A calibrating detector reads 0 dB, which is indistinguishable from an empty
    # room; recording it would write a confident "nobody here" after every
    # restart.
    for _ in range(4):
        _obs(0.0, 0.0, False, False, "empty", calib=True)
    check("calibrating samples are not archived", arc.rows_written == 0,
          f"{arc.rows_written} rows")

    # Mean and peak must both survive, because either alone is misleading: the
    # mean hides a one-second spike, the peak calls one twitch a bad night.
    for i in range(4):
        _obs(9.0 if i == 2 else 1.0, 30.0, True, False, "still", bpm=14.2, valid=True)
        time.sleep(0.3)
    _obs(1.0, 30.0, True, False, "still", bpm=14.2, valid=True)
    time.sleep(1.05)
    _obs(2.0, 20.0, True, False, "still", bpm=0.0, valid=False)
    time.sleep(1.05)
    _obs(2.0, 20.0, True, False, "still", bpm=0.0, valid=False)
    with arc._lock:
        arc._flush_locked()
    got = [dict(r) for r in arc._query("SELECT * FROM sense ORDER BY ts")]
    spiked = [r for r in got if (r["motion_max_db"] or 0) >= 9.0]
    check("bucket keeps the peak and the mean apart",
          len(spiked) == 1 and spiked[0]["motion_db"] < 5.0,
          f"peak={spiked[0]['motion_max_db'] if spiked else None} "
          f"mean={spiked[0]['motion_db'] if spiked else None}")
    # An invalid estimate carries bpm 0.0; averaging that in would drag every
    # night's mean toward zero in proportion to how often the lock dropped.
    check("an invalid breathing estimate stores NULL, not 0",
          any(r["bpm"] is None for r in got)
          and all(r["bpm"] is None or r["bpm"] > 10.0 for r in got),
          str([r["bpm"] for r in got]))
    check("every row records how many samples backed it",
          all((r["samples"] or 0) >= 1 for r in got))

    # --- sleep report, against a synthetic night with known ground truth ----
    arc2 = Archive(Path(tempfile.mkdtemp()) / "b.db", interval_s=10.0, retain_days=0)
    arc2.open()
    truth_night = _d.today() - _td(days=3)
    bed = _dt(truth_night.year, truth_night.month, truth_night.day, 23, 0).timestamp()
    lights_out, up_at = bed, bed + 8 * 3600
    wakes = [(bed + 2 * 3600, 300), (bed + 5 * 3600, 60)]  # 5 min, then 60 s

    night_rows = []
    t = bed - 1800
    while t < up_at + 1800:
        in_bed = lights_out <= t < up_at
        up = any(w <= t < w + d for w, d in wakes)
        if not in_bed:
            row = (0.8, 1.5, 0.2, 0.0, 0.0, "empty", None, None)
        elif up:
            row = (12.0, 18.0, 8.0, 1.0, 1.0, "active", None, None)
        else:
            row = (1.0, 2.5, 30.0, 1.0, 0.0, "still", 14.0, 0.8)
        night_rows.append((int(t),) + row + (-45.0, 10))
        t += 10
    with arc2._lock:
        arc2._db.executemany(
            "INSERT OR REPLACE INTO sense (ts,motion_db,motion_max_db,vital_db,"
            "occupied,moving,activity,bpm,bpm_conf,rssi,samples)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?)", night_rows)
        arc2._db.commit()

    rep = arc2.sleep_report(truth_night)
    check("sleep period found", rep["found"] is True, rep.get("reason"))
    check("time in bed matches the truth", abs(rep["in_bed_minutes"] - 480.0) < 1.0,
          f"{rep['in_bed_minutes']} min vs 480")
    check("breathing rate matches the truth",
          abs((rep["breathing"]["mean_bpm"] or 0) - 14.0) < 0.2,
          f"{rep['breathing']['mean_bpm']} vs 14.0")
    # 5 min qualifies, 60 s is a turn-over and must not.
    check("only disturbances past the floor are counted as awakenings",
          rep["awakening_count"] == 1,
          f"{rep['awakening_count']} of 2 disturbances (floor {AWAKENING_MIN_S}s)")
    check("restless time counts both disturbances", abs(rep["restless_minutes"] - 6.0) < 0.5,
          f"{rep['restless_minutes']} min vs 6")
    check("stillness is a fraction of time in bed",
          0.97 < rep["stillness"] <= 1.0, f"{rep['stillness']}")
    check("the night is named by the evening it began",
          truth_night.isoformat() in arc2.nights(), str(arc2.nights()))
    check("timeline is drawable and bounded",
          10 < len(rep["timeline"]) < 200 and all("still" in p for p in rep["timeline"]),
          f"{len(rep['timeline'])} points")
    check("sleep report JSON stays small", len(json.dumps(rep)) < 32768,
          f"{len(json.dumps(rep))} B")

    # A brief absence must not split one night; a long one must.
    def _night_with_gap(gap_s):
        a = Archive(Path(tempfile.mkdtemp()) / f"g{gap_s}.db", interval_s=10.0,
                    retain_days=0)
        a.open()
        rows, t2 = [], bed
        while t2 < bed + 8 * 3600:
            away = bed + 3 * 3600 <= t2 < bed + 3 * 3600 + gap_s
            r = ((0.7, 1.2, 0.1, 0.0, 0.0, "empty", None, None) if away
                 else (1.0, 2.5, 30.0, 1.0, 0.0, "still", 14.0, 0.8))
            rows.append((int(t2),) + r + (-45.0, 10))
            t2 += 10
        with a._lock:
            a._db.executemany(
                "INSERT OR REPLACE INTO sense (ts,motion_db,motion_max_db,vital_db,"
                "occupied,moving,activity,bpm,bpm_conf,rssi,samples)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?)", rows)
            a._db.commit()
        return a.sleep_report(truth_night)

    short_gap = _night_with_gap(300)
    long_gap = _night_with_gap(70 * 60)
    check("a brief absence does not split the night",
          short_gap["in_bed_minutes"] > 470, f"{short_gap['in_bed_minutes']} min")
    # An 8 h night broken by a 70 min absence 3 h in leaves segments of 180 and
    # 230 min.  The report must pick the longer one, and must NOT report the
    # whole 480 as one unbroken night.
    check("a long absence does split the night",
          abs(long_gap["in_bed_minutes"] - 230.0) < 1.0,
          f"{long_gap['in_bed_minutes']} min, expected the longer 230 min segment")

    # Honest refusals rather than an invented night.
    empty_arc = Archive(Path(tempfile.mkdtemp()) / "e.db", interval_s=10.0, retain_days=0)
    empty_arc.open()
    check("a night with no data is reported as such, not as zero sleep",
          empty_arc.sleep_report(truth_night)["found"] is False)
    check("no sleep stages are claimed anywhere in the report",
          not any(k in json.dumps(rep).lower() for k in ("rem", "deep sleep", "light sleep")))

    # Retention has to actually delete, or an appliance running for years fills
    # its own disk.  A dedicated archive, because arc2's night is itself three
    # days old and a 1-day horizon would legitimately sweep all of it.
    ret = Archive(Path(tempfile.mkdtemp()) / "r.db", interval_s=10.0, retain_days=1)
    ret.open()
    now_ts = int(time.time())
    with ret._lock:
        ret._db.executemany(
            "INSERT OR REPLACE INTO sense (ts,motion_db,samples) VALUES (?,?,?)",
            [(now_ts - 60, 1.0, 1), (now_ts - 3600, 1.0, 1),
             (now_ts - 9 * 86400, 1.0, 1)])
        ret._db.commit()
    pre = ret.span()["rows"]
    with ret._lock:
        ret._prune_locked(time.time())
    post = ret.span()["rows"]
    check("retention prunes only rows past the horizon",
          pre == 3 and post == 2, f"{pre} -> {post}, expected 3 -> 2")
    ret.close()

    check("no archive write errors throughout", arc.errors == 0 and arc2.errors == 0,
          f"{arc.errors}/{arc2.errors}")
    arc.close()
    arc2.close()

    elapsed = time.monotonic() - t_start
    print(f"\n{'='*68}")
    if failures:
        print(f"  {RED}{len(failures)} CHECK(S) FAILED{RST}  ({elapsed:.1f}s)")
        for f in failures:
            print(f"    - {f}")
        print("=" * 68 + "\n")
        return 1
    print(f"  {GREEN}ALL CHECKS PASSED{RST}  ({elapsed:.1f}s)")
    print("=" * 68 + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
