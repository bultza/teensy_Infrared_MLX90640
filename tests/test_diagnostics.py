import struct
import unittest
from thermal_viewer.diagnostics import parse_diagnostic, describe_record


def payload(result=0, phase=0, detail=0, count=2):
    return bytes([7, result, 0x33, 1, 1]) + b''.join(
        struct.pack('<HHBBBI', reg, 0x1234 if phase == 0 else 0, phase, detail, count, 200)
        for reg in (0x8000, 0x800D))


class DiagnosticTests(unittest.TestCase):
    def test_success(self):
        levels, records = parse_diagnostic(payload())
        self.assertEqual(levels, bytes([0x33, 1, 1]))
        self.assertIn('0x8000 = 0x1234', describe_record(records[0]))

    def test_fault_descriptions(self):
        for phase, detail, count, text in ((1, 1, 0, 'bus not idle'), (2, 2, 0, 'address NACK'),
                                          (2, 4, 0, 'timeout'), (3, 1, 1, '1/2 bytes'),
                                          (4, 0, 0, 'skipped')):
            _, records = parse_diagnostic(payload(4, phase, detail, count))
            self.assertIn(text, describe_record(records[0]))

    def test_reject_malformed(self):
        for data in (payload()[:-1], payload(4), payload(0, 2, 2, 0), payload(0, 0, 0, 1)):
            with self.assertRaises(ValueError):
                parse_diagnostic(data)
