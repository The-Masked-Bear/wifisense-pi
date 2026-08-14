#include "radio_tx.h"

#include <RF24.h>

#include "cc1101_driver.h"

#include "link_crypto.h"

static uint8_t g_link = LINK_USB;
static uint8_t g_frag_seq = 0;

static SPIClass g_spi(FSPI);
static bool g_spi_up = false;

// 1 MHz, not the library's 10 MHz default.  A bit-banged probe at ~250 kHz
// reads this chip perfectly while the library reported "no chip" at 10 MHz --
// the classic signature of SPI clocked faster than loose dupont jumpers can
// carry.  1 MHz is still ~8x more than the link needs: one CSI frame is five
// 32-byte packets, so 100 Hz is about 128 kbit/s of SPI traffic.
static RF24 g_nrf(NRF_CE, NRF_CSN, 1000000);
static CC1101 g_cc;

// Bring SPI2 up once and leave it up.  Both radios now share this single
// instance with their own chip selects, wrapped in begin/endTransaction, which
// is what SPI is for -- so there is no handover, and the class of bug where
// one driver end()s the bus out from under the other cannot occur.
static void spi_ensure() {
  if (!g_spi_up) {
    g_spi.begin(RADIO_SCK, RADIO_MISO, RADIO_MOSI, -1);
    g_spi_up = true;
  }
}

// Re-apply the SPI pin mapping after any bit-banged access.
//
// The diagnostics drive SCK/MOSI/MISO as plain GPIO, which detaches them from
// the SPI peripheral.  spi_ensure() alone will not put them back -- it sees the
// bus flagged as already up and does nothing -- so every hardware transaction
// afterwards clocks into dead pins.  That is why switching cc1101 -> nrf24
// hung: the warm-up probe silently unhooked the bus it was about to use.
static void spi_reclaim() {
  g_spi.end();
  g_spi.begin(RADIO_SCK, RADIO_MISO, RADIO_MOSI, -1);
  g_spi_up = true;
}

// Created during static construction, which on this core runs before setup()
// and after the FreeRTOS heap exists.  Doing it lazily on first lock would be
// a race the moment a second task appeared -- and one just did, in sensors.cpp.
static SemaphoreHandle_t g_bus_mutex = xSemaphoreCreateRecursiveMutex();

void radio_bus_lock() {
  if (g_bus_mutex) xSemaphoreTakeRecursive(g_bus_mutex, portMAX_DELAY);
}

void radio_bus_unlock() {
  if (g_bus_mutex) xSemaphoreGiveRecursive(g_bus_mutex);
}

// Scope guard, so an early return can never leave the bus locked.  Several of
// the functions below have half a dozen exit points.
namespace {
struct BusGuard {
  BusGuard() { radio_bus_lock(); }
  ~BusGuard() { radio_bus_unlock(); }
};
}  // namespace

SPIClass *radio_spi() {
  BusGuard guard;
  spi_ensure();
  return &g_spi;
}

// Must match DEFAULT_ADDRESS in wifisense/link/nrf24_link.py.
static const uint8_t NRF_ADDR[6] = "CSI01";

// ---------------------------------------------------------------------------
//  Probe helpers.  Defined first so every user below has them in scope.
//
//  All bit-banged, and they must NEVER touch g_spi: that instance belongs to
//  the RF24 driver, and having a diagnostic begin()/end() the same SPI host
//  left the peripheral in a state the library could not recover from -- a
//  read-only probe was breaking the very thing it existed to diagnose.
// ---------------------------------------------------------------------------

// Every GPIO usable on an ESP32-S3-WROOM-1 N16R8.  26-32 are the internal flash
// and 33-37 the octal PSRAM; probing those would hang the chip.
static const int SWEEP_PINS[] = {1,  2,  4,  5,  6,  7,  8,  9,  10, 11, 12, 13, 14,
                                 15, 16, 17, 18, 21, 38, 39, 40, 41, 42, 47, 48};
static const int SWEEP_N = sizeof(SWEEP_PINS) / sizeof(SWEEP_PINS[0]);

static uint8_t bb_read(int sck, int miso, int mosi, int cs, uint8_t addr) {
  pinMode(sck, OUTPUT);
  pinMode(mosi, OUTPUT);
  pinMode(cs, OUTPUT);
  pinMode(miso, INPUT_PULLDOWN);
  digitalWrite(cs, HIGH);
  digitalWrite(sck, LOW);
  delayMicroseconds(5);
  digitalWrite(cs, LOW);
  delayMicroseconds(5);
  for (int i = 7; i >= 0; i--) {
    digitalWrite(mosi, (addr >> i) & 1);
    delayMicroseconds(2);
    digitalWrite(sck, HIGH);
    delayMicroseconds(2);
    digitalWrite(sck, LOW);
    delayMicroseconds(2);
  }
  uint8_t v = 0;
  for (int i = 7; i >= 0; i--) {
    digitalWrite(sck, HIGH);
    delayMicroseconds(2);
    v |= (digitalRead(miso) & 1) << i;
    digitalWrite(sck, LOW);
    delayMicroseconds(2);
  }
  digitalWrite(cs, HIGH);
  pinMode(miso, INPUT);
  return v;
}

// Read a pin under an internal pull-up and again under a pull-down.  A floating
// pin follows the resistor, so the two disagree; a pin held by a powered chip
// reads the same both times.
static bool pin_is_driven(int pin) {
  pinMode(pin, INPUT_PULLUP);
  delayMicroseconds(300);
  int up = digitalRead(pin);
  pinMode(pin, INPUT_PULLDOWN);
  delayMicroseconds(300);
  int down = digitalRead(pin);
  pinMode(pin, INPUT);
  return up == down;
}

// nRF24 write-then-read-back on RX_ADDR_P2 (0x0B): a real chip echoes what was
// written, a floating bus cannot.
static bool nrf_writeread(int sck, int miso, int mosi, int cs, uint8_t *got) {
  auto xfer = [&](uint8_t v) -> uint8_t {
    uint8_t r = 0;
    for (int i = 7; i >= 0; i--) {
      digitalWrite(mosi, (v >> i) & 1);
      delayMicroseconds(2);
      digitalWrite(sck, HIGH);
      delayMicroseconds(1);
      r |= (digitalRead(miso) & 1) << i;
      delayMicroseconds(1);
      digitalWrite(sck, LOW);
      delayMicroseconds(2);
    }
    return r;
  };
  pinMode(sck, OUTPUT);
  pinMode(mosi, OUTPUT);
  pinMode(cs, OUTPUT);
  pinMode(miso, INPUT);
  digitalWrite(sck, LOW);
  digitalWrite(cs, HIGH);
  delayMicroseconds(20);

  const uint8_t magic = 0xC3;
  digitalWrite(cs, LOW);
  delayMicroseconds(5);
  xfer(0x20 | 0x0B);
  xfer(magic);
  digitalWrite(cs, HIGH);
  delayMicroseconds(20);

  digitalWrite(cs, LOW);
  delayMicroseconds(5);
  xfer(0x00 | 0x0B);
  uint8_t v = xfer(0xFF);
  digitalWrite(cs, HIGH);
  *got = v;
  return v == magic;
}

// ---------------------------------------------------------------------------
//  Link control
// ---------------------------------------------------------------------------

const char *radio_name(uint8_t mode) {
  switch (mode) {
    case LINK_NRF24: return "nrf24";
    case LINK_CC1101: return "cc1101";
    default: return "usb";
  }
}

static bool begin_nrf24() {
  // Put the bus in a defined idle state FIRST: chip-select deasserted, CE low,
  // clock low.  Straight out of reset these pins float, and the RF24 library
  // does not establish them before it probes -- which is why detection failed
  // on a fresh boot but succeeded if the bit-banged diagnostic (which does set
  // them) had run beforehand.  That difference is the whole bug.
  pinMode(NRF_CSN, OUTPUT);
  digitalWrite(NRF_CSN, HIGH);
  pinMode(NRF_CE, OUTPUT);
  digitalWrite(NRF_CE, LOW);
  pinMode(RADIO_SCK, OUTPUT);
  digitalWrite(RADIO_SCK, LOW);
  // The nRF24 needs ~100 ms after power-up before it answers reliably; the
  // ESP32 boots far faster than that.
  delay(120);

  // Then clock one complete bit-banged transaction before handing the bus to
  // the library.  This is empirical: detection failed reliably on a cold boot
  // and succeeded reliably if the bit-banged diagnostic had been run first,
  // and merely setting the idle pin levels was not enough to reproduce that.
  // A full byte clocked with CS asserted resynchronises the chip's SPI state
  // machine, which can come up mid-word after power-on and then misinterpret
  // every subsequent command.
  uint8_t warm = bb_read(RADIO_SCK, RADIO_MISO, RADIO_MOSI, NRF_CSN, 0x07);
  (void)warm;
  delay(5);
  spi_reclaim();   // the probe above detached the pins from the SPI peripheral

  spi_ensure();

  // Retry: the first transaction after power-up is the flaky one.
  bool up = false;
  for (int attempt = 0; attempt < 3 && !up; attempt++) {
    up = g_nrf.begin(&g_spi);
    if (!up) delay(50);
  }
  if (!up) return false;

  g_nrf.setChannel(NRF24_CHANNEL);
  g_nrf.setDataRate(RF24_2MBPS);
  g_nrf.setPALevel(RF24_PA_HIGH);
  g_nrf.setCRCLength(RF24_CRC_16);
  g_nrf.setPayloadSize(NRF_PAYLOAD);
  // Auto-ack ON with a short retry budget.  It was off originally on the
  // theory that acknowledging every fragment would cost throughput -- but a
  // 32-byte packet is only ~128 us at 2 Mbit/s, so even 600 packets/s is ~8%
  // duty and there is ample room to retry.  Without it a single lost fragment
  // discards its whole frame permanently, which measured as ~4.7% packet loss
  // compounding to ~25% frame loss.
  //
  // setRetries(1, 3): 500 us between attempts, at most 3 retries, so a packet
  // costs ~2 ms even in the worst case and a persistently deaf receiver cannot
  // stall the transmitter indefinitely.
  g_nrf.setAutoAck(true);
  g_nrf.setRetries(1, 3);
  g_nrf.openWritingPipe(NRF_ADDR);
  g_nrf.stopListening();

  for (int attempt = 0; attempt < 3; attempt++) {
    if (g_nrf.isChipConnected()) return true;
    delay(30);
  }
  return false;
}

static bool begin_cc1101() {
  spi_reclaim();
  g_cc.attach(&g_spi, CC_CSN);
  if (!g_cc.begin()) return false;
  // GDO pins are inputs only; nothing here depends on them.
  pinMode(CC_GDO0, INPUT);
  pinMode(CC_GDO2, INPUT);
  return true;
}

bool radio_begin(uint8_t mode) {
  BusGuard guard;
  // Re-selecting the mode already in use is a no-op.  Without this, switching
  // cc1101 -> cc1101 ran radio_stop() (which puts the chip to sleep) and then
  // tried to re-initialise a sleeping chip, hanging the command handler
  // forever: status frames kept flowing from their own task while the node
  // stopped answering commands entirely.
  if (mode == g_link) return true;

  radio_stop();
  g_link = LINK_USB;
  if (mode == LINK_NRF24) {
    // Warm the bus here, not in the caller.  This lives inside radio_begin so
    // that BOTH entry points get it -- the runtime "link" command and the
    // restore-from-NVS at boot.  Having it only on the command path meant the
    // node came up on USB after every reboot, silently undoing the setting it
    // had just been told to persist.
    char warm[220];
    radio_scan(warm, sizeof(warm));
    delay(10);
    if (!begin_nrf24()) return false;
  } else if (mode == LINK_CC1101) {
    if (!begin_cc1101()) return false;
  } else {
    return true;  // USB needs no radio
  }
  g_link = mode;
  return true;
}

void radio_stop() {
  BusGuard guard;
  if (g_link == LINK_NRF24) {
    g_nrf.powerDown();
  } else if (g_link == LINK_CC1101) {
    g_cc.idle();
  }
  g_link = LINK_USB;
}

// ---------------------------------------------------------------------------
//  Transmit
// ---------------------------------------------------------------------------

bool radio_send(const uint8_t *frame, size_t len) {
  BusGuard guard;
  if (g_link == LINK_USB || len == 0) return false;

  // Encrypt into a scratch buffer, prefixed with the 8-byte nonce header, then
  // fragment that.  Encryption happens BEFORE fragmentation so a listener
  // cannot even see frame boundaries in the payload, and after COBS so the
  // ciphertext's zero bytes are harmless -- the radio path is length-framed,
  // not zero-delimited.
  static uint8_t sealed[8 + MAX_CSI_BYTES + 96];
  const uint8_t *out = frame;
  size_t out_len = len;
  if (crypto_enabled()) {
    if (len + 8 > sizeof(sealed)) return false;
    memcpy(sealed + 8, frame, len);
    uint8_t header[8];
    if (crypto_encrypt(sealed + 8, len, header)) {
      memcpy(sealed, header, 8);
      out = sealed;
      out_len = len + 8;
    }
  }
  frame = out;
  len = out_len;

  const size_t packet = (g_link == LINK_NRF24) ? NRF_PAYLOAD : CC_PAYLOAD;
  const size_t body = packet - 2;
  const size_t total = (len + body - 1) / body;
  const uint8_t seq = g_frag_seq++;

  uint8_t buf[CC_PAYLOAD];
  for (size_t i = 0; i < total; i++) {
    const size_t off = i * body;
    size_t n = len - off;
    if (n > body) n = body;

    buf[0] = seq;
    buf[1] = (uint8_t)((i & 0x7F) | ((i == total - 1) ? 0x80 : 0x00));
    memcpy(buf + 2, frame + off, n);

    if (g_link == LINK_NRF24) {
      // Fixed payload size, so pad the final short fragment.  The receiver
      // trims each reassembled frame at its COBS terminator -- necessary
      // because once the stream is encrypted this padding decrypts to random
      // bytes rather than zeros, and would corrupt the following frame.
      if (n < body) memset(buf + 2 + n, 0, body - n);
      // multicast=false: request an acknowledgement for this packet.
      g_nrf.writeFast(buf, packet, false);
    } else {
      g_cc.send(buf, (uint8_t)(n + 2));
    }
  }

  if (g_link == LINK_NRF24) {
    // Short timeout, once per frame.  txStandBy blocks until the FIFO drains;
    // at 100 Hz the entire frame budget is 10 ms, so a longer wait does not
    // recover the packet, it only delays every frame behind it.
    g_nrf.txStandBy(3);
  }
  return true;
}

// ---------------------------------------------------------------------------
//  Diagnostics
// ---------------------------------------------------------------------------

void radio_scan(char *out, size_t n) {
  BusGuard guard;
  const bool had_spi = g_spi_up;
  uint8_t nrf_status = bb_read(RADIO_SCK, RADIO_MISO, RADIO_MOSI, NRF_CSN, 0x07);
  uint8_t nrf_config = bb_read(RADIO_SCK, RADIO_MISO, RADIO_MOSI, NRF_CSN, 0x00);
  uint8_t cc_part = bb_read(RADIO_SCK, RADIO_MISO, RADIO_MOSI, CC_CSN, 0x30 | 0xC0);
  uint8_t cc_ver = bb_read(RADIO_SCK, RADIO_MISO, RADIO_MOSI, CC_CSN, 0x31 | 0xC0);

  snprintf(out, n,
           "scan sck=%d miso=%d mosi=%d | nrf(cs=%d) status=0x%02X config=0x%02X %s"
           " | cc1101(cs=%d) part=0x%02X ver=0x%02X %s",
           RADIO_SCK, RADIO_MISO, RADIO_MOSI, NRF_CSN, nrf_status, nrf_config,
           (nrf_status != 0xFF && nrf_status != 0x00) ? "PRESENT" : "absent", CC_CSN,
           cc_part, cc_ver,
           (cc_ver == 0x14 || cc_ver == 0x04 || cc_ver == 0x17) ? "PRESENT" : "absent");
  if (had_spi) spi_reclaim();
}

// Dumps the CC1101 modem registers so they can be compared byte-for-byte with
// the receiver's.  Every one of these must match or the link is silent, and a
// mismatch is invisible from either side alone.
void radio_cc_regs(char *out, size_t n) {
  BusGuard guard;
  auto rd = [](uint8_t addr) { return bb_read(RADIO_SCK, RADIO_MISO, RADIO_MOSI, CC_CSN, addr | 0x80); };
  (void)0;
  snprintf(out, n,
           "cc regs MDMCFG4=0x%02X MDMCFG3=0x%02X MDMCFG2=0x%02X MDMCFG1=0x%02X "
           "DEVIATN=0x%02X SYNC1=0x%02X SYNC0=0x%02X PKTCTRL0=0x%02X PKTCTRL1=0x%02X "
           "ADDR=0x%02X FREQ2=0x%02X FREQ1=0x%02X FREQ0=0x%02X PATABLE=0x%02X FREND0=0x%02X",
           rd(0x10), rd(0x11), rd(0x12), rd(0x13), rd(0x15), rd(0x04), rd(0x05),
           rd(0x08), rd(0x07), rd(0x09), rd(0x0D), rd(0x0E), rd(0x0F),
           bb_read(RADIO_SCK, RADIO_MISO, RADIO_MOSI, CC_CSN, 0x3E | 0x80), rd(0x22));
}

void radio_pin_levels(void (*report)(const char *)) {
  BusGuard guard;
  struct {
    int pin;
    const char *what;
  } pins[] = {
      {NRF_IRQ, "nRF24 IRQ    (idles HIGH when powered)"},
      {CC_GDO0, "CC1101 GDO0  (driven when powered)"},
      {CC_GDO2, "CC1101 GDO2  (driven when powered)"},
      {RADIO_MISO, "shared MISO  (tri-state until CS asserted)"},
      {NRF_CSN, "nRF24 CSN    (we drive this)"},
      {CC_CSN, "CC1101 CSN   (we drive this)"},
  };
  char line[190];
  for (unsigned i = 0; i < sizeof(pins) / sizeof(pins[0]); i++) {
    int p = pins[i].pin;
    pinMode(p, INPUT_PULLUP);
    delayMicroseconds(400);
    int up = digitalRead(p);
    pinMode(p, INPUT_PULLDOWN);
    delayMicroseconds(400);
    int dn = digitalRead(p);
    pinMode(p, INPUT);
    const char *verdict = (up && dn)     ? "HELD HIGH -> chip powered"
                          : (!up && !dn) ? "HELD LOW  -> UNPOWERED (ESD clamp) or shorted"
                                         : "floating  -> nothing connected";
    snprintf(line, sizeof(line), "GPIO%-2d %-42s pu=%d pd=%d  %s", p, pins[i].what, up, dn,
             verdict);
    report(line);
  }
}

int radio_probe_pins(void (*report)(const char *)) {
  BusGuard guard;
  char line[190];
  int driven = 0;

  report("electrical probe: pull-up vs pull-down on every usable GPIO");
  String hits = "";
  for (int i = 0; i < SWEEP_N; i++) {
    int p = SWEEP_PINS[i];
    if (pin_is_driven(p)) {
      hits += String(p) + " ";
      driven++;
    }
  }
  snprintf(line, sizeof(line), "pins being driven by something external: %s",
           driven ? hits.c_str() : "(NONE -- no powered chip on any pin)");
  report(line);

  uint8_t got = 0;
  bool ok = nrf_writeread(RADIO_SCK, RADIO_MISO, RADIO_MOSI, NRF_CSN, &got);
  snprintf(line, sizeof(line),
           "nrf24 write/read-back on documented pins: wrote 0xC3 read 0x%02X -> %s", got,
           ok ? "REAL CHIP" : "no chip");
  report(line);
  return driven;
}

int radio_find_miso(void (*report)(const char *)) {
  BusGuard guard;
  char line[190];
  bool base[64] = {false};
  for (int i = 0; i < SWEEP_N; i++) base[SWEEP_PINS[i]] = pin_is_driven(SWEEP_PINS[i]);

  int found = 0;
  const int cs_candidates[] = {NRF_CSN, CC_CSN, 8, 7, 5, 4, 15, 16, 42, 41};
  const int n_cs = sizeof(cs_candidates) / sizeof(cs_candidates[0]);

  for (int c = 0; c < n_cs; c++) {
    int cs = cs_candidates[c];
    pinMode(cs, OUTPUT);
    digitalWrite(cs, HIGH);
    delayMicroseconds(200);
    digitalWrite(cs, LOW);
    delayMicroseconds(500);

    String woke = "";
    for (int i = 0; i < SWEEP_N; i++) {
      int p = SWEEP_PINS[i];
      if (p == cs) continue;
      if (!base[p] && pin_is_driven(p)) {
        woke += String(p) + " ";
        found++;
      }
    }
    digitalWrite(cs, HIGH);
    pinMode(cs, INPUT);

    if (woke.length()) {
      snprintf(line, sizeof(line), "CS=%d asserted -> pin(s) %s came alive (MISO candidate)", cs,
               woke.c_str());
      report(line);
    }
  }
  if (!found)
    report("no pin responded to any chip-select: CS not wired to a chip, or chip unpowered");
  return found;
}

int radio_sweep(void (*report)(const char *)) {
  BusGuard guard;
  char line[180];
  int hits = 0;

  snprintf(line, sizeof(line), "sweep: trying %d pins as MISO (sck/mosi/cs also swept on miss)",
           SWEEP_N);
  report(line);

  for (int m = 0; m < SWEEP_N; m++) {
    int miso = SWEEP_PINS[m];
    if (miso == RADIO_SCK || miso == RADIO_MOSI) continue;

    uint8_t ver = bb_read(RADIO_SCK, miso, RADIO_MOSI, CC_CSN, 0x31 | 0xC0);
    if (ver == 0x14 || ver == 0x04 || ver == 0x17) {
      snprintf(line, sizeof(line), "HIT cc1101 sck=%d mosi=%d cs=%d MISO=%d ver=0x%02X", RADIO_SCK,
               RADIO_MOSI, CC_CSN, miso, ver);
      report(line);
      hits++;
    }
    uint8_t st = bb_read(RADIO_SCK, miso, RADIO_MOSI, NRF_CSN, 0x07);
    if (st != 0x00 && st != 0xFF && (st & 0x80) == 0) {
      snprintf(line, sizeof(line), "HIT nrf24 sck=%d mosi=%d cs=%d MISO=%d status=0x%02X",
               RADIO_SCK, RADIO_MOSI, NRF_CSN, miso, st);
      report(line);
      hits++;
    }
  }
  return hits;
}
