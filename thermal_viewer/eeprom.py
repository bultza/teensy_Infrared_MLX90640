"""Validate and export a raw MLX90640 EEPROM dump, not calibrated temperatures."""
import hashlib
import json
from pathlib import Path
import struct
import zlib


def parse_eeprom(payload):
    """Check exact range, length, CRC, and obvious empty data before accepting a dump."""
    if len(payload) != 1674 or payload[:2] != bytes([8, 0]):
        raise ValueError('Invalid EEPROM response length or result')
    start, count, checksum = struct.unpack_from('<HHI', payload, 2)
    if (start, count) != (0x2400, 832):
        raise ValueError('Unexpected EEPROM range')
    raw = payload[10:]
    if zlib.crc32(raw) != checksum:
        raise ValueError('EEPROM data CRC mismatch')
    words = list(struct.unpack('<832H', raw))
    if all(word == 0 for word in words) or all(word == 0xffff for word in words):
        raise ValueError('EEPROM is entirely zero or 0xFFFF; dump is not accepted')
    return {
        'format': 'mlx90640-eeprom-v1',
        'sensor_address': 0x33,
        'start_address': start,
        'word_count': count,
        'byte_order': 'little-endian',
        'crc32': f'{checksum:08x}',
        'sha256': hashlib.sha256(raw).hexdigest(),
        'words': words,
    }


def export_eeprom(payload, output=None):
    """Print a summary and optionally create a JSON file without overwriting a backup."""
    data = parse_eeprom(payload)
    print(f'EEPROM: {data["word_count"]} words / 1664 bytes, 0x2400..0x273F')
    print(f'CRC32: {data["crc32"]}; SHA256: {data["sha256"]}')
    print('First 16 words: ' + ' '.join(f'{word:04X}' for word in data['words'][:16]))
    print('Transfer integrity checked; radiometric calibration parameters are not yet validated.')
    if output:
        with Path(output).open('x', encoding='utf-8') as file:
            json.dump(data, file, indent=2)
            file.write('\n')
        print(f'Saved EEPROM backup: {output}')
    return data
