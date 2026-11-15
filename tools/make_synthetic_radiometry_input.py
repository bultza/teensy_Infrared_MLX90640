"""Create deterministic numerical inputs without device identities or recorded scenes.

These artificial EEPROM/auxiliary values exercise the numerical formulas only.
They are not factory calibration and must never be uploaded to a sensor.
"""
import base64
import json
import struct
import zlib


def synthetic_input():
    words = [0] * 832
    settings = {16: 0x1000, 17: (-100) & 0xffff, 32: 0x1000, 33: 1000,
                48: 10000, 49: 12000, 50: 80, 51: 0x0180, 52: 0x1111,
                54: 0x0101, 55: 0x0101, 56: 0x2200, 57: 100, 63: 0x3311}
    for address, value in settings.items():
        words[address] = value
    for pixel in range(768):
        words[64 + pixel] = (1 + pixel % 8) << 4
    # EEPROM serial-number words 0x2407..0x2409 intentionally remain zero.
    calibration = zlib.crc32(struct.pack('<832H', *words))
    pages = []
    for page in (0, 1):
        ram = [0] * 832
        for pixel in range(768):
            ram[pixel] = (-100 + pixel % 100 + page * 2) & 0xffff
        for address, value in {768: 13595 + page * 15, 776: 100, 778: 10000 + page * 20,
                               800: 1000 + page * 2, 808: 101, 810: (-12288 + page) & 0xffff}.items():
            ram[address] = value
        header = struct.pack('<IIIIIBBHH', page, 1, 1000 + page * 500, 2,
                             calibration, page, 0, 8 | page, 0x1901)
        pages.append(base64.b64encode(header + struct.pack('<832H', *ram)).decode())
    return dict(input_provenance='Fully synthetic deterministic calibration and RAM; no sensor capture',
                input_generator='tools/make_synthetic_radiometry_input.py',
                eeprom=words, pages=pages)


if __name__ == '__main__':
    print(json.dumps(synthetic_input()))
