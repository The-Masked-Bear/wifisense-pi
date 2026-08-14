#pragma once

// Radio transmit path: sends COBS-framed CSI to the Pi over CC1101 or nRF24.
//
// Both radios carry far less than one CSI frame per packet -- 32 bytes on the
// nRF24, a 64-byte FIFO on the CC1101, against ~149 bytes per frame -- so
// frames are fragmented here and reassembled by radio_common.py on the Pi.
// The fragment header must stay byte-identical to that file:
//
//     [ seq u8 ][ frag u8 ][ payload ... ]
//
// where the low 7 bits of `frag` are the fragment index and bit 7 marks the
// last fragment of a frame.
//
// Only one radio is initialised at a time.  They share SPI2 with separate chip
// selects and could coexist electrically, but the RF24 and ELECHOUSE drivers
// each assume they own the bus, and interleaving their transactions is not
// worth the risk when the node only ever needs one uplink.

#include <Arduino.h>
#include <SPI.h>

#include "config.h"

// --- shared SPI2 (FSPI) bus, on the ESP32-S3 IO_MUX pins ---
#define RADIO_SCK 12
#define RADIO_MOSI 11
#define RADIO_MISO 13

// --- nRF24L01+ ---
#define NRF_CSN 10
#define NRF_CE 14
#define NRF_IRQ 9

// --- CC1101 ---
#define CC_CSN 21
#define CC_GDO0 18
#define CC_GDO2 17

#define LINK_USB 0
#define LINK_NRF24 1
#define LINK_CC1101 2

// nRF24 fixed payload.  Fixed rather than dynamic because dynamic payloads
// require auto-ack, and auto-ack on a one-way telemetry stream costs a return
// slot per fragment for data the frame CRC already protects end to end.
#define NRF_PAYLOAD 32

// Leaves headroom under the CC1101's 64-byte FIFO so the radio is never
// refilled right at the boundary while it is still draining.
#define CC_PAYLOAD 60

// The shared SPI2 instance, brought up on first use.  Exposed because the
// BMP280 sits on this same bus with its own chip select and must not begin()
// a second SPIClass on the same host -- that would reconfigure the peripheral
// out from under whichever radio is mid-transaction.
SPIClass *radio_spi();

// Serialises access to the SPI2 bus AND to the radio control pins.
//
// The SPI HAL already locks per transaction, which would be enough if every
// user spoke SPI.  The diagnostics here do not: they drive SCK/MOSI/MISO and
// the chip selects as plain bit-banged GPIO, which is invisible to
// transaction-level locking.  Recursive, because several of the public
// functions below call each other.
void radio_bus_lock();
void radio_bus_unlock();

bool radio_begin(uint8_t mode);
void radio_stop();
// Fragments and transmits one complete framed message.  Returns false if the
// radio is not up.  Never blocks longer than the packets themselves take.
bool radio_send(const uint8_t *frame, size_t len);
const char *radio_name(uint8_t mode);
// Raw SPI register probe of both chip selects; fills `out` with a report.
void radio_scan(char *out, size_t n);
// Hunts for either radio on any usable pin combination; returns the hit count.
int radio_sweep(void (*report)(const char *));
// Electrical (not protocol) test: which pins are driven by an external chip.
int radio_probe_pins(void (*report)(const char *));
// Finds MISO by asserting each candidate CS and seeing which pin starts driving.
int radio_find_miso(void (*report)(const char *));
// Per-pin logic levels: separates 'unpowered' from 'not connected'.
void radio_pin_levels(void (*report)(const char *));
// Dump CC1101 modem registers for comparison against the receiver.
void radio_cc_regs(char *out, size_t n);
