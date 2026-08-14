# WiFi Sense

[![validate](https://github.com/ApexAcer/wifisense-pi/actions/workflows/validate.yml/badge.svg)](https://github.com/ApexAcer/wifisense-pi/actions/workflows/validate.yml)

Detect motion, presence and breathing **through walls** using ordinary WiFi
signals. An ESP32-S3 measures the radio channel, a Raspberry Pi 4 analyses it,
and a browser dashboard shows the result live.

No camera. No PIR. Nothing worn. It works in complete darkness, and it detects a
person sitting perfectly still by picking up the movement of their chest as they
breathe.

![The live dashboard](docs/dashboard.webp)

## What it does, measured on real hardware

| | |
|---|---|
| **Motion** | walking, gesturing, sitting down. Reads ~28 dB above the noise floor — unmissable. |
| **Presence** | tells an empty room from a room with a *motionless* person in it. This is the hard one, and the thing PIR sensors get wrong constantly. |
| **Breathing rate** | 8–36 breaths/min, validated to within 0.1 bpm against known ground truth. |
| **Through walls** | 2.4 GHz passes through plasterboard, wood and glass. Brick and concrete attenuate heavily; foil-backed insulation blocks it. |
| **Activity class** | empty / still / subtle / active / vigorous. |
| **Environment** | temperature, humidity, pressure and a gas-based air quality index from a BMP280, DHT22 and MQ135 on the same node. |
| **Sleep report** | time in bed, still vs restless, disturbances, overnight breathing rate. |

## What it cannot do, and why

This matters more than the feature list, because most WiFi-sensing claims online
are describing a different instrument.

- **No imaging, no pose, no skeleton.** WiFi at 20 MHz bandwidth gives a range
  resolution of `c / 2B = 7.5 m` — larger than most rooms. No amount of signal
  processing recovers an image from that; the information is not present. The
  well-known MIT demos (RF-Pose, RF-Capture, WiTrack) do not use WiFi. They use
  purpose-built FMCW radar with GHz of bandwidth and a physical antenna array.
- **No locating a person.** One transmitter and one receiver measure a single
  path. You learn that something changed, not where. That needs three or more
  spatially separated nodes.
- **No counting people.** Two people disturb the channel more than one, but not
  in a reliably countable way.
- **No detecting someone who is motionless *and* holding their breath.** There is
  genuinely nothing to measure. Physics, not a missing feature.
- **No identity.**
- **No sleep stages.** No REM, no deep, no light. Those are distinguished by
  brain and eye activity, which is not present in this signal at any level. The
  report gives stillness, which is what it measures, and leaves whether that
  stillness was sleep to the reader.

It is also not a medical device. It is a bedroom instrument built from a $5
microcontroller.

## How it works

A WiFi signal reaches the receiver by many paths at once — direct, plus
reflections off walls, furniture and people. Those copies add up with different
delays, so some frequencies reinforce and others cancel. That fingerprint across
frequency is **Channel State Information (CSI)**, and most WiFi chips report it.

Move anything in the room and the path lengths change, so the fingerprint
changes. A body is mostly water and reflects 2.4 GHz strongly, which is what
makes people visible. One wavelength at 2.4 GHz is 12.5 cm, so a chest moving
6 mm while breathing shifts the path by a measurable fraction of a wavelength.

```
ESP32-S3  --- 64 sub-carriers, 100 times a second
   |          (52 carry signal; the rest are DC null and guard band)
   |  nRF24 radio (AES-128-CTR) or USB serial, COBS-framed with CRC-16
   v
Pi 4      --- cleans it up, then answers two questions:
   |            "is something moving?"  energy at 1.5-9 Hz
   |            "is someone there?"     energy at 0.13-0.7 Hz
   |  WebSocket
   v
Browser   --- live dashboard, on the Pi or on your phone
```

**Both bands are Doppler frequencies, not body-movement frequencies.** A
reflector moving at velocity `v` shifts the signal by `v / wavelength`, so
walking at 1 m/s writes energy near 8 Hz — not at walking pace. Breathing moves
the chest slowly enough to stay down at 0.13–0.7 Hz, which is the only reason the
two are separable.

The motion band starts at 1.5 Hz rather than 0.5 Hz deliberately: a chest
excursion of ~6 mm swings the carrier phase by ~0.6 rad, well outside the
small-angle regime, so the amplitude modulation is rich in harmonics. At a 0.5 Hz
edge a *motionless breathing subject* measured +10.4 dB and was classified as
walking.

**Everything is reported in dB above the receiver's own noise floor**, so an
empty room reads 0 dB in any room, at any distance, at any transmit power. There
is no calibration step and no learned baseline that can drift. Finding a
trustworthy noise floor is the whole trick: receiver noise is independent on
every sub-carrier because it is generated inside the radio, while anything
happening in the room moves all sub-carriers together. So the sub-carrier
covariance is decomposed and the weakest directions — the ones the room cannot
reach — define the noise level.

### The part that surprises people: you must generate traffic

CSI only exists when a packet is *received*. A silent channel produces no data at
all, no matter how good the hardware is.

| approach | measured CSI rate |
|---|---|
| listen passively to ambient WiFi | ~1 Hz, irregular |
| ESP32 transmits, capture CSI from the ACKs | ~1 Hz (does not work) |
| **Pi sends UDP to the ESP32** | **1:1, up to 200 Hz** |

The third is what this system does, and it is why the node joins your WiFi
network. The sample rate is therefore *chosen*, not discovered — which also means
it is evenly spaced, which is what the respiration FFT needs.

## Measured performance

| | |
|---|---|
| CSI sample rate | 100 Hz, 1:1 with the stimulus (200 Hz also works) |
| Wire rate | 149 bytes/frame, 119 kbit/s (~16% of the serial link) |
| Pi CPU | ~20% of one core |
| Empty room | +1.1 dB motion, +0.0 dB vitals |
| Walking | +27.8 dB motion |
| Still person breathing | +33.6 dB vitals |
| Breathing accuracy | 14.1 bpm measured against 14.0 truth; 22.0 against 22.0 |
| Encrypted nRF24 link | 108.1 Hz delivered of 113.6 Hz captured (95.1% end to end) |
| Serial link errors | 0 CRC, 0 framing, over sustained runs |
| DSP validation | 81/81 checks pass (`tools/validate_dsp.py`) |

## Hardware

| part | role | notes |
|---|---|---|
| ESP32-S3-WROOM-1 **N16R8** | the sensor | GPIO 26–37 are unavailable on this variant: quad flash + octal PSRAM. Wiring a peripheral there gives a board that will not boot. |
| Raspberry Pi 4 | DSP, dashboard, archive | any Linux box with a USB port works for the serial link |
| nRF24L01+ PA/LNA | optional wireless uplink | needs proper decoupling at the module or it dies after a few packets |
| CC1101 433 MHz | optional out-of-band uplink | complete in software; blocked on antenna matching here |
| BMP280 / DHT22 / MQ135 | optional environment sensing | all on the ESP32, because the Pi has no ADC and no spare SPI chip select |

Full pin maps, forbidden pins, power rules and the reasoning behind each choice
are in [`PROJECT.txt`](PROJECT.txt) section 4.

## Quick start

### With no hardware at all

The synthetic link is a physics-based simulator, not a mock — it drives the real
detector chain, so the dashboard behaves exactly as it does on hardware.

```sh
python3 -m venv .venv
./.venv/bin/pip install -r requirements.txt
cd pi
../.venv/bin/python -m wifisense --link synthetic
```

Then open `http://localhost:8080`.

### With hardware

```sh
cd firmware
pio run -t upload --upload-port /dev/ttyUSB0

cd ../pi
cp config.example.json config.json          # then set your radio key
../.venv/bin/python tools/node.py wifi "YourSSID" "YourPassword"
../.venv/bin/python -m wifisense --link serial
```

The SSID is 2.4 GHz only — the ESP32-S3 cannot see 5 GHz networks. Credentials
live in the node's flash, so this is a one-time step with no recompile.

Install as a service that starts at boot:

```sh
./install.sh              # server only
./install.sh --kiosk      # plus a full-screen browser on the Pi's display
./install.sh --uninstall
```

### Useful commands

```sh
cd pi
../.venv/bin/python tools/node.py info          # sensor node status
../.venv/bin/python tools/node.py monitor 30    # measure the CSI rate
../.venv/bin/python tools/node.py env           # read all three sensors now
../.venv/bin/python tools/validate_dsp.py       # verify the signal processing
```

`validate_dsp.py` drives the whole detector chain with synthetic CSI whose
breathing rate is known exactly. A real room cannot tell you whether
"14 breaths/min" was correct, so this is the only place the pipeline is
falsifiable — and it is what CI runs on every push.

## Getting good results

- Put the ESP32 and the WiFi router on **opposite sides** of the space you want
  to watch. You are sensing what happens between them, not around the node. This
  matters more than anything else.
- 2 to 8 metres apart works well.
- **A stronger signal is not better.** Keep RSSI around -40 to -60 dBm. Very
  close together the direct path dominates and your reflection is buried in it.
- Breathing needs the subject reasonably still and about 30 seconds of settling.
  While you move, the panel says "subject moving" and publishes nothing — that is
  correct, not a fault.
- Ceiling fans, curtains and pets all register. They are not false positives:
  something really is moving.

## Reading the dashboard

`http://<pi-address>:8080` live, `http://<pi-address>:8080/history` for the
archive and the sleep report.

The waterfall's horizontal bands are the room's standing multipath pattern;
vertical streaks are something moving. Dotted lines on the activity trace mark
the thresholds the system is actually deciding on. The packets panel separates
frames by type and counts loss at both the frame and radio-fragment layer,
because a single total cannot tell a link carrying CSI at 100 Hz from one
carrying only status frames once a second.

The air quality figure deserves one warning: it uses the standard 0–500 AQI scale
and category boundaries, but it is computed from the MQ135's CO2-equivalent
reading alone. A regulatory AQI is dominated by PM2.5, which this sensor cannot
measure at all — **so a smoky room can still read "Good"**. Section 6d of
`PROJECT.txt` covers this, along with why the baseline ratchets upward on its own.

## Radio link security

CSI reveals when a room is occupied, when people move, and when they sleep. In
the clear, anyone within range with a matching radio on the same channel reads
all of it. So everything sent over the radio is encrypted with AES-128 in counter
mode, with a fresh random session id per boot and replay rejection.

**The shipped key is all zeros, and a placeholder is not security.** Generate one
and put the same value in both ends:

```sh
python3 -c "import secrets;print(secrets.token_hex(16))"
```

- `firmware/src/config.h` → `RADIO_KEY` as `{0x.., 0x.., ...}`
  (or `firmware/src/secrets.h`, which is gitignored)
- `pi/config.json` → `radio_key` as 32 hex characters

This is confidentiality, not authentication: the CRC-16 inside the encrypted
payload means a random forgery survives with probability 1/65536, which is
proportionate for a home sensor but is not a MAC.

## Repository layout

```
PROJECT.txt              the real documentation: everything, in one file
firmware/
  src/config.h           tunables
  src/main.cpp           CSI capture and framing
  src/radio_tx.*         nRF24 + CC1101 uplink, SPI bus arbitration
  src/cc1101_driver.*    in-tree CC1101 register driver
  src/bmp280.*           in-tree BMP280/BME280 SPI driver
  src/sensors.*          DHT22, MQ135, and the sensor sampling task
  src/link_crypto.*      AES-128-CTR for the radio link
pi/
  wifisense/
    protocol.py          wire format: COBS framing, CRC-16, sub-carrier map
    stimulus.py          the UDP generator that sets the sample rate
    archive.py           SQLite long-term store + the sleep report
    spectrum.py          CC1101 sub-GHz band monitor
    dsp/                 csi -> filters -> motion / breathing -> pipeline
    link/                serial, nrf24, cc1101, replay, synthetic
    api/server.py        FastAPI, WebSocket, REST
  web/                   dashboard + history page, no build step, no dependencies
  tools/node.py          command-line control of the ESP32
  tools/validate_dsp.py  correctness tests against known ground truth
```

## Documentation

[**`PROJECT.txt`**](PROJECT.txt) is the canonical document and is written to be
read at the bench, away from the terminal: complete wiring, setup, tuning,
troubleshooting, and the reasoning behind every constant. It also records the
bugs that had to be fixed and what each one looked like — including the one where
a real subject's *seventh* harmonic measured 2.4 dB above the fundamental and the
peak picker duly reported a rate seven times too fast.
