"""Wire protocol shared by the ESP32-S3 sensor and the Pi receiver.

Every frame that crosses a link (USB CDC, UART, CC1101, nRF24) has the same
shape.  The link layer is responsible only for delivering a byte stream or
datagrams; framing, integrity and typing live here so all four transports
decode through one path.

Frame layout, before COBS encoding::

    +------+---------------------------+--------+
    | type | payload                   | crc16  |
    | u8   | 0..N bytes                 | u16 LE |
    +------+---------------------------+--------+

The CRC covers ``type`` and ``payload``.  The whole thing is then COBS
encoded and terminated with a single 0x00 byte, so a receiver that joins a
stream mid-frame resynchronises on the next delimiter without ever seeing a
0x00 inside a frame.

CSI payload (little-endian)::

    off  size  field
    0    4     timestamp_us   u32   ESP32 esp_timer_get_time() truncated
    4    1     rssi           i8    dBm
    5    1     noise_floor    i8    dBm
    6    1     rate           u8    wifi_promiscuous_pkt rate index
    7    1     sig_mode       u8    0=legacy 1=HT 3=VHT
    8    1     mcs            u8
    9    1     bandwidth      u8    0=20MHz 1=40MHz
    10   1     channel        u8    primary channel
    11   1     secondary_ch   u8    0=none 1=above 2=below
    12   1     antenna        u8
    13   1     csi_len        u8    number of int8s that follow
    14   2     seq            u16   ESP-side counter, for drop detection
    16   N     csi            i8[N] I/Q interleaved, imaginary first

At 20 MHz with LLTF only, N = 128 (64 subcarriers x 2 int8).  A complete
frame is then 16 + 128 + 3 = 147 bytes, ~149 after COBS.  100 Hz of that is
119 kbit/s -- comfortable over USB CDC and over UART at 921600 baud.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field

import numpy as np

# ---------------------------------------------------------------- frame types

FRAME_CSI = 0x01
FRAME_STATUS = 0x02
FRAME_FEATURES = 0x03
FRAME_LOG = 0x04
FRAME_ENV = 0x05
FRAME_CMD = 0x10  # Pi -> ESP32
FRAME_ACK = 0x11

FRAME_NAMES = {
    FRAME_CSI: "csi",
    FRAME_STATUS: "status",
    FRAME_FEATURES: "features",
    FRAME_LOG: "log",
    FRAME_ENV: "env",
    FRAME_CMD: "cmd",
    FRAME_ACK: "ack",
}

CSI_HEADER = struct.Struct("<IbbBBBBBBBBH")
assert CSI_HEADER.size == 16

# uptime_ms, csi_count, tx_count, dropped, free_heap, rssi,
# channel, wifi_state, sample_rate, link_mode, ip -- see StatusFrame below.
STATUS_STRUCT = struct.Struct("<IIIIIhBBHBI")
assert STATUS_STRUCT.size == 31

# node_ms, pressure_pa, gas_rs_ohms, gas_r0_ohms, bmp_temp x100, dht_temp x100,
# humidity x100, gas_mv, gas_ratio x1000, gas_ppm, flags, dht_fail, gas_abc.
#
# Fixed-point rather than floats throughout: this frame crosses a 32-byte-per-
# packet radio link, and four bytes saved is a real fraction of a fragment.
# The scales are chosen so the quantisation sits below each sensor's own noise
# -- 0.01 C against the DHT22's +/-0.5 C, 1 Pa against the BMP280's +/-12 Pa.
ENV_STRUCT = struct.Struct("<IIIIhhHHHHBBB")
assert ENV_STRUCT.size == 31

# Firmware deployed before the R0/automatic-baseline fields were added emits
# the original 26-byte payload.  The node can remain on that firmware while a
# Pi is upgraded, so decode both wire versions.  Missing metadata is reported
# as zero; the gas reading and AQI remain fully usable.
LEGACY_ENV_STRUCT = struct.Struct("<IIIhhHHHHBB")
assert LEGACY_ENV_STRUCT.size == 26

ENV_BMP_OK = 0x01
ENV_DHT_OK = 0x02
ENV_GAS_OK = 0x04
ENV_GAS_CALIBRATED = 0x08

# Sub-carrier bookkeeping for a 20 MHz 802.11n capture.
#
# The ESP32 reports 64 LLTF sub-carriers as int8 I/Q pairs, ordered
# 0..+31 then -32..-1.  So buffer index i maps to sub-carrier number::
#
#     i in  0..31  ->  sub-carrier +i
#     i in 32..63  ->  sub-carrier  i - 64   (that is, -32..-1)
#
# 802.11 at 20 MHz only occupies +/-1..26; index 0 is the DC null, +27..+31
# and -32..-27 are guard band.  Those carry no energy, so including them just
# injects noise into every statistic computed downstream.
#
# Mapping the occupied set through the index layout above:
#
#     +1..+26  ->  indices  1..26
#     -26..-1  ->  indices 38..63     (because -26 lands at 64 - 26 = 38)
#
# This matches Espressif's own valid-sub-carrier table (esp-radar's
# csi_sub_carrier_table.c), which lists the valid byte ranges for this capture
# geometry as [2,54) and [76,128) -- exactly these indices, doubled, since each
# sub-carrier occupies two bytes.
LLTF_SUBCARRIERS = 64

# Everything except the DC null: keeps the guard band, so mostly useful for
# diagnostics and for seeing the band edges in the waterfall.
LLTF_USABLE = np.array(
    [i for i in range(1, 32)] + [i for i in range(32, 64)],
    dtype=np.int32,
)

# The 52 sub-carriers that actually carry signal.  This is the right default.
LLTF_GUARD_TRIMMED = np.array(
    [i for i in range(1, 27)] + [i for i in range(38, 64)],
    dtype=np.int32,
)
assert LLTF_GUARD_TRIMMED.size == 52


# ------------------------------------------------------------------- checksum


def crc16_ccitt(data: bytes, crc: int = 0xFFFF) -> int:
    """CRC-16/CCITT-FALSE.  Poly 0x1021, init 0xFFFF, no reflection."""
    for byte in data:
        crc ^= byte << 8
        for _ in range(8):
            crc = ((crc << 1) ^ 0x1021) & 0xFFFF if crc & 0x8000 else (crc << 1) & 0xFFFF
    return crc


_CRC_TABLE = None


def _crc_table() -> list[int]:
    global _CRC_TABLE
    if _CRC_TABLE is None:
        table = []
        for i in range(256):
            crc = i << 8
            for _ in range(8):
                crc = ((crc << 1) ^ 0x1021) & 0xFFFF if crc & 0x8000 else (crc << 1) & 0xFFFF
            table.append(crc)
        _CRC_TABLE = table
    return _CRC_TABLE


def crc16(data: bytes) -> int:
    """Table-driven CRC-16/CCITT-FALSE.  Identical output to crc16_ccitt."""
    table = _crc_table()
    crc = 0xFFFF
    for byte in data:
        crc = ((crc << 8) & 0xFFFF) ^ table[(crc >> 8) ^ byte]
    return crc


# ----------------------------------------------------------------------- COBS


def cobs_encode(data: bytes) -> bytes:
    """Consistent Overhead Byte Stuffing.  Output contains no 0x00 bytes."""
    out = bytearray()
    code_index = 0
    out.append(0)  # placeholder for the first code byte
    code = 1
    for byte in data:
        if byte == 0:
            out[code_index] = code
            code_index = len(out)
            out.append(0)
            code = 1
        else:
            out.append(byte)
            code += 1
            if code == 0xFF:
                out[code_index] = code
                code_index = len(out)
                out.append(0)
                code = 1
    out[code_index] = code
    return bytes(out)


def cobs_decode(data: bytes) -> bytes:
    """Inverse of :func:`cobs_encode`.  Raises ValueError on malformed input."""
    out = bytearray()
    i = 0
    n = len(data)
    while i < n:
        code = data[i]
        if code == 0:
            raise ValueError("zero code byte in COBS payload")
        i += 1
        end = i + code - 1
        if end > n:
            raise ValueError("COBS block overruns buffer")
        out.extend(data[i:end])
        i = end
        if code != 0xFF and i < n:
            out.append(0)
    return bytes(out)


# ---------------------------------------------------------------- frame types


@dataclass(slots=True)
class CsiFrame:
    """One decoded CSI measurement."""

    timestamp_us: int
    rssi: int
    noise_floor: int
    rate: int
    sig_mode: int
    mcs: int
    bandwidth: int
    channel: int
    secondary_channel: int
    antenna: int
    seq: int
    raw: np.ndarray  # int8, I/Q interleaved

    # Filled in by the receiver, not the wire.
    host_time: float = 0.0

    @property
    def n_subcarriers(self) -> int:
        return len(self.raw) // 2

    def complex(self) -> np.ndarray:
        """CSI as complex64, one entry per sub-carrier.

        The ESP32 stores each sub-carrier as two signed bytes in
        ``[imaginary, real]`` order -- note that this is the opposite of the
        conventional (real, imag) pairing, and getting it backwards mirrors
        every phase you compute.
        """
        vals = self.raw.astype(np.float32)
        imag = vals[0::2]
        real = vals[1::2]
        return (real + 1j * imag).astype(np.complex64)

    def amplitude(self) -> np.ndarray:
        return np.abs(self.complex())

    def phase(self) -> np.ndarray:
        return np.angle(self.complex())


@dataclass(slots=True)
class StatusFrame:
    """Periodic health report from the sensor node."""

    uptime_ms: int
    csi_count: int
    tx_count: int
    dropped: int
    free_heap: int
    rssi: int
    channel: int
    wifi_state: int  # bit0 associated, bit1 CSI enabled, bit2 credentials stored
    sample_rate: int
    link_mode: int  # 1 = STA, 2 = sniffer
    ip: int = 0  # node's IPv4 as a little-endian u32, 0 when unassociated

    @property
    def ip_str(self) -> str:
        """Dotted-quad form of :attr:`ip`.

        The node packs ``WiFi.localIP()``, which on lwIP is already stored in
        network byte order, so the octets come out lowest-first.
        """
        v = self.ip
        return f"{v & 0xFF}.{(v >> 8) & 0xFF}.{(v >> 16) & 0xFF}.{(v >> 24) & 0xFF}"

    @property
    def associated(self) -> bool:
        return bool(self.wifi_state & 1)

    @property
    def csi_enabled(self) -> bool:
        return bool(self.wifi_state & 2)

    @property
    def has_credentials(self) -> bool:
        return bool(self.wifi_state & 4)


@dataclass(slots=True)
class LogFrame:
    text: str


@dataclass(slots=True)
class EnvFrame:
    """Environment reading from the BMP280, DHT22 and MQ135.

    Every field is decoded to engineering units here, so nothing downstream
    needs to know about the fixed-point scaling on the wire.  The ``*_ok``
    flags matter: a sensor that failed to answer sends zeros rather than
    dropping the frame, because the other two sensors' readings are still
    good and a missing frame would look like a link fault instead of a
    single detached wire.
    """

    node_ms: int
    pressure_pa: float
    gas_rs_ohms: float
    gas_r0_ohms: float
    bmp_temp_c: float
    dht_temp_c: float
    humidity_pct: float
    gas_mv: int
    gas_ratio: float
    gas_ppm: float
    flags: int
    dht_fail: int
    gas_abc: int = 0
    host_time: float = 0.0

    @property
    def bmp_ok(self) -> bool:
        return bool(self.flags & ENV_BMP_OK)

    @property
    def dht_ok(self) -> bool:
        return bool(self.flags & ENV_DHT_OK)

    @property
    def gas_ok(self) -> bool:
        return bool(self.flags & ENV_GAS_OK)

    @property
    def gas_calibrated(self) -> bool:
        return bool(self.flags & ENV_GAS_CALIBRATED)

    @property
    def temperature_c(self) -> float | None:
        """The temperature to show a human.

        The BMP280 wins when both are present.  It is the better thermometer
        of the two -- the DHT22's own polymer humidity element sits in the
        same package and self-heats, which reads consistently high by a
        degree or so -- but the DHT22 is still the fallback, because half a
        room's temperature beats none of it.
        """
        if self.bmp_ok:
            return self.bmp_temp_c
        if self.dht_ok:
            return self.dht_temp_c
        return None

    def altitude_m(self, sea_level_pa: float = 101325.0) -> float | None:
        """Altitude from pressure, via the international barometric formula.

        Only as good as ``sea_level_pa``: with the standard atmosphere assumed
        this tracks weather as much as height, so it moves tens of metres over
        a few days without anything having been picked up.
        """
        if not self.bmp_ok or self.pressure_pa <= 0:
            return None
        return 44330.0 * (1.0 - (self.pressure_pa / sea_level_pa) ** 0.1902949)

    def dew_point_c(self) -> float | None:
        """Dew point via the Magnus-Tetens approximation.

        Worth showing because it is the number that predicts condensation and
        felt mugginess, neither of which is obvious from temperature and
        relative humidity read separately.
        """
        if not self.dht_ok or not (0.0 < self.humidity_pct <= 100.0):
            return None
        t = self.temperature_c
        if t is None or not (-45.0 < t < 60.0):
            return None
        import math

        a, b = 17.62, 243.12
        gamma = (a * t) / (b + t) + math.log(self.humidity_pct / 100.0)
        return (b * gamma) / (a - gamma)


@dataclass(slots=True)
class DecodeStats:
    frames_ok: int = 0
    crc_errors: int = 0
    cobs_errors: int = 0
    short_frames: int = 0
    unknown_types: int = 0
    bytes_in: int = 0
    resyncs: int = 0
    # Per-type tallies.  frames_ok alone cannot tell a link that is delivering
    # CSI at 100 Hz from one delivering only status frames once a second -- both
    # look like "frames arriving".  Counting each type separately is what makes
    # a missing environment or CSI stream visible instead of being averaged
    # away into a healthy-looking total.
    csi_frames: int = 0
    status_frames: int = 0
    env_frames: int = 0
    log_frames: int = 0

    def as_dict(self) -> dict:
        return {
            "frames_ok": self.frames_ok,
            "crc_errors": self.crc_errors,
            "cobs_errors": self.cobs_errors,
            "short_frames": self.short_frames,
            "unknown_types": self.unknown_types,
            "bytes_in": self.bytes_in,
            "resyncs": self.resyncs,
            "csi_frames": self.csi_frames,
            "status_frames": self.status_frames,
            "env_frames": self.env_frames,
            "log_frames": self.log_frames,
        }


# --------------------------------------------------------------- build/decode


def build_frame(ftype: int, payload: bytes) -> bytes:
    """Wrap a payload into a complete, COBS-framed, 0x00-terminated frame."""
    body = bytes([ftype]) + payload
    body += struct.pack("<H", crc16(body))
    return cobs_encode(body) + b"\x00"


def build_command(text: str) -> bytes:
    """Pi -> ESP32 control message.

    Deliberately *not* COBS-framed.  The uplink (CSI) is high-rate binary and
    needs framing and a CRC; the downlink is a handful of short directives per
    session, where plain newline-terminated ASCII is worth far more than
    integrity checking -- it means the node can be driven from ``screen`` or
    ``minicom`` by hand when something is wrong, which is exactly when you
    need it most.  The firmware's command reader parses lines, not frames.
    """
    return text.encode("utf-8") + b"\n"


def decode_frame(raw: bytes, stats: DecodeStats | None = None):
    """Decode one de-stuffed frame body.  Returns None if it is unusable.

    ``raw`` is the content between two 0x00 delimiters, still COBS encoded.
    """
    if stats is None:
        stats = DecodeStats()

    if len(raw) < 4:
        stats.short_frames += 1
        return None

    try:
        body = cobs_decode(raw)
    except ValueError:
        stats.cobs_errors += 1
        return None

    if len(body) < 3:
        stats.short_frames += 1
        return None

    payload, got = body[:-2], struct.unpack("<H", body[-2:])[0]
    if crc16(payload) != got:
        stats.crc_errors += 1
        return None

    ftype, payload = payload[0], payload[1:]

    if ftype == FRAME_CSI:
        if len(payload) < CSI_HEADER.size:
            stats.short_frames += 1
            return None
        (
            ts,
            rssi,
            noise,
            rate,
            sig_mode,
            mcs,
            bw,
            chan,
            sec,
            ant,
            csi_len,
            seq,
        ) = CSI_HEADER.unpack_from(payload, 0)
        csi = payload[CSI_HEADER.size : CSI_HEADER.size + csi_len]
        if len(csi) != csi_len or csi_len == 0:
            stats.short_frames += 1
            return None
        stats.frames_ok += 1
        stats.csi_frames += 1
        return CsiFrame(
            timestamp_us=ts,
            rssi=rssi,
            noise_floor=noise,
            rate=rate,
            sig_mode=sig_mode,
            mcs=mcs,
            bandwidth=bw,
            channel=chan,
            secondary_channel=sec,
            antenna=ant,
            seq=seq,
            raw=np.frombuffer(csi, dtype=np.int8),
        )

    if ftype == FRAME_STATUS:
        if len(payload) < STATUS_STRUCT.size:
            stats.short_frames += 1
            return None
        vals = STATUS_STRUCT.unpack_from(payload, 0)
        stats.frames_ok += 1
        stats.status_frames += 1
        return StatusFrame(*vals)

    if ftype == FRAME_LOG:
        stats.frames_ok += 1
        stats.log_frames += 1
        return LogFrame(payload.decode("utf-8", errors="replace"))

    if ftype == FRAME_ENV:
        if len(payload) < LEGACY_ENV_STRUCT.size:
            stats.short_frames += 1
            return None

        if len(payload) >= ENV_STRUCT.size:
            (
                node_ms,
                pressure,
                gas_rs,
                gas_r0,
                bmp_t,
                dht_t,
                rh,
                gas_mv,
                ratio,
                ppm,
                flags,
                dht_fail,
                gas_abc,
            ) = ENV_STRUCT.unpack_from(payload, 0)
        else:
            # The deployed pre-R0 firmware has the same leading fields but
            # omits gas_r0 and gas_abc.  Defaults preserve the EnvFrame API;
            # those fields are metadata only and do not affect AQI.
            (
                node_ms,
                pressure,
                gas_rs,
                bmp_t,
                dht_t,
                rh,
                gas_mv,
                ratio,
                ppm,
                flags,
                dht_fail,
            ) = LEGACY_ENV_STRUCT.unpack_from(payload, 0)
            gas_r0 = 0
            gas_abc = 0

        stats.frames_ok += 1
        stats.env_frames += 1
        return EnvFrame(
            node_ms=node_ms,
            pressure_pa=float(pressure),
            gas_rs_ohms=float(gas_rs),
            gas_r0_ohms=float(gas_r0),
            bmp_temp_c=bmp_t / 100.0,
            dht_temp_c=dht_t / 100.0,
            humidity_pct=rh / 100.0,
            gas_mv=gas_mv,
            gas_ratio=ratio / 1000.0,
            gas_ppm=float(ppm),
            flags=flags,
            dht_fail=dht_fail,
            gas_abc=gas_abc,
        )

    stats.unknown_types += 1
    return None


class FrameDecoder:
    """Incremental 0x00-delimited frame reassembler for a byte stream.

    Feed it whatever the link hands you; it yields decoded frame objects.  A
    partial frame is carried across calls, and a frame that arrives with a bad
    CRC is dropped without disturbing the ones around it.
    """

    MAX_FRAME = 4096

    def __init__(self) -> None:
        self.buf = bytearray()
        self.stats = DecodeStats()
        self._synced = False
        # Optional tap receiving each complete frame still in wire form,
        # terminator included.  Recording needs the encoded bytes, not the
        # decoded objects, so that a replay exercises the entire decode path --
        # framing, CRC and all -- exactly as a live link does.
        self.raw_sink = None

    def feed(self, data: bytes):
        self.stats.bytes_in += len(data)
        self.buf.extend(data)
        out = []
        while True:
            idx = self.buf.find(0)
            if idx < 0:
                # No delimiter yet.  Guard against a wedged link filling RAM.
                if len(self.buf) > self.MAX_FRAME:
                    del self.buf[: len(self.buf) - self.MAX_FRAME]
                    self.stats.resyncs += 1
                    self._synced = False
                break
            chunk = bytes(self.buf[:idx])
            del self.buf[: idx + 1]
            if not chunk:
                # A delimiter with nothing before it: we are provably sitting on
                # a clean frame boundary, so anything that follows is a whole
                # frame.  This is also how a caller can force sync by feeding a
                # lone 0x00 before the real stream.
                self._synced = True
                continue
            if not self._synced:
                # First chunk after joining a live stream mid-frame is a
                # fragment.  Drop it rather than log a spurious CRC error.
                self._synced = True
                continue
            frame = decode_frame(chunk, self.stats)
            if frame is not None:
                if self.raw_sink is not None:
                    self.raw_sink(chunk + b"\x00")
                out.append(frame)
        return out

    def reset(self) -> None:
        self.buf.clear()
        self._synced = False


# --------------------------------------------------------------- test helpers


def build_csi_frame(
    timestamp_us: int,
    csi: np.ndarray,
    *,
    rssi: int = -45,
    noise_floor: int = -92,
    rate: int = 11,
    sig_mode: int = 1,
    mcs: int = 7,
    bandwidth: int = 0,
    channel: int = 6,
    secondary_channel: int = 0,
    antenna: int = 0,
    seq: int = 0,
) -> bytes:
    """Encode a CSI frame.  Used by the synthetic link and by the tests."""
    csi = np.asarray(csi, dtype=np.int8)
    header = CSI_HEADER.pack(
        timestamp_us & 0xFFFFFFFF,
        rssi,
        noise_floor,
        rate,
        sig_mode,
        mcs,
        bandwidth,
        channel,
        secondary_channel,
        antenna,
        len(csi),
        seq & 0xFFFF,
    )
    return build_frame(FRAME_CSI, header + csi.tobytes())


def build_status_frame(
    uptime_ms: int,
    csi_count: int,
    tx_count: int,
    dropped: int,
    free_heap: int,
    rssi: int,
    channel: int,
    wifi_state: int,
    sample_rate: int,
    link_mode: int,
    ip: int = 0,
) -> bytes:
    return build_frame(
        FRAME_STATUS,
        STATUS_STRUCT.pack(
            uptime_ms & 0xFFFFFFFF,
            csi_count & 0xFFFFFFFF,
            tx_count & 0xFFFFFFFF,
            dropped & 0xFFFFFFFF,
            free_heap & 0xFFFFFFFF,
            rssi,
            channel,
            wifi_state,
            sample_rate,
            link_mode,
            ip & 0xFFFFFFFF,
        ),
    )


def build_env_frame(
    node_ms: int,
    *,
    pressure_pa: float = 0.0,
    gas_rs_ohms: float = 0.0,
    gas_r0_ohms: float = 0.0,
    bmp_temp_c: float = 0.0,
    dht_temp_c: float = 0.0,
    humidity_pct: float = 0.0,
    gas_mv: int = 0,
    gas_ratio: float = 0.0,
    gas_ppm: float = 0.0,
    flags: int = 0,
    dht_fail: int = 0,
    gas_abc: int = 0,
) -> bytes:
    """Encode an environment frame, mirroring send_env_frame() in main.cpp.

    Exists so the decoder can be exercised without hardware -- the firmware is
    the only other producer, and a wire format with exactly one implementation
    on each side is a wire format nobody can test.
    """

    def _u16(value: float, scale: float) -> int:
        v = value * scale
        return 0 if not v > 0 else min(65535, int(round(v)))

    def _i16(value: float, scale: float) -> int:
        return max(-32768, min(32767, int(round(value * scale))))

    return build_frame(
        FRAME_ENV,
        ENV_STRUCT.pack(
            node_ms & 0xFFFFFFFF,
            max(0, int(round(pressure_pa))) & 0xFFFFFFFF,
            max(0, int(round(gas_rs_ohms))) & 0xFFFFFFFF,
            max(0, int(round(gas_r0_ohms))) & 0xFFFFFFFF,
            _i16(bmp_temp_c, 100.0),
            _i16(dht_temp_c, 100.0),
            _u16(humidity_pct, 100.0),
            gas_mv & 0xFFFF,
            _u16(gas_ratio, 1000.0),
            _u16(gas_ppm, 1.0),
            flags & 0xFF,
            dht_fail & 0xFF,
            gas_abc & 0xFF,
        ),
    )
