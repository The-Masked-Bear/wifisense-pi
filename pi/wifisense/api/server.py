"""FastAPI application: ingest thread, WebSocket fan-out, static UI, REST control."""

from __future__ import annotations

import asyncio
import json
import threading
import traceback
import time
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from ..archive import Archive
from ..config import Config
from ..dsp.pipeline import Pipeline
from ..link.base import Link
from ..protocol import StatusFrame, build_command
from ..recorder import Recorder, session_info
from ..spectrum import SubGHzScanner
from ..stimulus import Stimulator

WEB_DIR = Path(__file__).resolve().parent.parent.parent / "web"


def _json_default(obj):
    """Coerce numpy scalars and arrays that slip through into JSON types.

    Every value in a snapshot is meant to be cast to a Python type at its
    source, but a single missed cast anywhere in the DSP would otherwise raise
    inside json.dumps and tear down the WebSocket -- turning a cosmetic slip
    into a total loss of the live display.  This makes that failure mode
    impossible rather than merely unlikely.
    """
    if isinstance(obj, np.generic):
        return obj.item()
    if isinstance(obj, np.ndarray):
        return obj.tolist()
    raise TypeError(f"{type(obj).__name__} is not JSON serializable")


class NumpyJSONResponse(JSONResponse):
    """JSONResponse that tolerates numpy scalars, as above."""

    def render(self, content) -> bytes:
        return json.dumps(content, default=_json_default, allow_nan=False).encode("utf-8")


class SensorService:
    """Owns the link, the pipeline, the stimulus and the recorder.

    Frames are pumped on a dedicated thread rather than in the event loop: the
    DSP is CPU-bound numpy work, and running it in the loop would stall every
    WebSocket write behind an SVD.
    """

    def __init__(self, config: Config) -> None:
        self.config = config
        self.pipeline = Pipeline(
            nominal_rate=config.nominal_rate_hz,
            history_seconds=config.history_seconds,
            breathing_window=config.breathing_window_s,
            trim_guard=config.trim_guard_band,
            sea_level_hpa=getattr(config, "sea_level_hpa", 1013.25),
        )
        self.link: Link | None = None
        self.stimulus = Stimulator(
            target=config.stimulus_target or None, rate_hz=config.stimulus_rate_hz
        )
        self.recorder: Recorder | None = None
        self._pump: threading.Thread | None = None
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self.started_at = time.monotonic()
        # Radio-link watchdog.  A radio that initialises but delivers nothing
        # leaves the Pi blind with no indication of why, and in radio mode the
        # serial port is not even open to ask the node what is wrong.  Falling
        # back to the cable keeps the system sensing and restores the ability
        # to diagnose it.
        # Sub-GHz monitor.  Shares the CC1101 with the (optional) CSI
        # transport, so it must stand down if that radio is claimed.
        self.scanner: SubGHzScanner | None = None
        if getattr(config, "spectrum_enabled", True):
            self.scanner = SubGHzScanner(
                bus=config.cc1101_spi_bus,
                device=config.cc1101_spi_device,
                start_mhz=getattr(config, "spectrum_start_mhz", 430.0),
                stop_mhz=getattr(config, "spectrum_stop_mhz", 435.0),
                steps=getattr(config, "spectrum_steps", 32),
            )

        # Long-term store.  Independent of the recorder: that captures raw
        # frames for replay and is switched on deliberately, whereas this keeps
        # low-rate aggregates running continuously so a night can be looked at
        # tomorrow.
        self.archive: Archive | None = None
        if getattr(config, "archive_enabled", True):
            path = Path(config.archive_db)
            if not path.is_absolute():
                path = Path(__file__).resolve().parent.parent.parent.parent / path
            self.archive = Archive(
                path,
                interval_s=config.archive_interval_s,
                env_interval_s=config.archive_env_interval_s,
                retain_days=config.archive_retain_days,
            )
        # 1 Hz, or four samples per bucket if the bucket is shorter than 4 s, so
        # a mean is always backed by more than a single reading.
        self._sample_every = min(1.0, config.archive_interval_s / 4.0)
        self._last_sample = 0.0

        self.fallback_after = 60.0
        self.fell_back_from: str | None = None
        self._link_started = 0.0

    # ------------------------------------------------------------- link setup

    def _link_key(self):
        """The shared radio key, or None when encryption is switched off."""
        if not getattr(self.config, "radio_encrypt", True):
            return None
        key = (getattr(self.config, "radio_key", "") or "").strip()
        return key or None

    def _build_link(self) -> Link:
        kind = self.config.link
        if kind == "synthetic":
            from ..link.synthetic import SyntheticLink

            return SyntheticLink(rate_hz=self.config.nominal_rate_hz, auto_cycle=45.0)
        if kind == "replay":
            from ..link.replay import ReplayLink

            return ReplayLink(self.config.replay_file)
        if kind == "cc1101":
            from ..link.cc1101_link import CC1101Link

            return CC1101Link(
                bus=self.config.cc1101_spi_bus,
                device=self.config.cc1101_spi_device,
                frequency_mhz=self.config.cc1101_frequency_mhz,
                key=self._link_key(),
            )
        if kind == "nrf24":
            from ..link.nrf24_link import NRF24Link

            return NRF24Link(
                bus=self.config.nrf24_spi_bus,
                device=self.config.nrf24_spi_device,
                ce_pin=self.config.nrf24_ce_pin,
                channel=self.config.nrf24_channel,
                key=self._link_key(),
            )

        from ..link.serial_link import SerialLink

        return SerialLink(self.config.serial_port, self.config.serial_baud)

    def start(self) -> None:
        self.link = self._build_link()
        self.link.start()
        self._link_started = time.monotonic()
        if self.config.stimulus_enabled:
            self.stimulus.start()
        if self.config.record_on_start:
            self.start_recording()
        if self.scanner is not None:
            # The CC1101 cannot both carry CSI and sweep the band.
            if self.config.link == "cc1101":
                self.scanner.pause()
            self.scanner.start()
        if self.archive is not None and not self.archive.open():
            # A store that cannot be opened is reported and then ignored.  It is
            # a convenience feature; refusing to sense because a night's history
            # cannot be written would be the wrong trade entirely.
            self.pipeline.logs.append(f"archive disabled: {self.archive.last_error}")
            self.archive = None
        self._stop.clear()
        self._pump = threading.Thread(target=self._run_pump, name="pump", daemon=True)
        self._pump.start()

    def stop(self) -> None:
        self._stop.set()
        if self._pump:
            self._pump.join(timeout=3.0)
        if self.link:
            self.link.stop()
        if self.scanner is not None:
            self.scanner.stop()
        self.stimulus.stop()
        self.stop_recording()
        # Last, and after the pump has stopped: this flushes the open bucket, so
        # a clean shutdown does not discard the final interval.
        if self.archive is not None:
            self.archive.close()

    # ------------------------------------------------------------------ pump

    def _run_pump(self) -> None:
        targets: list[str] = []
        while not self._stop.is_set():
            link = self.link
            if link is None:
                self._stop.wait(0.1)
                continue

            frames = link.drain(512)
            if not frames:
                self._check_radio_watchdog()
                # Still sample while starved: an empty room delivering nothing
                # new is itself the observation, and skipping it here would
                # leave gaps in the archive exactly when the room is quietest.
                self._sample_archive()
                # Nothing to do; yielding here keeps a starved link from
                # spinning a core at 100%.
                self._stop.wait(0.005)
                continue

            # One acquisition per batch, not per frame.  At 100 Hz the
            # per-frame version handed the lock back and forth with the
            # snapshot thread thousands of times a second, and under load
            # (a browser starting on the same Pi) that starved the pump badly
            # enough to show multi-second 'stale' readings in the UI.
            with self._lock:
                for frame in frames:
                    self.pipeline.handle(frame)
                    if isinstance(frame, StatusFrame) and frame.ip:
                        targets.append(frame.ip_str)
            # Aim the stimulus at whatever address the node reports, so a DHCP
            # change is followed with no intervention.  Done outside the lock:
            # it touches only the stimulator.
            if targets and not self.config.stimulus_target:
                self.stimulus.set_target(targets[-1])
            targets.clear()
            self._sample_archive()

    def _sample_archive(self) -> None:
        """Feed one observation to the archive, at most once per _sample_every.

        Sampled on wall-clock rather than per frame: at 100 Hz a per-frame call
        would hand 100 observations a second to a store that aggregates over 10,
        and motion_db is itself a 6 s windowed statistic that the detector only
        re-evaluates at ~5 Hz -- so more often than 1 Hz cannot carry new
        information, and a brief movement still lifts that window enough to be
        caught by the bucket's peak.
        """
        arc = self.archive
        if arc is None:
            return
        now = time.monotonic()
        if now - self._last_sample < self._sample_every:
            return
        self._last_sample = now
        with self._lock:
            motion, breathing, env = self.pipeline.observation()
        arc.observe(motion, breathing, env)

    def _check_radio_watchdog(self) -> None:
        """Swap a silent radio link for the serial cable.

        Only fires when the link has delivered *nothing at all* since it
        started -- a working link that merely stalls is left alone, because a
        transport switch mid-session is far more disruptive than a gap.
        """
        if self.config.link not in ("nrf24", "cc1101") or self.fell_back_from:
            return
        if self.pipeline.stats.frames_in > 0:
            return
        if time.monotonic() - self._link_started < self.fallback_after:
            return

        dead = self.config.link
        self.fell_back_from = dead
        try:
            if self.link:
                self.link.stop()
            from ..link.serial_link import SerialLink

            self.link = SerialLink(self.config.serial_port, self.config.serial_baud)
            self.link.start()
            self._link_started = time.monotonic()
            # Tell the node to come back over the cable too, or it keeps
            # transmitting into a radio nobody is listening to.
            time.sleep(3.0)
            self.send_node_command("link usb")
            if dead == "cc1101" and self.scanner is not None:
                self.scanner.resume()
            self.pipeline.logs.append(
                f"{dead} delivered nothing for {self.fallback_after:.0f}s; fell back to serial"
            )
        except Exception as exc:  # noqa: BLE001
            self.pipeline.logs.append(f"fallback to serial failed: {exc}")

    # -------------------------------------------------------------- recording

    def _record_raw(self, raw: bytes) -> None:
        rec = self.recorder
        if rec is not None:
            rec.write(raw)

    def start_recording(self, name: str | None = None) -> dict:
        self.stop_recording()
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        base = Path(self.config.record_dir)
        if not base.is_absolute():
            base = Path(__file__).resolve().parent.parent.parent.parent / base
        path = base / f"{name or 'session'}-{stamp}.csi"
        self.recorder = Recorder(path)
        # Attach the tap only while recording, so the common case pays nothing.
        if self.link is not None:
            self.link.decoder.raw_sink = self._record_raw
        return {"recording": True, "path": str(path)}

    def stop_recording(self) -> dict:
        if self.link is not None:
            self.link.decoder.raw_sink = None
        if self.recorder is not None:
            path = self.recorder.path
            frames = self.recorder.frames
            self.recorder.close()
            self.recorder = None
            return {"recording": False, "path": str(path), "frames": frames}
        return {"recording": False}

    # ----------------------------------------------------------------- output

    def snapshot(self, waterfall_rows: int = 0, full: bool = True,
                 spectrum_rows: int = 0) -> dict:
        with self._lock:
            snap = self.pipeline.snapshot(waterfall_rows=waterfall_rows, full=full)
            snap["waterfall_total"] = self.pipeline.waterfall_total
        snap["full"] = full
        if full:
            snap["link"] = self.link.info() if self.link else {"name": "none", "connected": False}
            snap["stimulus"] = self.stimulus.info()
            snap["recording"] = self.recorder is not None
        if full and self.scanner is not None:
            snap["spectrum"] = self.scanner.snapshot(new_rows=spectrum_rows)
        if self.fell_back_from:
            snap["fell_back_from"] = self.fell_back_from
        return snap

    def send_node_command(self, text: str) -> bool:
        link = self.link
        if link is not None and hasattr(link, "send"):
            return bool(link.send(build_command(text)))
        return False


# --------------------------------------------------------------------- app


def create_app(config: Config | None = None) -> FastAPI:
    cfg = config or Config.load()
    service = SensorService(cfg)
    app = FastAPI(title="WiFi Sense", docs_url=None, redoc_url=None)
    app.state.service = service
    app.state.config = cfg

    @app.on_event("startup")
    def _startup() -> None:
        service.start()

    @app.on_event("shutdown")
    def _shutdown() -> None:
        service.stop()

    # ------------------------------------------------------------ websocket

    @app.websocket("/ws")
    async def ws(sock: WebSocket) -> None:
        await sock.accept()
        interval = 1.0 / max(cfg.ui_update_hz, 1.0)
        # Each connection tracks how much of the waterfall it has seen, so a
        # client that joins late or reconnects gets a full window once and
        # deltas thereafter.
        # Backfill a full canvas worth on connect so the display is immediately
        # populated; ~38 KB once, then a few rows per update.
        seen = max(0, service.pipeline.waterfall_total - 1400)
        spec_seen = 0
        if service.scanner is not None:
            spec_seen = max(0, service.scanner.total_rows - 240)
        tick = 0
        # Slow-changing panels refresh a few times a second; state and the
        # waterfall delta stream at the full rate.  This is what keeps the feed
        # near 100 kbit/s instead of the ~1 Mbit/s that sending everything
        # every frame cost.
        full_every = max(1, int(round(cfg.ui_update_hz / 3.0)))
        try:
            while True:
                total = service.pipeline.waterfall_total
                new_rows = max(0, min(total - seen, 1500))
                full = (tick % full_every) == 0
                spec_new = 0
                if full and service.scanner is not None:
                    spec_total = service.scanner.total_rows
                    spec_new = max(0, min(spec_total - spec_seen, 600))
                snap = await asyncio.to_thread(service.snapshot, new_rows, full, spec_new)
                seen = snap.get("waterfall_total", total)
                if full and "spectrum" in snap:
                    spec_seen = snap["spectrum"].get("total_rows", spec_seen)
                await sock.send_text(json.dumps(snap, default=_json_default, allow_nan=False))
                tick += 1
                await asyncio.sleep(interval)
        except (WebSocketDisconnect, RuntimeError, ConnectionError):
            return
        except Exception:
            # One bad client must not take the server down -- but it must also
            # not vanish without trace, or a serialisation bug looks identical
            # to a client that simply hung up.
            traceback.print_exc()
            return

    # ----------------------------------------------------------------- REST

    @app.get("/api/state")
    async def state():
        return NumpyJSONResponse(await asyncio.to_thread(service.snapshot, 0))

    @app.get("/api/config")
    async def get_config():
        return NumpyJSONResponse(cfg.as_dict())

    @app.post("/api/config")
    async def set_config(payload: dict):
        changed = {}
        for key, value in payload.items():
            if hasattr(cfg, key):
                setattr(cfg, key, value)
                changed[key] = value
        cfg.save()
        # Apply the settings that can take effect without a restart.
        if "stimulus_rate_hz" in changed:
            service.stimulus.set_rate(float(changed["stimulus_rate_hz"]))
        if "stimulus_target" in changed and changed["stimulus_target"]:
            service.stimulus.set_target(str(changed["stimulus_target"]))
        return NumpyJSONResponse({"ok": True, "changed": changed, "config": cfg.as_dict()})

    @app.post("/api/node/{command}")
    async def node_command(command: str, payload: dict | None = None):
        text = command if not payload else f"{command} {payload.get('value', '')}".strip()
        ok = service.send_node_command(text)
        return NumpyJSONResponse({"ok": ok, "command": text})

    @app.post("/api/record/start")
    async def record_start(payload: dict | None = None):
        name = (payload or {}).get("name")
        return NumpyJSONResponse(service.start_recording(name))

    @app.post("/api/record/stop")
    async def record_stop():
        return NumpyJSONResponse(service.stop_recording())

    @app.get("/api/recordings")
    async def recordings():
        base = Path(cfg.record_dir)
        if not base.is_absolute():
            base = Path(__file__).resolve().parent.parent.parent.parent / base
        if not base.exists():
            return NumpyJSONResponse([])
        out = []
        for p in sorted(base.glob("*.csi"), reverse=True)[:25]:
            try:
                out.append(session_info(p))
            except (OSError, ValueError):
                continue
        return NumpyJSONResponse(out)

    # ------------------------------------------------------- history / sleep

    @app.get("/api/history")
    async def history(hours: float = 12.0, end: float | None = None,
                      points: int = 720):
        """Archived series over a window ending now (or at ``end``)."""
        arc = service.archive
        if arc is None:
            return NumpyJSONResponse({"enabled": False, "sense": [], "env": []})
        stop = float(end) if end else time.time()
        start = stop - max(0.1, float(hours)) * 3600.0
        data = await asyncio.to_thread(
            arc.series, start, stop, max(60, min(int(points), 4000))
        )
        data["enabled"] = True
        return NumpyJSONResponse(data)

    @app.get("/api/history/stats")
    async def history_stats():
        arc = service.archive
        if arc is None:
            return NumpyJSONResponse({"enabled": False})
        return NumpyJSONResponse(await asyncio.to_thread(arc.stats))

    @app.get("/api/sleep/nights")
    async def sleep_nights(limit: int = 30):
        arc = service.archive
        if arc is None:
            return NumpyJSONResponse({"enabled": False, "nights": []})
        nights = await asyncio.to_thread(
            arc.nights, max(1, min(int(limit), 365)),
            window_start_h=cfg.sleep_window_start_h,
            window_end_h=cfg.sleep_window_end_h,
        )
        return NumpyJSONResponse({"enabled": True, "nights": nights})

    @app.get("/api/sleep")
    async def sleep(night: str | None = None):
        """Sleep report for one night, named by the evening it began.

        Defaults to last night rather than today: asked in the morning, "the
        report" means the night that just ended, whose evening was yesterday.
        """
        arc = service.archive
        if arc is None:
            return NumpyJSONResponse({"enabled": False, "found": False,
                                      "reason": "archive disabled"})
        target = night or (datetime.now() - timedelta(days=1)).date().isoformat()
        try:
            report = await asyncio.to_thread(
                arc.sleep_report, target,
                window_start_h=cfg.sleep_window_start_h,
                window_end_h=cfg.sleep_window_end_h,
            )
        except ValueError:
            return NumpyJSONResponse(
                {"enabled": True, "found": False,
                 "reason": f"'{target}' is not a YYYY-MM-DD date"},
                status_code=400,
            )
        report["enabled"] = True
        return NumpyJSONResponse(report)

    @app.post("/api/reset")
    async def reset():
        with service._lock:
            service.pipeline.reset()
        return NumpyJSONResponse({"ok": True})

    @app.get("/api/health")
    async def health():
        snap = await asyncio.to_thread(service.snapshot, 0)
        link = snap.get("link", {})
        stats = snap.get("stats", {})
        stale = stats.get("stale")
        healthy = bool(link.get("connected")) and stale is not None and stale < 5.0
        return NumpyJSONResponse(
            {
                "healthy": healthy,
                "link": link.get("name"),
                "connected": link.get("connected"),
                "stale_seconds": stale,
                "sample_rate": snap.get("motion", {}).get("sample_rate"),
                "uptime": round(time.monotonic() - service.started_at, 1),
            },
            status_code=200 if healthy else 503,
        )

    # ------------------------------------------------------------- static UI

    if WEB_DIR.exists():
        app.mount("/static", StaticFiles(directory=str(WEB_DIR)), name="static")

        @app.get("/")
        async def index():
            return FileResponse(str(WEB_DIR / "index.html"))

        @app.get("/history")
        async def history_page():
            return FileResponse(str(WEB_DIR / "history.html"))

    return app
