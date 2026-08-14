#include "sensors.h"

#include <Preferences.h>
#include <freertos/FreeRTOS.h>
#include <freertos/semphr.h>
#include <freertos/task.h>
#include <math.h>
#include <soc/gpio_reg.h>

#include "bmp280.h"
#include "radio_tx.h"

// ---------------------------------------------------------------- shared state

static EnvReading g_env;
static SemaphoreHandle_t g_env_mutex = nullptr;
static Preferences g_env_prefs;

#if HAVE_BMP280
static BMP280 g_bmp;
#endif

#if HAVE_MQ135
// Clean-air resistance of the sensing element.  Zero means "never calibrated",
// which is reported honestly rather than silently substituting a datasheet
// typical -- part-to-part spread on these sensors is large enough that a
// borrowed R0 produces numbers that look precise and are not.
static float g_r0 = 0.0f;
static bool g_five_volt = (MQ135_VCC_MV >= 4000);

// Automatic baseline correction state.  g_rs_ema smooths the raw resistance so
// a single noisy sample cannot ratchet the baseline permanently; the window
// tracks the highest smoothed value seen, which is the cleanest air observed.
static float g_rs_ema = 0.0f;
static float g_rs_win_max = 0.0f;
static uint32_t g_abc_start = 0;
static uint16_t g_abc_count = 0;
#endif

// ---------------------------------------------------------------------- DHT22
#if HAVE_DHT22

static portMUX_TYPE g_dht_mux = portMUX_INITIALIZER_UNLOCKED;

// Read the pin straight out of the GPIO input register.
//
// digitalRead() would work, but it resolves a pin table and is not guaranteed
// to be in IRAM on every core version.  Inside a critical section a flash
// fetch can stall the CPU for microseconds if the other core happens to be
// reading flash at that moment -- and microseconds are exactly the units this
// protocol encodes its bits in.
static inline int IRAM_ATTR dht_level(int pin) {
  return pin < 32 ? (int)((REG_READ(GPIO_IN_REG) >> pin) & 1)
                  : (int)((REG_READ(GPIO_IN1_REG) >> (pin - 32)) & 1);
}

// Spin until the pin reaches `level`, returning how long that took in
// microseconds, or -1 on timeout.  The return value is the duration of the
// level we were *leaving*, which is what carries the data.
static inline int32_t IRAM_ATTR wait_level(int pin, int level, uint32_t timeout_us) {
  uint32_t start = (uint32_t)esp_timer_get_time();
  while (dht_level(pin) != level) {
    if ((uint32_t)esp_timer_get_time() - start > timeout_us) return -1;
  }
  return (int32_t)((uint32_t)esp_timer_get_time() - start);
}

// The timing-critical half, isolated so it can live in IRAM and hold a
// critical section for as short a window as possible.
//
// The critical section is unavoidable: the DHT22 encodes bits as pulse
// *widths* of 26 us versus 70 us, so a task switch in the middle of a bit
// rewrites the data.  It is affordable only because this task is pinned to
// core 1 while the WiFi driver -- and therefore the CSI callback -- runs on
// core 0, and portENTER_CRITICAL masks interrupts on the calling core alone.
// Pinned the other way round, this would punch a 5 ms hole in the CSI stream
// every few seconds.
static bool IRAM_ATTR dht22_capture(int pin, uint8_t *data) {
  portENTER_CRITICAL(&g_dht_mux);

  // Handshake: the sensor answers our start pulse by pulling low for ~80 us,
  // releasing for ~80 us, then pulling low again to begin the first bit.
  bool ok = wait_level(pin, LOW, 250) >= 0 && wait_level(pin, HIGH, 250) >= 0 &&
            wait_level(pin, LOW, 250) >= 0;

  if (ok) {
    for (int i = 0; i < 40; i++) {
      // Each bit is a fixed ~50 us low followed by a high whose length is the
      // value.  Only the high is measured; the low is just a separator.
      if (wait_level(pin, HIGH, 150) < 0) { ok = false; break; }
      int32_t high_us = wait_level(pin, LOW, 250);
      if (high_us < 0) { ok = false; break; }
      data[i >> 3] = (uint8_t)(data[i >> 3] << 1);
      // 45 us splits 26 and 70 with enormous margin either side, so loop
      // overhead and clock drift cannot reach the boundary.
      if (high_us > 45) data[i >> 3] |= 1;
    }
  }

  portEXIT_CRITICAL(&g_dht_mux);
  return ok;
}

static bool dht22_read(int pin, float *temp_c, float *hum) {
  uint8_t data[5] = {0, 0, 0, 0, 0};

  // Start signal: hold the line low for at least 1 ms, then let go.
  pinMode(pin, OUTPUT);
  digitalWrite(pin, LOW);
  delay(2);
  // Release to the pull-up rather than driving high.  The sensor replies by
  // pulling this same line down, and a push-pull high would be fighting it --
  // which on a 3.3 V part means contention current through both drivers.
  pinMode(pin, INPUT_PULLUP);

  if (!dht22_capture(pin, data)) return false;

  if ((uint8_t)(data[0] + data[1] + data[2] + data[3]) != data[4]) return false;

  uint16_t raw_h = (uint16_t)((data[0] << 8) | data[1]);
  uint16_t raw_t = (uint16_t)((data[2] << 8) | data[3]);

  *hum = raw_h / 10.0f;
  // Bit 15 of the temperature word is a sign flag, not part of the magnitude --
  // this is sign-and-magnitude, not two's complement, and treating it as the
  // latter turns -1.0 C into +3276.7 C.
  *temp_c = (raw_t & 0x8000) ? -((raw_t & 0x7FFF) / 10.0f) : (raw_t / 10.0f);

  if (*hum > 100.0f || *hum < 0.0f) return false;
  if (*temp_c < -41.0f || *temp_c > 81.0f) return false;
  return true;
}
#endif  // HAVE_DHT22

// ---------------------------------------------------------------------- MQ135
#if HAVE_MQ135

static float mq_divider() { return g_five_volt ? (float)(MQ135_DIVIDER) : 1.0f; }
static float mq_vcc_mv() { return g_five_volt ? 5000.0f : 3300.0f; }

// Reads AO and converts to the sensing element's resistance.
//
// The breakout wires its sensing element in series with a fixed load resistor
// to ground and taps the junction, so the pin voltage is a plain divider:
//
//     Vao = Vcc * RL / (Rs + RL)   =>   Rs = RL * (Vcc - Vao) / Vao
//
// Rs falls as gas concentration rises, so a *rising* voltage here means
// *worse* air.
static bool mq135_read(uint16_t *ao_mv_out, float *rs_out) {
  uint32_t sum = 0;
  // 16 samples averaged.  The MQ135's output is genuinely noisy at the
  // millivolt level and the ADC adds its own; a single read jitters by enough
  // to move the derived ppm by tens.
  for (int i = 0; i < 16; i++) sum += analogReadMilliVolts(MQ135_PIN);
  float adc_mv = (float)sum / 16.0f;

  float ao_mv = adc_mv * mq_divider();
  float vcc = mq_vcc_mv();
  *ao_mv_out = (uint16_t)lroundf(ao_mv);

  // Below a few millivolts the pin is floating, not measuring; above ~98% of
  // supply the divider maths blows up and the answer would be meaningless
  // regardless.
  if (ao_mv < 8.0f || ao_mv > vcc * 0.98f) return false;

  *rs_out = MQ135_RL_OHMS * (vcc - ao_mv) / ao_mv;
  return *rs_out > 0.0f && isfinite(*rs_out);
}

// Sensitivity curve, ppm = A * (Rs/R0)^-B.
//
// Treat the output as an index that moves the right way, not as a calibrated
// CO2 measurement.  The part responds to alcohol, smoke, ammonia and solvents
// as well, with no way to tell them apart from a single resistance.
static float mq135_ppm(float ratio) {
  if (ratio <= 0.0f) return 0.0f;
  float ppm = MQ135_CURVE_A * powf(ratio, -MQ135_CURVE_B);
  if (!isfinite(ppm) || ppm < 0.0f) return 0.0f;
  return ppm > 10000.0f ? 10000.0f : ppm;
}

// The resistance the element would show at atmospheric CO2.  Inverting the
// curve above at MQ135_ATMO_PPM is what ties R0 to the same convention the ppm
// calculation uses -- the whole point of this function existing rather than a
// constant ratio.
static float mq135_r0_from(float rs_clean) {
  return rs_clean * powf(MQ135_ATMO_PPM / MQ135_CURVE_A, 1.0f / MQ135_CURVE_B);
}

// Correction factor for the element's temperature and humidity dependence,
// the empirical fit in common use for this part.  Rs is divided by it.
//
// Returns 1.0 when there is no humidity reading to correct with, which is the
// honest fallback: guessing at ambient humidity would move Rs by more than the
// correction is worth.
static float mq135_correction(bool have_th, float temp_c, float humidity) {
#if MQ135_COMPENSATE
  if (!have_th) return 1.0f;
  if (humidity < 1.0f || humidity > 100.0f) return 1.0f;
  if (temp_c < -10.0f || temp_c > 60.0f) return 1.0f;
  float f = (temp_c < 20.0f)
                ? (0.00035f * temp_c * temp_c - 0.02718f * temp_c + 1.39538f -
                   (humidity - 33.0f) * 0.0018f)
                : (-0.0033333333f * temp_c - 0.0019230769f * humidity + 1.130128205f);
  // Anything outside this band is the fit being extrapolated past where it
  // means anything; applying it would do more harm than skipping it.
  if (!isfinite(f) || f < 0.3f || f > 2.0f) return 1.0f;
  return f;
#else
  (void)have_th; (void)temp_c; (void)humidity;
  return 1.0f;
#endif
}
#endif  // HAVE_MQ135

// ----------------------------------------------------------------- sample task

static void sensor_task(void *arg) {
  (void)arg;
  // Stagger the first sample so it does not land inside the boot burst of
  // status and log frames.
  vTaskDelay(pdMS_TO_TICKS(1500));

  EnvReading cur;
  for (;;) {
#if HAVE_BMP280
    {
      float t = 0.0f, p = 0.0f;
      // The BMP280 shares SPI2 with the radios, and the radio diagnostics
      // repurpose those pins as bit-banged GPIO.  Transaction-level locking in
      // the SPI HAL cannot protect against that; this lock can.
      radio_bus_lock();
      bool ok = g_bmp.read(&t, &p);
      radio_bus_unlock();
      cur.bmp_ok = ok;
      if (ok) {
        cur.bmp_temp_c = t;
        cur.pressure_pa = p;
        // International barometric formula, inverted for height.
        cur.altitude_m = 44330.0f * (1.0f - powf(p / (float)SEA_LEVEL_PA, 0.1902949f));
      }
    }
#endif

#if HAVE_DHT22
    {
      float t = 0.0f, h = 0.0f;
      bool ok = false;
      // Up to three attempts.  A single failed read is normal on this part --
      // it is a slow sensor with a marginal one-wire protocol -- and treating
      // one miss as a fault would show the user a broken sensor several times
      // an hour.
      for (int i = 0; i < 3 && !ok; i++) {
        if (i) vTaskDelay(pdMS_TO_TICKS(60));
        ok = dht22_read(DHT22_PIN, &t, &h);
      }
      cur.dht_ok = ok;
      if (ok) {
        cur.dht_temp_c = t;
        cur.humidity_pct = h;
        cur.dht_fail = 0;
      } else if (cur.dht_fail < 255) {
        cur.dht_fail++;
      }
    }
#endif

#if HAVE_MQ135
    {
      uint16_t mv = 0;
      float rs = 0.0f;
      bool ok = mq135_read(&mv, &rs);
      cur.gas_mv = mv;
      cur.gas_ok = ok;
      if (ok) {
        // Compensate with this node's own thermometer and hygrometer.  The
        // BMP280 is the better thermometer when it is present; humidity can
        // only come from the DHT22.
        float t_c = cur.bmp_ok ? cur.bmp_temp_c : cur.dht_temp_c;
        bool have_th = cur.dht_ok && (cur.bmp_ok || cur.dht_ok);
        rs /= mq135_correction(have_th, t_c, cur.humidity_pct);
        cur.gas_rs = rs;

        // Feed the baseline tracker.  Smoothed first: the baseline is only
        // ever ratcheted upward, so a single high outlier would raise it
        // permanently and there would be nothing to bring it back down.
        g_rs_ema = (g_rs_ema > 0.0f) ? (0.9f * g_rs_ema + 0.1f * rs) : rs;
        if (g_rs_ema > g_rs_win_max) g_rs_win_max = g_rs_ema;

        // Calibrate once the heater has settled, treating the air at that
        // moment as ordinary outdoor background.  Doing it automatically is
        // the only way this ever gets done -- but it is also why the reading
        // is an index rather than a measurement: calibrate in a stuffy room
        // and everything downstream is offset by however stuffy it was.
        if (g_r0 <= 0.0f && millis() > (uint32_t)MQ135_WARMUP_S * 1000UL) {
          g_r0 = mq135_r0_from(rs);
          g_env_prefs.begin("env", false);
          g_env_prefs.putFloat("r0b", g_r0);
          g_env_prefs.end();
          // Start the first correction window here rather than at boot, so it
          // measures a period during which a baseline actually existed.
          g_abc_start = millis();
          g_rs_win_max = 0.0f;
        }

#if MQ135_ABC_ENABLE
        // Once per window: if the cleanest air seen still reads below outdoor
        // background, the baseline is provably too low.  Raise it until that
        // cleanest sample reads as outdoor air again.
        if (g_r0 > 0.0f && g_rs_win_max > 0.0f &&
            millis() - g_abc_start >= (uint32_t)MQ135_ABC_WINDOW_S * 1000UL) {
          float target = mq135_r0_from(g_rs_win_max);
          // The 1% floor keeps ordinary jitter from writing flash every window
          // for a correction too small to see.
          if (target > g_r0 * 1.01f) {
            g_r0 = target;
            g_abc_count++;
            g_env_prefs.begin("env", false);
            g_env_prefs.putFloat("r0b", g_r0);
            g_env_prefs.end();
          }
          g_abc_start = millis();
          g_rs_win_max = 0.0f;
        }
#endif
        cur.gas_r0 = g_r0;
        cur.gas_abc = (uint8_t)(g_abc_count > 255 ? 255 : g_abc_count);
        if (g_r0 > 0.0f) {
          cur.gas_ratio = rs / g_r0;
          cur.gas_ppm = mq135_ppm(cur.gas_ratio);
          cur.gas_calibrated = true;
        } else {
          cur.gas_ratio = 0.0f;
          cur.gas_ppm = 0.0f;
          cur.gas_calibrated = false;
        }
      }
    }
#endif

    cur.updated_ms = millis();

    if (g_env_mutex) xSemaphoreTake(g_env_mutex, portMAX_DELAY);
    g_env = cur;
    if (g_env_mutex) xSemaphoreGive(g_env_mutex);

    vTaskDelay(pdMS_TO_TICKS(ENV_INTERVAL_MS));
  }
}

// --------------------------------------------------------------------- public

void sensors_begin() {
  g_env_mutex = xSemaphoreCreateMutex();

#if HAVE_MQ135
  g_env_prefs.begin("env", true);
  g_r0 = g_env_prefs.getFloat("r0b", 0.0f);
  g_five_volt = g_env_prefs.getBool("mq5v", MQ135_VCC_MV >= 4000);
  g_env_prefs.end();
  if (!(g_r0 > 0.0f) || !isfinite(g_r0)) g_r0 = 0.0f;

  // 11 dB of attenuation gives the full ~0-3.1 V input span.  The default is
  // 0 dB, which saturates at ~950 mV -- well inside the range this sensor
  // uses, so without this the reading pins to the ceiling and never moves.
  analogSetPinAttenuation(MQ135_PIN, ADC_11db);
  analogReadResolution(12);
#endif

#if HAVE_BMP280
  // radio_spi() brings SPI2 up if no radio has done so yet, which is the case
  // whenever the node is running over USB.
  radio_bus_lock();
  g_bmp.begin(radio_spi(), BMP280_CS);
  radio_bus_unlock();
#endif

  // Priority 1 and pinned to core 1.  Core 1 is essential, not cosmetic: the
  // DHT22 read masks interrupts on whichever core it runs on, and core 0 is
  // where the WiFi driver and the CSI callback live.
  xTaskCreatePinnedToCore(sensor_task, "env", 4096, nullptr, 1, nullptr, 1);
}

void sensors_get(EnvReading *out) {
  if (!out) return;
  if (g_env_mutex) xSemaphoreTake(g_env_mutex, portMAX_DELAY);
  *out = g_env;
  if (g_env_mutex) xSemaphoreGive(g_env_mutex);
}

void sensors_describe(char *out, size_t n) {
  EnvReading e;
  sensors_get(&e);

  char bmp[64] = "bmp280=absent";
#if HAVE_BMP280
  if (e.bmp_ok) {
    snprintf(bmp, sizeof(bmp), "bmp280=%.1fC %.1fhPa %.0fm", e.bmp_temp_c,
             e.pressure_pa / 100.0f, e.altitude_m);
  } else {
    snprintf(bmp, sizeof(bmp), "bmp280=%s", g_bmp.present() ? "read-fail" : "absent");
  }
#endif

  char dht[48] = "dht22=absent";
#if HAVE_DHT22
  if (e.dht_ok) {
    snprintf(dht, sizeof(dht), "dht22=%.1fC %.1f%%RH", e.dht_temp_c, e.humidity_pct);
  } else {
    snprintf(dht, sizeof(dht), "dht22=no-reply(%u)", (unsigned)e.dht_fail);
  }
#endif

  char mq[96] = "mq135=absent";
#if HAVE_MQ135
  if (!e.gas_ok) {
    snprintf(mq, sizeof(mq), "mq135=out-of-range(%umV)", (unsigned)e.gas_mv);
  } else if (e.gas_calibrated) {
    snprintf(mq, sizeof(mq), "mq135=%umV rs=%.0f r0=%.0f ratio=%.2f ~%.0fppm abc=%u %s",
             (unsigned)e.gas_mv, e.gas_rs, g_r0, e.gas_ratio, e.gas_ppm,
             (unsigned)g_abc_count, g_five_volt ? "5v" : "3v3");
  } else {
    snprintf(mq, sizeof(mq), "mq135=%umV rs=%.0f UNCALIBRATED (warming, %lus) %s",
             (unsigned)e.gas_mv, e.gas_rs, (unsigned long)(millis() / 1000), g_five_volt ? "5v" : "3v3");
  }
#endif

  snprintf(out, n, "%s | %s | %s", bmp, dht, mq);
}

bool sensors_calibrate_gas(char *out, size_t n) {
#if HAVE_MQ135
  EnvReading e;
  sensors_get(&e);
  if (!e.gas_ok || e.gas_rs <= 0.0f) {
    snprintf(out, n, "mq135 calibrate FAILED: no valid reading (%umV)", (unsigned)e.gas_mv);
    return false;
  }
  if (millis() < 60000UL) {
    snprintf(out, n, "mq135 calibrate FAILED: only %lus of warm-up, need 60s minimum",
             (unsigned long)(millis() / 1000));
    return false;
  }
  g_r0 = mq135_r0_from(e.gas_rs);
  g_rs_ema = e.gas_rs;
  g_rs_win_max = 0.0f;
  g_abc_start = millis();
  g_env_prefs.begin("env", false);
  g_env_prefs.putFloat("r0b", g_r0);
  g_env_prefs.end();
  snprintf(out, n, "mq135 calibrated: rs=%.0f -> r0=%.0f (this air is now the baseline)",
           e.gas_rs, g_r0);
  return true;
#else
  snprintf(out, n, "mq135 not compiled in");
  return false;
#endif
}

bool sensors_set_gas_supply(bool five_volt, char *out, size_t n) {
#if HAVE_MQ135
  g_five_volt = five_volt;
  // R0 was derived under the old supply assumption, so it is now meaningless.
  // Silently keeping it would leave every ppm figure wrong by a constant
  // factor with nothing on screen to say so.
  g_r0 = 0.0f;
  g_rs_ema = 0.0f;
  g_rs_win_max = 0.0f;
  g_abc_count = 0;
  g_env_prefs.begin("env", false);
  g_env_prefs.putBool("mq5v", five_volt);
  g_env_prefs.remove("r0b");
  g_env_prefs.end();
  snprintf(out, n, "mq135 supply=%s divider=%.1f -- R0 cleared, recalibrating after warm-up",
           five_volt ? "5v" : "3v3", (double)mq_divider());
  return true;
#else
  (void)five_volt;
  snprintf(out, n, "mq135 not compiled in");
  return false;
#endif
}
