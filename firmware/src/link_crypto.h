#pragma once

// AES-128-CTR for the radio uplink.
//
// Why this exists: CSI is not innocuous telemetry.  It reveals when a room is
// occupied, when people move, and when they sleep.  Transmitted in the clear,
// anyone within range with a matching nRF24 on the same channel can read all
// of it.  Encrypting the payload makes the stream useless to a passive
// listener without the key.
//
// Wire format of an encrypted radio frame, before fragmentation::
//
//     +-----------+-----------+---------------------------+
//     | session   | counter   | ciphertext                |
//     | u32 LE    | u32 LE    | N bytes                   |
//     +-----------+-----------+---------------------------+
//
// The 8-byte header travels in the clear because the receiver needs it to
// rebuild the counter block.  It leaks nothing: a random session id and a
// frame counter.
//
// The CTR nonce is ``session || counter || 8 zero bytes``.  Counter-mode
// security rests entirely on never reusing a (key, nonce) pair, so:
//
//   * ``counter`` increments once per frame and is 32 bits -- at 100 Hz that
//     is 497 days before it wraps.
//   * ``session`` is drawn from the hardware RNG at every boot, so a reboot
//     (which resets the counter) starts a fresh nonce space rather than
//     replaying the previous one.
//
// This provides CONFIDENTIALITY, not authentication.  A forged frame cannot be
// read or crafted meaningfully without the key, and the CRC16 inside the
// encrypted payload means a random forgery survives with probability 1/65536 --
// proportionate for a home sensor link, but it is not a MAC and should not be
// relied on where an active attacker matters.

#include <Arduino.h>

// Initialise with the shared key.  Safe to call more than once.
void crypto_begin(const uint8_t key[16]);

// Encrypt `len` bytes in place and write the 8-byte header into `header`.
// Returns false if encryption is disabled or not initialised.
bool crypto_encrypt(uint8_t *buf, size_t len, uint8_t header[8]);

// True when a key has been installed and encryption is enabled.
bool crypto_enabled();

// Session id in use, for diagnostics.
uint32_t crypto_session();
