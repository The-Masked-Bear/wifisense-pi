#pragma once

// Minimal, self-contained CC1101 driver.
//
// Written to replace the ELECHOUSE library, which assumes it owns the SPI bus:
// it drives the global Arduino `SPI` object and latches an internal
// "already initialised" flag, so once that bus is handed to the nRF24 it will
// never re-begin it.  On this board both radios share SPI2, and the result was
// that a switch from nRF24 to CC1101 either reported "no chip on SPI" or hung
// the command handler outright.
//
// This driver takes an SPIClass by reference and wraps every access in a
// begin/endTransaction pair with its own chip select, which is exactly what
// SPI is designed for -- so the two radios coexist with no handover, no global
// state, and no ownership assumptions.
//
// It also does not depend on GDO0.  The library's SendData() busy-waits on
// that pin for up to a second per packet, and GDO0 is not connected here.
// Transmit completion is detected by polling MARCSTATE instead.

#include <Arduino.h>
#include <SPI.h>

class CC1101 {
 public:
  // `spi` must already be begun.  `cs` is the chip-select GPIO.
  void attach(SPIClass *spi, int cs);

  // Full reset and configuration for 433.92 MHz, 2-FSK, 38.4 kBaud,
  // 20.6 kHz deviation, 101.6 kHz RX bandwidth, variable length + CRC,
  // 16/16 sync on 0xD391, 8-byte preamble.  Returns false if the chip does
  // not answer with a plausible VERSION.
  bool begin();

  // Transmit one packet (length byte is added automatically).  Blocks only for
  // as long as the packet actually takes, bounded by `timeout_ms`.
  bool send(const uint8_t *data, uint8_t len, uint32_t timeout_ms = 40);

  void idle();
  uint8_t version();
  uint8_t marcState();

  void writeReg(uint8_t addr, uint8_t value);
  uint8_t readReg(uint8_t addr);
  uint8_t readStatus(uint8_t addr);
  void strobe(uint8_t cmd);

 private:
  void select();
  void deselect();
  void writeBurst(uint8_t addr, const uint8_t *src, uint8_t len);

  SPIClass *spi_ = nullptr;
  int cs_ = -1;
};
