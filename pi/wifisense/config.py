"""Runtime configuration, loaded from JSON with sane defaults.

Kept deliberately flat and JSON-backed so the web UI can edit it and so a
deployment can be tuned without touching code.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path

DEFAULT_PATH = Path(__file__).resolve().parent.parent / "config.json"


@dataclass
class Config:
    # --- link ---------------------------------------------------------------
    link: str = "serial"  # serial | synthetic | replay | cc1101 | nrf24
    serial_port: str | None = None  # None = autodetect /dev/ttyUSB* or ttyACM*
    serial_baud: int = 921600
    replay_file: str = ""

    # --- stimulus -----------------------------------------------------------
    # The Pi generates the traffic that produces CSI.  100 Hz measured 1:1 on
    # this hardware and leaves the serial link ~84% idle.
    stimulus_enabled: bool = True
    stimulus_rate_hz: float = 100.0
    stimulus_target: str = ""  # blank = learn it from the node's status frames

    # --- DSP ----------------------------------------------------------------
    nominal_rate_hz: float = 100.0
    history_seconds: float = 60.0
    breathing_window_s: float = 45.0
    trim_guard_band: bool = True

    # Detection thresholds, in dB above the spatial noise floor.  An empty room
    # sits at 0 dB by construction, so these are absolute and portable between
    # rooms -- they should rarely need changing.
    motion_on_db: float = 6.0
    motion_off_db: float = 3.5
    presence_on_db: float = 4.5
    presence_off_db: float = 2.5

    # --- server -------------------------------------------------------------
    host: str = "0.0.0.0"
    port: int = 8080
    ui_update_hz: float = 12.0

    # --- recording ----------------------------------------------------------
    record_dir: str = "recordings"
    record_on_start: bool = False

    # --- radios (optional, off unless wired and enabled) --------------------
    cc1101_enabled: bool = False
    cc1101_spi_bus: int = 0
    cc1101_spi_device: int = 1  # /dev/spidev0.1, CE1 / BCM7
    cc1101_frequency_mhz: float = 433.92

    nrf24_enabled: bool = False
    nrf24_spi_bus: int = 0
    nrf24_spi_device: int = 0  # /dev/spidev0.0, CE0 / BCM8
    nrf24_ce_pin: int = 25  # BCM 25, physical 22
    # 2400 + N MHz.  80 = 2480 MHz: inside the licence-free ISM band and clear
    # of WiFi channel 7 (2432-2452).  Must match NRF24_CHANNEL in the firmware.
    # Do not push this above 83; beyond 2483.5 MHz you leave ISM and the
    # module's own antenna match falls off a cliff.
    nrf24_channel: int = 80

    # --- radio link encryption ---------------------------------------------
    # 16-byte key as 32 hex characters, identical to RADIO_KEY in the firmware.
    # CSI reveals occupancy and movement, so the over-the-air stream is
    # encrypted by default.  Regenerate with:
    #   python3 -c "import secrets;print(secrets.token_hex(16))"
    radio_key: str = ""
    radio_encrypt: bool = True

    # --- sub-GHz spectrum monitor ------------------------------------------
    # The CC1101 is idle whenever CSI travels over USB or the nRF24, so it
    # spends that time sweeping the 433 MHz band.  Automatically paused if the
    # CC1101 becomes the CSI transport, since it cannot do both.
    spectrum_enabled: bool = True
    spectrum_start_mhz: float = 430.0
    spectrum_stop_mhz: float = 435.0
    spectrum_steps: int = 32

    # --- environment sensors ------------------------------------------------
    # The BMP280 measures absolute pressure; turning that into an altitude
    # needs a reference, and the standard atmosphere is only correct on an
    # average day.  Put your local QNH here (in hectopascals, as an aviation
    # weather report gives it) and the altitude becomes meaningful; leave it
    # and the figure tracks weather as much as height.
    sea_level_hpa: float = 1013.25

    # --- long-term archive --------------------------------------------------
    # Everything above is live-only: history_seconds is an in-memory display
    # window and nothing survives a restart.  This is the on-disk store, and it
    # is what makes an overnight sleep report possible at all.
    #
    # One row per archive_interval_s, holding aggregates over that bucket rather
    # than an instantaneous sample -- a 10 s mean would hide a 1 s movement
    # spike, so the peak is kept alongside the mean.  10 s is far finer than
    # anything being reported (breathing rate moves over minutes) and costs
    # ~8,600 rows a day, which SQLite does not notice.
    archive_enabled: bool = True
    archive_db: str = "recordings/history.db"
    archive_interval_s: float = 10.0
    # Environment changes over minutes, not seconds, so it is sampled far more
    # slowly than the CSI detectors.
    archive_env_interval_s: float = 60.0
    # 0 keeps everything.  60 days is ~30 MB, which is nothing on an SD card,
    # but unbounded growth on an appliance that runs for years is a real bug.
    archive_retain_days: int = 60

    # --- sleep report -------------------------------------------------------
    # The nightly window searched for a sleep period, as local hours.  Crossing
    # midnight is normal and handled.
    sleep_window_start_h: int = 21
    sleep_window_end_h: int = 11

    @classmethod
    def load(cls, path: Path | str | None = None) -> "Config":
        p = Path(path) if path else DEFAULT_PATH
        if not p.exists():
            return cls()
        try:
            data = json.loads(p.read_text())
        except (json.JSONDecodeError, OSError):
            # A corrupt config must not prevent the sensor from running.
            return cls()
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in data.items() if k in known})

    def save(self, path: Path | str | None = None) -> None:
        p = Path(path) if path else DEFAULT_PATH
        p.write_text(json.dumps(asdict(self), indent=2) + "\n")

    def as_dict(self) -> dict:
        return asdict(self)
