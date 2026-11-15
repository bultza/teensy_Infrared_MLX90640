import struct
import unittest
from thermal_viewer.settings import configuration_payload, confirmed_configuration


class SettingsTests(unittest.TestCase):
    def test_default_and_slow_bus(self):
        self.assertEqual(configuration_payload(), struct.pack('<IB', 400000, 3))
        self.assertEqual(configuration_payload(100, 1), struct.pack('<IB', 100000, 2))
        self.assertEqual(confirmed_configuration(bytes([6, 0])+struct.pack('<IHI', 400000, 0x1981, 3), 400, 2), (3, 0x1981))

    def test_invalid_combinations(self):
        for clock, fps in ((100, 2), (200, 1), (400, 4), (400, float('nan'))):
            with self.assertRaises(ValueError):
                configuration_payload(clock, fps)

    def test_readback_must_match(self):
        for extension in (struct.pack('<IHI', 100000, 0x1981, 3), struct.pack('<IHI', 400000, 0x1901, 3),
                          struct.pack('<IHI', 400000, 0x1981, 0), struct.pack('<IHI', 400000, 0x1985, 3)):
            with self.assertRaises(ValueError):
                confirmed_configuration(bytes([6, 0])+extension, 400, 2)
