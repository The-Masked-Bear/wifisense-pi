// ESP32-S3-WROOM-1 N16R8 -- WiFi CSI sensor node.
//
// Captures 802.11 Channel State Information and streams it to a Raspberry Pi
// over the CP2102 UART bridge using the COBS/CRC16 framing in protocol.py.
//
// Two structural decisions drive everything below.
//
// 1. The CSI callback runs inside the WiFi driver's own task.  Blocking there
//    -- and Serial.write() at 921600 baud very much blocks -- stalls the
//    driver and makes it drop packets at the radio, which surfaces downstream
//    as an irregular sample rate that quietly ruins the respiration FFT.  So
//    the callback only copies into a queue; a separate task drains it.
//
// 2. Mode and credentials are runtime state in NVS, not compile-time macros.
//    Which network to join is a deployment detail, and reflashing to change it
//    is a terrible workflow -- particularly since the board must be physically
//    reachable to reflash but only needs to be powered to be reconfigured.

#include <Arduino.h>
#include <Preferences.h>
#include <WiFi.h>
#include <WiFiUdp.h>
#include <esp_timer.h>
#include <esp_wifi.h>
#include <freertos/FreeRTOS.h>
#include <freertos/queue.h>
#include <freertos/semphr.h>
#include <freertos/task.h>

#include "config.h"
#include "link_crypto.h"
#include "radio_tx.h"
#include "sensors.h"

// ---------------------------------------------------------------- wire format

static const uint8_t FRAME_CSI = 0x01;
static const uint8_t FRAME_STATUS = 0x02;
static const uint8_t FRAME_LOG = 0x04;
static const uint8_t FRAME_ENV = 0x05;

typedef struct {
  uint32_t ts_us;
  int8_t rssi;
  int8_t noise_floor;
  uint8_t rate;
  uint8_t sig_mode;
  uint8_t mcs;
  uint8_t bandwidth;
  uint8_t channel;
  uint8_t secondary_channel;
  uint8_t antenna;
  uint8_t len;
  uint16_t seq;
  int8_t data[MAX_CSI_BYTES];
} csi_record_t;

static QueueHandle_t csi_queue = nullptr;

// ------------------------------------------------------------------ run state

static volatile uint32_t g_csi_count = 0;
static volatile uint32_t g_dropped = 0;
static volatile uint32_t g_tx_count = 0;
static volatile uint16_t g_seq = 0;
static volatile uint32_t g_rate_measured = 0;
static volatile bool g_streaming = true;
static volatile bool g_csi_ok = false;
static volatile uint8_t g_channel = 0;
static volatile uint16_t g_probe_hz = PROBE_HZ;
static volatile uint16_t g_stimulus_hz = STIMULUS_HZ;
static volatile uint8_t g_mode = MODE_SNIFFER;
static volatile uint8_t g_link_mode = DEFAULT_LINK;
static volatile uint32_t g_radio_frames = 0;

static Preferences g_prefs;
static String g_ssid, g_pass;

// -------------------------------------------------------------------- CRC-16

static uint16_t crc16_ccitt(const uint8_t *data, size_t len) {
  uint16_t crc = 0xFFFF;
  for (size_t i = 0; i < len; i++) {
    crc ^= (uint16_t)data[i] << 8;
    for (int b = 0; b < 8; b++) {
      crc = (crc & 0x8000) ? (uint16_t)((crc << 1) ^ 0x1021) : (uint16_t)(crc << 1);
    }
  }
  return crc;
}

// ---------------------------------------------------------------------- COBS

// Encodes so the output contains no 0x00, letting a single 0x00 delimit
// frames.  A receiver that joins mid-stream or hits corruption resynchronises
// by scanning to the next zero byte -- no escapes, no ambiguity.
static size_t cobs_encode(const uint8_t *in, size_t len, uint8_t *out) {
  size_t read = 0, write = 1, code_idx = 0;
  uint8_t code = 1;
  while (read < len) {
    if (in[read] == 0) {
      out[code_idx] = code;
      code_idx = write++;
      code = 1;
    } else {
      out[write++] = in[read];
      if (++code == 0xFF) {
        out[code_idx] = code;
        code_idx = write++;
        code = 1;
      }
    }
    read++;
  }
  out[code_idx] = code;
  return write;
}

static uint8_t g_frame_buf[MAX_CSI_BYTES + 64];
static uint8_t g_cobs_buf[MAX_CSI_BYTES + 96];
static SemaphoreHandle_t g_tx_mutex = nullptr;

// Called from three tasks (writer, status, command handler) that share one
// encode buffer, so it must be serialised.  Unserialised, the interleaving
// yields frames that are valid COBS but carry spliced payloads; the CRC then
// rejects them, so the symptom is silently vanishing status and log frames
// rather than anything that looks like a race.
static void send_frame(uint8_t type, const uint8_t *payload, size_t len) {
  if (len + 3 > sizeof(g_frame_buf)) return;
  if (g_tx_mutex) xSemaphoreTake(g_tx_mutex, portMAX_DELAY);
  g_frame_buf[0] = type;
  memcpy(g_frame_buf + 1, payload, len);
  uint16_t crc = crc16_ccitt(g_frame_buf, len + 1);
  g_frame_buf[len + 1] = (uint8_t)(crc & 0xFF);
  g_frame_buf[len + 2] = (uint8_t)(crc >> 8);
  size_t n = cobs_encode(g_frame_buf, len + 3, g_cobs_buf);
  g_cobs_buf[n++] = 0x00;

  // Copy out and release the lock BEFORE transmitting.  The mutex exists only
  // to protect the shared encode buffers; holding it across the write turns a
  // slow or stalled radio into a system-wide stall, because the status task and
  // the command handler both block on it and the node goes completely mute --
  // exactly when you most need to ask it what is wrong.
  static uint8_t out[MAX_CSI_BYTES + 96];
  uint8_t local[MAX_CSI_BYTES + 96];
  size_t out_n = n;
  memcpy(local, g_cobs_buf, n);
  (void)out;
  if (g_tx_mutex) xSemaphoreGive(g_tx_mutex);

  if (g_link_mode == LINK_USB) {
    Serial.write(local, out_n);
  } else if (type == FRAME_CSI) {
    if (radio_send(local, out_n)) g_radio_frames++;
  } else {
    // Status, log and environment frames always take the cable.
    Serial.write(local, out_n);
    // They are ALSO mirrored over the radio on the nRF24, because a genuinely
    // wireless deployment has no cable and the receiver needs the node's
    // address to aim its packet stimulus.
    //
    // Not on the CC1101: that driver's SendData() blocks until the packet has
    // been clocked out, and calling it from the status task stalled the task
    // outright -- the node fell silent one second after boot with no status and
    // no way to issue a command.  CSI still goes over the CC1101 from the
    // writer task, where blocking merely limits throughput instead of taking
    // the whole node down.
    if (g_link_mode == LINK_NRF24) radio_send(local, out_n);
  }
}

static void send_log(const char *msg) {
  send_frame(FRAME_LOG, (const uint8_t *)msg, strlen(msg));
}

// --------------------------------------------------------------- CSI capture

// Runs in the WiFi driver task.  Keep it short: copy and get out.
static void csi_callback(void *ctx, wifi_csi_info_t *info) {
  (void)ctx;
  if (!info || !info->buf || !g_streaming) return;

  const int8_t *src = info->buf;
  uint16_t len = info->len;

  if (len == 0) return;
  if (len > MAX_CSI_BYTES) len = MAX_CSI_BYTES;

  // Static, not stack: this record is ~400 bytes and the WiFi task's stack has
  // little headroom.  The driver invokes this callback serially from a single
  // task, so there is no reentrancy to guard against.
  static csi_record_t rec;
  const wifi_pkt_rx_ctrl_t *rx = &info->rx_ctrl;
  rec.ts_us = (uint32_t)esp_timer_get_time();
  rec.rssi = (int8_t)rx->rssi;
  rec.noise_floor = (int8_t)rx->noise_floor;
  rec.rate = (uint8_t)rx->rate;
  rec.sig_mode = (uint8_t)rx->sig_mode;
  rec.mcs = (uint8_t)rx->mcs;
  rec.bandwidth = (uint8_t)rx->cwb;
  rec.channel = (uint8_t)rx->channel;
  rec.secondary_channel = (uint8_t)rx->secondary_channel;
  rec.antenna = (uint8_t)rx->ant;
  rec.seq = g_seq++;

#if TRIM_SUBCARRIERS
  // Keep only the sub-carriers that carry signal.  The buffer is ordered
  // 0..+31 then -32..-1, two bytes each, so the occupied set +1..+26 and
  // -26..-1 lives at byte ranges [2,54) and [76,128) -- exactly the valid
  // ranges in Espressif's own sub-carrier table.
  if (len == 128) {
    memcpy(rec.data, src + 2, 52);
    memcpy(rec.data + 52, src + 76, 52);
    len = 104;
    // The first four bytes of a flagged capture are garbage, and they fall
    // inside the range copied above.  Zero that sub-carrier rather than
    // shifting the buffer: shifting would slide every later sub-carrier by two
    // positions and silently misalign the whole frequency axis.
    if (info->first_word_invalid) {
      rec.data[0] = 0;
      rec.data[1] = 0;
    }
  } else {
    memcpy(rec.data, src, len);
  }
#else
  memcpy(rec.data, src, len);
#endif
  rec.len = (uint8_t)len;

  // Never block.  A full queue means the writer is behind, and the right
  // response is to drop this sample and count it -- stalling the WiFi task
  // instead would corrupt the timing of every sample that follows.
  //
  // xQueueSend with zero timeout, not the FromISR variant: this is task
  // context, not an interrupt.
  if (xQueueSend(csi_queue, &rec, 0) != pdTRUE) {
    g_dropped++;
  } else {
    g_csi_count++;
  }
}

static bool start_csi() {
  wifi_csi_config_t cfg;
  memset(&cfg, 0, sizeof(cfg));
  // Legacy Long Training Field only: 64 sub-carriers, 128 bytes.  Enabling the
  // HT-LTF fields triples the wire rate for information these detectors do not
  // use.
  cfg.lltf_en = true;
  cfg.htltf_en = false;
  cfg.stbc_htltf2_en = false;
  cfg.ltf_merge_en = true;
  // Disabled deliberately: the channel filter smooths adjacent sub-carriers,
  // which destroys exactly the independence the spatial noise-floor estimate
  // depends on.  The Pi separates signal from noise by observing that channel
  // variation is correlated across sub-carriers while receiver noise is not --
  // pre-smoothing here would correlate the noise too and inflate the floor.
  cfg.channel_filter_en = false;
  cfg.manu_scale = false;
  cfg.shift = 0;
  // The whole STA strategy depends on this.  We transmit a UDP datagram at a
  // chosen rate and the AP answers each one with an 802.11 ACK -- that ACK is
  // the packet we want CSI from, because its timing is ours to control.  ACK
  // capture is disabled by default, so without this the only CSI that arrives
  // is the AP's ~1 Hz beacon and the sample rate collapses regardless of how
  // fast we transmit.
  cfg.dump_ack_en = true;

  if (esp_wifi_set_csi_config(&cfg) != ESP_OK) return false;
  if (esp_wifi_set_csi_rx_cb(csi_callback, nullptr) != ESP_OK) return false;
  return esp_wifi_set_csi(true) == ESP_OK;
}

// ------------------------------------------------------------ packet stimulus

static WiFiUDP g_udp;
static IPAddress g_target;

// Transmitting to the AP forces an 802.11 ACK back, and CSI is captured from
// that ACK.  This is what makes the sample rate ours to choose rather than
// whatever the network happens to be doing.  Port 9 is discard/RFC863, so
// nothing upstream cares about the contents.
static void stimulus_task(void *arg) {
  (void)arg;
  uint8_t payload[8] = {0xC5, 0x11, 0, 0, 0, 0, 0, 0};
  while (true) {
    uint16_t hz = g_stimulus_hz;
    if (g_mode != MODE_STA || hz == 0 || WiFi.status() != WL_CONNECTED || !g_streaming) {
      vTaskDelay(pdMS_TO_TICKS(200));
      continue;
    }
    g_udp.beginPacket(g_target, 9);
    g_udp.write(payload, sizeof(payload));
    g_udp.endPacket();
    g_tx_count++;
    // vTaskDelay, deliberately not vTaskDelayUntil.  DelayUntil keeps an
    // absolute phase, so whenever the WiFi driver holds this task off it then
    // fires back-to-back to catch up -- and once it slips a full period behind
    // it never recovers, degenerating into a busy loop that transmits at
    // hundreds of Hz and starves the receiver of the very CSI we want.
    uint32_t ms = 1000u / hz;
    vTaskDelay(pdMS_TO_TICKS(ms ? ms : 1));
  }
}

// A wildcard probe request: broadcast destination, zero-length SSID element,
// basic rate set.  Every AP on the channel must answer one of these, which is
// the only way to raise the sample rate in sniffer mode without associating.
static uint8_t g_probe[] = {
    0x40, 0x00,                          // frame control: mgmt, probe request
    0x00, 0x00,                          // duration
    0xff, 0xff, 0xff, 0xff, 0xff, 0xff,  // addr1 destination: broadcast
    0x00, 0x00, 0x00, 0x00, 0x00, 0x00,  // addr2 source: filled in at boot
    0xff, 0xff, 0xff, 0xff, 0xff, 0xff,  // addr3 BSSID: broadcast
    0x00, 0x00,                          // sequence control
    0x00, 0x00,                          // IE 0 (SSID), length 0 -> wildcard
    0x01, 0x08, 0x82, 0x84, 0x8b, 0x96,  // IE 1 (supported rates)
    0x0c, 0x12, 0x18, 0x24,
};

static void probe_task(void *arg) {
  (void)arg;
  uint8_t mac[6];
  esp_wifi_get_mac(WIFI_IF_STA, mac);
  memcpy(g_probe + 10, mac, 6);

  while (true) {
    uint16_t hz = g_probe_hz;
    if (g_mode != MODE_SNIFFER || hz == 0 || !g_streaming) {
      vTaskDelay(pdMS_TO_TICKS(200));
      continue;
    }
    esp_wifi_80211_tx(WIFI_IF_STA, g_probe, sizeof(g_probe), true);
    g_tx_count++;
    uint32_t ms = 1000u / hz;
    vTaskDelay(pdMS_TO_TICKS(ms ? ms : 1));
  }
}

// ------------------------------------------------------------- serial writer

static void writer_task(void *arg) {
  (void)arg;
  csi_record_t rec;
  uint8_t payload[16 + MAX_CSI_BYTES];
  while (true) {
    if (xQueueReceive(csi_queue, &rec, portMAX_DELAY) == pdTRUE) {
      memcpy(payload + 0, &rec.ts_us, 4);
      payload[4] = (uint8_t)rec.rssi;
      payload[5] = (uint8_t)rec.noise_floor;
      payload[6] = rec.rate;
      payload[7] = rec.sig_mode;
      payload[8] = rec.mcs;
      payload[9] = rec.bandwidth;
      payload[10] = rec.channel;
      payload[11] = rec.secondary_channel;
      payload[12] = rec.antenna;
      payload[13] = rec.len;
      memcpy(payload + 14, &rec.seq, 2);
      memcpy(payload + 16, rec.data, rec.len);
      send_frame(FRAME_CSI, payload, 16 + rec.len);
      // Guarantee a yield every frame.  The CC1101 driver polls its status
      // register without sleeping, so without this the writer can hold the core
      // indefinitely on a busy or misconfigured radio.
      vTaskDelay(1);
    }
  }
}

// ---------------------------------------------------------- environment frame

// Scales a float into a fixed-point integer, saturating instead of wrapping.
// A humidity of 101.2% is a sensor glitch; wrapping it to 0 would turn that
// glitch into a plausible-looking reading, which is far worse than a pegged
// one the Pi can recognise.
static uint16_t clamp_u16(float v, float scale) {
  float s = v * scale;
  if (!(s > 0.0f)) return 0;
  return s > 65535.0f ? 65535 : (uint16_t)lroundf(s);
}

static int16_t clamp_i16(float v, float scale) {
  float s = v * scale;
  if (s > 32767.0f) return 32767;
  if (s < -32768.0f) return -32768;
  return (int16_t)lroundf(s);
}

// Emitted once per status tick -- deliberately more often than the sensors are
// sampled, so each reading is transmitted about three times, a second apart.
//
// This is redundancy against radio loss, and it is not paranoia.  Measured on
// this link: with the CSI stream idle, every environment frame arrives; with
// CSI running at 100 Hz the radio is carrying ~500 packets a second and roughly
// two thirds of environment frames are lost in the contention.  That loss rate
// barely matters to CSI, where a hundred frames a second means a dropped one is
// invisible -- but an environment frame arrives every three seconds, so losing
// two in three means the dashboard shows air that is fifteen seconds stale.
//
// Retransmitting costs six extra packets every three seconds against about
// fifteen hundred, roughly 0.4% of the link.  Spacing the copies a second apart
// rather than sending them back to back is the point: loss here is bursty, and
// three copies emitted together would be lost together.
//
// The receiver discards repeats by the node_ms field, so the duplicates never
// reach the UI or the history.
static void send_env_frame(const EnvReading &e) {
  uint8_t buf[31];
  size_t o = 0;
  uint32_t ts = e.updated_ms;
  uint32_t pressure = (uint32_t)(e.pressure_pa > 0.0f ? lroundf(e.pressure_pa) : 0);
  uint32_t rs = (uint32_t)(e.gas_rs > 0.0f && e.gas_rs < 4.0e9f ? lroundf(e.gas_rs) : 0);
  // R0 travels with every frame so the baseline the index rests on is visible
  // from the dashboard.  Without it the only way to see whether automatic
  // baseline correction had done anything was a serial cable -- and opening
  // that port resets the board, which restarts the very window being observed.
  uint32_t r0 = (uint32_t)(e.gas_r0 > 0.0f && e.gas_r0 < 4.0e9f ? lroundf(e.gas_r0) : 0);

  memcpy(buf + o, &ts, 4); o += 4;
  memcpy(buf + o, &pressure, 4); o += 4;
  memcpy(buf + o, &rs, 4); o += 4;
  memcpy(buf + o, &r0, 4); o += 4;
  int16_t bt = clamp_i16(e.bmp_temp_c, 100.0f); memcpy(buf + o, &bt, 2); o += 2;
  int16_t dt = clamp_i16(e.dht_temp_c, 100.0f); memcpy(buf + o, &dt, 2); o += 2;
  uint16_t rh = clamp_u16(e.humidity_pct, 100.0f); memcpy(buf + o, &rh, 2); o += 2;
  uint16_t mv = e.gas_mv; memcpy(buf + o, &mv, 2); o += 2;
  uint16_t ratio = clamp_u16(e.gas_ratio, 1000.0f); memcpy(buf + o, &ratio, 2); o += 2;
  uint16_t ppm = clamp_u16(e.gas_ppm, 1.0f); memcpy(buf + o, &ppm, 2); o += 2;

  uint8_t flags = (e.bmp_ok ? 0x01 : 0) | (e.dht_ok ? 0x02 : 0) | (e.gas_ok ? 0x04 : 0) |
                  (e.gas_calibrated ? 0x08 : 0);
  buf[o++] = flags;
  buf[o++] = e.dht_fail;
  buf[o++] = e.gas_abc;

  send_frame(FRAME_ENV, buf, o);
}

// --------------------------------------------------------------- status task

static void status_task(void *arg) {
  (void)arg;
  uint32_t last_count = 0;
  uint32_t last_ms = millis();
  uint8_t buf[31];
  while (true) {
    vTaskDelay(pdMS_TO_TICKS(STATUS_INTERVAL_MS));

    {
      EnvReading env;
      sensors_get(&env);
      if (env.updated_ms) send_env_frame(env);
    }

    uint32_t now = millis();
    uint32_t count = g_csi_count;
    uint32_t dt = now - last_ms;
    // Measured, not configured: this is how the host learns whether the link
    // is actually delivering the rate that was asked for.
    g_rate_measured = dt ? ((count - last_count) * 1000) / dt : 0;
    last_count = count;
    last_ms = now;

    uint32_t uptime = now;
    uint32_t heap = ESP.getFreeHeap();
    int16_t rssi = (WiFi.status() == WL_CONNECTED) ? (int16_t)WiFi.RSSI() : 0;
    uint8_t primary = 0;
    wifi_second_chan_t sec;
    if (esp_wifi_get_channel(&primary, &sec) == ESP_OK) g_channel = primary;

    // Bit 0 associated, bit 1 CSI enabled, bit 2 credentials stored.
    uint8_t wifi_state = ((WiFi.status() == WL_CONNECTED) ? 1 : 0) | (g_csi_ok ? 2 : 0) |
                         (g_ssid.length() ? 4 : 0);
    uint16_t rate = (uint16_t)g_rate_measured;

    size_t o = 0;
    memcpy(buf + o, &uptime, 4); o += 4;
    memcpy(buf + o, &count, 4); o += 4;
    uint32_t tx = g_tx_count; memcpy(buf + o, &tx, 4); o += 4;
    uint32_t drop = g_dropped; memcpy(buf + o, &drop, 4); o += 4;
    memcpy(buf + o, &heap, 4); o += 4;
    memcpy(buf + o, &rssi, 2); o += 2;
    buf[o++] = g_channel;
    buf[o++] = wifi_state;
    memcpy(buf + o, &rate, 2); o += 2;
    buf[o++] = g_mode;
    // The Pi generates the packet stream that produces CSI, so it needs to
    // know where to aim it.  Shipping the IP in every status frame keeps the
    // system self-configuring across DHCP lease changes.
    uint32_t ip = (uint32_t)WiFi.localIP();
    memcpy(buf + o, &ip, 4); o += 4;
    send_frame(FRAME_STATUS, buf, o);
  }
}

// ----------------------------------------------------------------- radio setup

// CSI arrives via its own callback; this exists only because promiscuous mode
// requires a receive callback to be installed before it can be enabled.
static void promiscuous_cb(void *buf, wifi_promiscuous_pkt_type_t type) {
  (void)buf;
  (void)type;
}

static uint8_t pick_busiest_channel() {
  // Each AP beacons ~10x/second, so AP count is a direct proxy for the sample
  // rate a passive listener can expect.
  uint8_t channel = 6;
  int n = WiFi.scanNetworks(false, true);
  if (n > 0) {
    int votes[15] = {0};
    for (int i = 0; i < n; i++) {
      int c = WiFi.channel(i);
      if (c >= 1 && c <= 13) votes[c]++;
    }
    int best = 0;
    const int candidates[3] = {1, 6, 11};
    for (int i = 0; i < 3; i++) {
      int c = candidates[i];
      // 2.4 GHz channels overlap, so neighbours' beacons are still partly
      // received; count them at reduced weight.
      int score = votes[c] * 2;
      for (int d = 1; d <= 2; d++) {
        if (c - d >= 1) score += votes[c - d];
        if (c + d <= 13) score += votes[c + d];
      }
      if (score > best) {
        best = score;
        channel = (uint8_t)c;
      }
    }
  }
  WiFi.scanDelete();
  return channel;
}

static void enter_sniffer_mode() {
  g_mode = MODE_SNIFFER;
  esp_wifi_set_csi(false);
  esp_wifi_set_promiscuous(false);
  WiFi.disconnect(true);
  WiFi.mode(WIFI_STA);
  WiFi.setSleep(false);

  uint8_t channel = SNIFFER_CHANNEL ? SNIFFER_CHANNEL : pick_busiest_channel();

  esp_wifi_set_promiscuous(true);
  esp_wifi_set_promiscuous_rx_cb(promiscuous_cb);
  // Without an explicit filter the driver delivers a narrower set than we
  // want.  MGMT carries the beacons that act as a metronome; DATA adds
  // whatever the neighbours are actually transferring.
  wifi_promiscuous_filter_t filter;
  memset(&filter, 0, sizeof(filter));
  filter.filter_mask = WIFI_PROMIS_FILTER_MASK_MGMT | WIFI_PROMIS_FILTER_MASK_DATA;
  esp_wifi_set_promiscuous_filter(&filter);
  esp_wifi_set_channel(channel, WIFI_SECOND_CHAN_NONE);
  g_channel = channel;

  g_csi_ok = start_csi();
}

static bool enter_sta_mode(uint32_t timeout_ms = 20000) {
  g_mode = MODE_STA;
  // Promiscuous mode and association are mutually exclusive in the driver;
  // leaving promiscuous on here is precisely what silences the CSI callback.
  esp_wifi_set_csi(false);
  esp_wifi_set_promiscuous(false);

  WiFi.mode(WIFI_STA);
  WiFi.setSleep(false);  // power save gates the receiver and gaps the CSI
  WiFi.begin(g_ssid.c_str(), g_pass.c_str());

  uint32_t start = millis();
  while (WiFi.status() != WL_CONNECTED && millis() - start < timeout_ms) {
    delay(200);
  }
  if (WiFi.status() != WL_CONNECTED) return false;

  g_target = WiFi.gatewayIP();
  g_udp.begin(9);
  uint8_t primary = 0;
  wifi_second_chan_t sec;
  if (esp_wifi_get_channel(&primary, &sec) == ESP_OK) g_channel = primary;

  g_csi_ok = start_csi();
  return true;
}

// ------------------------------------------------------------------- commands

static void handle_command(const String &cmd) {
  if (cmd == "ping") {
    send_log("pong");
  } else if (cmd == "info") {
    char msg[192];
    snprintf(msg, sizeof(msg),
             "mode=%s csi=%s ch=%u rate=%luHz link=%s radio_tx=%lu enc=%s assoc=%s ssid=%s ip=%s heap=%lu",
             g_mode == MODE_STA ? "sta" : "sniffer", g_csi_ok ? "ok" : "FAILED",
             (unsigned)g_channel, (unsigned long)g_rate_measured,
             radio_name(g_link_mode), (unsigned long)g_radio_frames,
             crypto_enabled() ? "aes128" : "off",
             WiFi.status() == WL_CONNECTED ? "yes" : "no",
             g_ssid.length() ? g_ssid.c_str() : "(none)",
             WiFi.localIP().toString().c_str(), (unsigned long)ESP.getFreeHeap());
    send_log(msg);
  } else if (cmd == "start") {
    g_streaming = true;
    send_log("streaming on");
  } else if (cmd == "stop") {
    g_streaming = false;
    send_log("streaming off");
  } else if (cmd == "reset") {
    g_csi_count = 0;
    g_dropped = 0;
    g_tx_count = 0;
    send_log("counters reset");
  } else if (cmd.startsWith("wifi ")) {
    // "wifi <ssid> <password>" -- password may contain spaces, SSID may not.
    // Stored in NVS so the node keeps them across reboots and power cycles.
    String rest = cmd.substring(5);
    int sp = rest.indexOf(' ');
    if (sp <= 0) {
      send_log("usage: wifi <ssid> <password>");
      return;
    }
    g_ssid = rest.substring(0, sp);
    g_pass = rest.substring(sp + 1);
    g_prefs.begin("csi", false);
    g_prefs.putString("ssid", g_ssid);
    g_prefs.putString("pass", g_pass);
    g_prefs.end();
    send_log("credentials saved, connecting");
    if (enter_sta_mode()) {
      char msg[160];
      snprintf(msg, sizeof(msg), "connected ssid=%s ip=%s ch=%u csi=%s", g_ssid.c_str(),
               WiFi.localIP().toString().c_str(), (unsigned)g_channel,
               g_csi_ok ? "ok" : "FAILED");
      send_log(msg);
    } else {
      send_log("connect FAILED, falling back to sniffer");
      enter_sniffer_mode();
    }
  } else if (cmd == "env") {
    char msg[240];
    sensors_describe(msg, sizeof(msg));
    send_log(msg);
  } else if (cmd == "mq135 cal") {
    char msg[160];
    sensors_calibrate_gas(msg, sizeof(msg));
    send_log(msg);
  } else if (cmd == "mq135 supply 5v" || cmd == "mq135 supply 3v3") {
    char msg[160];
    sensors_set_gas_supply(cmd.endsWith("5v"), msg, sizeof(msg));
    send_log(msg);
  } else if (cmd.startsWith("mq135")) {
    send_log("usage: mq135 cal | mq135 supply 5v | mq135 supply 3v3");
  } else if (cmd == "ccregs") {
    char msg[240];
    radio_cc_regs(msg, sizeof(msg));
    send_log(msg);
  } else if (cmd == "levels") {
    radio_pin_levels([](const char *m) { send_log(m); });
    send_log("levels done");
  } else if (cmd == "findmiso") {
    send_log("asserting each candidate CS, watching for a pin to start driving");
    radio_find_miso([](const char *m) { send_log(m); });
    send_log("findmiso done");
  } else if (cmd == "pinprobe") {
    radio_probe_pins([](const char *m) { send_log(m); });
  } else if (cmd == "radiosweep") {
    send_log("sweeping every usable GPIO for a CC1101 or nRF24 -- ~20 s");
    int hits = radio_sweep([](const char *m) { send_log(m); });
    char msg[100];
    snprintf(msg, sizeof(msg), "sweep done: %d module(s) found on any pin combination", hits);
    send_log(msg);
  } else if (cmd == "radioscan") {
    char msg[220];
    radio_scan(msg, sizeof(msg));
    send_log(msg);
  } else if (cmd.startsWith("link ")) {
    String want = cmd.substring(5);
    want.trim();
    uint8_t mode = want == "nrf24" ? LINK_NRF24 : want == "cc1101" ? LINK_CC1101
                 : want == "usb" ? LINK_USB : 0xFF;
    if (mode == 0xFF) {
      send_log("usage: link usb|nrf24|cc1101");
    } else if (mode == LINK_USB) {
      radio_stop();
      g_link_mode = LINK_USB;
      g_prefs.begin("csi", false); g_prefs.putUChar("link", mode); g_prefs.end();
      send_log("link=usb");
    } else if (radio_begin(mode)) {
      g_link_mode = mode;
      g_prefs.begin("csi", false); g_prefs.putUChar("link", mode); g_prefs.end();
      char msg[64];
      snprintf(msg, sizeof(msg), "link=%s ok", radio_name(mode));
      send_log(msg);
    } else {
      char msg[80];
      snprintf(msg, sizeof(msg), "link=%s FAILED (no chip on SPI?), staying on %s",
               radio_name(mode), radio_name(g_link_mode));
      send_log(msg);
    }
  } else if (cmd == "forget") {
    g_prefs.begin("csi", false);
    g_prefs.clear();
    g_prefs.end();
    g_ssid = "";
    g_pass = "";
    send_log("credentials cleared");
  } else if (cmd == "mode sta") {
    if (!g_ssid.length()) {
      send_log("no credentials; use: wifi <ssid> <password>");
    } else if (enter_sta_mode()) {
      send_log("sta mode ok");
    } else {
      send_log("sta connect failed");
      enter_sniffer_mode();
    }
  } else if (cmd == "mode sniffer") {
    enter_sniffer_mode();
    send_log("sniffer mode ok");
  } else if (cmd.startsWith("rate ")) {
    int hz = cmd.substring(5).toInt();
    if (hz >= 0 && hz <= 400) {
      g_stimulus_hz = (uint16_t)hz;
      send_log("stimulus rate set");
    }
  } else if (cmd.startsWith("probe ")) {
    int hz = cmd.substring(6).toInt();
    if (hz >= 0 && hz <= 200) {
      g_probe_hz = (uint16_t)hz;
      send_log("probe rate set");
    }
  } else if (cmd.startsWith("chan ")) {
    int ch = cmd.substring(5).toInt();
    if (ch >= 1 && ch <= 14 && g_mode == MODE_SNIFFER) {
      esp_wifi_set_channel((uint8_t)ch, WIFI_SECOND_CHAN_NONE);
      g_channel = (uint8_t)ch;
      send_log("channel set");
    } else {
      send_log("chan only valid in sniffer mode");
    }
  } else if (cmd == "reboot") {
    send_log("rebooting");
    delay(100);
    ESP.restart();
  }
}

// ---------------------------------------------------------------------- setup

void setup() {
  Serial.begin(SERIAL_BAUD);
  delay(200);

  g_tx_mutex = xSemaphoreCreateMutex();
  csi_queue = xQueueCreate(CSI_QUEUE_DEPTH, sizeof(csi_record_t));
  if (!csi_queue || !g_tx_mutex) {
    while (true) {
      Serial.println("FATAL: allocation failed");
      delay(1000);
    }
  }

  WiFi.persistent(false);

  g_prefs.begin("csi", true);
  g_ssid = g_prefs.getString("ssid", "");
  g_pass = g_prefs.getString("pass", "");
  uint8_t saved_link = (uint8_t)g_prefs.getUChar("link", DEFAULT_LINK);
  g_prefs.end();

  // NOTE: the radio is deliberately NOT started here.  Doing so before the
  // tasks exist means any stall inside a radio driver happens while nothing is
  // running to report it -- the node goes completely silent with no status
  // frames and no way to issue a command, which is unrecoverable without a
  // reflash.  Restored after the tasks are up instead; see below.

  // Compile-time credentials act as a fallback for a pre-provisioned build,
  // but anything stored in NVS wins.
  if (!g_ssid.length() && strcmp(WIFI_SSID, "CHANGE_ME") != 0) {
    g_ssid = WIFI_SSID;
    g_pass = WIFI_PASS;
  }

  bool up = false;
  if (g_ssid.length()) up = enter_sta_mode();
  if (!up) enter_sniffer_mode();

  // Writer sits above the stimulus so a backlog drains before more work is
  // created, and below the WiFi driver so it can never delay the radio.
  xTaskCreatePinnedToCore(writer_task, "csi_tx", 4096, nullptr, 2, nullptr, 1);
  // Status outranks the writer deliberately: the CC1101 driver busy-waits
  // inside SendData(), and with the writer above status the node went silent
  // one second after boot -- the reporting path starved by the thing it was
  // supposed to report on.
  xTaskCreatePinnedToCore(status_task, "status", 3072, nullptr, 4, nullptr, 1);
  xTaskCreatePinnedToCore(stimulus_task, "stimulus", 3072, nullptr, 3, nullptr, 1);
  xTaskCreatePinnedToCore(probe_task, "probe", 3072, nullptr, 3, nullptr, 1);

#if RADIO_ENCRYPT
  {
    // Install the key only AFTER WiFi is up.  esp_random() is only a true
    // hardware RNG once the RF subsystem is running; called earlier it returns
    // the same value on every boot.  That gave every reboot an identical
    // session id, and since the frame counter also restarts at zero the
    // receiver correctly judged every frame to be a replay of the last
    // session's -- 29,000 rejections and not one frame delivered.
    static const uint8_t link_key[16] = RADIO_KEY;
    crypto_begin(link_key);
  }
#endif

  // Now that the status task and command handler are running, bring up the
  // radio.  A failure -- or even a long stall -- is now visible and
  // recoverable, because the node can still be talked to over the cable.
  if (saved_link != LINK_USB) {
    if (radio_begin(saved_link)) {
      g_link_mode = saved_link;
    } else {
      g_link_mode = LINK_USB;
      send_log("radio init FAILED at boot, falling back to usb");
    }
  }

  // Last, and deliberately so.  The BMP280 shares SPI2 with the radios, so it
  // must not be probed until whichever radio is going to own that bus has
  // finished claiming it -- and the status task must already be running, so
  // that a sensor that misbehaves is reported rather than silently fatal.
  sensors_begin();

  char msg[192];
  snprintf(msg, sizeof(msg), "boot mode=%s csi=%s ch=%u link=%s ssid=%s ip=%s",
           g_mode == MODE_STA ? "sta" : "sniffer", g_csi_ok ? "ok" : "FAILED",
           (unsigned)g_channel, radio_name(g_link_mode),
           g_ssid.length() ? g_ssid.c_str() : "(none)",
           WiFi.localIP().toString().c_str());
  send_log(msg);

  // Give the sensor task one cycle to produce a reading, then report what
  // actually answered.  Attaching three parts to a running system is exactly
  // when you want the node to tell you which ones it can see.
  delay(2200);
  char env[240];
  sensors_describe(env, sizeof(env));
  send_log(env);
}

void loop() {
  static String line;
  while (Serial.available()) {
    char c = (char)Serial.read();
    if (c == '\n' || c == '\r') {
      line.trim();
      if (line.length()) handle_command(line);
      line = "";
    } else if (line.length() < 128) {
      line += c;
    }
  }
  delay(20);
}
