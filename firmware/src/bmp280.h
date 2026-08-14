#pragma once

// Minimal BMP280 / BME280 driver over 4-wire SPI.
//
// Written in-tree for the same reason as cc1101_driver: the popular libraries
// (Adafruit_BMP280, SparkFun) take an SPIClass but then call begin() on it
// themselves, which re-initialises a bus that already belongs to the radios.
// This one is handed a bus that is already up and never touches its
// configuration -- it only asserts its own chip select inside a transaction.
//
// A BME280 answers on the same registers with the same layout for temperature
// and pressure, so it is accepted too; the humidity channel it adds is simply
// not read, because the DHT22 already covers humidity here.

#include <Arduino.h>
#include <SPI.h>

class BMP280 {
 public:
  // `spi` must already be begun.  Returns false if nothing on `cs` answers
  // with a recognised chip id.
  bool begin(SPIClass *spi, int cs);

  // Latest compensated reading.  Returns false if the part is not present or
  // stopped responding.
  bool read(float *temp_c, float *pressure_pa);

  uint8_t chipId() const { return chip_id_; }
  bool present() const { return present_; }

 private:
  uint8_t readReg(uint8_t reg);
  void readRegs(uint8_t reg, uint8_t *dst, size_t n);
  void writeReg(uint8_t reg, uint8_t value);

  SPIClass *spi_ = nullptr;
  int cs_ = -1;
  uint8_t chip_id_ = 0;
  bool present_ = false;

  // Factory trim, burned into every part at manufacture.  Without these the
  // raw ADC counts are meaningless -- the compensation polynomial they feed is
  // the entire difference between "23.4 C" and an arbitrary integer.
  uint16_t dig_T1_ = 0;
  int16_t dig_T2_ = 0, dig_T3_ = 0;
  uint16_t dig_P1_ = 0;
  int16_t dig_P2_ = 0, dig_P3_ = 0, dig_P4_ = 0, dig_P5_ = 0;
  int16_t dig_P6_ = 0, dig_P7_ = 0, dig_P8_ = 0, dig_P9_ = 0;
};
