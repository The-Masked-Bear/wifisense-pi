#include "link_crypto.h"

#include <esp_random.h>
#include <mbedtls/aes.h>
#include <string.h>

#include "config.h"

static mbedtls_aes_context g_aes;
static bool g_ready = false;
static uint32_t g_session = 0;
static uint32_t g_counter = 0;

void crypto_begin(const uint8_t key[16]) {
  if (g_ready) {
    mbedtls_aes_free(&g_aes);
    g_ready = false;
  }
  mbedtls_aes_init(&g_aes);
  // CTR mode encrypts in both directions with the *encryption* key schedule --
  // setkey_dec here would silently produce a keystream the receiver cannot
  // reproduce.
  if (mbedtls_aes_setkey_enc(&g_aes, key, 128) != 0) {
    mbedtls_aes_free(&g_aes);
    return;
  }
  // esp_random() is the hardware RNG, properly seeded once WiFi is up.  A
  // fresh session per boot is what keeps the counter from replaying a nonce
  // that was already used with this key before the last reset.
  g_session = esp_random();
  g_counter = 0;
  g_ready = true;
}

bool crypto_enabled() { return g_ready; }

uint32_t crypto_session() { return g_session; }

bool crypto_encrypt(uint8_t *buf, size_t len, uint8_t header[8]) {
  if (!g_ready || len == 0) return false;

  const uint32_t counter = g_counter++;
  memcpy(header + 0, &g_session, 4);
  memcpy(header + 4, &counter, 4);

  uint8_t nonce[16];
  memcpy(nonce + 0, &g_session, 4);
  memcpy(nonce + 4, &counter, 4);
  memset(nonce + 8, 0, 8);

  // nc_off must start at 0 for each independent message; carrying it between
  // frames would desynchronise the keystream from the receiver, which restarts
  // its own counter block per frame.
  size_t nc_off = 0;
  uint8_t stream_block[16];
  memset(stream_block, 0, sizeof(stream_block));

  return mbedtls_aes_crypt_ctr(&g_aes, len, &nc_off, nonce, stream_block, buf, buf) == 0;
}
