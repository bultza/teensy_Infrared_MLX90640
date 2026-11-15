"""USB acquisition worker, independent of the desktop UI."""
import queue
import struct
import os
import sys
import time
from datetime import datetime, timezone
from .protocol import Decoder, Pairer, encode


def report(messages, text):
    try:
        messages.get_nowait()
    except queue.Empty:
        pass
    messages.put_nowait(text)


def run_serial(port, stop, images, messages, real=True, subpages=None, emissivity=0.95, reflected_temperature=None, i2c_khz=400, fps=2.0, frame_sink=None):
    import serial
    from .settings import configuration_payload, confirmed_configuration
    config_request = configuration_payload(i2c_khz, fps)
    config_revision = None
    confirmed_control = None
    configured = False
    previous_image_time = None
    eeprom_words = None
    decoder, pairer = Decoder(), Pairer()
    calibration = None
    request_id = 0
    pending = None
    deadline = 0
    streaming = False
    simulated = False
    hello_attempts = 1
    start_attempted = False

    def request(link, command, payload=b''):
        nonlocal request_id, pending, deadline
        request_id += 1
        pending = command
        frame = encode(command, request_id, payload)
        if link.write(frame) != len(frame):
            raise RuntimeError('Incomplete USB write')
        deadline = time.monotonic() + 3

    try:
        options = {'exclusive': True} if os.name == 'posix' else {}
        with serial.Serial(port, 115200, timeout=0.05, write_timeout=1, **options) as link:
            # Allow USB control-line changes to settle before starting the session.
            if stop.wait(0.2):
                return
            link.reset_input_buffer()
            link.write(b'\0')  # Terminate any stale partial device-side packet.
            request(link, 1)
            try:
                while not stop.is_set():
                    for packet in decoder.feed(link.read(4096)):
                        if packet.kind == 0x81 and pending == 3 and packet.identifier == request_id:
                            if len(packet.payload) != 1668:
                                raise ValueError('Invalid calibration length')
                            calibration = struct.unpack_from('<I', packet.payload)[0]
                        elif packet.kind == 0x80 and packet.identifier == request_id and pending is not None:
                            if len(packet.payload) < 2 or packet.payload[0] != pending:
                                raise ValueError('Invalid command response')
                            command, result = packet.payload[:2]
                            if result:
                                name = {5: 'STOP', 6: 'SET_CONFIG', 8: 'READ_EEPROM', 9: 'START_REAL'}.get(command, f'Command {command}')
                                reason = {1: 'BUSY', 2: 'INVALID_ARGUMENT', 3: 'NOT_READY', 4: 'I2C_ERROR', 5: 'UNSUPPORTED'}.get(result, str(result))
                                raise RuntimeError(f'{name} failed: {reason}; no automatic retry')
                            pending = None
                            if command == 1:
                                if len(packet.payload) != 13:
                                    raise ValueError('Invalid HELLO schema')
                                capabilities = struct.unpack_from('<I', packet.payload, 8)[0]
                                simulated = not real and bool(struct.unpack_from('<I', packet.payload, 8)[0] & 1)
                                if real:
                                    if capabilities & 24 != 24:
                                        raise RuntimeError('Upload current firmware: configurable real acquisition is required')
                                    request(link, 5)
                                else:
                                    request(link, 3)
                            elif command == 5 and real:
                                request(link, 6, config_request)
                            elif command == 6:
                                config_revision, confirmed_control = confirmed_configuration(packet.payload, i2c_khz, fps)
                                configured = True
                                report(messages, f'Confirmed {i2c_khz} kHz, target {fps:g} FPS; waiting for camera startup')
                                print(f'Confirmed I2C {i2c_khz} kHz, target {fps:g} complete FPS, control 0x{confirmed_control:04X}')
                                if stop.wait(5):
                                    return
                                request(link, 8)
                            elif command == 3:
                                if calibration is None:
                                    raise RuntimeError('Calibration missing before OK')
                                start_attempted = True
                                request(link, 4)
                            elif command == 8:
                                from .eeprom import parse_eeprom
                                data = parse_eeprom(packet.payload)
                                calibration = int(data['crc32'], 16)
                                eeprom_words = data['words']
                                from .radiometry import Radiometry
                                pairer = Pairer(Radiometry(data['words'], emissivity, reflected_temperature))
                                start_attempted = True
                                request(link, 9)
                            elif command in (4, 9):
                                streaming = True
                                deadline = time.monotonic() + (8 if real else 5)
                                report(messages, 'Streaming synthetic gradient' if simulated else 'Streaming calibrated temperatures (°C)')
                        elif packet.kind == 0x82 and streaming:
                            if len(packet.payload) != 1690 or struct.unpack_from('<I', packet.payload, 16)[0] != calibration:
                                raise ValueError('Invalid subpage or calibration revision')
                            if real and (not configured or struct.unpack_from('<I', packet.payload, 12)[0] != config_revision or struct.unpack_from('<H', packet.payload, 24)[0] != confirmed_control):
                                raise ValueError('Subpage does not match confirmed configuration')
                            if subpages is not None:
                                report(subpages, packet.payload)
                            image = pairer.accept(packet.payload)
                            deadline = time.monotonic() + (8 if real else 5)
                            if image is not None:
                                now = time.monotonic()
                                if frame_sink is not None:
                                    frame_sink(image, {
                                        'source': 'camera' if real else 'test-pattern',
                                        'units': 'celsius' if real else 'synthetic counts',
                                        'received_monotonic': now,
                                        'received_utc': datetime.now(timezone.utc).isoformat(),
                                        'i2c_khz': i2c_khz if real else None,
                                        'target_fps': fps if real else 10,
                                        'emissivity': emissivity if real else None,
                                        'reflected_temperature': reflected_temperature,
                                        'reflected_assumption': 'Ta - 8 C' if reflected_temperature is None else 'explicit',
                                        'control': confirmed_control,
                                        'config_revision': config_revision,
                                        'calibration_revision': calibration,
                                        'eeprom_words': eeprom_words,
                                        'subpages': pairer.converter.last_subpages if real else (),
                                        'packet_errors': decoder.errors,
                                        'sequence_gaps': pairer.dropped,
                                    })
                                report(images, image)
                                measured = 0 if previous_image_time is None else 1 / max(now - previous_image_time, 0.000001)
                                previous_image_time = now
                                report(messages, f'{"Simulated gradient" if simulated else "Temperature (°C)"} | {measured:.1f} FPS | frame {image[2]} | packet errors {decoder.errors} | gaps {pairer.dropped}')
                        elif packet.kind == 0x83:
                            if len(packet.payload) == 5 and packet.payload[0] == 1:
                                _, phase, detail, address = struct.unpack('<BBBH', packet.payload)
                                raise RuntimeError(f'Acquisition stopped: phase {phase}, detail {detail}, address 0x{address:04X}; no automatic retry')
                            raise RuntimeError(f'Device event: {packet.payload.hex()}')
                    if time.monotonic() > deadline:
                        if pending == 1 and hello_attempts < 3:
                            # HELLO is read-only; never replay START/configuration writes.
                            hello_attempts += 1
                            report(messages, f'Waiting for HELLO on {port} (attempt {hello_attempts}/3)')
                            link.write(b'\0')
                            request(link, 1)
                        elif pending:
                            name = {1: 'HELLO', 3: 'GET_CALIBRATION', 4: 'START', 5: 'STOP', 6: 'SET_CONFIG', 8: 'READ_EEPROM', 9: 'START_REAL'}.get(pending, str(pending))
                            raise RuntimeError(f'{name} timeout on {port} (packet errors: {decoder.errors}). '
                                               'Close other viewers/serial monitors and check the uploaded firmware.')
                        else:
                            raise RuntimeError(f'No subpages for {8 if real else 5} seconds on {port}')
            finally:
                # Best effort: never replay a modifying command after a timeout.
                if start_attempted:
                    link.write(encode(5, request_id + 1))
    except (OSError, ValueError, RuntimeError) as exc:
        report(messages, f'Connection stopped: {exc}')
        print(f'Connection stopped: {exc}', file=sys.stderr)
