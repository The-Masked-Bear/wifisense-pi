#!/usr/bin/env python3
"""Command-line control for the ESP32-S3 sensor node.

    python tools/node.py info
    python tools/node.py wifi <SSID> <PASSWORD>
    python tools/node.py mode sta|sniffer
    python tools/node.py link usb|nrf24|cc1101
    python tools/node.py radiosweep        # hunt for radios on any GPIO
    python tools/node.py pinprobe          # which ESP32 pins are driven externally
    python tools/node.py findmiso          # assert each CS, see which pin answers
    python tools/node.py rate 100          # STA stimulus rate, Hz
    python tools/node.py probe 10          # sniffer probe rate, Hz
    python tools/node.py chan 6            # sniffer channel
    python tools/node.py env               # BMP280 / DHT22 / MQ135 readings
    python tools/node.py mq135 cal         # treat the air right now as clean
    python tools/node.py mq135 supply 5v   # or 3v3, if AO is wired direct
    python tools/node.py monitor [seconds]
    python tools/node.py reboot

Opening the port asserts DTR, which on these dev boards is wired to EN and
therefore **resets the board**.  Anything written during the ~2-5 s boot (a
WiFi scan runs in sniffer mode) is silently discarded, so every command here
waits for the node to announce itself before transmitting.  Getting this wrong
looks exactly like a command parser bug.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import serial

from wifisense.link.serial_link import find_port
from wifisense.protocol import (
    CsiFrame,
    FrameDecoder,
    LogFrame,
    StatusFrame,
    build_command,
)

BAUD = 921600


def open_node(port: str | None = None, wait: float = 30.0):
    """Open the port and block until the node is demonstrably alive.

    Readiness is a status frame, not a timer: the boot time varies with
    whether a WiFi scan or an association attempt runs.
    """
    port = port or find_port()
    if port is None:
        print("error: no serial device found (looked for /dev/ttyUSB* and /dev/ttyACM*)")
        raise SystemExit(2)

    ser = serial.Serial(port, BAUD, timeout=0.2)
    dec = FrameDecoder()
    dec.feed(b"\x00")

    deadline = time.time() + wait
    while time.time() < deadline:
        data = ser.read(4096)
        if data:
            for frame in dec.feed(data):
                if isinstance(frame, (StatusFrame, LogFrame)):
                    return ser, dec, port
    print(f"warning: {port} opened but the node never reported in; sending anyway")
    return ser, dec, port


def collect(ser, dec, seconds: float, *, show_logs: bool = True):
    out = {"csi": 0, "status": None, "logs": []}
    end = time.time() + seconds
    while time.time() < end:
        data = ser.read(8192)
        if not data:
            continue
        for frame in dec.feed(data):
            if isinstance(frame, CsiFrame):
                out["csi"] += 1
            elif isinstance(frame, StatusFrame):
                out["status"] = frame
            elif isinstance(frame, LogFrame):
                out["logs"].append(frame.text)
                if show_logs:
                    print(f"  node: {frame.text}")
    return out


def describe(st: StatusFrame) -> str:
    return (
        f"mode={'sta' if st.link_mode == 1 else 'sniffer'} "
        f"assoc={'yes' if st.wifi_state & 1 else 'no'} "
        f"csi={'ok' if st.wifi_state & 2 else 'FAILED'} "
        f"creds={'yes' if st.wifi_state & 4 else 'no'} "
        f"ch={st.channel} rate={st.sample_rate}Hz rssi={st.rssi}dBm "
        f"csi_count={st.csi_count} tx={st.tx_count} dropped={st.dropped} heap={st.free_heap}"
    )


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 1

    cmd = sys.argv[1]
    ser, dec, port = open_node()
    print(f"connected: {port}\n")

    try:
        if cmd == "monitor":
            secs = float(sys.argv[2]) if len(sys.argv) > 2 else 20.0
            print(f"monitoring {secs:.0f} s ...")
            r = collect(ser, dec, secs)
            print(f"\nCSI frames: {r['csi']}  ->  {r['csi']/secs:.1f} Hz")
            if r["status"]:
                print(f"node: {describe(r['status'])}")
            print(
                f"decode: ok={dec.stats.frames_ok} crc_err={dec.stats.crc_errors} "
                f"cobs_err={dec.stats.cobs_errors}"
            )
            return 0

        if cmd == "wifi":
            if len(sys.argv) < 4:
                print("usage: node.py wifi <SSID> <PASSWORD>")
                return 1
            ssid, password = sys.argv[2], " ".join(sys.argv[3:])
            # Not echoed: this ends up in shell history and scrollback already.
            print(f"provisioning SSID {ssid!r} ...")
            ser.write(build_command(f"wifi {ssid} {password}"))
            collect(ser, dec, 25.0)
        elif cmd in ("radiosweep", "radioscan", "pinprobe", "findmiso"):
            # The sweep bit-bangs every usable GPIO and takes ~20 s.
            ser.write(build_command(cmd))
            collect(ser, dec, 95.0 if cmd == "radiosweep" else 45.0)
        elif cmd in ("info", "ping", "reboot", "forget", "start", "stop", "reset", "env"):
            ser.write(build_command(cmd))
            collect(ser, dec, 3.0)
        elif cmd == "mq135":
            # "mq135 cal" and "mq135 supply 5v|3v3" -- the node parses the
            # whole line, so pass the arguments through untouched.
            ser.write(build_command(" ".join(sys.argv[1:])))
            collect(ser, dec, 3.0)
        elif cmd in ("mode", "rate", "probe", "chan", "link"):
            if len(sys.argv) < 3:
                print(f"usage: node.py {cmd} <value>")
                return 1
            ser.write(build_command(f"{cmd} {sys.argv[2]}"))
            collect(ser, dec, 3.0)
        else:
            print(f"unknown command: {cmd}")
            print(__doc__)
            return 1

        # Always finish with a fresh status so the caller sees the real effect
        # rather than trusting that the command did what it claimed.
        ser.write(build_command("info"))
        r = collect(ser, dec, 4.0, show_logs=True)
        if r["status"]:
            print(f"\nstatus: {describe(r['status'])}")
        return 0
    finally:
        ser.close()


if __name__ == "__main__":
    raise SystemExit(main())
