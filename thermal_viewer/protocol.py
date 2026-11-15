"""Version 1 framing, independent of serial and GUI dependencies."""
import struct
import time
import zlib
from dataclasses import dataclass

HEADER = struct.Struct('<BBHI')
SUBPAGE = struct.Struct('<IIIIIBBHH')


def cobs_encode(data):
    result = bytearray([0])
    index, code = 0, 1
    for value in data:
        if value == 0:
            result[index] = code
            index = len(result)
            result.append(0)
            code = 1
        else:
            result.append(value)
            code += 1
            if code == 255:
                result[index] = code
                index = len(result)
                result.append(0)
                code = 1
    result[index] = code
    return bytes(result)


def cobs_decode(data):
    result = bytearray()
    index = 0
    while index < len(data):
        code = data[index]
        if not code or index + code > len(data):
            raise ValueError('Invalid COBS')
        result.extend(data[index + 1:index + code])
        index += code
        if code != 255 and index < len(data):
            result.append(0)
    return bytes(result)


@dataclass
class Packet:
    kind: int
    identifier: int
    payload: bytes


def encode(kind, identifier, payload=b''):
    if len(payload) > 4096:
        raise ValueError('Payload too large')
    body = HEADER.pack(1, kind, len(payload), identifier) + payload
    return cobs_encode(body + struct.pack('<I', zlib.crc32(body))) + b'\0'


class Decoder:
    def __init__(self):
        self.buffer = bytearray()
        self.discard = False
        self.last = time.monotonic()
        self.errors = 0

    def feed(self, data):
        now = time.monotonic()
        if self.buffer and now - self.last > 1:
            self.buffer.clear()
            self.discard = True
            self.errors += 1
        if data:
            self.last = now
        packets = []
        for value in data:
            if value == 0:
                if self.buffer and not self.discard:
                    try:
                        body = cobs_decode(self.buffer)
                        version, kind, length, ident = HEADER.unpack_from(body)
                        if version != 1 or length > 4096 or len(body) != length + 12:
                            raise ValueError('Invalid header')
                        if zlib.crc32(body[:-4]) != struct.unpack('<I', body[-4:])[0]:
                            raise ValueError('CRC mismatch')
                        packets.append(Packet(kind, ident, body[8:-4]))
                    except (ValueError, struct.error):
                        self.errors += 1
                self.buffer.clear()
                self.discard = False
            elif not self.discard:
                self.buffer.append(value)
                if len(self.buffer) > 4140:
                    self.buffer.clear()
                    self.discard = True
                    self.errors += 1
        return packets


class Pairer:
    """Retain an adjacent coherent pair; optionally convert complete subpages to temperatures."""
    def __init__(self, converter=None):
        self.converter = converter
        self.pending = None
        self.last_sequence = None
        self.dropped = 0

    def accept(self, payload):
        if len(payload) != 1690:
            raise ValueError('Invalid SUBPAGE length')
        seq, pair, stamp, config, calibration, page, reserved, status, control = SUBPAGE.unpack_from(payload)
        if page not in (0, 1) or reserved or status & 1 != page:
            raise ValueError('Invalid subpage metadata')
        if self.last_sequence is not None and seq != (self.last_sequence + 1) & 0xffffffff:
            self.pending = None
            self.dropped += 1
        self.last_sequence = seq
        key = pair, config, calibration, control
        current = (key, page, payload, stamp)
        previous = self.pending
        self.pending = current
        if previous is None or previous[0] != key or previous[1] == page:
            return None
        self.pending = None
        if self.converter is not None:
            return self.converter.convert_pair(previous[2], payload), stamp, pair
        words = [struct.unpack_from('<768h', previous[2], 26),
                 struct.unpack_from('<768h', payload, 26)]
        # Control bit 12 selects chess mode; interleaved mode selects row parity.
        pixels = []
        for i in range(768):
            row, col = divmod(i, 32)
            selected = (row ^ col) & 1 if control & 0x1000 else row & 1
            pixels.append(words[0 if selected == previous[1] else 1][i])
        return pixels, stamp, pair
