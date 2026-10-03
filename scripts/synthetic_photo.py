"""Synthetic "phone photos" of packing lists: grey PNGs with block-letter text and noise.

Standard library only (zlib + struct). Good enough for Textract to read, imperfect enough to
give realistic, sub-0.95 confidences.
"""

from __future__ import annotations

import random
import struct
import zlib

# fmt: off
_FONT: dict[str, tuple[int, ...]] = {
    "A": (14, 17, 17, 31, 17, 17, 17), "B": (30, 17, 17, 30, 17, 17, 30),
    "C": (14, 17, 16, 16, 16, 17, 14), "D": (30, 17, 17, 17, 17, 17, 30),
    "E": (31, 16, 16, 30, 16, 16, 31), "F": (31, 16, 16, 30, 16, 16, 16),
    "G": (14, 17, 16, 23, 17, 17, 15), "H": (17, 17, 17, 31, 17, 17, 17),
    "I": (14, 4, 4, 4, 4, 4, 14), "J": (7, 2, 2, 2, 2, 18, 12),
    "K": (17, 18, 20, 24, 20, 18, 17), "L": (16, 16, 16, 16, 16, 16, 31),
    "M": (17, 27, 21, 21, 17, 17, 17), "N": (17, 17, 25, 21, 19, 17, 17),
    "O": (14, 17, 17, 17, 17, 17, 14), "P": (30, 17, 17, 30, 16, 16, 16),
    "Q": (14, 17, 17, 17, 21, 18, 13), "R": (30, 17, 17, 30, 20, 18, 17),
    "S": (15, 16, 16, 14, 1, 1, 30), "T": (31, 4, 4, 4, 4, 4, 4),
    "U": (17, 17, 17, 17, 17, 17, 14), "V": (17, 17, 17, 17, 17, 10, 4),
    "W": (17, 17, 17, 21, 21, 21, 10), "X": (17, 17, 10, 4, 10, 17, 17),
    "Y": (17, 17, 17, 10, 4, 4, 4), "Z": (31, 1, 2, 4, 8, 16, 31),
    "0": (14, 17, 19, 21, 25, 17, 14), "1": (4, 12, 4, 4, 4, 4, 14),
    "2": (14, 17, 1, 2, 4, 8, 31), "3": (31, 2, 4, 2, 1, 17, 14),
    "4": (2, 6, 10, 18, 31, 2, 2), "5": (31, 16, 30, 1, 1, 17, 14),
    "6": (6, 8, 16, 30, 17, 17, 14), "7": (31, 1, 2, 4, 8, 8, 8),
    "8": (14, 17, 17, 14, 17, 17, 14), "9": (14, 17, 17, 15, 1, 2, 12),
    "-": (0, 0, 0, 31, 0, 0, 0), ".": (0, 0, 0, 0, 0, 12, 12),
    ":": (0, 12, 12, 0, 12, 12, 0), "/": (0, 1, 2, 4, 8, 16, 0),
    "#": (10, 10, 31, 10, 31, 10, 10), ",": (0, 0, 0, 0, 12, 4, 8),
    " ": (0, 0, 0, 0, 0, 0, 0),
}
# fmt: on


def _png(width: int, height: int, rows: list[bytearray]) -> bytes:
    def chunk(kind: bytes, data: bytes) -> bytes:
        body = kind + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body))

    raw = b"".join(b"\x00" + bytes(row) for row in rows)
    header = struct.pack(">IIBBBBB", width, height, 8, 0, 0, 0, 0)  # 8-bit greyscale
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", zlib.compress(raw, 9))
        + chunk(b"IEND", b"")
    )


def render(lines: list[str], *, seed: int = 1, scale: int = 4, blur: bool = True) -> bytes:
    """Dark text on a paper-grey background, with sensor noise and uneven light."""
    rng = random.Random(seed)
    text = [line.upper() for line in lines]
    margin = 6 * scale
    width = max(len(line) for line in text) * 6 * scale + 2 * margin
    height = len(text) * 10 * scale + 2 * margin
    rows = [
        bytearray(
            max(0, min(255, 214 + (x * 18) // width - (y * 14) // height + rng.randint(-9, 9)))
            for x in range(width)
        )
        for y in range(height)
    ]
    for n, line in enumerate(text):
        top = margin + n * 10 * scale
        for i, char in enumerate(line):
            glyph = _FONT.get(char, _FONT[" "])
            left = margin + i * 6 * scale
            for gy, bits in enumerate(glyph):
                for gx in range(5):
                    if not bits & (1 << (4 - gx)):
                        continue
                    for dy in range(scale):
                        row = rows[top + gy * scale + dy]
                        for dx in range(scale):
                            row[left + gx * scale + dx] = 40 + rng.randint(0, 30)
    if blur:
        for y in range(1, height - 1):
            above, row, below = rows[y - 1], rows[y], rows[y + 1]
            for x in range(1, width - 1, 3):
                row[x] = (row[x - 1] + row[x] + row[x + 1] + above[x] + below[x]) // 5
    return _png(width, height, rows)
