#include "cc1101_driver.h"

// --- register map ----------------------------------------------------------
#define R_IOCFG2 0x00
#define R_IOCFG0 0x02
#define R_FIFOTHR 0x03
#define R_SYNC1 0x04
#define R_SYNC0 0x05
#define R_PKTLEN 0x06
#define R_PKTCTRL1 0x07
#define R_PKTCTRL0 0x08
#define R_ADDR 0x09
#define R_CHANNR 0x0A
#define R_FSCTRL1 0x0B
#define R_FSCTRL0 0x0C
#define R_FREQ2 0x0D
#define R_FREQ1 0x0E
#define R_FREQ0 0x0F
#define R_MDMCFG4 0x10
#define R_MDMCFG3 0x11
#define R_MDMCFG2 0x12
#define R_MDMCFG1 0x13
#define R_MDMCFG0 0x14
#define R_DEVIATN 0x15
#define R_MCSM1 0x17
#define R_MCSM0 0x18
#define R_FOCCFG 0x19
#define R_BSCFG 0x1A
#define R_AGCCTRL2 0x1B
#define R_AGCCTRL1 0x1C
#define R_AGCCTRL0 0x1D
#define R_FREND1 0x21
#define R_FREND0 0x22
#define R_FSCAL3 0x23
#define R_FSCAL2 0x24
#define R_FSCAL1 0x25
#define R_FSCAL0 0x26
#define R_TEST2 0x2C
#define R_TEST1 0x2D
#define R_TEST0 0x2E

#define S_SRES 0x30
#define S_STX 0x35
#define S_SIDLE 0x36
#define S_SFTX 0x3B

#define ST_VERSION 0x31
#define ST_MARCSTATE 0x35

#define PATABLE 0x3E
#define TXFIFO 0x3F

#define WRITE_BURST 0x40
#define READ_SINGLE 0x80
#define READ_BURST 0xC0

#define MARC_IDLE 0x01
#define MARC_TX 0x13
#define MARC_TX_END 0x14

// SPI mode 0, MSB first.  1 MHz rather than the CC1101's 6.5 MHz ceiling:
// the same conservative clock that made the nRF24 reliable over dupont
// jumpers, and the link needs a tiny fraction of it.
static SPISettings kSpi(1000000, MSBFIRST, SPI_MODE0);

void CC1101::attach(SPIClass *spi, int cs) {
  spi_ = spi;
  cs_ = cs;
  pinMode(cs_, OUTPUT);
  digitalWrite(cs_, HIGH);
}

void CC1101::select() {
  spi_->beginTransaction(kSpi);
  digitalWrite(cs_, LOW);
  // The datasheet requires waiting for MISO to fall after CS goes low: that is
  // the chip signalling its crystal is stable and it is ready for a command.
  // Skipping this is the classic source of intermittent register corruption.
  uint32_t start = micros();
  while (digitalRead(MISO) && (micros() - start) < 5000) {
  }
}

void CC1101::deselect() {
  digitalWrite(cs_, HIGH);
  spi_->endTransaction();
}

void CC1101::writeReg(uint8_t addr, uint8_t value) {
  select();
  spi_->transfer(addr);
  spi_->transfer(value);
  deselect();
}

uint8_t CC1101::readReg(uint8_t addr) {
  select();
  spi_->transfer(addr | READ_SINGLE);
  uint8_t v = spi_->transfer(0x00);
  deselect();
  return v;
}

uint8_t CC1101::readStatus(uint8_t addr) {
  select();
  spi_->transfer(addr | READ_BURST);
  uint8_t v = spi_->transfer(0x00);
  deselect();
  return v;
}

void CC1101::strobe(uint8_t cmd) {
  select();
  spi_->transfer(cmd);
  deselect();
}

void CC1101::writeBurst(uint8_t addr, const uint8_t *src, uint8_t len) {
  select();
  spi_->transfer(addr | WRITE_BURST);
  for (uint8_t i = 0; i < len; i++) spi_->transfer(src[i]);
  deselect();
}

uint8_t CC1101::version() { return readStatus(ST_VERSION); }
uint8_t CC1101::marcState() { return readStatus(ST_MARCSTATE) & 0x1F; }
void CC1101::idle() { strobe(S_SIDLE); }

bool CC1101::begin() {
  if (!spi_) return false;

  // Manual power-on reset per the datasheet: CS high-low-high, settle, then
  // SRES.  Without this a chip that was left in an odd state (mid-TX, asleep,
  // or half-configured by another driver) never responds correctly.
  digitalWrite(cs_, HIGH);
  delayMicroseconds(30);
  digitalWrite(cs_, LOW);
  delayMicroseconds(30);
  digitalWrite(cs_, HIGH);
  delayMicroseconds(45);
  strobe(S_SRES);
  delay(10);

  uint8_t ver = version();
  if (ver == 0x00 || ver == 0xFF) return false;

  // 433.92 MHz, 26 MHz crystal.
  writeReg(R_FREQ2, 0x10);
  writeReg(R_FREQ1, 0xB0);
  writeReg(R_FREQ0, 0x71);

  // 38.4 kBaud, 101.6 kHz RX bandwidth, 20.6 kHz deviation -> modulation
  // index 1.07.  Identical values are written on the Pi so both ends are
  // provably the same; a mismatch here is silent and total.
  writeReg(R_MDMCFG4, 0xCA);
  writeReg(R_MDMCFG3, 0x83);
  writeReg(R_MDMCFG2, 0x02);  // 2-FSK, no Manchester, 16/16 sync
  writeReg(R_MDMCFG1, 0x42);  // 8-byte preamble, channel spacing exp 2
  writeReg(R_MDMCFG0, 0xF8);
  writeReg(R_DEVIATN, 0x35);

  writeReg(R_SYNC1, 0xD3);
  writeReg(R_SYNC0, 0x91);

  writeReg(R_PKTLEN, 0x3D);    // 61 bytes max
  writeReg(R_PKTCTRL1, 0x04);  // append RSSI/LQI, no address check
  writeReg(R_PKTCTRL0, 0x05);  // variable length, CRC on, no whitening
  writeReg(R_ADDR, 0x00);
  writeReg(R_CHANNR, 0x00);
  writeReg(R_FIFOTHR, 0x07);

  writeReg(R_FSCTRL1, 0x06);
  writeReg(R_FSCTRL0, 0x00);

  // Auto-calibrate when leaving IDLE, and return to IDLE after a packet so the
  // transmit path always starts from a known state.
  writeReg(R_MCSM0, 0x18);
  writeReg(R_MCSM1, 0x30);

  writeReg(R_FOCCFG, 0x16);
  writeReg(R_BSCFG, 0x6C);
  writeReg(R_AGCCTRL2, 0x43);
  writeReg(R_AGCCTRL1, 0x40);
  writeReg(R_AGCCTRL0, 0x91);

  writeReg(R_FREND1, 0x56);
  writeReg(R_FREND0, 0x10);
  writeReg(R_FSCAL3, 0xE9);
  writeReg(R_FSCAL2, 0x2A);
  writeReg(R_FSCAL1, 0x00);
  writeReg(R_FSCAL0, 0x1F);
  writeReg(R_TEST2, 0x81);
  writeReg(R_TEST1, 0x35);
  writeReg(R_TEST0, 0x09);

  writeReg(R_IOCFG2, 0x29);
  writeReg(R_IOCFG0, 0x06);

  // Maximum output power for the 433 MHz band.
  uint8_t pa = 0xC0;
  writeBurst(PATABLE, &pa, 1);

  strobe(S_SIDLE);
  delay(1);
  return version() == ver;
}

bool CC1101::send(const uint8_t *data, uint8_t len, uint32_t timeout_ms) {
  if (!spi_ || len == 0 || len > 61) return false;

  strobe(S_SIDLE);
  strobe(S_SFTX);

  // Length byte first, then the payload -- variable-length packet format.
  select();
  spi_->transfer(TXFIFO | WRITE_BURST);
  spi_->transfer(len);
  for (uint8_t i = 0; i < len; i++) spi_->transfer(data[i]);
  deselect();

  strobe(S_STX);

  // Poll the state machine rather than GDO0, which is not wired on this board.
  uint32_t start = millis();
  while (millis() - start < timeout_ms) {
    uint8_t st = marcState();
    if (st != MARC_TX && st != MARC_TX_END) break;
    delayMicroseconds(200);
  }
  strobe(S_SFTX);
  strobe(S_SIDLE);
  return true;
}
