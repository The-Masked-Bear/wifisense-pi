"""Long-term on-disk store, and the overnight sleep report built on it.

Everything else in this system is live-only.  ``Pipeline`` keeps a 60 s motion
trace and half an hour of environment readings in memory, and a restart throws
both away.  That is the right choice for an instrument display, but it means the
one thing this hardware is uniquely good at -- watching a motionless person
breathe, through a wall, with nothing worn -- can never be looked at after the
fact.  This module is where that data accumulates.

WHAT IS STORED, AND AT WHAT RATE

CSI arrives at ~100 Hz.  Storing it, or even the per-frame detector output,
would be absurd: 8.6 million rows a day to describe something that changes over
minutes.  So each row is an *aggregate* over ``interval_s`` (10 s by default),
which is still far finer than anything being reported.

Aggregates keep both the mean and the peak of ``motion_db``, and that pairing is
the point.  A 10 s mean hides a one-second movement spike completely -- exactly
the event that separates "asleep" from "turned over" -- while the peak alone
would call a single twitch a restless night.  Both are cheap; neither is
sufficient alone.

Environment is sampled far more slowly again (60 s), because a room's
temperature and CO2 do not move faster than that and the sensor itself only
reports every 3 s.

FAILURE POLICY

A write failure here must never stop the node sensing.  A full disk, a
read-only filesystem or a corrupt database degrades this feature to nothing and
leaves detection untouched -- the same principle as ``Config.load()`` returning
defaults rather than raising on a corrupt file.  Errors are counted and
surfaced, not raised.
"""

from __future__ import annotations

import sqlite3
import threading
import time
from dataclasses import dataclass, field
from datetime import date as _date
from datetime import datetime, timedelta
from pathlib import Path

# Gross motion below this counts as "still" for the purposes of the sleep
# report.  Not arbitrary: PROJECT.txt section 6c establishes that respiration
# only resolves with motion under roughly +5 dB, and an empty room sits at
# 0-1 dB by construction, so this is the same threshold the breathing detector
# is implicitly working to.
QUIET_DB = 5.0

# A person leaving the room briefly -- the bathroom, a glass of water -- must not
# split one night into two.  20 minutes is long enough to cover that and short
# enough that an evening on the sofa does not merge into the night in bed.
SLEEP_GAP_TOLERANCE_S = 20 * 60

# Movement shorter than this is a turn-over, not an awakening.  Without a floor
# here every shift of position inflates the count into meaninglessness.
AWAKENING_MIN_S = 120

# A sleep period must be at least this long to be reported at all, so an hour
# on the sofa is not presented as a night's sleep.
MIN_SLEEP_PERIOD_S = 45 * 60

SCHEMA = """
CREATE TABLE IF NOT EXISTS sense (
    ts            INTEGER PRIMARY KEY,  -- unix seconds, start of the bucket
    motion_db     REAL,                 -- mean over the bucket
    motion_max_db REAL,                 -- peak over the bucket
    vital_db      REAL,
    occupied      REAL,                 -- fraction of the bucket, 0..1
    moving        REAL,
    activity      TEXT,                 -- most common label in the bucket
    bpm           REAL,                 -- mean of *valid* estimates, else NULL
    bpm_conf      REAL,
    rssi          REAL,
    samples       INTEGER               -- how many observations backed this row
);

CREATE TABLE IF NOT EXISTS env (
    ts             INTEGER PRIMARY KEY,
    temp_c         REAL,
    humidity       REAL,
    pressure_hpa   REAL,
    gas_ppm        REAL,
    aqi            INTEGER,
    baseline_stale INTEGER
);
"""


@dataclass
class _Bucket:
    """Accumulator for one interval.  Reset in place rather than reallocated."""

    start: float = 0.0
    n: int = 0
    motion_sum: float = 0.0
    motion_max: float = -1e9
    vital_sum: float = 0.0
    occupied_n: int = 0
    moving_n: int = 0
    rssi_sum: float = 0.0
    bpm_sum: float = 0.0
    bpm_n: int = 0
    conf_sum: float = 0.0
    activities: dict = field(default_factory=dict)

    def reset(self, start: float) -> None:
        self.start = start
        self.n = 0
        self.motion_sum = 0.0
        self.motion_max = -1e9
        self.vital_sum = 0.0
        self.occupied_n = 0
        self.moving_n = 0
        self.rssi_sum = 0.0
        self.bpm_sum = 0.0
        self.bpm_n = 0
        self.conf_sum = 0.0
        self.activities.clear()


class Archive:
    """Buckets detector output to SQLite and answers history queries."""

    def __init__(
        self,
        path: str | Path,
        *,
        interval_s: float = 10.0,
        env_interval_s: float = 60.0,
        retain_days: int = 60,
    ) -> None:
        self.path = Path(path)
        self.interval_s = max(1.0, float(interval_s))
        self.env_interval_s = max(self.interval_s, float(env_interval_s))
        self.retain_days = max(0, int(retain_days))

        self.rows_written = 0
        self.env_rows_written = 0
        self.errors = 0
        self.last_error: str | None = None

        self._db: sqlite3.Connection | None = None
        self._lock = threading.Lock()
        self._bucket = _Bucket()
        self._bucket_open = False
        self._last_env_write = 0.0
        self._last_prune = 0.0

    # ------------------------------------------------------------- lifecycle

    def open(self) -> bool:
        """Create the database if needed.  False if it could not be opened."""
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            # check_same_thread=False because the sampler runs on the pump
            # thread while queries arrive from the event loop's worker threads;
            # every access is serialised through self._lock instead.
            db = sqlite3.connect(str(self.path), check_same_thread=False)
            # WAL so a history query cannot block the sampler, and NORMAL sync
            # because losing the last few seconds of a 10 s aggregate to a power
            # cut is not worth an fsync per row on an SD card.
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("PRAGMA synchronous=NORMAL")
            db.executescript(SCHEMA)
            db.commit()
            # Set once here rather than per query: the writer shares this
            # connection and mutating its state under the lock on every read is
            # needless churn.
            db.row_factory = sqlite3.Row
            self._db = db
            return True
        except (sqlite3.Error, OSError) as exc:
            self._fail(exc)
            return False

    def close(self) -> None:
        with self._lock:
            if self._bucket_open and self._db is not None:
                try:
                    self._flush_locked()
                except (sqlite3.Error, OSError) as exc:
                    self._fail(exc)
            if self._db is not None:
                try:
                    self._db.commit()
                    self._db.close()
                except sqlite3.Error:
                    pass
                self._db = None

    def _fail(self, exc: Exception) -> None:
        self.errors += 1
        self.last_error = f"{type(exc).__name__}: {exc}"

    # ---------------------------------------------------------------- writing

    def observe(self, motion: dict, breathing: dict, env: dict | None = None) -> None:
        """Fold one detector reading into the current bucket.

        Safe to call at any rate; the bucket closes on wall-clock time, not on a
        sample count, so an irregular link produces correctly-spaced rows with
        an honest ``samples`` count rather than stretched buckets.
        """
        if self._db is None:
            return
        # A calibrating detector reports 0 dB, which is indistinguishable from an
        # empty room.  Recording it would write a confident "nobody here" for
        # the first 30 s of every restart.
        if motion.get("calibrating"):
            return

        now = time.time()
        with self._lock:
            if not self._bucket_open:
                self._bucket.reset(now - (now % self.interval_s))
                self._bucket_open = True
            elif now - self._bucket.start >= self.interval_s:
                try:
                    self._flush_locked()
                except (sqlite3.Error, OSError) as exc:
                    self._fail(exc)
                self._bucket.reset(now - (now % self.interval_s))

            b = self._bucket
            b.n += 1
            m = float(motion.get("motion_db") or 0.0)
            b.motion_sum += m
            if m > b.motion_max:
                b.motion_max = m
            b.vital_sum += float(motion.get("vital_db") or 0.0)
            if motion.get("occupied"):
                b.occupied_n += 1
            if motion.get("moving"):
                b.moving_n += 1
            b.rssi_sum += float(motion.get("rssi") or 0.0)
            act = motion.get("activity") or "empty"
            b.activities[act] = b.activities.get(act, 0) + 1
            # Only *valid* estimates are averaged.  An invalid one carries a bpm
            # of 0.0, and folding that in would drag every night's mean toward
            # zero in proportion to how often the lock dropped.
            if breathing.get("valid"):
                b.bpm_sum += float(breathing.get("bpm") or 0.0)
                b.bpm_n += 1
                b.conf_sum += float(breathing.get("confidence") or 0.0)

            if env and now - self._last_env_write >= self.env_interval_s:
                try:
                    self._write_env_locked(now, env)
                    self._last_env_write = now
                except (sqlite3.Error, OSError) as exc:
                    self._fail(exc)

            if self.retain_days and now - self._last_prune >= 3600.0:
                self._last_prune = now
                try:
                    self._prune_locked(now)
                except (sqlite3.Error, OSError) as exc:
                    self._fail(exc)

    def _flush_locked(self) -> None:
        b = self._bucket
        if self._db is None or b.n == 0:
            return
        activity = max(b.activities.items(), key=lambda kv: kv[1])[0] if b.activities else None
        self._db.execute(
            "INSERT OR REPLACE INTO sense (ts, motion_db, motion_max_db, vital_db,"
            " occupied, moving, activity, bpm, bpm_conf, rssi, samples)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (
                int(b.start),
                round(b.motion_sum / b.n, 2),
                round(b.motion_max, 2),
                round(b.vital_sum / b.n, 2),
                round(b.occupied_n / b.n, 3),
                round(b.moving_n / b.n, 3),
                activity,
                round(b.bpm_sum / b.bpm_n, 2) if b.bpm_n else None,
                round(b.conf_sum / b.bpm_n, 3) if b.bpm_n else None,
                round(b.rssi_sum / b.n, 1),
                b.n,
            ),
        )
        self._db.commit()
        self.rows_written += 1

    def _write_env_locked(self, now: float, env: dict) -> None:
        if self._db is None:
            return
        self._db.execute(
            "INSERT OR REPLACE INTO env (ts, temp_c, humidity, pressure_hpa,"
            " gas_ppm, aqi, baseline_stale) VALUES (?,?,?,?,?,?,?)",
            (
                int(now - (now % self.env_interval_s)),
                env.get("temp_c"),
                env.get("humidity"),
                env.get("pressure_hpa"),
                env.get("gas_ppm"),
                env.get("aqi"),
                1 if env.get("baseline_stale") else 0,
            ),
        )
        self._db.commit()
        self.env_rows_written += 1

    def _prune_locked(self, now: float) -> None:
        if self._db is None:
            return
        cutoff = int(now - self.retain_days * 86400)
        self._db.execute("DELETE FROM sense WHERE ts < ?", (cutoff,))
        self._db.execute("DELETE FROM env WHERE ts < ?", (cutoff,))
        self._db.commit()

    # ---------------------------------------------------------------- reading

    def _query(self, sql: str, args: tuple = ()) -> list[sqlite3.Row]:
        if self._db is None:
            return []
        with self._lock:
            try:
                return self._db.execute(sql, args).fetchall()
            except sqlite3.Error as exc:
                self._fail(exc)
                return []

    def span(self) -> dict:
        """Oldest and newest data held, and how much of it."""
        rows = self._query(
            "SELECT MIN(ts) AS lo, MAX(ts) AS hi, COUNT(*) AS n FROM sense"
        )
        if not rows or rows[0]["n"] == 0:
            return {"rows": 0, "first": None, "last": None, "days": 0.0}
        lo, hi, n = rows[0]["lo"], rows[0]["hi"], rows[0]["n"]
        return {
            "rows": n,
            "first": lo,
            "last": hi,
            "days": round((hi - lo) / 86400.0, 2),
        }

    def series(self, start: float, end: float, max_points: int = 720) -> dict:
        """Sense and environment rows over a window, decimated for display.

        Decimation is by bucketed averaging rather than by taking every Nth row,
        because a whole day at 10 s is 8,640 points against a canvas ~900 px
        wide -- picking every 12th row would drop 11 out of every 12 movement
        peaks, which are the entire signal in a sleep trace.  ``motion_max_db``
        is aggregated with MAX for the same reason, while means are averaged.
        """
        start, end = int(start), int(end)
        span = max(1, end - start)
        step = max(int(self.interval_s), int(span / max(1, max_points)))
        sense = self._query(
            "SELECT (ts / ?) * ? AS t,"
            " AVG(motion_db) AS motion_db, MAX(motion_max_db) AS motion_max_db,"
            " AVG(vital_db) AS vital_db, AVG(occupied) AS occupied,"
            " AVG(moving) AS moving, AVG(bpm) AS bpm, AVG(bpm_conf) AS bpm_conf,"
            " AVG(rssi) AS rssi, SUM(samples) AS samples"
            " FROM sense WHERE ts >= ? AND ts < ?"
            " GROUP BY t ORDER BY t",
            (step, step, start, end),
        )
        env_step = max(int(self.env_interval_s), step)
        env = self._query(
            "SELECT (ts / ?) * ? AS t, AVG(temp_c) AS temp_c, AVG(humidity) AS humidity,"
            " AVG(pressure_hpa) AS pressure_hpa, AVG(gas_ppm) AS gas_ppm,"
            " AVG(aqi) AS aqi, MAX(baseline_stale) AS baseline_stale"
            " FROM env WHERE ts >= ? AND ts < ? GROUP BY t ORDER BY t",
            (env_step, env_step, start, end),
        )

        def r(v, d=2):
            return None if v is None else round(float(v), d)

        return {
            "start": start,
            "end": end,
            "step": step,
            "sense": [
                {
                    "t": int(x["t"]),
                    "motion_db": r(x["motion_db"]),
                    "motion_max_db": r(x["motion_max_db"]),
                    "vital_db": r(x["vital_db"]),
                    "occupied": r(x["occupied"], 3),
                    "moving": r(x["moving"], 3),
                    "bpm": r(x["bpm"], 1),
                    "bpm_conf": r(x["bpm_conf"], 3),
                }
                for x in sense
            ],
            "env": [
                {
                    "t": int(x["t"]),
                    "temp_c": r(x["temp_c"]),
                    "humidity": r(x["humidity"], 1),
                    "pressure_hpa": r(x["pressure_hpa"]),
                    "gas_ppm": r(x["gas_ppm"], 0),
                    "aqi": r(x["aqi"], 0),
                    "baseline_stale": bool(x["baseline_stale"]),
                }
                for x in env
            ],
        }

    def stats(self) -> dict:
        return {
            "enabled": self._db is not None,
            "path": str(self.path),
            "interval_s": self.interval_s,
            "retain_days": self.retain_days,
            "rows_written": self.rows_written,
            "env_rows_written": self.env_rows_written,
            "errors": self.errors,
            "error": self.last_error,
            "span": self.span(),
        }

    # ----------------------------------------------------------- sleep report

    def nights(self, limit: int = 30, *, window_start_h: int = 21,
               window_end_h: int = 11) -> list[str]:
        """Dates that hold occupied night-time data, named by the evening.

        A night spans two calendar dates, so the local date alone attributes
        everything after midnight to the wrong one -- 02:00 belongs to the
        evening before.  Shifting back by ``window_start_h`` folds both halves
        onto the evening's date, and the hour filter keeps daytime occupancy
        from listing a night that does not exist.
        """
        rows = self._query(
            "SELECT DISTINCT date(ts, 'unixepoch', 'localtime',"
            "                    '-' || ? || ' hours') AS d"
            " FROM sense"
            " WHERE occupied >= 0.5"
            "   AND (CAST(strftime('%H', ts, 'unixepoch', 'localtime') AS INTEGER) >= ?"
            "        OR CAST(strftime('%H', ts, 'unixepoch', 'localtime') AS INTEGER) < ?)"
            " ORDER BY d DESC LIMIT ?",
            (int(window_start_h), int(window_start_h), int(window_end_h), int(limit)),
        )
        return [x["d"] for x in rows]

    def sleep_report(
        self, night: str | _date, *, window_start_h: int = 21, window_end_h: int = 11
    ) -> dict:
        """Analyse one night, named by the evening it began.

        "night of 2026-08-13" spans 21:00 on the 13th to 11:00 on the 14th, so
        asking on a morning for yesterday's date gives last night.

        THIS IS NOT POLYSOMNOGRAPHY.  It derives everything from gross movement
        and respiration through one radio path, so it can report time in bed,
        stillness and breathing rate honestly -- and cannot report sleep stages
        at all.  REM and deep sleep are distinguished by brain and eye activity,
        which is not present in this signal at any level, so no stage breakdown
        is offered rather than one being invented.
        """
        d = _date.fromisoformat(night) if isinstance(night, str) else night
        begin = datetime(d.year, d.month, d.day, window_start_h)
        finish = datetime(d.year, d.month, d.day) + timedelta(days=1)
        finish = finish.replace(hour=window_end_h)
        start_ts, end_ts = begin.timestamp(), finish.timestamp()

        rows = self._query(
            "SELECT ts, motion_db, motion_max_db, vital_db, occupied, bpm, bpm_conf"
            " FROM sense WHERE ts >= ? AND ts < ? ORDER BY ts",
            (int(start_ts), int(end_ts)),
        )

        base = {
            "night": d.isoformat(),
            "window": [int(start_ts), int(end_ts)],
            "found": False,
            "reason": "no data recorded for this night",
            "buckets": len(rows),
        }
        if not rows:
            return base

        period = self._longest_occupied(rows)
        if period is None:
            return dict(base, reason="room was never occupied during this window")
        in_bed_start, in_bed_end = period
        if in_bed_end - in_bed_start < MIN_SLEEP_PERIOD_S:
            return dict(
                base,
                reason=f"longest occupied period was only "
                f"{int((in_bed_end - in_bed_start) / 60)} min",
            )

        inside = [r for r in rows if in_bed_start <= r["ts"] < in_bed_end]
        iv = self.interval_s

        quiet_s = restless_s = 0.0
        bpms: list[float] = []
        awakenings: list[dict] = []
        run_start: float | None = None

        for r in inside:
            occupied = (r["occupied"] or 0.0) >= 0.5
            peak = r["motion_max_db"]
            disturbed = occupied and peak is not None and peak >= QUIET_DB
            if disturbed:
                restless_s += iv
                if run_start is None:
                    run_start = r["ts"]
            else:
                if occupied:
                    quiet_s += iv
                    if r["bpm"] is not None:
                        bpms.append(float(r["bpm"]))
                if run_start is not None:
                    if r["ts"] - run_start >= AWAKENING_MIN_S:
                        awakenings.append(
                            {"at": int(run_start), "seconds": int(r["ts"] - run_start)}
                        )
                    run_start = None
        if run_start is not None and in_bed_end - run_start >= AWAKENING_MIN_S:
            awakenings.append(
                {"at": int(run_start), "seconds": int(in_bed_end - run_start)}
            )

        in_bed_s = in_bed_end - in_bed_start
        # A timeline coarse enough to draw and fine enough to see a disturbance:
        # one point per 5 minutes across the night.
        timeline = self._timeline(inside, in_bed_start, in_bed_end, 300)

        return {
            "night": d.isoformat(),
            "window": [int(start_ts), int(end_ts)],
            "found": True,
            "reason": "ok",
            "buckets": len(rows),
            "in_bed_start": int(in_bed_start),
            "in_bed_end": int(in_bed_end),
            "in_bed_minutes": round(in_bed_s / 60.0, 1),
            "still_minutes": round(quiet_s / 60.0, 1),
            "restless_minutes": round(restless_s / 60.0, 1),
            # Fraction of time in bed spent still.  Deliberately called
            # stillness rather than "sleep efficiency": the instrument measures
            # stillness, and whether that stillness was sleep is an inference
            # this hardware cannot make.
            "stillness": round(quiet_s / in_bed_s, 3) if in_bed_s else None,
            "awakenings": awakenings,
            "awakening_count": len(awakenings),
            "breathing": {
                "samples": len(bpms),
                "mean_bpm": round(sum(bpms) / len(bpms), 1) if bpms else None,
                "min_bpm": round(min(bpms), 1) if bpms else None,
                "max_bpm": round(max(bpms), 1) if bpms else None,
                "coverage": round(len(bpms) * iv / quiet_s, 3) if quiet_s else None,
            },
            "timeline": timeline,
        }

    def _longest_occupied(self, rows) -> tuple[float, float] | None:
        """Longest occupied stretch, tolerating brief absences.

        Merging across short gaps is what stops a trip to the bathroom being
        reported as the end of the night.
        """
        iv = self.interval_s
        segments: list[list[float]] = []
        for r in rows:
            if (r["occupied"] or 0.0) < 0.5:
                continue
            ts = float(r["ts"])
            if segments and ts - segments[-1][1] <= SLEEP_GAP_TOLERANCE_S:
                segments[-1][1] = ts + iv
            else:
                segments.append([ts, ts + iv])
        if not segments:
            return None
        best = max(segments, key=lambda s: s[1] - s[0])
        return best[0], best[1]

    def _timeline(self, rows, start: float, end: float, step: float) -> list[dict]:
        out: list[dict] = []
        if not rows:
            return out
        edge = start + step
        peak = -1e9
        vital_sum = motion_sum = 0.0
        n = 0
        bpm_sum = 0.0
        bpm_n = 0

        def emit(at: float):
            if n:
                out.append(
                    {
                        "t": int(at - step),
                        "motion_db": round(motion_sum / n, 2),
                        "motion_max_db": round(peak, 2),
                        "vital_db": round(vital_sum / n, 2),
                        "bpm": round(bpm_sum / bpm_n, 1) if bpm_n else None,
                        "still": bool(peak < QUIET_DB),
                    }
                )

        for r in rows:
            while r["ts"] >= edge:
                emit(edge)
                edge += step
                peak, vital_sum, motion_sum, n, bpm_sum, bpm_n = -1e9, 0.0, 0.0, 0, 0.0, 0
            n += 1
            motion_sum += float(r["motion_db"] or 0.0)
            vital_sum += float(r["vital_db"] or 0.0)
            p = r["motion_max_db"]
            if p is not None and float(p) > peak:
                peak = float(p)
            if r["bpm"] is not None:
                bpm_sum += float(r["bpm"])
                bpm_n += 1
        emit(edge)
        return out
