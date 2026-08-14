#pragma once

// ---------------------------------------------------------------------------
//  User configuration.  Edit this file, then rebuild and flash.
// ---------------------------------------------------------------------------

// Per-deployment secrets -- the radio key, and optionally WIFI_SSID/WIFI_PASS
// -- may live in src/secrets.h, which is gitignored.  Anything defined there
// wins over the defaults below, because every default here is behind #ifndef.
// So a published tree carries no keys while a local build still knows its own.
// Absent, the defaults apply and the build still works.
#if defined(__has_include)
#  if __has_include("secrets.h")
#    include "secrets.h"
#  endif
#endif

// How the node obtains a packet stream to measure CSI on.
//
// CSI is only produced when a packet is *received*, so something has to keep
// the air busy.  The two strategies trade configuration against sample rate:
//
//   MODE_STA      Join a 2.4 GHz network and transmit to the AP at a fixed
//                 rate.  Every unicast frame is answered by an 802.11 ACK,
//                 and CSI is captured from those ACKs -- so the sample rate is
//                 exactly the rate we choose, evenly spaced.  This is the good
//                 one, and the only one that gives clean respiration data.
//
//   MODE_SNIFFER  Listen promiscuously on a fixed channel and take CSI from
//                 whatever is already on the air (mostly neighbouring APs'
//                 beacons).  Needs no credentials at all, but the rate is
//                 whatever the neighbourhood provides -- typically 10-80 Hz,
//                 bursty and irregular.  Fine for motion, poor for breathing.
//
// The ESP32-S3 is 2.4 GHz only.  A 5 GHz-only SSID will never associate.
#define MODE_STA 1
#define MODE_SNIFFER 2

// Defaults to SNIFFER so a freshly-flashed board produces CSI with no
// configuration at all.  Set WIFI_SSID/WIFI_PASS below and switch this to
// MODE_STA for the steady, evenly-spaced sample rate that breathing detection
// needs.
#ifndef CSI_MODE
#define CSI_MODE MODE_SNIFFER
#endif

// --- MODE_STA settings -----------------------------------------------------
#ifndef WIFI_SSID
#define WIFI_SSID "CHANGE_ME"
#endif
#ifndef WIFI_PASS
#define WIFI_PASS "CHANGE_ME"
#endif

// Target CSI sample rate in Hz.  100 Hz gives 119 kbit/s on the wire, ~16% of
// a 921600-baud link.  Going much above ~200 Hz starts to contend with the
// WiFi driver's own work and the ACKs begin to arrive irregularly, which hurts
// breathing detection more than the extra samples help.
// 0 by default.  Transmitting from the node only elicits 802.11 ACKs, and
// measurement showed ACK-derived CSI does not materialise even with
// dump_ack_en set -- the rate stayed at the AP's ~1 Hz beacon.  What does work
// is the *Pi* sending UDP to the node: those arrive as ordinary data frames
// and yield one CSI sample each, measured at 1:1 up to 200 Hz.  See
// wifisense/stimulus.py.  Left configurable for experimentation.
#ifndef STIMULUS_HZ
#define STIMULUS_HZ 0
#endif

// --- MODE_SNIFFER settings -------------------------------------------------
// 0 means auto: scan at boot and camp on whichever of the non-overlapping
// channels (1, 6, 11) carries the most access points, since more APs means
// more beacons means a higher CSI rate.  Set 1-13 to pin it manually.
//
// The channel must not hop.  CSI is only comparable between packets captured
// on the same channel -- hopping would make every statistic here meaningless.
#ifndef SNIFFER_CHANNEL
#define SNIFFER_CHANNEL 0
#endif

// Passive sniffing only sees what the neighbourhood happens to transmit, which
// in a quiet area can be as little as 1 Hz -- far too slow for anything but
// coarse motion.  Broadcasting a wildcard probe request makes every AP on the
// channel reply immediately, so each probe yields roughly one CSI sample per
// AP in range.
//
// This does put traffic on a shared medium, so keep it modest.  10 Hz is
// comparable to a phone scanning for networks and is plenty when several APs
// are audible.  Set to 0 for fully passive listening.
#ifndef PROBE_HZ
#define PROBE_HZ 0
#endif

// --- Link ------------------------------------------------------------------
#ifndef SERIAL_BAUD
#define SERIAL_BAUD 921600
#endif

// Emit a status frame this often (ms).  The Pi uses it to show node health and
// to detect a wedged sensor.
#define STATUS_INTERVAL_MS 1000

// Queue depth between the WiFi callback and the serial writer.  Each slot is
// ~400 bytes.  48 slots is about half a second of buffer at 100 Hz -- enough
// to ride out a USB scheduling hiccup, small enough that a genuine stall is
// noticed rather than hidden.
#define CSI_QUEUE_DEPTH 48

// Largest CSI payload we will forward.  20 MHz LLTF-only is 128 bytes; this
// leaves room for HT-LTF captures if they are enabled later.
#define MAX_CSI_BYTES 384

// --- Radio uplink ----------------------------------------------------------
// Which transport carries CSI to the Pi.  Runtime-selectable with the "link"
// command and stored in NVS, so this is only the factory default.
//
//   usb     the USB cable.  Fastest, lossless, and adds nothing to the air.
//   nrf24   2.4 GHz.  ~7x the headroom needed for 100 Hz raw CSI, but shares
//           the band being sensed -- pinned above it, see NRF24_CHANNEL.
//   cc1101  433/868 MHz.  Completely outside the sensed band, so it cannot
//           disturb the measurement.  ~169 Hz ceiling at 500 kbit/s.
#ifndef DEFAULT_LINK
#define DEFAULT_LINK LINK_USB
#endif

// nRF24 RF channel.  N sits at 2400 + N MHz.
//
// 80 = 2480 MHz: inside the licence-free ISM band (2400-2483.5) and clear of
// the WiFi channel in use (ch 7 spans 2432-2452), so the link and the
// measurement stay out of each other's way.
//
// This was originally 108 (2508 MHz) to get further from WiFi, which was a
// mistake on two counts: it is outside the ISM allocation, and the PA/LNA
// module's antenna match and front-end filter are tuned for 2.4-2.48 GHz, so
// it transmits and receives badly up there.  Measured delivery was 25-33%
// regardless of packet rate -- the flat, rate-independent loss that says
// "bad RF", not "buffer overflow".
//
// If your AP moves to channel 11 or 13, move this DOWN (e.g. 15 = 2415 MHz)
// rather than up.  Must match nrf24_channel on the Pi.
#ifndef NRF24_CHANNEL
#define NRF24_CHANNEL 80
#endif

// CC1101 band.  Must match the module's hardware and its antenna -- 433 and
// 868 MHz boards are not interchangeable, and the silkscreen is the authority.
#ifndef CC1101_MHZ
#define CC1101_MHZ 433.92
#endif

// CC1101 data rate, kbit/s.  500 reaches ~169 Hz of raw CSI but costs
// sensitivity and therefore range; 250 still clears 90 Hz with better margin.
#ifndef CC1101_KBPS
#define CC1101_KBPS 38.4
#endif

// --- Radio link encryption ------------------------------------------------
// CSI over the air is encrypted with AES-128 in counter mode.  Without this
// the stream is plaintext: anyone with a matching nRF24 on the same channel
// could read every frame, and CSI reveals when people are present and moving
// in your home.
//
// THIS IS A PLACEHOLDER, AND A PLACEHOLDER IS NOT SECURITY.  All-zero is a
// perfectly valid AES key, so the link will encrypt happily and offer no
// confidentiality whatsoever against anyone who has read this file.
//
// Generate one key and put it in BOTH ends:
//   python3 -c "import secrets;print(secrets.token_hex(16))"
//     firmware/src/config.h  RADIO_KEY   as {0x.., 0x.., ...}
//     pi/config.json         radio_key   as 32 hex characters
// A mismatch between the two shows up as cobs_errors climbing with frames_ok
// stuck at zero.
#ifndef RADIO_KEY
#define RADIO_KEY {0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, \
                   0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00}
#endif

// Set to 0 to transmit in the clear (useful only when debugging the link).
#ifndef RADIO_ENCRYPT
#define RADIO_ENCRYPT 1
#endif

// --- Environment sensors ---------------------------------------------------
// All three live on the ESP32 rather than the Pi, and not by preference: the
// MQ135 is an analogue part and the Pi 4 has no ADC at all, while the Pi's
// SPI0 exposes only CE0 and CE1, both already taken by the nRF24 and CC1101.
//
// Set any of these to 0 to compile the sensor out entirely.
#ifndef HAVE_BMP280
#define HAVE_BMP280 1
#endif
#ifndef HAVE_DHT22
#define HAVE_DHT22 1
#endif
#ifndef HAVE_MQ135
#define HAVE_MQ135 1
#endif

// BMP280 chip select.  SCK/MOSI/MISO are shared with the two radios on SPI2 --
// see radio_tx.h -- so this pin is the only one the part needs to itself.
#ifndef BMP280_CS
#define BMP280_CS 15
#endif

// DHT22 data line.  Bit-banged, so any GPIO will do.
#ifndef DHT22_PIN
#define DHT22_PIN 16
#endif

// MQ135 analogue output.  MUST be GPIO1-10: those are ADC1, and ADC2 is
// physically unavailable whenever WiFi is running -- the radio owns that
// converter, and a read returns an error instead of a value.  Since WiFi is
// the entire point of this node, ADC2 is permanently off the table.
#ifndef MQ135_PIN
#define MQ135_PIN 4
#endif

// How the MQ135 is powered, and what sits between its AO pin and the ADC.
//
//   5 V supply  -> MQ135_VCC_MV 5000, MQ135_DIVIDER 2.0
//                  AO can swing to 5 V, which would destroy a 3.3 V input, so
//                  two equal resistors halve it.  This is the accurate option:
//                  the heater is specified for 5 V.
//   3V3 supply  -> MQ135_VCC_MV 3300, MQ135_DIVIDER 1.0
//                  AO can never exceed 3.3 V so it connects directly, but the
//                  heater runs cool and absolute ppm drifts.
//
// Changeable at runtime with "mq135 supply 5v|3v3" and stored in NVS, so a
// wrong guess here costs a command rather than a reflash.
#ifndef MQ135_VCC_MV
#define MQ135_VCC_MV 5000
#endif
#ifndef MQ135_DIVIDER
#define MQ135_DIVIDER 2.0f
#endif

// Load resistor fitted to the MQ135 breakout, in ohms.  The board divides its
// sensing element Rs against this to ground, so it sets the whole Rs scale.
// Most of the blue "Flying Fish" boards fit 1 k; some fit 10 k or 20 k.  It
// only shifts Rs by a constant factor, and R0 is calibrated in the same units,
// so the Rs/R0 ratio the ppm estimate actually uses is unaffected -- getting
// this wrong changes the reported resistance, not the reported air quality.
#ifndef MQ135_RL_OHMS
#define MQ135_RL_OHMS 10000.0f
#endif

// MQ135 CO2 sensitivity curve, fitted as  ppm = A * (Rs/R0)^-B.
//
// These are the constants in general use for this part, derived from the
// datasheet's log-log CO2 plot.  They also DEFINE what R0 means here, and that
// is the trap: the datasheet's own R0 is measured at 100 ppm ammonia, where
// clean air sits at Rs/R0 ~= 3.6, but this curve's R0 is the resistance at
// atmospheric CO2.  Calibrating against one convention and evaluating with the
// other is self-consistent, silent, and wrong by two orders of magnitude --
// it read 4 ppm in ordinary room air.
#ifndef MQ135_CURVE_A
#define MQ135_CURVE_A 116.6020682f
#endif
#ifndef MQ135_CURVE_B
#define MQ135_CURVE_B 2.769034857f
#endif

// Background CO2 assumed during calibration, in ppm.  Outdoor air is ~420 ppm
// and rising a couple of ppm a year.
//
// Calibrating indoors therefore builds in an optimistic baseline: a closed
// room is usually 500-900 ppm, so anchoring that to 420 makes every later
// reading low by the difference.  For an honest zero, run "mq135 cal" with the
// node beside an open window.  Auto-calibration exists so the reading is
// usable without that ritual, not as a substitute for it.
#ifndef MQ135_ATMO_PPM
#define MQ135_ATMO_PPM 420.0f
#endif

// Automatic baseline correction (ABC), and the window it works over.
//
// R0 is not a constant. A new MQ135's element resistance climbs steadily for
// its first day or two of power -- measured on this build, Rs rose 27% in two
// hours, which dragged the reported concentration from 420 ppm down to 215 and
// pinned the air quality index at zero for as long as the drift continued.
// A baseline captured once at boot is therefore wrong within the hour.
//
// The correction rests on one physical fact: indoor air can never be cleaner
// than the outdoor air feeding it. Outdoor is the floor and occupants only add
// to it. So if the CLEANEST air seen across a whole window still reads below
// outdoor background, that is not clean air -- it is proof the baseline has
// drifted, and R0 is raised until that cleanest sample reads as outdoor again.
//
// This is what commercial NDIR CO2 sensors call automatic baseline correction,
// and the correction here is deliberately ONE-WAY. Raising R0 can only make
// the room look dirtier, so it can never manufacture a reassuring reading.
// Lowering it would require knowing the air was genuinely clean, which cannot
// be known from the sensor alone -- in a continuously occupied room a
// two-directional tracker walks the baseline down until everything reads
// "fresh". Downward correction stays a deliberate act: "mq135 cal".
#ifndef MQ135_ABC_ENABLE
#define MQ135_ABC_ENABLE 1
#endif
#ifndef MQ135_ABC_WINDOW_S
#define MQ135_ABC_WINDOW_S 600
#endif

// Temperature and humidity compensation for the MQ135's tin-dioxide element,
// whose conductivity depends strongly on both.
//
// Normally this is a reason to distrust these sensors, because the correction
// needs measurements the sensor cannot make.  Here the DHT22 is on the same
// board, so the correction can actually be applied -- worth roughly 16% on Rs
// in a hot, humid room, which is far from negligible.  Set to 0 to use the
// raw resistance instead.
#ifndef MQ135_COMPENSATE
#define MQ135_COMPENSATE 1
#endif

// Seconds of continuous power before the heater has stabilised enough for a
// calibration sample to mean anything.  The datasheet asks for 24-48 h of
// initial burn-in for absolute accuracy; three minutes is what it takes for
// readings to stop sliding within a session.
#ifndef MQ135_WARMUP_S
#define MQ135_WARMUP_S 180
#endif

// Sea-level pressure used to turn the BMP280's absolute pressure into an
// altitude, in pascals.  1013.25 hPa is the standard atmosphere; put your local
// QNH here if you want the altitude to mean anything.
#ifndef SEA_LEVEL_PA
#define SEA_LEVEL_PA 101325.0f
#endif

// How often the environment frame is emitted, in milliseconds.  The DHT22
// refuses to be read faster than once every 2 s and self-heats if pushed, and
// none of these quantities changes meaningfully inside a second anyway.
#ifndef ENV_INTERVAL_MS
#define ENV_INTERVAL_MS 3000
#endif

// --- CSI payload trimming --------------------------------------------------
// The radio reports all 64 LLTF sub-carriers, but 802.11 at 20 MHz only
// occupies +/-1..26.  Index 0 is the DC null and +/-27..32 are guard band:
// they carry no signal and the Pi discards them on arrival anyway.
//
// Dropping them at the node cuts the CSI payload from 128 to 104 bytes, which
// takes a whole fragment off every radio frame (6 -> 5) and therefore ~17% off
// the packet rate.  That matters because the nRF24's RX FIFO is only three
// deep: packets lost to FIFO overrun are acknowledged in hardware before
// software reads them, so auto-ack cannot recover them and fewer packets is
// the only real fix.
#ifndef TRIM_SUBCARRIERS
#define TRIM_SUBCARRIERS 1
#endif
