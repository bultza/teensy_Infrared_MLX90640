# Architecture and USB protocol

## Status and scope

The current firmware and host implement the version 1 framing and a simulated
USB stream. The Teensy generates gradient subpages; the Python viewer reconstructs
and displays them. Synthetic streaming makes no I2C transactions. An explicit diagnostic command
reads the real status and control registers. Raw sensor acquisition and host-side radiometric conversion are implemented.
Timelapse recording, MP4 export, and optional host virtual webcam output are implemented. Additional sensor settings
remain design work. I2C clock and
refresh are configurable from Python. The acquisition and recording sections below describe requirements, not
implemented recording behavior.

## Simulator contract

All requests currently have empty payloads. HELLO, GET_STATUS, GET_CALIBRATION,
START, STOP, and SET_CONFIG are implemented. SET_CONFIG returns BUSY (1)
while streaming and accepts the schema defined below. Other commands return UNSUPPORTED. Nonempty request payloads
return INVALID_ARGUMENT (2). Invalid framing/CRC packets are silently discarded
and counted. There are no EVENT messages in the simulator. Command 0x07 is the explicit
I2C diagnostic described below.

HELLO RESPONSE contains command/result followed by `<IBBIB`: connection session
u32, firmware major u8 (0), minor u8 (5), capabilities u32, state u8. Capability
bit 0 identifies synthetic data; bit 1 identifies I2C diagnostic support; bit 2 identifies real EEPROM reads;
bit 3 identifies real-subpage acquisition; bit 4 identifies configurable acquisition;
remaining bits are reserved. State 1 is IDLE,
2 is STREAMING. Session increments on a new USB serial connection (DTR); it is
not a persistent identifier across resets. HELLO is observational within a session.
Python validates this response layout and labels simulated streams explicitly.

GET_STATUS RESPONSE adds `<BIII` (state, next capture sequence, rejected packet
count, dropped capture count), followed by `<IHIB` (current MCU I2C clock, last
confirmed control, configuration revision, known-configuration flag). The simulator backpressures generation rather
than dropping captures; dropped count is zero. Counters are u32 and reset on boot.
GET_CALIBRATION requires HELLO, and sends revision 1 plus 832 zero words, followed
by OK. These words are dummy data and must never enter radiometric conversion.
START requires successful HELLO and calibration request; STOP is always accepted.

SUBPAGE uses configuration/calibration revision 1, chess control 0x1000, status
equal to the subpage index, and zero auxiliary words. Only pixels belonging to
the transmitted chess subpage carry data; others are zero. For pixel (x,y),
the expected value is `1000 + 100*x + 50*y`, plus 2000 when `x == pair_id % 32`.
This moving column detects stale display frames. Two subpages share one pair ID;
IDs continue across stops. The first pair starts immediately; subsequent pairs
start approximately 100 ms after the preceding pair is queued. Slow USB consumers
reduce generation rate instead of accumulating an image queue.

The firmware uses one bounded in-flight TX packet and incremental writes according
to USB capacity. It processes commands between packets, so STOP completes the
in-flight packet before acknowledging and suppresses later images. Calibration
and its final response stay ordered. Disconnection clears pending data and requires
a new handshake. Firmware RX and host RX discard oversized or stale partial packets
through the next delimiter. No terminal text shares the stream.

Validation on the connected Teensy 3.1 covers 50 exact gradient pairs, intentional
bad-CRC request recovery, STOP acknowledgment and absence of later subpages,
idempotent STOP, and unsupported configuration. Receiver CRC errors and sequence
gaps are zero in this short test. `python -m tools.check_stream --port DEVICE --test-pattern`
repeats the check. Camera acquisition, long sessions, and physical mid-transfer
disconnection are not covered by this result.

The project goals are live thermal display, visual and radiometric adjustments,
video recording, timelapse, and preservation of original measurement data.

## Minimal I2C diagnostic contract

Command 0x07 (DIAGNOSE_I2C) requires HELLO and an idle stream; otherwise it returns
NOT_READY or BUSY without sensor traffic. The Python `--diagnose-i2c` path sends
HELLO, STOP, then exactly one diagnostic request. It runs without a GUI. Only
HELLO has bounded retries; the diagnostic itself is never replayed automatically.

The response is command u8, result u8, address u8, initial SDA level u8, initial
SCL level u8, followed by two `<HHBBBI` records (11 bytes each): register address,
raw value, phase, detail, received byte count, elapsed microseconds. Payload size
is 27 bytes. Result is OK (0) only for two completed reads, otherwise I2C_ERROR (4).
Records appear in order 0x8000 then 0x800D. Failed-record values are invalid.

Phase 0 means success; phase 1 means the bus did not become idle; phase 2 means
register-pointer transmission failure (detail is Wire's return code); phase 3
means fewer than two valid received bytes; phase 4 means skipped due to an earlier
latched fault. A read failure cannot distinguish NACK from timeout through Wire's
byte-count-only API. Transmit codes 2 and 3 distinguish address and data NACK;
code 4 includes timeout and other bus errors rather than proving a timeout alone.

Wire initializes lazily on a diagnostic, EEPROM, or configuration request. The
boot clock is 100 kHz; SET_CONFIG selects 100 or 400 kHz on
pins SDA 18 / SCL 19. Each read sends two big-endian register-address bytes,
issues repeated START, and receives two bytes followed by STOP. Sensor words
are converted to the USB protocol's little-endian order. There are no sensor
configuration writes, status clear, memory dumps, scans, or periodic polls.

The installed Teensyduino 1.62 WireKinetis source bounds transfer waits internally
(start around 5 ms, byte waits around 6 ms). Its wait_idle() includes a bus-recovery
pulse sequence. To avoid entering that path on a stuck bus, the diagnostic checks
SDA/SCL and controller BUSY before each new register-address transaction, with a passive 1 ms limit.
It latches failures and skips subsequent I2C transactions until firmware restart.
The passive bus-idle wait allows the previous read STOP to settle. The
read phase intentionally retains bus ownership for repeated START. There is no application-requested recovery or reset; Wire may reset its
controller on an internal transfer error. A fault arising after preflight remains
subject to the library's behavior; fault-injection tests are not yet performed.

Hardware check on the connected setup: status 0x8000 returns 0x0009, control
0x800D returns 0x1901, SDA/SCL initially read high, and each read takes around
0.5 ms. The first STOP-separated implementation returned 0x0009 for both addresses;
repeated START produces the distinct control value. Use repeated START for register
reads on this setup rather than copying the legacy STOP-separated register helpers.
An immediate preflight also briefly rejected the second read; the passive 1 ms
idle wait resolves the observed STOP-settling issue. These observations establish
register communication, not valid image acquisition or injected-fault recovery.

Register definitions also appear in the [Melexis API header](https://github.com/melexis/mlx90640-library/blob/master/headers/MLX90640_API.h).

## Real EEPROM dump contract

Command 0x08 (READ_EEPROM), with an empty payload, requires HELLO, idle streaming,
and at least five seconds of firmware uptime. It returns NOT_READY (3) or BUSY (1)
without reading if these conditions fail. Python sends HELLO, STOP, waits five
seconds, and sends one request without automatic retries. This preserves the
legacy startup margin without asserting a datasheet requirement.

Success RESPONSE payload is `<BBHHI` followed by 832 u16 little-endian words:
command 8, result 0, start 0x2400, word count 832, CRC32 of the 1664 data bytes,
and the data itself. Total payload length is 1674 bytes. The enclosing packet also
has its normal CRC. This data CRC is not a checksum provided by the sensor.

On I2C failure the response contains command 8, result 4, and an 11-byte diagnostic
record `<HHBBBI>`: failing block address, invalid value zero, phase, detail,
received byte count, block duration. Phase/detail use the diagnostic definitions;
read counts expect 32 bytes. Phase 5 denotes exhaustion of the one-second dump
budget before starting the next block. No partial dump is published. The fault
latches until firmware restart. Other rejection responses contain only command
and result.

Firmware reads exactly 26 blocks of 16 words, 0x2400..0x273F. Each block uses a
register-pointer write, repeated START, a 32-byte read, and STOP. A compile-time
check enforces the installed Wire buffer limit. Unlike live RAM, EEPROM has no
alternating subpage coherence requirement; each block explicitly sets its address.
The driver retains the diagnostic's passive idle check and bounded WireKinetis
waits. The whole-read budget is checked between blocks; an active Wire call can
extend past that budget until its own internal deadline. There is no automatic
retry or reset, sensor configuration write, status clear, or RAM acquisition.

The cache becomes available only after all blocks complete. Later READ_EEPROM
requests serve it without new I2C reads, even across USB reconnections; restart
invalidates it. GET_CALIBRATION (0x03) deliberately remains the simulator's dummy
data and is not replaced by this cache. Synthetic SUBPAGE revision 1 still refers
to the dummy calibration, not to the real EEPROM.

Python rejects incorrect range/length/data CRC and uniformly empty dumps before
export. A JSON backup records format, sensor address, range, byte order, checksums,
and all 832 numeric words. Exclusive file creation prevents overwriting an existing
backup. The dump command itself does not interpret coefficients; the viewer performs
coefficient extraction and bad-pixel checks in its radiometric path.

Hardware observation: the first real dump contains all 832 words. Device-specific
EEPROM values and checksums remain local and are not included in public documentation.
A second request served from cache matches all 832 words and checksums exactly.
The subsequent register diagnostic still returns status 0x0009 and control 0x1901;
five simulated image pairs also pass the USB integration check. The backup is
local under captures/ and is excluded from Git. EEPROM bounds match
the [Melexis API constants](https://github.com/melexis/mlx90640-library/blob/master/headers/MLX90640_API.h).

## Real subpage acquisition

Command 0x09 (START_REAL) has an empty payload. It requires HELLO, a complete
real EEPROM cache, no latched I2C fault, and idle streaming. Otherwise it returns
NOT_READY or BUSY. It reads control and status without changing configuration.
It accepts subpages enabled (control bit 0),
alternation enabled (bit 3 clear), data hold disabled (bit 2 clear), and refresh
code 0..3 (0.5..4 Hz). Other settings return UNSUPPORTED. Command 0x04 always
selects the simulator and returns BUSY if real acquisition is running.

The default Python viewer requires capability bits 3 and 4, sends STOP, confirms
SET_CONFIG, waits five seconds, requests EEPROM with 0x08, validates it, then starts 0x09. It never sends dummy GET_CALIBRATION.
Real SUBPAGE carries the confirmed SET_CONFIG revision and calibration revision equal to the
CRC32 of the normalized cached EEPROM. It retains all 832 RAM words, auxiliary
measurements, captured status, control, and read-completion timestamp. The host
validates both revisions before pairing and labels the display as a raw stream.
The optional receiver subpage queue retains complete payloads for inspection;
normal display uses calibrated object temperatures. `--test-pattern` selects
command 0x04 and preserves the raw synthetic-count display.

The state machine polls status every 10 ms with a six-second data-ready deadline.
When ready, it reads 26 blocks of 16 words at 0x0400..0x073F, one block per loop
between USB packets/commands. RAM is not assumed frozen: default data-hold is
clear and the sensor can update RAM even when status overwrite is disabled.
After reading, status bits 0..4 must match the starting status and control must
match its START_REAL snapshot. Capture duration must be less than
`1000000 >> refresh_code` microseconds, a conservative bound shorter than one
subpage interval, to prevent a double page toggle from escaping the status check.
The RAM-read state also has a one-second deadline. No partial capture is published.
This does not replace future long-duration or adversarial coherence validation.

On a status or duration mismatch the capture is discarded without clearing the
newer data-ready state. Sequence/drop/pair counters advance and pairing restarts
at page 0. Three consecutive coherence failures stop acquisition. A valid capture
resets that retry count. I2C faults, control changes, and acquisition deadlines
stop immediately and latch the fault; no automatic bus recovery is requested.

After a successful complete and checked read, firmware writes 0x0000 to status
0x8000, matching the MSP430 acknowledgment order. It never writes control or
EEPROM. Only the expected page is published; an initial orphan page 1 is consumed
and dropped. Pages 0 and 1 share a pair ID. STOP between blocks aborts incomplete
RAM without clearing status; a later start reads it anew. STOP's response follows
any in-flight packet, and no subpages follow its OK. USB disconnect also aborts.

EVENT 0x83 has five payload bytes `<BBBH>`: event code 1 (acquisition stopped),
phase, detail, and failing address. Phases 1..4 reuse diagnostic meanings; phase 6
means acquisition deadline, phase 7 coherence/configuration mismatch, phase 8
status-clear failure. Detail identifies the failed check (phase 7: 2 invalid page,
3 status/duration changed, 4 control changed). The host reports these fields in
its window title and terminal. It does not automatically restart the sensor.

Hardware validation receives ten real pairs, checks real calibration/configuration
revisions, ready/page metadata, nonzero auxiliary data and nonconstant raw pixels,
and confirms no SUBPAGE follows STOP OK. Receiver CRC errors and sequence gaps
are zero in this short run. This is not a temperature-accuracy test or long soak.
The first read crossed a sensor update and was correctly rejected; the current
implementation uses bounded coherent-read attempts instead of assuming frozen RAM.
The existing raw viewer receiver also reconstructs and renders a real image.

The [Melexis measurement flow](https://www.melexis.com/-/media/files/documents/datasheets/mlx90640-datasheet-melexis.pdf)
describes reading data then acknowledging it; the supplied MSP430 code provides
the tested acknowledgment value. Control and status interpretation follow the
register tables; control is 0x1901 at 1 complete FPS and 0x1981 at 2 complete FPS
on this setup.

## Host radiometric conversion

The default viewer path constructs a Radiometry instance from validated real
EEPROM and gives it to Pairer. Pair identity, adjacent sequences, control, and
configuration/calibration revisions are validated before conversion. Both complete
subpages enter the converter independently as 832 RAM words plus captured control
and subpage index. Auxiliary measurements from different subpages are never merged.
The output is 768 object temperatures in °C.

The numerical engine is adapted from the MIT-licensed
[Adafruit CircuitPython MLX90640 driver](https://github.com/adafruit/Adafruit_CircuitPython_MLX90640).
Only EEPROM extraction and numerical functions are retained; there is no Adafruit
I2C transport or CircuitPython dependency. The retained license and upstream source
hash are in thermal_viewer/vendor/. All EEPROM and coefficient arrays are per-instance.
Zero-valued coefficient scaling loops terminate, avoiding the upstream zero-loop
case. Invalid divisors, uniform EEPROM, zero measured gain, auxiliary invalid-data
markers, domain errors, and nonfinite measured pixels reject conversion. Factory
broken/outlier pixels are identified and masked with NaN, not displayed as measured
absolute-zero temperatures. No interpolation invents replacement measurements.

The engine compensates gain, per-pixel offset/sensitivity, ambient temperature,
supply voltage, compensation pixels, acquisition-mode differences, emissivity,
and object-temperature ranges. Emissivity defaults to 0.95. Reflected apparent
temperature defaults separately per subpage to its ambient estimate minus 8°C,
matching the Adafruit open-air example. --emissivity and --reflected-temperature
change host calculations only; they issue no I2C configuration writes.

Public numerical verification uses deterministic synthetic EEPROM and RAM inputs,
with expected outputs generated by unmodified official Melexis C functions
compiled with gcc -O2. Tests cover chess and interleaved conversion (interleaved
vectors reuse the RAM as numerical inputs, not as a physical TV-mode acquisition),
emissivities 0.8/0.95/1.0, and default/explicit reflected temperatures. Maximum pixel
difference on the six synthetic cases is below 0.0001°C; test tolerance is 0.001°C. Ambient
and Vdd outputs are compared too. This does not establish physical temperature
accuracy or validate all sensors and temperature ranges.

The fixture tests/data/radiometry_reference.json records reference source SHA256,
EEPROM, base64 subpage payloads, and expected C outputs. To regenerate with an
external checkout containing the official source and headers:

```bash
python -m tools.make_synthetic_radiometry_input > /tmp/synthetic-input.json
python -m tools.make_radiometry_vectors --reference-dir /path/to/reference --input /tmp/synthetic-input.json --output /tmp/vectors.json
```

Input JSON has `eeprom` and `pages`. The synthetic input generator sets device
serial words to zero and does not read local captures or real sensor calibration. gcc is needed only for vector regeneration, not the viewer
or standard tests. No firmware changes are required for the host conversion.

## Configurable clock and frame rate

Defaults in Python are --i2c-khz 400 and --fps 2, where FPS means complete paired
images, not sensor subpages. Supported clocks are 100000 and 400000 Hz. Complete
FPS 0.25/0.5/1/2 map to sensor refresh codes 0/1/2/3 (0.5/1/2/4 subpages/s).
At 100 kHz, code 3 is rejected before sensor traffic because the conservative RAM
read budget cannot be met. 400 kHz accepts codes 0..3. No automatic downgrade occurs.

SET_CONFIG (0x06) has payload `<IB`: MCU I2C clock in Hz, sensor refresh code.
All other requests retain empty payloads. With idle streaming and HELLO completed,
firmware validates the combination, marks configuration unknown, initializes or
updates the MCU clock, reads control 0x800D, and verifies supported subpage mode.
It replaces only mask 0x0380 and writes control once if needed, then reads control
back and requires an exact match. It neither writes EEPROM nor changes resolution,
pattern, data hold, or subpage mode. Failure stops the handshake, leaves settings
unknown, and latches bus/readback faults. Requests during streaming return BUSY.
Malformed payloads return INVALID_ARGUMENT; missing HELLO/fault latch returns
NOT_READY; incompatible sensor modes return UNSUPPORTED.

Success RESPONSE is command/result followed by `<IHI`: actual selected MCU clock,
confirmed control, configuration revision. Payload length is 12 bytes. The revision
starts at 2 on boot and increments after every confirmed configuration. Python
validates the echoed rate/control/revision before starting. SUBPAGE must match
both negotiated revision and control. Old firmware without capability bit 4 is
rejected with an upload instruction; no implicit settings are assumed.

Real startup is HELLO, STOP, SET_CONFIG, five-second margin, READ_EEPROM,
START_REAL. Modifying-command timeouts are never replayed. GET_STATUS exposes
cached confirmed configuration for reconciliation without issuing I2C operations.
The MCU clock is software-selected, not measured on a logic analyzer. Python
reports frame arrival intervals in the window; the integration test measures
paired capture timestamps. USB baud rate remains independent of I2C clock.

No settings are persisted to EEPROM. A firmware restart initializes the MCU clock
to 100 kHz; a powered sensor can retain its last volatile refresh setting. Each
viewer startup reapplies explicit choices. Read-only diagnostics/EEPROM commands
keep the current MCU clock and do not issue SET_CONFIG.

Hardware validation at 400 kHz/2 FPS receives twenty real pairs with 1.97 complete
FPS measured, zero receiver packet errors and zero sequence gaps, and confirms
STOP. At 100 kHz/1 FPS, eight pairs measure 0.99 complete FPS with zero receiver
packet errors and zero sequence gaps. Slower clock settings require --fps 1 or lower. These are short functional
checks, not proof that every cable or pull-up arrangement works at 400 kHz.

## Qt desktop and timelapse

The application uses PySide6/Qt Widgets, with a thermal canvas on the left and
connection controls plus Camera/Display/Timelapse tabs on the right. Qt paint
operations stay on the GUI thread; the thermal image is a copied RGB QImage scaled
without smoothing. NumPy and Matplotlib colormaps supply pixels; no Matplotlib
plot window or animation timer is used. CLI diagnostics remain console-only.
Launching without a port opens a disconnected window; --port and --demo autostart.

thermal_viewer/receiver.py owns USB acquisition independently of the frontend.
Its existing run_serial entry point is re-exported by __main__ for compatibility.
It delivers the latest display image through a bounded queue and optionally calls
a frame sink for each freshly converted pair. The sink context includes monotonic
and UTC reception time, units/source, confirmed configuration, EEPROM, radiometric
assumptions, counters, and both full subpage payloads. Radiometry retains those
payloads only after successful pair conversion; they are never merged for storage.

Camera presets copy Balanced 400 kHz/2 FPS, Robust cable 100 kHz/1 FPS, or Slow
100 kHz/0.5 FPS into controls. Apply & restart validates choices, signals the old
worker to stop, waits asynchronously for its exit, then starts a fresh worker.
That worker performs HELLO/STOP/SET_CONFIG/EEPROM/START_REAL with readback checks.
Display palette/range changes do not touch the sensor. Recording locks acquisition
and display controls; Disconnect remains available and finalizes recording.

Timelapse selects only new image IDs on a monotonic schedule. Its default interval
is ten seconds; playback defaults to 24 FPS and is independent. Overdue slots
advance the schedule once and increment a skipped-slot counter instead of
repeating a stale image. A lock serializes stop/submit so finalization cannot race
with one last queued frame. A separate writer owns a bounded 16-frame queue;
queue overflow is counted and never waits for disk in the USB thread. Disk errors
stop the writer and appear in the recording status/summary while display can continue.
A source error or disconnect stops recording; reconnecting starts a separate session.

Every recording directory is unique. PNGs are 640 × 512 (nearest-neighbor image
plus timestamp/units/range caption). NPZs contain float32 values (24 × 32) and raw
uint8 SUBPAGE payloads (2 × 1690 for camera data; empty for synthetic sources).
session.json records the first accepted frame's source, calibration and acquisition
context, palette/range, interval, and complete EEPROM. frames.jsonl is authoritative
for successfully saved images: index, pair ID, device time, UTC reception time,
monotonic elapsed time, scale, skipped slots, and communication counters.
summary.json records saved/skipped counts, stop reason, and errors on finalization. No float
temperature matrix is reconstructed from the colored PNG. Files use temporary
names until complete; the manifest is appended only after PNG and NPZ succeed.

Export runs off the GUI thread after recording drains. The bundled executable
from imageio-ffmpeg encodes the numbered PNG sequence as H.264/yuv420p MP4 at the
selected playback rate. It requires a finalized manifest, checks frame files,
and refuses an existing destination. Recording originals remain intact. A crashed
process can leave unlisted temporary/orphan files; only manifest-listed complete
frames are part of the recording. Closing the UI waits asynchronously for the USB
worker, queued writes, and any active export rather than abandoning them.

Validation includes offscreen Qt button/preset/display/record/stop/export tests,
monotonic selection and duplicate prevention, original NPZ/EEPROM preservation,
bounded slow-disk queue drops, disk-error reporting, and MP4 no-overwrite checks.
A real Teensy session records five camera captures at about 0.5-second intervals
through the desktop, retains both subpages per capture, and exports a 640 × 512
MP4 decoded as five frames at 24 FPS. Applying Robust and then Balanced from
the desktop also produces valid images after each confirmed restart. This is a short desktop/USB test, not a long
recording soak or a guarantee against power loss.

## Pixel probes

ThermalCanvas shares one aspect-ratio image_rect() between painting and hit
testing. Pointer coordinates map to zero-based sensor column/row, excluding the
letterbox margins, color bar, and exclusive right/bottom edges. Hover reads the
original latest numeric matrix, independently of palette or display clipping.

Shared `colorize()` reverses RGB columns for the corrected scene orientation.
The viewer, virtual webcam, and recording images therefore use the same horizontal
orientation. Numeric measurements, saved NPZ values, and raw subpages stay in
original sensor order. Hit testing maps display column x to sensor column 31-x;
cross painting uses the inverse mapping. The left/right displayed edges represent
sensor columns 31/0. Synthetic sources use the same rendering convention.
An asymmetric synthetic-image Qt test checks edge colors, probe readings, recording
orientation, and preservation of the numeric matrix; physical orientation remains
subject to confirmation with the connected scene.

Left-click pins up to five distinct coordinates; further clicks or duplicate
coordinates leave the selection unchanged. White crosses with dark halos and
numbers identify those cells. A persistent panel shows each current reading;
set_values() refreshes the matrix and readouts on each new displayed frame, so a
stationary pointer and all pinned probes update without new mouse events. Masked
NaN values are labeled unavailable, never fabricated as measured temperatures.
Camera values use °C, synthetic sources use counts. Reset points clears all pins.
Resizing recomputes geometry while retaining sensor coordinates; source restart
clears stale values and pins. All interaction stays on the GUI thread, produces no
I2C traffic, and leaves recorder data untouched. Probes are desktop overlays.

Offscreen Qt tests exercise hover, frame-to-frame updates, all five selections,
duplicate/sixth-click rejection, edge cells, resize mapping, ignored margins/bar,
reset, empty frames, and invalid pixels. A rendered screenshot verifies cross and
readout layout against deterministic synthetic radiometric test data.

## Responsibilities and transport

The Teensy owns I2C initialization, acquisition, coherence checks, timestamps,
error counters, and packet transmission. A single acquisition state machine owns
sensor access; USB commands submit requests to it rather than accessing I2C directly.

Python owns continuous USB reception, packet validation, radiometric conversion,
display, and recording. Conversion uses EEPROM calibration, raw subpages, control
registers, and auxiliary measurements. The MSP430 reference calculates sensor
supply voltage and internal temperature, but lacks full per-pixel conversion.

Palette, display range, interpolation, and orientation belong to the host. Emissivity
and reflected temperature belong to radiometric processing. These adjustments do
not require camera register writes.

The selected transport is USB CDC: Teensy `Serial` and Python `pyserial`, without
an external USB-UART adapter. Typical device names are COMx on Windows,
/dev/ttyACMx on Linux, and /dev/cu.usbmodem… on macOS. The conventional opening
baud rate is 115200; it does not set the native USB transfer rate. See
[PJRC USB Serial](https://www.pjrc.com/teensy/td_serial.html).
CDC provides bidirectional access without a custom USB implementation. The host
also supplies optional webcam output through pyvirtualcam; firmware USB remains CDC.

## Host virtual webcam

`webcam.py` owns an optional pyvirtualcam backend on a dedicated Python thread.
Qt submits the rendered 24 × 32 RGB image and the source's monotonic reception
timestamp through a locked latest-image mailbox. All backend operations, including
opening, nearest-neighbor enlargement to 640 × 480, sending, and closing, run
outside Qt and acquisition. Output uses a monotonic 30 FPS schedule that skips
missed slots. It repeats the latest image without creating new sensor data.
After ten seconds without a new source frame the worker sends black frames.
Palette/range changes replace the RGB image without refreshing its source age.

The GUI starts output explicitly from Display controls. Disconnect, acquisition
failure, applying settings, and closing request shutdown. A new source requires
an explicit webcam start. The GUI polls completion and errors and waits for output
cleanup before closing, without joining a worker on the Qt thread. A backend
failure releases its context and leaves viewing/recording available. The backend
is external native code; a backend call that hangs can delay worker completion.

The optional `requirements-webcam.txt` installs pyvirtualcam. Linux requires a
separately installed/loaded v4l2loopback device; the program never changes kernel
modules or privileges. The package guide documents installation and other backend
platforms. This output transports display colors, without raw temperatures, probes,
or the desktop scale. Native firmware UVC remains unimplemented.

### Virtual webcam validation

Fake-backend tests exercise output repetition and RGB scaling, latest-image
replacement, stale-image blanking, backend errors, and resource release. Qt tests
exercise start/stop, restart, source disconnect, failure feedback, and window close.
System virtual-camera and receiving-application validation require a configured
OS backend; no Windows/macOS validation is claimed.

A Linux smoke test uses the Qt viewer with deterministic synthetic pixels and the
actual pyvirtualcam/v4l2loopback backend at `/dev/video10`. A separate FFmpeg
process captures three 640 × 480 RGB frames for each of inferno and gray palettes.
Checks confirm nonblank images, colored versus grayscale channels, and clean
output shutdown. This validates the local backend with FFmpeg; it does not test
physical sensor acquisition, conferencing/browser clients, or other platforms.

## Acquisition baseline and limitations

The user reports that incorrect I2C command ordering can stop acquisition until
the camera loses and regains power. MCU reset and bus retries do not constitute
proven recovery procedures.

The local MSP430 reference is example_code/thermalCamera.c. Its sequence is:

1. Write 0x1981 to control register 0x800D and read registers.
2. Wait until uptime exceeds five seconds before reading calibration.
3. Read 832 EEPROM words starting at 0x2400.
4. Poll for new data on subpage 0, then read RAM.
5. Clear status and poll for new data on subpage 1, then read RAM and clear status.
6. Read registers, combine data, calculate sensor temperature, and append a CRC.

Each RAM read writes the address, issues a repeated START, and reads **832 words
(1664 bytes), 0x0400–0x073F**. After success, the reference writes 0x0000 to status
register 0x8000. Its comment explicitly warns that reading 1024 words locks the
camera and requires a power-on reset. This is a reference-code observation, not
a reproduced Teensy test.

The baseline preserves those transaction boundaries. Decoding 0x1981 and checking
the sequence against the included datasheet are prerequisites for sensor setting
changes. The five-second delay is inherited behavior, not a verified datasheet
requirement. Controller buffer limits and coherent capture checks require validation
before adopting block reads; the permitted address range stays unchanged.

Both subpages retain their complete auxiliary data, registers, and timestamps.
The legacy even/odd-word merge is not a general reconstruction algorithm.
Pixel selection depends on acquisition mode. The reference also contains ignored
initialization errors, unbounded waits, and low-level driver inconsistencies;
it is a behavioral reference rather than code for direct transplantation.

## Version 1 packet framing

USB CDC is a byte stream. Reads can contain partial packets or multiple packets.
Each packet body uses COBS encoding followed by a 0x00 delimiter. Diagnostics use
packets too; free-form text does not share the stream.

All multibyte fields use explicit little-endian serialization without C struct
padding. Sensor words are normalized before transmission; Python does not swap
them again.

| Field | Size | Meaning |
| --- | --- | --- |
| Version | u8 | 1 |
| Type | u8 | Message type |
| Length | u16 | Payload bytes, maximum 4096 |
| Identifier | u32 | Request ID or asynchronous event sequence |
| Payload | Variable | Message-specific data |
| CRC | u32 | CRC-32/ISO-HDLC over header and payload |

The CRC matches Python zlib.crc32; the check vector is ASCII `123456789` →
0xCBF43926. CRC bytes also use little-endian order.

The receiver validates COBS, version, exact length, and CRC before interpretation.
It discards invalid packets and counts failures. The encoded-packet buffer limit
is 4140 bytes; overflow causes discard until the next delimiter. A partial packet
expires after one second without additional bytes. Recovery discards through a
delimiter before accepting another packet.

## Message types

| Code | Message | Direction and purpose |
| --- | --- | --- |
| 0x01 | HELLO | Host → device: version and capabilities |
| 0x02 | GET_STATUS | Host → device: state, counters, last error |
| 0x03 | GET_CALIBRATION | Host → device: simulator dummy calibration |
| 0x04 | START | Host → device: start streaming |
| 0x05 | STOP | Host → device: stop at a safe boundary |
| 0x06 | SET_CONFIG | Host → device: idle-only I2C clock and refresh configuration |
| 0x07 | DIAGNOSE_I2C | Host → device: read status and control |
| 0x08 | READ_EEPROM | Host → device: real cached EEPROM dump |
| 0x09 | START_REAL | Host → device: real raw subpage acquisition |
| 0x80 | RESPONSE | Device → host: command result |
| 0x81 | CALIBRATION | Device → host: EEPROM and calibration revision |
| 0x82 | SUBPAGE | Device → host: raw subpage capture |
| 0x83 | EVENT | Device → host: asynchronous state change or error |

RESPONSE echoes the request ID. Its payload begins with the original command
code (u8) and result (u8): OK=0, BUSY=1, INVALID_ARGUMENT=2, NOT_READY=3,
I2C_ERROR=4, UNSUPPORTED=5, followed by command-specific data.
GET_CALIBRATION returns CALIBRATION with the same request ID before final OK.

HELLO and GET_STATUS schemas are defined in the simulator contract above.
SET_CONFIG and acquisition EVENT schemas are defined in their sections above.

## Calibration and subpage payloads

CALIBRATION contains a u32 revision followed by 832 u16 EEPROM words: 1668 bytes.
Initialization reads and validates calibration; USB requests serve the cached copy
without initiating another EEPROM transaction. Python requires a matching revision
before converting a capture.

SUBPAGE contains these fields, in order and without padding:

| Field | Type | Meaning |
| --- | --- | --- |
| Capture sequence | u32 | Increments for every acquired subpage, including dropped captures |
| Pair ID | u32 | Associates accepted subpages 0 and 1 |
| Timestamp | u32 | Uptime in milliseconds at read completion |
| Configuration revision | u32 | Identifies capture settings |
| Calibration revision | u32 | Identifies the associated EEPROM |
| Subpage | u8 | 0 or 1 |
| Reserved | u8 | Zero |
| Status | u16 | Register associated with the capture |
| Control | u16 | Register associated with the capture |
| RAM | 832 × u16 | Raw pixels and auxiliary data |

The payload size is 1690 bytes. Status and control describe the capture rather
than an unrelated later state query. The driver rejects captures whose coherence
it cannot establish. The exact coherence check remains an implementation prerequisite.

Python pairs distinct subpages only when pair ID and both revisions match.
A lost subpage invalidates its pair. Timestamp arithmetic handles the u32
millisecond wrap at approximately 49.7 days; session identity distinguishes resets.
Sequence and pair counters also use modular u32 arithmetic.

Two subpages contain 3380 payload bytes per complete image. An illustrative rate
of 16 complete images/s requires about 54 kB/s plus encapsulation. This is a
transport estimate, not a sensor performance claim. Measurements distinguish
subpages/s from complete images/s and include the I2C bottleneck.

## Commands, state, and recovery

The design states are INITIALIZING, IDLE, STREAMING, and ERROR. HELLO and
GET_STATUS remain available during initialization and sensor failure. START
requires valid calibration and known configuration. START while streaming and
STOP while idle are idempotent.

The host keeps one request pending with an initial three-second timeout.
Connection startup waits 200 ms for USB control-line settling and sends a delimiter
to terminate stale device input. Only the read-only HELLO query has automatic
retries (three attempts total, each with a new request ID); stale replies do not
advance the handshake. START and configuration writes are never replayed.
On POSIX the viewer requests exclusive access. Timeout diagnostics identify the
command, port, and parser error count. Shutdown attempts STOP even when the START
acknowledgment is missing. A
response indicates completed work rather than simple receipt. STOP completes or
safely aborts the active acquisition; no SUBPAGE packets follow its OK until START.
SET_CONFIG while streaming returns BUSY. A setting change follows STOP,
SET_CONFIG, START. Validation precedes writes and control readback confirms the
result. Failure enters ERROR without asserting that the old settings remain intact.
Arbitrary register writes are outside the command interface.

A timeout on a modifying command does not trigger automatic replay: the command
may already have taken effect. GET_STATUS reconciles the state. All I2C operations
and data-ready waits have finite deadlines. Invalid captures never appear as valid.
Errors identify phase, cause, and counters, distinguishing NACK, timeout,
incomplete reads, and data-ready expiration. Blind periodic I2C resets and unlimited
retries are excluded. Power-cycle recovery requires user intervention unless an
actual sensor power-control circuit is present; no such circuit is documented.

## Flow control and reconnection

The device uses bounded buffers and incremental USB writes, with command-response
priority between packets. A slow host cannot block acquisition indefinitely.
Queue overflow drops whole captures and increments a counter; packet bytes never
interleave and an in-flight packet completes before the next packet starts.
Host sequence checks expose losses.

Without a host, images do not accumulate. Disconnection stops streaming at a safe
boundary. Simulator reconnection requires HELLO, GET_CALIBRATION, and START;
real reconnection requires HELLO, READ_EEPROM, and START_REAL. Startup does
not depend on an infinite `while (!Serial)` wait.

Python reception and validation run independently of display and disk writes.
The display selects the most recent image when necessary. Recording has a bounded
queue and reports losses. The serial monitor and viewer do not share the port.

## Recording formats

The implemented timelapse format and export behavior are described above. General
continuous video recording, editing, and recovery after power loss remain separate
work. Original subpages/EEPROM permit later radiometric recalculation with different
host assumptions; rendered MP4 pixels alone do not preserve measurement precision.

## Implementation and validation checklist

- Confirm module power, pull-ups, and single-camera wiring.
- Reproduce the reference acquisition sequence with bounded waits.
- Validate calibration and coherent subpage pairs before changing settings.
- Complete unresolved payload schemas and implement firmware/host framing together.
- Check fragmentation, CRC failures, resynchronization, disconnection, and slow hosts.
- Validate radiometric conversion and live display.
- Extend long-duration timelapse validation and add continuous recording as needed.
- Measure throughput, queue sizes, deadlines, and long-session stability on hardware.
