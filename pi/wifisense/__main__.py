"""Entry point:  python -m wifisense  [--link serial|synthetic|replay] [--port 8080]"""

from __future__ import annotations

import argparse

import uvicorn

from .api.server import create_app
from .config import Config


def main() -> int:
    ap = argparse.ArgumentParser(prog="wifisense", description="WiFi CSI sensing server")
    ap.add_argument("--link", choices=["serial", "synthetic", "replay", "cc1101", "nrf24"])
    ap.add_argument("--port", type=int)
    ap.add_argument("--host")
    ap.add_argument("--serial-port", dest="serial_port")
    ap.add_argument("--replay-file", dest="replay_file")
    ap.add_argument("--rate", type=float, dest="stimulus_rate_hz",
                    help="Pi->node stimulus rate in Hz (this sets the CSI sample rate)")
    ap.add_argument("--no-stimulus", action="store_true")
    ap.add_argument("--record", action="store_true")
    args = ap.parse_args()

    cfg = Config.load()
    for key in ("link", "port", "host", "serial_port", "replay_file", "stimulus_rate_hz"):
        val = getattr(args, key, None)
        if val is not None:
            setattr(cfg, key, val)
    if args.no_stimulus:
        cfg.stimulus_enabled = False
    if args.record:
        cfg.record_on_start = True

    app = create_app(cfg)
    print(f"\n  WiFi Sense  ->  http://{cfg.host}:{cfg.port}   (link={cfg.link})\n")
    uvicorn.run(app, host=cfg.host, port=cfg.port, log_level="warning", access_log=False)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
