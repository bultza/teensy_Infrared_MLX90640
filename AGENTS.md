# Repository instructions

## Documentation

- Use English for all repository documentation, code comments, and docstrings.
- Place explanatory comments immediately above the code they describe, not inline.
- Keep README.md in English and use present tense.
- Keep the README concise: project purpose, actual implementation status, setup,
  wiring, essential limitations, and links to detailed documentation.
- Put architecture, protocol details, and design rationale in docs/architecture.md.
- Clearly distinguish implemented behavior, selected design, proposals, and
  hardware observations. Present tense must not imply that a proposal already works.
- Update implementation status when code changes. Do not report hardware validation
  without an actual hardware test.

## Project context

- Hardware: Teensy 3.1 and MLX90640; SDA connects to pin 18 and SCL to pin 19.
- The selected host transport is USB CDC through Teensy Serial and Python pyserial.
- Project goals include live thermal display, adjustments, video, and timelapse.
- docs/architecture.md holds the current design proposal, including raw-subpage
  transport and host-side radiometric processing. Check it before implementing
  firmware or host communication; unresolved payloads require definition.
- example_code/ contains legacy MSP430 reference code. It is locally available
  but currently excluded by .gitignore; do not assume a fresh clone includes it.

## MLX90640 constraints

- Treat I2C ordering and transaction boundaries as critical. The user reports
  acquisition lockups that require removing and restoring camera power.
- In thermalCamera_readRAM, the MSP430 reference warns that reading 1024 RAM words
  locks the camera. Read only 832 words / 1664 bytes at 0x0400–0x073F.
- Preserve the reference sequence as the initial baseline: address write,
  repeated START, RAM read, then status-register clear after a successful read.
  The reference writes 0x0000 to 0x8000. This sequence is not yet Teensy-validated.
- Do not assume MCU reset, bus reset, or retries recover a locked sensor.
- Use bounded waits and propagate errors. Do not publish failed or incoherent
  captures as valid, silently retry configuration writes, or reset I2C periodically.
- Preserve separate subpages and their auxiliary data until processing. Do not
  blindly copy the legacy even/odd-word merge or its low-level MSP430 driver.
- Validate register meanings, buffer limits, and capture coherence before changing
  acquisition settings or splitting I2C reads. Keep hardware observations separate
  from datasheet requirements; the legacy five-second delay is not yet verified.

- The minimal register diagnostic on this Teensy requires repeated START between
  register-pointer write and read. A STOP-separated attempt returned status 0x0009
  for both addresses; repeated START returns status 0x0009 and control 0x1901.
  Do not copy the legacy STOP-separated register helpers. Diagnostics read only
  0x8000 and 0x800D at 0x33; they do not clear status or change sensor settings.

- Real EEPROM reads use explicit command 0x08, 26 blocks of 16 words covering
  0x2400..0x273F, repeated START, and a complete cache until firmware restart.
  Never substitute real EEPROM for the simulator's dummy GET_CALIBRATION data.
  captures/ contains local sensor backups and is excluded from Git.

- Real raw acquisition is explicit via command 0x09 / the default Python viewer. RAM reads
  cover exactly 0x0400..0x073F with repeated START, followed by status/control and
  duration checks, then status clear 0x0000 only after a successful complete read.
  Do not assume RAM is frozen just because status overwrite is zero: data-hold
  control bit 2 is also relevant, and is clear in the observed control 0x1901.
  Preserve bounded coherence discards and pairing isolation from simulated data.

- The default USB viewer uses real-camera radiometry in °C. --test-pattern is
  the explicit synthetic USB mode; --real is removed. --demo remains host-only.
- Preserve each full subpage and its auxiliary data through radiometric conversion.
  Numerical routines in thermal_viewer/vendor/ derive from Adafruit under MIT;
  retain its license and provenance. Reference vectors compare to Melexis C.
- Emissivity defaults to 0.95 and reflected apparent temperature to Ta - 8°C,
  adjustable on the host. Numerical agreement is not physical accuracy validation.

- Python defaults to 400 kHz I2C and 2 complete FPS. --i2c-khz 100 --fps 1 is
  the conservative fallback. Supported complete FPS: 0.25/0.5/1/2; 100 kHz rejects 2.
- Startup must STOP, SET_CONFIG, confirm its echoed clock/control/revision, fetch
  EEPROM, then START_REAL. SET_CONFIG changes only control mask 0x0380 and the
  MCU clock; never auto-replay configuration writes or persist settings to EEPROM.
- Real SUBPAGE revision/control must match the negotiated configuration, rather
  than assuming a fixed revision of 2. Keep test-pattern mode free of I2C traffic.

- Desktop UI uses PySide6 Qt Widgets in gui.py. Acquisition lives in receiver.py;
  keep all Qt operations on the GUI thread and disk/MP4 work outside it.
- Timelapse records only fresh complete images on a monotonic schedule, preserves
  temperatures plus original subpages/EEPROM, and counts missed slots/queue drops.
  Keep the queue bounded, stop/submit synchronized, and drain writes on UI close.
- Capture interval and video playback FPS are independent. Settings remain locked
  during recording; apply camera changes only through stop/restart/readback.

- Pixel inspection maps the painted image rectangle to original zero-based sensor
  coordinates. Support up to five distinct live probes with white numbered crosses
  and Reset points. Update values every frame, preserve pins on resize, and clear
  them on source restart. Hover/probes never issue additional I2C requests.
  Shared RGB rendering reverses columns for scene orientation. Preserve original
  numeric/raw sensor order; map display column x to sensor column 31-x for hit
  testing and use the inverse mapping for probe crosses. Webcam/recordings reuse
  the oriented RGB output without another flip.

- Publication: keep captures/, example_code/, generated IDE/build files, and local
  account/agent configuration out of Git. Never publish real EEPROM dumps, sensor
  serials, or recorded thermal scenes as fixtures. Public radiometry vectors come
  from tools/make_synthetic_radiometry_input.py and the external Melexis reference.
  Retain third-party copyright/license attribution.

- Optional host webcam output lives in webcam.py and uses pyvirtualcam on its own
  worker thread. Keep its mailbox bounded to the latest RGB image; no Qt/backend
  calls on the acquisition thread. Output repeats at 30 FPS, while sensor FPS stays
  independent. Preserve source timestamps for ten-second stale-image blanking.
  Disconnect/restart/failure/close stop output, and close waits asynchronously for
  cleanup. Kernel-module setup stays external; firmware transport remains USB CDC.
