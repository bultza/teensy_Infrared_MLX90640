import base64
import json
import math
from pathlib import Path
import struct
import unittest

from thermal_viewer.radiometry import Radiometry
from thermal_viewer.protocol import Pairer

DATA = json.loads((Path(__file__).parent/'data/radiometry_reference.json').read_text())


class RadiometryTests(unittest.TestCase):
    def test_public_fixture_contains_only_generated_inputs(self):
        from tools.make_synthetic_radiometry_input import synthetic_input
        generated = synthetic_input()
        self.assertEqual(DATA['eeprom'], generated['eeprom'])
        self.assertEqual(DATA['cases'][0]['pages'], generated['pages'])
        self.assertEqual(DATA['eeprom'][7:10], [0, 0, 0])
        self.assertIn('Fully synthetic', DATA['input_provenance'])

    def test_official_melexis_vectors(self):
        # C float arithmetic and Python doubles agree within 0.001°C on these vectors.
        for case in DATA['cases']:
            with self.subTest(chess=case['chess'], emissivity=case['emissivity']):
                converter = Radiometry(DATA['eeprom'], case['emissivity'], case['reflected_temperature'])
                pairer = Pairer(converter)
                first, second = (base64.b64decode(p) for p in case['pages'])
                self.assertIsNone(pairer.accept(first))
                actual, _, _ = pairer.accept(second)
                self.assertEqual(len(actual), 768)
                for a, expected in zip(actual, case['expected']):
                    self.assertAlmostEqual(a, expected, delta=0.001)
                self.assertAlmostEqual(converter.last_ambient, case['ambient'], delta=0.001)
                self.assertAlmostEqual(converter.last_vdd, case['vdd'], delta=0.0001)

    def test_invalid_settings_and_calibration(self):
        for emissivity in (0, -1, 1.1, math.nan, math.inf):
            with self.assertRaises(ValueError):
                Radiometry(DATA['eeprom'], emissivity)
        for reflected in (-273.15, math.nan, math.inf):
            with self.assertRaises(ValueError):
                Radiometry(DATA['eeprom'], reflected_temperature=reflected)
        for words in ([0]*832, [65535]*832, DATA['eeprom'][:-1]):
            with self.assertRaises(ValueError):
                Radiometry(words)

    def test_zero_gain_and_auxiliary_marker_rejected(self):
        first, second = (base64.b64decode(p) for p in DATA['cases'][0]['pages'])
        for index, value in ((778, 0), (800, 0x7fff), (0, 0x7fff)):
            damaged = bytearray(first)
            struct.pack_into('<H', damaged, 26 + 2*index, value)
            with self.assertRaises(ValueError):
                Radiometry(DATA['eeprom']).convert_pair(damaged, second)

    def test_calibration_instances_do_not_share_arrays(self):
        first, second = Radiometry(DATA['eeprom']), Radiometry(DATA['eeprom'])
        first.math.alpha[0] = 123
        first.math.brokenPixels.append(0)
        self.assertNotEqual(second.math.alpha[0], 123)
        self.assertEqual(second.math.brokenPixels, [])

    def test_faulty_pixels_are_masked(self):
        converter = Radiometry(DATA['eeprom'])
        converter.bad_pixels.add(0)
        result = converter.convert_pair(*(base64.b64decode(p) for p in DATA['cases'][0]['pages']))
        self.assertTrue(math.isnan(result[0]))
        self.assertTrue(math.isfinite(result[1]))
