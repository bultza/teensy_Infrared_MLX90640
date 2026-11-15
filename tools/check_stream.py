"""Hardware integration check: python -m tools.check_stream --port /dev/ttyACM0."""
import argparse
import struct
import time

import serial
from thermal_viewer.protocol import Decoder, Pairer, encode


def check(port, frames, real=True, i2c_khz=400, fps=2):
    decoder, pairer = Decoder(), Pairer()
    ident = 0
    stash = []
    with serial.Serial(port, 115200, timeout=0.05, write_timeout=1) as link:
        link.reset_input_buffer()

        def receive():
            if stash:
                return stash.pop(0)
            stash.extend(decoder.feed(link.read(4096)))
            return stash.pop(0) if stash else None

        def command(code, result=0, payload=b''):
            nonlocal ident
            ident += 1
            link.write(encode(code, ident, payload))
            deadline = time.monotonic() + 3
            calibration = None
            while time.monotonic() < deadline:
                packet = receive()
                if packet is None:
                    continue
                if packet.kind == 0x81 and packet.identifier == ident:
                    assert len(packet.payload) == 1668
                    calibration = struct.unpack_from('<I', packet.payload)[0]
                if packet.kind == 0x80 and packet.identifier == ident:
                    assert packet.payload[:2] == bytes([code, result]), packet
                    if code == 3:
                        assert calibration == 1
                    return packet.payload
            raise RuntimeError(f'Command {code} timed out')

        hello = command(1)
        assert len(hello) == 13 and struct.unpack_from('<I', hello, 8)[0] & (8 if real else 1)
        if real:
            from thermal_viewer.settings import configuration_payload, confirmed_configuration
            command(5)
            config_revision, confirmed_control = confirmed_configuration(command(6, payload=configuration_payload(i2c_khz, fps)), i2c_khz, fps)
            time.sleep(5)
            from thermal_viewer.eeprom import parse_eeprom
            calibration = int(parse_eeprom(command(8))['crc32'], 16)
        else:
            command(3)
        # Corrupt a valid CRC without damaging packet framing. No response is expected.
        from thermal_viewer.protocol import cobs_decode, cobs_encode
        corrupt = bytearray(cobs_decode(encode(2, 999)[:-1]))
        corrupt[-1] ^= 1
        link.write(cobs_encode(corrupt) + b'\0')
        status = command(2)
        assert struct.unpack_from('<I', status, 7)[0] >= 1
        command(9 if real else 4)
        try:
            deadline = time.monotonic() + (max(10, frames * 6) if real else max(5, frames / 5))
            count = 0
            first_stamp = last_stamp = None
            while count < frames and time.monotonic() < deadline:
                packet = receive()
                if packet is not None and packet.kind == 0x83:
                    raise RuntimeError(f'Acquisition event: {packet.payload.hex()}')
                if packet is None or packet.kind != 0x82:
                    continue
                if real:
                    seq, pair, stamp, config, revision, page, reserved, status, control = struct.unpack_from('<IIIIIBBHH', packet.payload)
                    assert config == config_revision and revision == calibration
                    assert control == confirmed_control
                    assert status & 8
                    assert page == status & 1
                    assert any(struct.unpack_from('<64H', packet.payload, 26 + 768 * 2))
                image = pairer.accept(packet.payload)
                if image is None:
                    continue
                pixels, stamp, pair = image
                if first_stamp is None:
                    first_stamp = stamp
                last_stamp = stamp
                expected = [1000 + (i % 32)*100 + (i // 32)*50 +
                            (2000 if i % 32 == pair % 32 else 0) for i in range(768)]
                if real:
                    assert len(pixels) == 768 and len(set(pixels)) > 1
                else:
                    assert pixels == expected, f'Gradient mismatch in pair {pair}'
                count += 1
            assert count == frames, f'Only received {count}/{frames} frames'
            assert decoder.errors == 0
            if not real:
                assert pairer.dropped == 0
        finally:
            command(5)
        deadline = time.monotonic() + 0.3
        while time.monotonic() < deadline:
            packet = receive()
            assert packet is None or packet.kind != 0x82, 'Image after STOP acknowledgement'
        command(5)  # Idempotent STOP.
        command(6, result=2)  # Unsupported configuration.
        if real and frames > 1:
            measured = (frames - 1) * 1000 / ((last_stamp - first_stamp) & 0xffffffff)
            print(f'Measured {measured:.2f} complete FPS; I2C {i2c_khz} kHz; control 0x{confirmed_control:04X}')
        print(f'PASS: {frames} {"real raw" if real else "exact gradient"} frames; CRC recovery; STOP; no packet errors; gaps={pairer.dropped}.')


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--port', required=True)
    parser.add_argument('--test-pattern', action='store_true')
    parser.add_argument('--frames', type=int, default=30)
    parser.add_argument('--i2c-khz', type=int, choices=(100, 400), default=400)
    parser.add_argument('--fps', type=float, choices=(0.25, 0.5, 1, 2), default=2)
    args = parser.parse_args()
    if args.frames <= 0:
        parser.error('--frames must be positive')
    check(args.port, args.frames, not args.test_pattern, args.i2c_khz, args.fps)
