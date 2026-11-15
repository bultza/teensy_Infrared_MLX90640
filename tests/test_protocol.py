import struct
import unittest

from thermal_viewer.protocol import Decoder, Pairer, SUBPAGE, cobs_decode, cobs_encode, encode


def subpage(seq, page, pair=1, control=0x1000):
    return SUBPAGE.pack(seq, pair, 100, 1, 1, page, 0, page, control) + struct.pack('<832H', *([10 + page] * 832))


class ProtocolTests(unittest.TestCase):
    def test_cobs_boundaries(self):
        for data in (b'', b'\0', bytes(range(256)), b'x'*254, b'x'*510, b'\0'*100):
            self.assertEqual(cobs_decode(cobs_encode(data)), data)

    def test_fragmented_and_combined(self):
        wire = encode(1, 42, bytes(range(256))) + encode(4, 43)
        decoder = Decoder()
        packets = []
        for byte in wire:
            packets.extend(decoder.feed(bytes([byte])))
        self.assertEqual([(p.kind, p.identifier) for p in packets], [(1, 42), (4, 43)])
        self.assertEqual(packets[0].payload, bytes(range(256)))

    def test_corruption_overflow_and_recovery(self):
        wire = bytearray(encode(1, 1, b'hello'))
        wire[-3] ^= 1
        decoder = Decoder()
        packets = decoder.feed(bytes(wire) + b'x'*4200 + b'\0' + encode(4, 2))
        self.assertEqual([p.identifier for p in packets], [2])
        self.assertEqual(decoder.errors, 2)

    def test_timeout_discards_to_delimiter(self):
        decoder = Decoder()
        decoder.feed(b'abc')
        decoder.last -= 2
        self.assertEqual(decoder.feed(b'def\0' + encode(1, 1))[0].identifier, 1)
        self.assertEqual(decoder.errors, 1)

    def test_chess_and_interleaved(self):
        for control in (0x1000, 0):
            pairer = Pairer()
            self.assertIsNone(pairer.accept(subpage(1, 0, control=control)))
            pixels, _, _ = pairer.accept(subpage(2, 1, control=control))
            self.assertEqual(pixels[0], 10)
            self.assertEqual(pixels[1], 11 if control else 10)
            self.assertEqual(pixels[32], 11)

    def test_gap_and_pair_mismatch(self):
        pairer = Pairer()
        pairer.accept(subpage(1, 0))
        self.assertIsNone(pairer.accept(subpage(3, 1)))
        self.assertIsNone(pairer.accept(subpage(4, 0, pair=2)))
        self.assertEqual(pairer.dropped, 1)


if __name__ == '__main__':
    unittest.main()
