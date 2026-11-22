import csv
import io
import unittest

from app import pack, transfer


class VerdictTests(unittest.TestCase):
    def test_inclusive_interval(self):
        self.assertEqual(pack.verdict(1000, 1000, 2000), "pass")
        self.assertEqual(pack.verdict(2000, 1000, 2000), "pass")
        self.assertEqual(pack.verdict(1001, 1000, 2000), "pass")

    def test_outside(self):
        self.assertEqual(pack.verdict(999, 1000, 2000), "narrow")
        self.assertEqual(pack.verdict(2001, 1000, 2000), "wide")


class RoundTripTests(unittest.TestCase):
    def test_every_codec_round_trips(self):
        values = [1435000, 1435001, 1435003, 1435003, 1434998, 1435002]
        for codec in pack.ALL_CODECS:
            payload = pack.encode_payload(codec, values)
            blob = pack.build_package(codec, 0, values, payload, 1000, 2000, 5)
            decoded = pack.decode_package(blob)
            self.assertEqual(decoded["values"], values, codec)
            self.assertEqual(decoded["codec"], pack.CODEC_NAMES[codec])

    def test_empty_and_single(self):
        for values in ([], [7]):
            for codec in pack.ALL_CODECS:
                payload = pack.encode_payload(codec, values)
                blob = pack.build_package(codec, 0, values, payload, 0, 10, 1)
                self.assertEqual(pack.decode_package(blob)["values"], values)

    def test_crc_detects_corruption(self):
        values = [1, 2, 3]
        blob = pack.build_package(
            pack.CODEC_RAW, 0, values,
            pack.encode_payload(pack.CODEC_RAW, values), 0, 10, 1,
        )
        broken = bytearray(blob)
        broken[HEADER_PAYLOAD_BYTE] ^= 0xFF
        with self.assertRaises(ValueError):
            pack.decode_package(bytes(broken))


HEADER_PAYLOAD_BYTE = pack.HEADER_SIZE


class CsvTests(unittest.TestCase):
    def test_one_per_line(self):
        readings = transfer.parse_readings(b"100\n200\n\n300\n")
        self.assertEqual(readings.values, [100, 200, 300])

    def test_columns_and_header(self):
        readings = transfer.parse_readings(b"value,100,200,300\n")
        self.assertEqual(readings.values, [100, 200, 300])

    def test_rejects_non_integer(self):
        with self.assertRaises(transfer.InputError):
            transfer.parse_readings(b"100\n1.5\n")

    def test_rejects_out_of_range(self):
        with self.assertRaises(transfer.InputError):
            transfer.parse_readings(b"99999999999\n")

    def test_rejects_empty(self):
        with self.assertRaises(transfer.InputError):
            transfer.parse_readings(b"\n\n")


class DeliveryTests(unittest.TestCase):
    def test_lossless_when_it_fits(self):
        values = [1435000 + (i % 7) for i in range(500)]
        delivery = transfer.deliver(values, 1434000, 1436000, 0, 4096)
        self.assertFalse(delivery.approximated)
        self.assertEqual(delivery.max_error, 0)
        self.assertTrue(delivery.verified)

    def test_threshold_boundary(self):
        values = [1435000 + (i % 7) for i in range(500)]
        exact = transfer.deliver(values, 1434000, 1436000, 0, 10**9)
        size = len(exact.package)
        self.assertFalse(
            transfer.deliver(values, 1434000, 1436000, 0, size).approximated
        )
        with self.assertRaises(pack.PackageTooLarge):
            transfer.deliver(values, 1434000, 1436000, 0, size - 1)

    def test_approximation_within_limit(self):
        # Alternating noise is expensive to store losslessly at eps=0; averaging
        # whole blocks reproduces it exactly, but is flagged as approximated.
        values = [1500000 + (500 if i % 2 else -500) for i in range(20000)]
        lossless = transfer.deliver(values, 1400000, 1600000, 0, 10**9)
        approx = transfer.deliver(
            values, 1400000, 1600000, 600, 100,
        )
        self.assertTrue(approx.approximated)
        self.assertGreater(approx.max_error, 0)
        self.assertLessEqual(approx.max_error, 600)
        self.assertLessEqual(len(approx.package), 100)
        self.assertLessEqual(len(approx.package), len(lossless.package))
        decoded = pack.decode_package(approx.package)
        self.assertEqual(decoded["count"], len(values))
        self.assertTrue(all(
            abs(a - b) <= 600 for a, b in zip(values, decoded["values"])
        ))

    def test_approximation_respects_error_and_verdicts(self):
        import random
        rng = random.Random(1234)
        values = [1500000 + rng.randint(-300, 300) for _ in range(8000)]
        eps = 400
        lossless = transfer.deliver(values, 1400000, 1600000, 0, 10**9)
        approx = transfer.deliver(
            values, 1400000, 1600000, eps, 2000,
        )
        self.assertTrue(approx.approximated)
        self.assertLess(len(approx.package), len(lossless.package))
        self.assertLessEqual(len(approx.package), 2000)
        self.assertLessEqual(approx.max_error, eps)
        decoded = pack.decode_package(approx.package)
        self.assertEqual(decoded["eps"], eps)
        for original, restored in zip(values, decoded["values"]):
            self.assertLessEqual(abs(original - restored), eps)
            self.assertEqual(
                pack.verdict(original, 1400000, 1600000),
                pack.verdict(restored, 1400000, 1600000),
            )

    def test_no_partial_package(self):
        values = list(range(1000))
        with self.assertRaises(pack.PackageTooLarge):
            transfer.deliver(values, 0, 2000, 0, pack.HEADER_SIZE - 1)


if __name__ == "__main__":
    unittest.main()
