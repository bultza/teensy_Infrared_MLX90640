#include <Arduino.h>
#include <Wire.h>

// USB simulator with an explicit, read-only camera register diagnostic.
constexpr size_t MAX_BODY = 4108, MAX_WIRE = 4140;
uint8_t body[MAX_BODY], rx[MAX_WIRE], tx[MAX_WIRE];
size_t rxSize = 0, txSize = 0, txOffset = 0;
uint32_t lastRx = 0, session = 0, sequence = 0, pairId = 0;
uint32_t errors = 0, dropped = 0, nextFrame = 0;
uint32_t calibrationRequest = 0;
bool discard = false, connected = false, greeted = false, calibrated = false;
bool streaming = false, calibrationReply = false;
uint8_t page = 0;
bool i2cInitialized = false, i2cFault = false;
constexpr uint8_t CAMERA_ADDRESS = 0x33;
constexpr uint16_t EEPROM_START = 0x2400, EEPROM_WORDS = 832;
constexpr uint8_t EEPROM_BLOCK_WORDS = 16;
static_assert(EEPROM_BLOCK_WORDS * 2 <= BUFFER_LENGTH, "EEPROM block exceeds Wire buffer");
uint8_t eepromData[EEPROM_WORDS * 2];
bool eepromValid = false;
uint32_t i2cClock = 100000, configRevision = 2;
uint16_t configuredControl = 0;
bool configKnown = false;
bool realMode = false;
uint8_t realState = 0, realExpected = 0, coherenceRetries = 0;
uint32_t realStarted = 0;
uint16_t realOffset = 0, realStatus = 0, realControl = 0;
uint32_t realDeadline = 0, realPoll = 0, realTimestamp = 0;
uint8_t realData[1664];

// Writes a 16-bit integer as two bytes in little-endian order.
void put16(uint8_t* p, uint16_t v) { p[0] = v; p[1] = v >> 8; }
// Writes a 32-bit integer as four bytes in little-endian order.
void put32(uint8_t* p, uint32_t v) {
  for (uint8_t i = 0; i < 4; ++i) p[i] = v >> (8 * i);
}
// Reads a 16-bit integer from two little-endian bytes.
uint16_t get16(const uint8_t* p) { return p[0] | (uint16_t(p[1]) << 8); }
// Reads a 32-bit integer from four little-endian bytes.
uint32_t get32(const uint8_t* p) {
  uint32_t v = 0;
  for (uint8_t i = 0; i < 4; ++i) v |= uint32_t(p[i]) << (8 * i);
  return v;
}
// Computes CRC-32/ISO-HDLC for a block, matching Python zlib.crc32.
uint32_t crc32(const uint8_t* data, size_t count) {
  uint32_t crc = 0xffffffff;
  while (count--) {
    crc ^= *data++;
    for (uint8_t i = 0; i < 8; ++i)
      crc = (crc >> 1) ^ ((crc & 1) ? 0xedb88320 : 0);
  }
  return ~crc;
}
// Payload is already in body + 8. Only one packet is in flight at a time.
// Builds the header and CRC, applies COBS encoding, and queues the packet for transmission.
// The payload starts at body + 8; loop() sends tx without interleaving packets.
void packet(uint8_t type, uint32_t id, uint16_t length) {
  body[0] = 1; body[1] = type;
  put16(body + 2, length); put32(body + 4, id);
  put32(body + 8 + length, crc32(body, 8 + length));
  size_t out = 1, mark = 0;
  uint8_t code = 1;
  for (size_t i = 0; i < size_t(length) + 12; ++i) {
    if (body[i] == 0) {
      tx[mark] = code; mark = out++; code = 1;
    } else {
      tx[out++] = body[i];
      if (++code == 255) { tx[mark] = code; mark = out++; code = 1; }
    }
  }
  tx[mark] = code; tx[out++] = 0;
  txSize = out; txOffset = 0;
}
// Prepares a response containing the original command, result, and additional data.
// The identifier lets Python match the response to its pending request.
void response(uint8_t command, uint32_t id, uint8_t result, uint16_t extra = 0) {
  body[8] = command; body[9] = result;
  packet(0x80, id, 2 + extra);
}
// Checks physical lines and controller BUSY before Wire can enter automatic bus recovery.
bool diagnosticBusIdle() {
  const uint32_t started = micros();
  do {
    if (digitalRead(18) && digitalRead(19) && !(I2C0_S & I2C_S_BUSY)) return true;
  } while (uint32_t(micros() - started) < 1000);
  return false;
}

// Initializes the main I2C bus lazily, without changing any sensor registers.
void initializeCameraBus() {
  if (i2cInitialized) return;
  Wire.setSDA(18);
  Wire.setSCL(19);
  Wire.begin();
  Wire.setClock(i2cClock);
  i2cInitialized = true;
}

// Reads a bounded camera-memory block and normalizes its words into little-endian bytes.
// On failure, records the address/phase/detail/count/time and latches the I2C fault.
bool readCameraBlock(uint16_t address, uint8_t* destination, uint8_t* record) {
  const uint32_t started = micros();
  memset(record, 0, 11);
  put16(record, address);
  if (i2cFault) {
    record[4] = 4;
    return false;
  }
  if (!diagnosticBusIdle()) {
    record[4] = 1;
    record[5] = 1;
  } else {
    Wire.beginTransmission(CAMERA_ADDRESS);
    Wire.write(uint8_t(address >> 8));
    Wire.write(uint8_t(address));
    const uint8_t result = Wire.endTransmission(false);
    if (result) {
      record[4] = 2;
      record[5] = result;
    } else {
      const uint8_t expected = EEPROM_BLOCK_WORDS * 2;
      const uint8_t count = Wire.requestFrom(CAMERA_ADDRESS, expected, uint8_t(1));
      record[6] = count;
      if (count != expected || Wire.available() != expected) {
        record[4] = 3;
        record[5] = 1;
      } else {
        for (uint8_t i = 0; i < EEPROM_BLOCK_WORDS; ++i) {
          const uint16_t high = Wire.read();
          put16(destination + 2 * i, (high << 8) | uint16_t(Wire.read()));
        }
      }
    }
  }
  put32(record + 7, micros() - started);
  if (record[4]) i2cFault = true;
  return record[4] == 0;
}

// Reads only 0x2400..0x273F once per boot and serves later requests from a complete cache.
// Partial reads are never published. Synthetic GET_CALIBRATION remains separate.
void readEEPROM(uint32_t id) {
  if (streaming) { response(8, id, 1); return; }
  if (!greeted || millis() < 5000) { response(8, id, 3); return; }
  if (!eepromValid) {
    initializeCameraBus();
    const uint32_t started = millis();
    for (uint16_t offset = 0; offset < EEPROM_WORDS; offset += EEPROM_BLOCK_WORDS) {
      // Stop before another block if the whole dump exceeds its one-second budget.
      if (uint32_t(millis() - started) >= 1000) {
        memset(body + 10, 0, 11);
        put16(body + 10, EEPROM_START + offset);
        body[14] = 5;
        i2cFault = true;
        response(8, id, 4, 11);
        return;
      }
      if (!readCameraBlock(EEPROM_START + offset, eepromData + 2 * offset, body + 10)) {
        response(8, id, 4, 11);
        return;
      }
    }
    eepromValid = true;
  }
  // Success extension: start address u16, word count u16, data CRC u32, 832 words.
  put16(body + 10, EEPROM_START);
  put16(body + 12, EEPROM_WORDS);
  put32(body + 14, crc32(eepromData, sizeof(eepromData)));
  memcpy(body + 18, eepromData, sizeof(eepromData));
  response(8, id, 0, 8 + sizeof(eepromData));
}

// Reads one register with a repeated START between the address and data phases.
// The record is <register u16, value u16, phase u8, detail u8, count u8, elapsed_us u32>.
// WireKinetis bounds each transfer wait internally; no retry or bus reset is requested here.
bool diagnosticRead(uint16_t reg, uint8_t* record) {
  const uint32_t started = micros();
  memset(record, 0, 11);
  put16(record, reg);
  if (i2cFault) {
    record[4] = 4;
    return false;
  }
  if (!diagnosticBusIdle()) {
    record[4] = 1;
    record[5] = 1;
  } else {
    Wire.beginTransmission(CAMERA_ADDRESS);
    Wire.write(uint8_t(reg >> 8));
    Wire.write(uint8_t(reg));
    const uint8_t result = Wire.endTransmission(false);
    if (result) {
      record[4] = 2;
      record[5] = result;
    } else {
      // Wire still owns the bus; requestFrom issues repeated START and finishes with STOP.
      {
        const uint8_t count = Wire.requestFrom(CAMERA_ADDRESS, uint8_t(2), uint8_t(1));
        record[6] = count;
        if (count == 2 && Wire.available() == 2) {
          const uint16_t high = Wire.read();
          put16(record + 2, (high << 8) | uint16_t(Wire.read()));
        } else {
          record[4] = 3;
          record[5] = 1;
        }
      }
    }
  }
  put32(record + 7, micros() - started);
  if (record[4]) i2cFault = true;
  return record[4] == 0;
}

// Reads only status and control on explicit request; never clears status or changes settings.
// A fault latches until firmware restart, preventing repeated traffic on a damaged sequence.
void diagnostic(uint32_t id) {
  if (streaming) { response(7, id, 1); return; }
  if (!greeted) { response(7, id, 3); return; }
  initializeCameraBus();
  body[10] = CAMERA_ADDRESS;
  body[11] = digitalRead(18) ? 1 : 0;
  body[12] = digitalRead(19) ? 1 : 0;
  const bool statusOK = diagnosticRead(0x8000, body + 13);
  const bool controlOK = diagnosticRead(0x800D, body + 24);
  response(7, id, statusOK && controlOK ? 0 : 4, 25);
}

// Stops real acquisition and reports phase/detail/address without publishing partial RAM.
void acquisitionError(uint8_t phase, uint8_t detail, uint16_t address) {
  streaming = false;
  realState = 0;
  i2cFault = true;
  body[8] = 1;
  body[9] = phase;
  body[10] = detail;
  put16(body + 11, address);
  packet(0x83, sequence, 5);
}

// Acknowledges a fully read subpage using the working MSP430 status-clear value.
// No acquisition configuration or EEPROM writes occur here.
bool clearCameraStatus() {
  if (!diagnosticBusIdle()) return false;
  Wire.beginTransmission(CAMERA_ADDRESS);
  Wire.write(uint8_t(0x80));
  Wire.write(uint8_t(0x00));
  Wire.write(uint8_t(0x00));
  Wire.write(uint8_t(0x00));
  return Wire.endTransmission(true) == 0;
}

// Applies validated host settings while idle, preserving every control bit except refresh.
// Switches only the MCU bus clock, writes control once if needed, and confirms by readback.
void configureCamera(uint32_t id) {
  if (streaming) { response(6, id, 1); return; }
  if (!greeted || i2cFault) { response(6, id, 3); return; }
  const uint32_t requestedClock = get32(body + 8);
  const uint8_t refresh = body[12];
  if ((requestedClock != 100000 && requestedClock != 400000) || refresh > 3 ||
      (requestedClock == 100000 && refresh > 2)) { response(6, id, 2); return; }
  configKnown = false;
  i2cClock = requestedClock;
  initializeCameraBus();
  Wire.setClock(i2cClock);
  uint8_t record[11];
  if (!diagnosticRead(0x800D, record)) { response(6, id, 4); return; }
  const uint16_t oldControl = get16(record + 2);
  if (!(oldControl & 1) || (oldControl & 0x000C)) { response(6, id, 5); return; }
  const uint16_t requestedControl = (oldControl & ~uint16_t(0x0380)) | (uint16_t(refresh) << 7);
  if (oldControl != requestedControl) {
    if (!diagnosticBusIdle()) { i2cFault = true; response(6, id, 4); return; }
    Wire.beginTransmission(CAMERA_ADDRESS);
    Wire.write(uint8_t(0x80));
    Wire.write(uint8_t(0x0D));
    Wire.write(uint8_t(requestedControl >> 8));
    Wire.write(uint8_t(requestedControl));
    if (Wire.endTransmission(true)) { i2cFault = true; response(6, id, 4); return; }
  }
  if (!diagnosticRead(0x800D, record) || get16(record + 2) != requestedControl) {
    i2cFault = true;
    response(6, id, 4);
    return;
  }
  configuredControl = requestedControl;
  configKnown = true;
  ++configRevision;
  // Success extension: bus clock u32, confirmed control u16, configuration revision u32.
  put32(body + 10, i2cClock);
  put16(body + 14, configuredControl);
  put32(body + 16, configRevision);
  response(6, id, 0, 10);
}

// Starts explicit real streaming only with cached EEPROM and a healthy sensor bus.
// Requires alternating subpages and the explicitly confirmed clock/refresh configuration.
void startReal(uint32_t id) {
  if (streaming) { response(9, id, 1); return; }
  if (!greeted || !eepromValid || i2cFault || !configKnown) { response(9, id, 3); return; }
  uint8_t record[11];
  if (!diagnosticRead(0x800D, record)) { response(9, id, 4); return; }
  realControl = get16(record + 2);
  if (realControl != configuredControl) { configKnown = false; response(9, id, 3); return; }
  if (!diagnosticRead(0x8000, record)) { response(9, id, 4); return; }
  if (!(realControl & 1) || (realControl & 0x000C) || ((realControl >> 7) & 7) > 3) {
    response(9, id, 5); return;
  }
  realMode = true;
  streaming = true;
  realState = realExpected = coherenceRetries = 0;
  ++pairId;
  realPoll = millis();
  realDeadline = millis() + 6000;
  response(9, id, 0);
}

// Advances one bounded acquisition step between USB packets, keeping commands responsive.
// Default RAM can update during reads; require stable status/control and a short capture duration.
void acquireReal() {
  uint8_t record[11];
  if (int32_t(millis() - realDeadline) >= 0) {
    acquisitionError(6, realState, 0x8000);
    return;
  }
  if (realState == 0) {
    if (int32_t(millis() - realPoll) < 0) return;
    realPoll = millis() + 10;
    if (!diagnosticRead(0x8000, record)) { acquisitionError(record[4], record[5], 0x8000); return; }
    realStatus = get16(record + 2);
    if (!(realStatus & 8)) return;
    if (realStatus & 6) { acquisitionError(7, 2, 0x8000); return; }
    realOffset = 0;
    realStarted = micros();
    realState = 1;
    realDeadline = millis() + 1000;
    return;
  }
  if (realState == 1) {
    if (!readCameraBlock(0x0400 + realOffset, realData + 2 * realOffset, record)) {
      acquisitionError(record[4], record[5], 0x0400 + realOffset);
      return;
    }
    realOffset += EEPROM_BLOCK_WORDS;
    if (realOffset == 832) { realState = 2; realTimestamp = millis(); }
    return;
  }
  if (!diagnosticRead(0x8000, record)) { acquisitionError(record[4], record[5], 0x8000); return; }
  if ((get16(record + 2) & 0x001F) != (realStatus & 0x001F) ||
      uint32_t(micros() - realStarted) >= (1000000u >> ((realControl >> 7) & 7))) {
    // Discard without clearing the newer data-ready state; retry at most three times.
    ++dropped;
    ++sequence;
    ++pairId;
    realExpected = realState = 0;
    realDeadline = millis() + 6000;
    if (++coherenceRetries >= 3) acquisitionError(7, 3, 0x8000);
    return;
  }
  coherenceRetries = 0;
  if (!diagnosticRead(0x800D, record)) { acquisitionError(record[4], record[5], 0x800D); return; }
  if (get16(record + 2) != realControl) { acquisitionError(7, 4, 0x800D); return; }
  if (!clearCameraStatus()) { acquisitionError(8, 1, 0x8000); return; }
  const uint32_t capture = sequence++;
  const uint8_t capturedPage = realStatus & 1;
  realState = 0;
  realDeadline = millis() + 6000;
  // Consume an initial orphan or unexpected page without combining different pairs.
  if (capturedPage != realExpected) {
    ++dropped;
    ++pairId;
    realExpected = 0;
    return;
  }
  uint8_t* p = body + 8;
  put32(p, capture); put32(p + 4, pairId); put32(p + 8, realTimestamp);
  put32(p + 12, configRevision); put32(p + 16, crc32(eepromData, sizeof(eepromData)));
  p[20] = capturedPage; p[21] = 0;
  put16(p + 22, realStatus); put16(p + 24, realControl);
  memcpy(p + 26, realData, sizeof(realData));
  packet(0x82, capture, 1690);
  realExpected ^= 1;
  if (realExpected == 0) ++pairId;
}

// Validates an incoming packet and handles HELLO, GET_STATUS, GET_CALIBRATION, START, or STOP.
// Commands 6, 7, 8, and 9 access I2C; command 4 always selects synthetic streaming.
void command(size_t length) {
  if (length < 12 || body[0] != 1 || get16(body + 2) > 4096 ||
      length != size_t(get16(body + 2)) + 12 ||
      get32(body + length - 4) != crc32(body, length - 4)) { ++errors; return; }
  const uint8_t cmd = body[1];
  const uint32_t id = get32(body + 4);
  if (get16(body + 2) != (cmd == 6 ? 5 : 0)) { response(cmd, id, 2); return; }
  if (cmd == 1) {
    greeted = true;
    // HELLO extension: session u32, firmware major/minor u8, capabilities u32, state u8.
    put32(body + 10, session); body[14] = 0; body[15] = 5;
    put32(body + 16, 31); body[20] = streaming ? 2 : 1;
    response(cmd, id, 0, 11);
  } else if (cmd == 2) {
    body[10] = streaming ? 2 : 1;
    put32(body + 11, sequence); put32(body + 15, errors); put32(body + 19, dropped);
    put32(body + 23, i2cClock); put16(body + 27, configuredControl);
    put32(body + 29, configRevision); body[33] = configKnown ? 1 : 0;
    response(cmd, id, 0, 24);
  } else if (cmd == 3) {
    if (!greeted) { response(cmd, id, 3); return; }
    put32(body + 8, 1);
    // Dummy EEPROM, never radiometric calibration.
    memset(body + 12, 0, 1664);
    packet(0x81, id, 1668);
    calibrationReply = true; calibrationRequest = id;
    calibrated = true;
  } else if (cmd == 4) {
    if (!greeted || !calibrated) { response(cmd, id, 3); return; }
    if (streaming && realMode) { response(cmd, id, 1); return; }
    realMode = false;
    if (!streaming) { page = 0; nextFrame = millis(); }
    streaming = true;
    response(cmd, id, 0);
  } else if (cmd == 5) {
    streaming = false; page = 0; realState = realExpected = 0;
    response(cmd, id, 0);
  } else if (cmd == 6) {
    configureCamera(id);
  } else if (cmd == 7) {
    diagnostic(id);
  } else if (cmd == 8) {
    readEEPROM(id);
  } else if (cmd == 9) {
    startReal(id);
  } else {
    response(cmd, id, cmd == 6 && streaming ? 1 : 5);
  }
}
// Receives up to 512 bytes per call and processes at most one complete packet.
// Decodes COBS, bounds buffer usage, and discards invalid input through the delimiter.
void receive() {
  if (rxSize && uint32_t(millis() - lastRx) > 1000) {
    rxSize = 0; discard = true; ++errors;
  }
  // Process at most one command per loop; transmission owns body while queued.
  for (size_t budget = 0; budget < 512 && Serial.available(); ++budget) {
    const uint8_t b = Serial.read(); lastRx = millis();
    if (b == 0) {
      if (rxSize && !discard) {
        size_t in = 0, out = 0;
        bool valid = true;
        while (in < rxSize) {
          uint8_t code = rx[in++];
          if (!code || in + code - 1 > rxSize) { valid = false; break; }
          for (uint8_t n = 1; n < code; ++n) {
            if (out >= MAX_BODY) { valid = false; break; }
            body[out++] = rx[in++];
          }
          if (!valid) break;
          if (code != 255 && in < rxSize) {
            if (out >= MAX_BODY) { valid = false; break; }
            body[out++] = 0;
          }
        }
        if (valid) command(out); else ++errors;
      }
      rxSize = 0; discard = false;
      return;
    }
    if (!discard) {
      if (rxSize == MAX_WIRE) { rxSize = 0; discard = true; ++errors; }
      else rx[rxSize++] = b;
    }
  }
}
// Generates a synthetic subpage containing a gradient and a moving bright column.
// Two chess-pattern subpages form a 32 x 24 image; auxiliary words are zero.
void syntheticSubpage() {
  uint8_t* p = body + 8;
  put32(p, sequence++); put32(p + 4, pairId); put32(p + 8, millis());
  put32(p + 12, 1); put32(p + 16, 1);
  p[20] = page; p[21] = 0; put16(p + 22, page); put16(p + 24, 0x1000);
  for (uint16_t i = 0; i < 832; ++i) {
    uint16_t value = 0;
    if (i < 768 && (((i / 32) ^ (i % 32)) & 1) == page) {
      const uint16_t x = i % 32, y = i / 32;
      value = 1000 + x * 100 + y * 50;
      // Moving bright column proves that new frames reach the host.
      if (x == pairId % 32) value += 2000;
    }
    put16(p + 26 + 2 * i, value);
  }
  packet(0x82, sequence - 1, 1690);
  if (page == 0) page = 1;
  else { page = 0; ++pairId; nextFrame = millis() + 100; }
}

// Arduino initialization: runs once after the Teensy boots.
// Configures the LED and USB serial without waiting for the Python application.
void setup() {
  // Configures the built-in LED as a diagnostic output.
  pinMode(LED_BUILTIN, OUTPUT);
  // Starts USB Serial; 115200 is conventional and does not limit USB speed.
  Serial.begin(115200);
}

// Arduino main loop: the framework calls loop() repeatedly from its main().
// Handles USB connection state, completes queued transmissions, and generates images when due.
void loop() {
  // Checks whether the host has the USB serial connection open.
  const bool host = bool(Serial);
  // Clears session state and pending data when disconnected.
  if (!host) {
    // Allows detection of the next connection.
    connected = false;
    // Stops streaming and resets the handshake.
    streaming = greeted = calibrated = calibrationReply = false;
    // Clears reception and cancels pending transmission from the previous session.
    rxSize = txSize = txOffset = 0;
    // Leaves discard mode for incomplete or oversized packets.
    discard = false;
    // Selects the initial subpage for the next stream start.
    page = 0;
    // Keeps the LED off while no host is connected.
    digitalWrite(LED_BUILTIN, LOW);
    // Ends this iteration; Arduino calls loop() again.
    return;
  }
  // Detects the first iteration with the USB port open.
  if (!connected) {
    // Records the connection so each iteration does not create another session.
    connected = true;
    // Advances the session identifier returned by HELLO.
    ++session;
  }
  // Prioritizes completion of the packet already being transmitted.
  if (txOffset < txSize) {
    // Checks how many bytes the USB buffer can currently accept.
    const int capacity = Serial.availableForWrite();
    // Writes only when buffer space is available.
    if (capacity > 0) {
      // Calculates the number of packet bytes still pending.
      size_t count = txSize - txOffset;
      // Limits the block to available buffer space.
      if (count > size_t(capacity)) count = capacity;
      // Sends the block and advances by the number of accepted bytes.
      txOffset += Serial.write(tx + txOffset, count);
    }
    // Preserves the packet until complete, without processing commands or generating new images.
    return;
  }
  // Releases the transmission buffer after completing the packet.
  txSize = txOffset = 0;
  // Checks whether the calibration request still needs acknowledgment after its data packet.
  if (calibrationReply) {
    // Marks the acknowledgment as handled.
    calibrationReply = false;
    // Queues GET_CALIBRATION OK with the original request ID.
    response(3, calibrationRequest, 0);
    // Reserves the next iteration for transmitting the response before continuing.
    return;
  }
  // Handles USB input and prepares a response when a complete command arrives.
  receive();
  // Sends the command response before generating another subpage.
  if (txSize) return;
  // Advances real acquisition once per loop while preserving USB command priority.
  if (streaming && realMode) acquireReal();
  // Generates synthetic data only when explicit real acquisition is not selected.
  if (streaming && !realMode && (page == 1 || int32_t(millis() - nextFrame) >= 0)) syntheticSubpage();
  // Blinks the LED while streaming and turns it off while idle.
  digitalWrite(LED_BUILTIN, streaming && ((millis() / 500) & 1));
}
