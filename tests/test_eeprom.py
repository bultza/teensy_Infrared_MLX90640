import json
from pathlib import Path
import struct
import tempfile
import unittest
import zlib
from contextlib import redirect_stdout
from io import StringIO

from thermal_viewer.eeprom import parse_eeprom, export_eeprom


def payload(words=None):
    raw = struct.pack('<832H', *(list(range(832)) if words is None else words))
    return bytes([8, 0]) + struct.pack('<HHI', 0x2400, 832, zlib.crc32(raw)) + raw


class EEPROMTests(unittest.TestCase):
    def test_complete_dump_and_byte_order(self):
        data = parse_eeprom(payload())
        self.assertEqual(data['words'], list(range(832)))
        self.assertEqual(data['word_count'], 832)
        self.assertEqual(len(data['sha256']), 64)

    def test_corruption_range_length_and_empty(self):
        wrong_range = bytearray(payload())
        wrong_range[2] ^= 1
        corrupt = bytearray(payload())
        corrupt[-1] ^= 1
        for data in (payload()[:-1], bytes(corrupt), bytes(wrong_range),
                     payload([0]*832), payload([65535]*832)):
            with self.assertRaises(ValueError):
                parse_eeprom(data)

    def test_export_does_not_overwrite_backup(self):
        with tempfile.TemporaryDirectory() as directory, redirect_stdout(StringIO()):
            path = Path(directory) / 'eeprom.json'
            export_eeprom(payload(), path)
            before = path.read_bytes()
            self.assertEqual(json.loads(before)['words'][100], 100)
            with self.assertRaises(FileExistsError):
                export_eeprom(payload(), path)
            self.assertEqual(path.read_bytes(), before)
