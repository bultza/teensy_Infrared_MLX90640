"""Exercise the actual receiver through a simulated serial device."""
import queue
import struct
import threading
import unittest
import zlib
import json
import base64
from pathlib import Path
from unittest.mock import patch

from thermal_viewer.receiver import run_serial
from thermal_viewer.protocol import Decoder, SUBPAGE, encode


class StreamTests(unittest.TestCase):
    def exercise_stream(self, lose_first_hello=False, real=False, config_failure=None):
        stop = threading.Event()
        commands = []
        fixture = json.loads((Path(__file__).parent/'data/radiometry_reference.json').read_text()) if real else None
        raw_eeprom = struct.pack('<832H', *(fixture['eeprom'] if real else range(832)))
        revision = zlib.crc32(raw_eeprom) if real else 7

        class Device:
            def __init__(self, *args, **kwargs):
                self.data = bytearray()

            def __enter__(self):
                return self

            def __exit__(self, *args):
                pass

            def reset_input_buffer(self):
                pass

            def write(self, data):
                packets = Decoder().feed(data)
                if not packets:
                    return len(data)
                packet = packets[0]
                commands.append(packet.kind)
                if lose_first_hello and commands == [1]:
                    return len(data)
                if packet.kind == 3:
                    self.data.extend(encode(0x81, packet.identifier, struct.pack('<I', 7) + bytes(1664)))
                extra = struct.pack('<IBBIB', 1, 0, 5, 31, 1) if packet.kind == 1 else b''
                if packet.kind == 6:
                    self.assert_config = packet.payload
                    if packet.payload != struct.pack('<IB', 400000, 3):
                        raise ValueError('Host did not request default 400 kHz / 2 FPS')
                    extra = struct.pack('<IHI', 100000 if config_failure == 'readback' else 400000, 0x1981, 3)
                    if config_failure == 'timeout':
                        return len(data)
                if packet.kind == 8:
                    extra = struct.pack('<HHI', 0x2400, 832, revision) + raw_eeprom
                self.data.extend(encode(0x80, packet.identifier, bytes([packet.kind, 0]) + extra))
                if packet.kind == (9 if real else 4):
                    for page in (0, 1):
                        if real:
                            payload = bytearray(base64.b64decode(fixture['cases'][0]['pages'][page]))
                            struct.pack_into('<I', payload, 12, 3)
                            struct.pack_into('<H', payload, 24, 0x1981)
                        else:
                            payload = SUBPAGE.pack(page, 1, 100, 1, revision, page, 0, page | 8, 0x1000)
                            payload += struct.pack('<832H', *([100 + page]*832))
                        self.data.extend(encode(0x82, page, payload))
                return len(data)

            def read(self, size):
                if not self.data and commands[-1] == (9 if real else 4):
                    stop.set()
                    return b''
                result = bytes(self.data[:13])
                del self.data[:13]
                return result

        images, messages = queue.Queue(maxsize=1), queue.Queue(maxsize=1)
        from itertools import count
        with patch('serial.Serial', Device), patch.object(stop, 'wait', return_value=False), patch('thermal_viewer.receiver.time.monotonic', side_effect=count(0, 0.01)):
            run_serial('fake', stop, images, messages, real)
        if config_failure:
            self.assertEqual(commands, [1, 5, 6])
            self.assertTrue(images.empty())
            self.assertIn('Connection stopped:', messages.get_nowait())
            return
        self.assertEqual(commands, ([1, 1] if lose_first_hello else [1]) + ([5, 6, 8, 9, 5] if real else [3, 4, 5]))
        pixels = images.get_nowait()[0]
        if real:
            for actual, expected in zip(pixels, fixture['cases'][0]['expected']):
                self.assertAlmostEqual(actual, expected, delta=0.001)
        else:
            self.assertEqual(pixels[:2], [100, 101])

    def test_handshake_stream_and_stop(self):
        self.exercise_stream()

    def test_first_hello_lost(self):
        self.exercise_stream(lose_first_hello=True)

    def test_real_handshake_and_pairing(self):
        self.exercise_stream(real=True)

    def test_configuration_readback_failure_does_not_start(self):
        self.exercise_stream(real=True, config_failure='readback')

    def test_configuration_timeout_is_not_replayed(self):
        self.exercise_stream(real=True, config_failure='timeout')


if __name__ == '__main__':
    unittest.main()
