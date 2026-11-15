# Thermal Viewer package

This package is the desktop half of the Teensy 3.1 / MLX90640 thermal camera.
The Teensy owns I2C transactions and sends complete raw subpages over USB. Python
validates the packets, applies factory calibration, combines coherent subpages,
and displays object temperatures in a Qt window.

The [repository README](../README.md) covers wiring, firmware installation, and
basic operation. [Architecture and protocol](../docs/architecture.md) contains
the exact packet layouts, acquisition constraints, and validation results.

## Start the application

Run from the repository root, using the project's virtual environment:

```bash
python -m pip install -r requirements.txt
python -m thermal_viewer
```

Select the port and click **Connect**, or connect automatically:

```bash
python -m thermal_viewer --port /dev/ttyACM0
python -m thermal_viewer --port COM3
```

Useful modes and settings:

```bash
python -m thermal_viewer --demo
python -m thermal_viewer --port /dev/ttyACM0 --test-pattern
python -m thermal_viewer --port /dev/ttyACM0 --i2c-khz 100 --fps 1
python -m thermal_viewer --port /dev/ttyACM0 --emissivity 0.95 --reflected-temperature 20
python -m thermal_viewer --list-ports
python -m thermal_viewer --port /dev/ttyACM0 --diagnose-i2c
python -m thermal_viewer --port /dev/ttyACM0 --read-eeprom --output captures/eeprom.json
```

The camera is the default source: 400 kHz I2C, 2 complete images/s, emissivity
0.95, and reflected apparent temperature equal to sensor ambient minus 8°C unless
an explicit value is supplied. `--demo` uses no hardware. `--test-pattern` receives
synthetic counts from the Teensy and does not access the sensor. Diagnostics run
in the terminal without creating a desktop window. Existing EEPROM backup files
are not overwritten; create the output directory before selecting an output path.

## File map

| File | Responsibility |
| --- | --- |
| [`__init__.py`](__init__.py) | Package marker and short package description. It performs no acquisition on import. |
| [`__main__.py`](__main__.py) | Command-line entry point for `python -m thermal_viewer`; validates arguments, lists ports, dispatches diagnostics, and launches Qt. |
| [`gui.py`](gui.py) | Desktop layout, thermal canvas, presets, connection lifecycle, display controls, pixel probes, and timelapse/export buttons. |
| [`receiver.py`](receiver.py) | Background USB worker and startup state machine; receives/validates data and delivers complete images plus optional recording context. |
| [`protocol.py`](protocol.py) | Dependency-light binary framing, COBS, CRC validation, stream resynchronization, and subpage pairing. |
| [`settings.py`](settings.py) | Shared acquisition-setting validation, FPS-to-refresh mapping, SET_CONFIG serialization, and response/readback checks. |
| [`radiometry.py`](radiometry.py) | Validated EEPROM-to-temperature adapter; converts each subpage with its own auxiliary measurements and masks defective pixels. |
| [`rendering.py`](rendering.py) | Shared numeric-to-RGB palette/range conversion and PNG rendering for timelapse images. |
| [`timelapse.py`](timelapse.py) | Monotonic capture selection, bounded writer queue, PNG/NPZ/metadata persistence, and finalized-session MP4 export. |
| [`diagnostics.py`](diagnostics.py) | One-shot status/control or EEPROM requests, structured error descriptions, capability checks, and terminal output. |
| [`eeprom.py`](eeprom.py) | EEPROM response validation, checksums, summary output, and exclusive JSON backup creation. |
| [`vendor/__init__.py`](vendor/__init__.py) | Marker for the third-party numerical routines. |
| [`vendor/radiometry_math.py`](vendor/radiometry_math.py) | Host-only numerical routines adapted from Adafruit: parameter extraction, supply/ambient compensation, and object-temperature calculation. |
| [`vendor/LICENSE_ADAFRUIT.txt`](vendor/LICENSE_ADAFRUIT.txt) | Original MIT license and copyright notice for those adapted numerical routines; it stays with the code. |
| [`README.md`](README.md) | This package guide. |

## How the files work together

```text
__main__ → GUI on the main thread
              ├─ receiver on a USB worker thread
              │    ├─ settings: validate and confirm configuration
              │    ├─ protocol: decode packets and pair subpages
              │    └─ radiometry → vendor math: calculate object temperatures
              ├─ rendering: map values to display colors
              └─ timelapse writer thread → PNG / NPZ / JSON → MP4 export thread

__main__ → diagnostics → protocol / eeprom     (console-only modes)
```

### Command-line entry and diagnostics

`__main__.py` parses the source, port, I2C/FPS, and radiometric options. The GUI
loads only for a display session, so port listing and one-shot diagnostics do not
require a running Qt event loop. `run_serial` is re-exported for compatibility
with the initial viewer implementation; its actual code lives in `receiver.py`.

`diagnostics.py` performs HELLO and STOP before a single diagnostic or EEPROM
request. Only the read-only HELLO query has bounded retries. It prints distinct
bus-idle, transmit-NACK, incomplete-read, and latched-fault information where the
firmware exposes it. It does not repeatedly poll or silently recover the sensor.

`eeprom.py` checks the EEPROM response's exact range, word count, length, and data
CRC, and rejects all-zero/all-0xFFFF dumps. `parse_eeprom` returns numeric words
and provenance fields; `export_eeprom` optionally creates a JSON backup with CRC32
and SHA256. These checks verify the transfer, not physical calibration accuracy.

### Receiving and pairing

`receiver.py` runs outside Qt. For the real camera its handshake is HELLO → STOP
→ SET_CONFIG/readback → startup margin → READ_EEPROM → START_REAL. The synthetic
USB source uses dummy GET_CALIBRATION and START. Each command has a deadline;
configuration writes are not automatically replayed after ambiguous failures.

The latest displayed image and status use bounded queues. An optional frame sink
receives each newly completed image and context for recording, independently of
screen refresh. Shutdown sends STOP on a best-effort basis. The next real startup
still waits for its own STOP acknowledgment before configuring the sensor.

`protocol.py` provides `encode`, `Decoder`, and `Pairer`. Decoder handles fragmented
or combined USB reads, enforces packet limits, checks CRC, and resynchronizes at
COBS delimiters. Pairer requires adjacent sequences, distinct subpages, matching
pair IDs, calibration/configuration revisions, and control registers. It either
returns signed raw pixels for tests or passes both complete payloads to a converter.

`settings.py` accepts 100/400 kHz and 0.25/0.5/1/2 complete FPS. At 100 kHz it
rejects 2 FPS to preserve the RAM-read budget. Confirmation checks the echoed MCU
clock, sensor refresh bits, supported acquisition mode, and configuration revision.

### Radiometric conversion

`radiometry.py` builds `Radiometry` from the real EEPROM and selected host-side
assumptions. `convert_pair` supplies each complete subpage's RAM, control, and page
index independently to the numerical engine. It checks measurement validity,
rejects bad divisors/nonfinite results, and exposes ambient/Vdd estimates. Factory
broken/outlier pixels are masked with NaN rather than shown as measured values.
`last_subpages` retains the two original accepted payloads for recording.

The adapted engine in `vendor/radiometry_math.py` contains only numerical work,
with no I2C driver or CircuitPython dependencies. Coefficient arrays and EEPROM
belong to each instance. Its header records the original source hash and changes;
the adjacent MIT license preserves attribution. Do not replace it with an unreviewed
hardware driver, share mutable calibration across cameras, or merge auxiliary data
from different subpages before calculation.

### Desktop and pixel probes

`gui.py` contains `Viewer` and `ThermalCanvas`. Viewer owns Qt widgets and polls
worker queues without blocking on USB or disk. **Apply & restart** waits for the
old worker to exit before starting a new confirmed connection. Presets populate
controls; palette/range changes affect only rendering.

ThermalCanvas shares the exact drawing geometry with pointer hit testing. Hover
reads the current numeric value; up to five clicked sensor coordinates retain
numbered white crosses and live readouts. Coordinates survive resizing and reset
on source restart. Reset points clears them explicitly. This reads host memory,
not additional sensor registers, and does not alter saved timelapse images.

`rendering.py` supplies palettes and `colorize`, which returns a 24 × 32 RGB array
plus actual limits. Invalid pixels appear black. `recording_image` creates a
640 × 512 PNG with timestamp, units, and display-range caption. UI and saved
images therefore share the same palette/range calculations.

### Timelapse and export

`timelapse.py` selects fresh complete images at a monotonic interval. It advances
past missed slots without duplicating stale images. A bounded 16-frame queue
separates acquisition from file writing; slow-disk overflow and errors are reported.
Stop/submit are synchronized, and orderly close drains pending writes.

Each unique recording folder contains:

| Path | Contents |
| --- | --- |
| `session.json` | Source/units, EEPROM, radiometric assumptions, confirmed acquisition settings, palette/range, and interval. |
| `frames/000000.png` | Colored image with time, units, and scale; numbering continues for accepted saved images. |
| `data/000000.npz` | Float32 `values` of shape `(24, 32)` and uint8 `subpages` of shape `(2, 1690)` for real camera frames. Synthetic sources have no real subpages. |
| `frames.jsonl` | One record per complete saved frame: IDs, actual timing, limits, and skip/communication counters. |
| `summary.json` | Final saved/skipped counts, stop reason, and writer errors. |
| `timelapse-24fps.mp4` | Optional exported video; the name reflects selected playback FPS. |

`export_mp4` requires a finalized recording and refuses an existing output video.
The imageio-ffmpeg executable encodes PNGs with H.264/yuv420p. Playback FPS is
independent of capture interval. MP4 preserves appearance; NPZ plus EEPROM preserve
the numerical inputs. Abrupt process/power loss can leave temporary files; the
manifest identifies complete saved frames.

## Related files outside this package

| Path | Purpose |
| --- | --- |
| [`../teensy_Infrared_MLX90640.ino`](../teensy_Infrared_MLX90640.ino) | Sensor/USB firmware; this is the only component that accesses camera registers. |
| [`../platformio.ini`](../platformio.ini) | Teensy board, framework, USB, source filter, and uploader configuration. |
| [`../requirements.txt`](../requirements.txt) | Runtime dependencies for UI, processing, transport, image files, and video export. |
| [`../tests/`](../tests/) | Protocol, EEPROM, settings, radiometry, simulated USB startup, Qt interaction, and timelapse tests. |
| [`../tests/data/radiometry_reference.json`](../tests/data/radiometry_reference.json) | Fully synthetic numerical inputs and expected results from the Melexis C reference; no physical device ID or scene. |
| [`../tools/check_stream.py`](../tools/check_stream.py) | Explicit hardware integration check for real subpages or the USB test pattern. |
| [`../tools/make_synthetic_radiometry_input.py`](../tools/make_synthetic_radiometry_input.py) | Deterministic artificial EEPROM/RAM input generator; serial words are zero. |
| [`../tools/make_radiometry_vectors.py`](../tools/make_radiometry_vectors.py) | Optional gcc/ctypes harness for generating reference outputs from an external Melexis source checkout. |

## Tests and reference regeneration

From the repository root:

```bash
python -m unittest discover -s tests -v
```

Hardware checks require the Teensy and exclusive access to its port:

```bash
python -m tools.check_stream --port /dev/ttyACM0 --frames 10
python -m tools.check_stream --port /dev/ttyACM0 --test-pattern --frames 10
```

Numerical-vector regeneration requires gcc and the official Melexis C source and
headers in an external directory. The regular viewer and tests do not require gcc:

```bash
python -m tools.make_synthetic_radiometry_input > /tmp/synthetic-input.json
python -m tools.make_radiometry_vectors --reference-dir /path/to/melexis/reference --input /tmp/synthetic-input.json --output /tmp/synthetic-reference.json
```

The published fixture uses only generated inputs. Real captures, calibration
backups, thermal scenes, editor-generated absolute paths, credentials, and local
agent/account configuration stay outside version control through `.gitignore`.
