"""Lossless / bounded-error packing of ordered integer micrometer gauge readings.

Container format (all integers little-endian, no padding):

    offset  size  field
    0       4     magic  b"TGPA"  (Track Gauge Package)
    4       1     version = 1
    5       1     codec id (see CODEC_* constants)
    6       1     flags  (bit 0: approximated, i.e. samples differ from input)
    7       1     reserved = 0
    8       4     sample_count          (uint32)
    12      4     payload_length        (uint32)
    16      4     crc32 of payload      (uint32)
    20      8     lower bound (int64, micrometers, inclusive)
    28      8     upper bound (int64, micrometers, inclusive)
    36      8     per-sample absolute error limit (int64, micrometers)
    44      4     header crc32 (uint32, over bytes 0..43)
    48      ...   payload

The package is self-describing: it carries the pass interval, the per-sample
error limit, the sample count and the codec id, so :func:`decode_package`
reconstructs the values in the original sampling order without out-of-band
information.  See FORMAT.md for the full specification.
"""

from __future__ import annotations

import struct
import zlib
from typing import List, Sequence

MAGIC = b"TGPA"
VERSION = 1
HEADER_SIZE = 48
FLAG_APPROXIMATED = 1 << 0

# Hard ceilings that keep a crafted header from forcing huge allocations.
MAX_SAMPLES = 5_000_000
INT32_MIN = -(2**31)
INT32_MAX = 2**31 - 1

CODEC_RAW = 1                    # int32 per sample, little-endian
CODEC_DELTA_VARINT = 2           # first value int64, then zigzag varint deltas
CODEC_DEFLATE_RAW = 3            # deflate over the int32 stream
CODEC_DEFLATE_DELTA = 4          # deflate over the delta-varint stream
CODEC_DELTA2_VARINT = 5          # first value, first delta, then zigzag 2nd deltas

CODEC_NAMES = {
    CODEC_RAW: "raw-int32",
    CODEC_DELTA_VARINT: "delta-zigzag-varint",
    CODEC_DEFLATE_RAW: "deflate-int32",
    CODEC_DEFLATE_DELTA: "deflate-delta-varint",
    CODEC_DELTA2_VARINT: "delta2-zigzag-varint",
}
ALL_CODECS = tuple(CODEC_NAMES)


class PackError(Exception):
    """Raised when a package cannot be produced."""


class PackageTooLarge(PackError):
    """The smallest acceptable package still exceeds the byte budget."""


def verdict(value: int, lower: int, upper: int) -> str:
    """Classify a single reading.  The accepted interval is inclusive."""
    if value < lower:
        return "narrow"   # 偏窄: below the accepted interval
    if value > upper:
        return "wide"     # 偏宽: above the accepted interval
    return "pass"         # 合格


VERDICT_LABELS = {"narrow": "偏窄", "pass": "合格", "wide": "偏宽"}


def _zigzag(n: int) -> int:
    return (n << 1) ^ (n >> 63)


def _unzigzag(n: int) -> int:
    return (n >> 1) ^ -(n & 1)


def _varint_encode(n: int, out: bytearray) -> None:
    while True:
        b = n & 0x7F
        n >>= 7
        if n:
            out.append(b | 0x80)
        else:
            out.append(b)
            return


def _varint_decode(buf: bytes, pos: int) -> tuple[int, int]:
    shift = 0
    result = 0
    while True:
        if pos >= len(buf):
            raise ValueError("truncated varint")
        b = buf[pos]
        pos += 1
        result |= (b & 0x7F) << shift
        if not b & 0x80:
            return result, pos
        shift += 7
        if shift > 63:
            raise ValueError("varint too long")


def _delta_stream(values: Sequence[int], second_order: bool) -> bytes:
    out = bytearray()
    if not values:
        return bytes(out)
    out += struct.pack("<q", values[0])
    if len(values) == 1:
        return bytes(out)
    first_delta = values[1] - values[0]
    out += struct.pack("<q", first_delta) if second_order else b""
    if second_order:
        prev = values[1]
        prev_delta = first_delta
        for v in values[2:]:
            d = v - prev
            _varint_encode(_zigzag(d - prev_delta), out)
            prev = v
            prev_delta = d
    else:
        prev = values[0]
        for v in values[1:]:
            _varint_encode(_zigzag(v - prev), out)
            prev = v
    return bytes(out)


def encode_payload(codec: int, values: Sequence[int]) -> bytes:
    if codec == CODEC_RAW:
        return struct.pack(f"<{len(values)}i", *values)
    if codec == CODEC_DELTA_VARINT:
        return _delta_stream(values, second_order=False)
    if codec == CODEC_DEFLATE_RAW:
        return zlib.compress(struct.pack(f"<{len(values)}i", *values), 9)
    if codec == CODEC_DEFLATE_DELTA:
        return zlib.compress(_delta_stream(values, second_order=False), 9)
    if codec == CODEC_DELTA2_VARINT:
        return _delta_stream(values, second_order=True)
    raise PackError(f"unknown codec {codec}")


def _bounded_inflate(payload: bytes, expected: int) -> bytes:
    """Inflate without ever producing more than *expected* bytes + 1."""
    obj = zlib.decompressobj()
    raw = obj.decompress(payload, expected + 1)
    if len(raw) > expected or obj.unconsumed_tail:
        raise ValueError("deflate payload expands beyond declared size")
    raw += obj.flush()
    if len(raw) > expected or not obj.eof:
        raise ValueError("deflate payload expands beyond declared size")
    return raw


def _decode_payload(codec: int, payload: bytes, count: int) -> List[int]:
    if count > MAX_SAMPLES:
        raise ValueError("sample count above decoder limit")
    if codec == CODEC_RAW:
        if len(payload) != count * 4:
            raise ValueError("raw payload length mismatch")
        return list(struct.unpack(f"<{count}i", payload))
    if codec == CODEC_DEFLATE_RAW:
        raw = _bounded_inflate(payload, count * 4)
        if len(raw) != count * 4:
            raise ValueError("deflate payload length mismatch")
        return list(struct.unpack(f"<{count}i", raw))
    if codec == CODEC_DELTA_VARINT:
        if count == 0:
            if payload:
                raise ValueError("unexpected payload for empty package")
            return []
        if len(payload) < 8:
            raise ValueError("truncated delta payload")
        (first,) = struct.unpack_from("<q", payload, 0)
        values = [first]
        pos = 8
        prev = first
        while len(values) < count:
            zz, pos = _varint_decode(payload, pos)
            prev += _unzigzag(zz)
            if not INT32_MIN <= prev <= INT32_MAX:
                raise ValueError("decoded value out of int32 range")
            values.append(prev)
        if pos != len(payload):
            raise ValueError("trailing bytes in delta payload")
        return values
    if codec == CODEC_DEFLATE_DELTA:
        # Worst case delta stream: first value + first delta + 10 bytes/sample.
        limit = 16 + count * 10
        return _decode_payload(
            CODEC_DELTA_VARINT, _bounded_inflate(payload, limit), count
        )
    if codec == CODEC_DELTA2_VARINT:
        if count == 0:
            if payload:
                raise ValueError("unexpected payload for empty package")
            return []
        if len(payload) < 8:
            raise ValueError("truncated delta2 payload")
        (first,) = struct.unpack_from("<q", payload, 0)
        values = [first]
        pos = 8
        prev = first
        prev_delta = 0
        if count >= 2:
            if len(payload) < 16:
                raise ValueError("truncated delta2 payload")
            (prev_delta,) = struct.unpack_from("<q", payload, 8)
            prev = first + prev_delta
            if not INT32_MIN <= prev <= INT32_MAX:
                raise ValueError("decoded value out of int32 range")
            values.append(prev)
            pos = 16
        while len(values) < count:
            zz, pos = _varint_decode(payload, pos)
            prev_delta += _unzigzag(zz)
            prev += prev_delta
            if not INT32_MIN <= prev <= INT32_MAX:
                raise ValueError("decoded value out of int32 range")
            values.append(prev)
        if pos != len(payload):
            raise ValueError("trailing bytes in delta2 payload")
        return values
    raise ValueError(f"unknown codec {codec}")


def build_package(
    codec: int,
    flags: int,
    values: Sequence[int],
    payload: bytes,
    lower: int,
    upper: int,
    eps: int,
) -> bytes:
    # The payload length and CRC are uint32 fields; refuse anything that would
    # silently wrap rather than emitting a package that decodes to garbage.
    crc = zlib.crc32(payload) & 0xFFFFFFFF
    count = len(values)
    if count > 0xFFFFFFFF or len(payload) > 0xFFFFFFFF:
        raise PackError("package exceeds uint32 header fields")
    head = struct.pack(
        "<4sBBBBIIIqqqI",
        MAGIC, VERSION, codec, flags, 0,
        count, len(payload), crc,
        lower, upper, eps, 0,
    )
    head_crc = zlib.crc32(head[:-4]) & 0xFFFFFFFF
    return head[:-4] + struct.pack("<I", head_crc) + payload


def decode_package(blob: bytes) -> dict:
    """Decode a package and return its values and self-describing metadata."""
    if len(blob) < HEADER_SIZE:
        raise ValueError("package shorter than header")
    (
        magic, version, codec, flags, _reserved,
        count, payload_len, payload_crc,
        lower, upper, eps, header_crc,
    ) = struct.unpack_from("<4sBBBBIIIqqqI", blob, 0)
    if magic != MAGIC:
        raise ValueError("bad magic")
    if version != VERSION:
        raise ValueError(f"unsupported version {version}")
    if codec not in CODEC_NAMES:
        raise ValueError(f"unknown codec id {codec}")
    if header_crc != zlib.crc32(blob[: HEADER_SIZE - 4]) & 0xFFFFFFFF:
        raise ValueError("header crc mismatch")
    if count > MAX_SAMPLES:
        raise ValueError("sample count above decoder limit")
    payload = blob[HEADER_SIZE:]
    if len(payload) != payload_len:
        raise ValueError("payload length mismatch")
    if zlib.crc32(payload) & 0xFFFFFFFF != payload_crc:
        raise ValueError("payload crc mismatch")
    if not lower <= upper:
        raise ValueError("invalid pass interval in header")
    if eps < 0:
        raise ValueError("invalid error limit in header")
    values = _decode_payload(codec, payload, count)
    if len(values) != count:
        raise ValueError("decoded sample count mismatch")
    return {
        "values": values,
        "lower": lower,
        "upper": upper,
        "eps": eps,
        "approximated": bool(flags & FLAG_APPROXIMATED),
        "codec": CODEC_NAMES[codec],
        "count": count,
        "bytes": len(blob),
    }
