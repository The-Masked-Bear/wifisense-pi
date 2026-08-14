#pragma once

// Environment sensing: BMP280 (pressure/temperature over SPI), DHT22
// (temperature/humidity, bit-banged) and MQ135 (gas, analogue).
//
// All three are sampled by one low-priority task so that nothing here can ever
// delay the CSI path, which is the only latency-sensitive thing on the node.
// The task publishes into a small struct behind a mutex; main.cpp reads that
// struct and emits it as a FRAME_ENV.
//
// The one place this touches shared hardware is the BMP280, which sits on the
// same SPI2 bus as the two radios with its own chip select.  Access is
// serialised through radio_bus_lock() rather than trusting the Arduino SPI
// HAL's internal locking, because the radio diagnostics bit-bang those same
// pins as plain GPIO and a transaction-level lock cannot see that coming.

#include <Arduino.h>

#include "config.h"

struct EnvReading {
  // Pressure and the BMP280's own temperature.
  float pressure_pa = 0.0f;
  float bmp_temp_c = 0.0f;
  float altitude_m = 0.0f;
  bool bmp_ok = false;

  // DHT22.
  float dht_temp_c = 0.0f;
  float humidity_pct = 0.0f;
  bool dht_ok = false;
  uint8_t dht_fail = 0;  // consecutive failed reads, saturating at 255

  // MQ135.
  uint16_t gas_mv = 0;      // voltage at the sensor's AO pin, divider undone
  float gas_rs = 0.0f;      // sensing element resistance, ohms
  float gas_ratio = 0.0f;   // Rs/R0
  float gas_ppm = 0.0f;     // CO2-equivalent estimate
  float gas_r0 = 0.0f;      // clean-air reference the ratio is taken against
  uint8_t gas_abc = 0;      // automatic baseline corrections applied, saturating
  bool gas_ok = false;
  bool gas_calibrated = false;

  uint32_t updated_ms = 0;
};

// Brings up whatever is compiled in and starts the sampling task.  Safe to
// call with nothing attached: each sensor independently reports itself absent.
void sensors_begin();

// Copies the most recent reading.  Cheap, lock-protected, never blocks long.
void sensors_get(EnvReading *out);

// One-line human-readable summary for the "env" command.
void sensors_describe(char *out, size_t n);

// Re-derives R0 from the current reading, treating the present air as clean.
// Returns false if the sensor is not ready or the reading is out of range.
bool sensors_calibrate_gas(char *out, size_t n);

// Switches the assumed MQ135 supply between 5 V (with a halving divider) and
// 3.3 V (direct).  Persisted, and invalidates the existing R0 because the
// resistance scale it was calibrated against has just changed.
bool sensors_set_gas_supply(bool five_volt, char *out, size_t n);
