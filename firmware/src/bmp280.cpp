#include "bmp280.h"

namespace {

constexpr uint8_t REG_CALIB = 0x88;
constexpr uint8_t REG_ID = 0xD0;
constexpr uint8_t REG_RESET = 0xE0;
constexpr uint8_t REG_STATUS = 0xF3;
constexpr uint8_t REG_CTRL_MEAS = 0xF4;
constexpr uint8_t REG_CONFIG = 0xF5;
constexpr uint8_t REG_PRESS_MSB = 0xF7;

constexpr uint8_t ID_BMP280_S0 = 0x56;  // engineering samples, otherwise identical
constexpr uint8_t ID_BMP280_S1 = 0x57;
constexpr uint8_t ID_BMP280 = 0x58;
constexpr uint8_t ID_BME280 = 0x60;

// 8 MHz.  The part is rated to 10 MHz, and this bus is shared with the nRF24
// which is clocked at 1 MHz -- but each device sets its own speed inside its
// own transaction, so they do not have to agree.
const SPISettings kSpi(8000000, MSBFIRST, SPI_MODE0);

}  // namespace

// In SPI mode the register address carries the direction in bit 7: set to
// read, clear to write.  This is the opposite convention to the CC1101, which
// is a fine way to lose an afternoon if the two are confused.
uint8_t BMP280::readReg(uint8_t reg) {
  uint8_t v;
  readRegs(reg, &v, 1);
  return v;
}

void BMP280::readRegs(uint8_t reg, uint8_t *dst, size_t n) {
  spi_->beginTransaction(kSpi);
  digitalWrite(cs_, LOW);
  spi_->transfer((uint8_t)(reg | 0x80));
  for (size_t i = 0; i < n; i++) dst[i] = spi_->transfer(0x00);
  digitalWrite(cs_, HIGH);
  spi_->endTransaction();
}

void BMP280::writeReg(uint8_t reg, uint8_t value) {
  spi_->beginTransaction(kSpi);
  digitalWrite(cs_, LOW);
  spi_->transfer((uint8_t)(reg & 0x7F));
  spi_->transfer(value);
  digitalWrite(cs_, HIGH);
  spi_->endTransaction();
}

bool BMP280::begin(SPIClass *spi, int cs) {
  spi_ = spi;
  cs_ = cs;
  present_ = false;

  pinMode(cs_, OUTPUT);
  digitalWrite(cs_, HIGH);
  // The part powers up in I2C mode and only latches SPI on the first falling
  // edge of CS.  Give it a moment with CS idle high before talking to it, or
  // the first transaction is answered by a state machine that is still deciding
  // which interface it is.
  delay(5);

  chip_id_ = readReg(REG_ID);
  if (chip_id_ != ID_BMP280 && chip_id_ != ID_BMP280_S0 && chip_id_ != ID_BMP280_S1 &&
      chip_id_ != ID_BME280) {
    return false;
  }

  writeReg(REG_RESET, 0xB6);
  delay(10);

  // NVM copy runs after reset; reading trim before it completes yields zeros,
  // and a dig_T1 of zero makes every later temperature nonsense.
  for (int i = 0; i < 100 && (readReg(REG_STATUS) & 0x01); i++) delay(2);

  uint8_t c[24];
  readRegs(REG_CALIB, c, sizeof(c));
  dig_T1_ = (uint16_t)(c[0] | (c[1] << 8));
  dig_T2_ = (int16_t)(c[2] | (c[3] << 8));
  dig_T3_ = (int16_t)(c[4] | (c[5] << 8));
  dig_P1_ = (uint16_t)(c[6] | (c[7] << 8));
  dig_P2_ = (int16_t)(c[8] | (c[9] << 8));
  dig_P3_ = (int16_t)(c[10] | (c[11] << 8));
  dig_P4_ = (int16_t)(c[12] | (c[13] << 8));
  dig_P5_ = (int16_t)(c[14] | (c[15] << 8));
  dig_P6_ = (int16_t)(c[16] | (c[17] << 8));
  dig_P7_ = (int16_t)(c[18] | (c[19] << 8));
  dig_P8_ = (int16_t)(c[20] | (c[21] << 8));
  dig_P9_ = (int16_t)(c[22] | (c[23] << 8));

  // An all-zero or all-ones trim block means the reads are not landing, even
  // though the chip id happened to look plausible -- treat that as absent
  // rather than reporting confident garbage.
  if (dig_T1_ == 0 || dig_T1_ == 0xFFFF || dig_P1_ == 0 || dig_P1_ == 0xFFFF) return false;

  // config: 1000 ms standby, IIR filter coefficient 16, 4-wire SPI.
  //
  // The filter matters more than it looks.  Unfiltered, the pressure reading
  // twitches by tens of pascals sample to sample, which is several metres of
  // apparent altitude and makes the displayed number visibly unstable.  A
  // coefficient of 16 costs response time this application does not need --
  // room pressure does not step.
  writeReg(REG_CONFIG, (0x05 << 5) | (0x04 << 2));
  // ctrl_meas: temperature oversampling x2, pressure oversampling x16, normal
  // mode.  Pressure is oversampled harder because it is the noisier channel and
  // the one whose resolution is actually being used.
  writeReg(REG_CTRL_MEAS, (0x02 << 5) | (0x05 << 2) | 0x03);

  present_ = true;
  return true;
}

bool BMP280::read(float *temp_c, float *pressure_pa) {
  if (!present_) return false;

  uint8_t d[6];
  readRegs(REG_PRESS_MSB, d, sizeof(d));

  int32_t adc_P = ((int32_t)d[0] << 12) | ((int32_t)d[1] << 4) | ((int32_t)d[2] >> 4);
  int32_t adc_T = ((int32_t)d[3] << 12) | ((int32_t)d[4] << 4) | ((int32_t)d[5] >> 4);

  // 0x80000 is the reset value of both data registers: it means no conversion
  // has completed yet, not "measured zero".
  if (adc_T == 0x80000 || adc_P == 0x80000) return false;

  // Bosch's fixed-point compensation, transcribed from the BMP280 datasheet
  // section 3.11.3.  Deliberately kept in integer arithmetic exactly as
  // published rather than rewritten in floating point: the polynomial is
  // ill-conditioned and the published rounding is part of the specification.
  int32_t var1 = ((((adc_T >> 3) - ((int32_t)dig_T1_ << 1))) * ((int32_t)dig_T2_)) >> 11;
  int32_t var2 = (((((adc_T >> 4) - ((int32_t)dig_T1_)) * ((adc_T >> 4) - ((int32_t)dig_T1_))) >> 12) *
                  ((int32_t)dig_T3_)) >> 14;
  int32_t t_fine = var1 + var2;
  int32_t T = (t_fine * 5 + 128) >> 8;  // hundredths of a degree

  int64_t p1 = ((int64_t)t_fine) - 128000;
  int64_t p2 = p1 * p1 * (int64_t)dig_P6_;
  p2 = p2 + ((p1 * (int64_t)dig_P5_) << 17);
  p2 = p2 + (((int64_t)dig_P4_) << 35);
  p1 = ((p1 * p1 * (int64_t)dig_P3_) >> 8) + ((p1 * (int64_t)dig_P2_) << 12);
  p1 = (((((int64_t)1) << 47) + p1)) * ((int64_t)dig_P1_) >> 33;
  if (p1 == 0) return false;  // the datasheet's own guard against divide-by-zero
  int64_t p = 1048576 - adc_P;
  p = (((p << 31) - p2) * 3125) / p1;
  p1 = (((int64_t)dig_P9_) * (p >> 13) * (p >> 13)) >> 25;
  p2 = (((int64_t)dig_P8_) * p) >> 19;
  p = ((p + p1 + p2) >> 8) + (((int64_t)dig_P7_) << 4);  // Q24.8 pascals

  *temp_c = (float)T / 100.0f;
  *pressure_pa = (float)p / 256.0f;

  // Sanity band.  Anything outside this is a bus fault dressed up as a
  // reading: the lower bound is well below the summit of Everest and the upper
  // is well above any weather system.
  if (*pressure_pa < 30000.0f || *pressure_pa > 120000.0f) return false;
  if (*temp_c < -45.0f || *temp_c > 90.0f) return false;
  return true;
}
