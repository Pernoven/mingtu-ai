"""Generate the public, deterministic image used by the E2B smoke test.

The generator uses only the Python standard library.  It deliberately omits
timestamps and text chunks so repeated runs produce byte-for-byte identical
output without leaking host metadata.
"""

from __future__ import annotations

import argparse
import binascii
from pathlib import Path
import struct
import zlib


WIDTH = 640
HEIGHT = 400
BACKGROUND = (248, 249, 252)
DEFAULT_OUTPUT = Path(__file__).parent / "dataset/generated/gemma4-smoke-shapes.png"


def _inside_triangle(
    x: int,
    y: int,
    first: tuple[int, int],
    second: tuple[int, int],
    third: tuple[int, int],
) -> bool:
    """Return whether a pixel lies inside the triangle, edges included."""

    def edge(a: tuple[int, int], b: tuple[int, int]) -> int:
        return (x - b[0]) * (a[1] - b[1]) - (a[0] - b[0]) * (y - b[1])

    values = (edge(first, second), edge(second, third), edge(third, first))
    return not (
        any(value < 0 for value in values) and any(value > 0 for value in values)
    )


def _pixel(x: int, y: int) -> tuple[int, int, int]:
    """Return one RGB pixel for the deliberately simple test illustration."""

    if 70 <= x < 230 and 120 <= y < 280:
        return (43, 109, 232)  # blue square
    if (x - 355) ** 2 + (y - 200) ** 2 <= 80**2:
        return (242, 139, 34)  # orange circle
    if _inside_triangle(x, y, (505, 280), (585, 280), (545, 120)):
        return (42, 166, 93)  # green triangle
    return BACKGROUND


def _chunk(kind: bytes, payload: bytes) -> bytes:
    checksum = binascii.crc32(kind + payload) & 0xFFFFFFFF
    return struct.pack(">I", len(payload)) + kind + payload + struct.pack(">I", checksum)


def build_png() -> bytes:
    """Build a deterministic RGB PNG containing three colored shapes."""

    rows = bytearray()
    for y in range(HEIGHT):
        rows.append(0)  # PNG filter type: None
        for x in range(WIDTH):
            rows.extend(_pixel(x, y))

    header = struct.pack(">IIBBBBB", WIDTH, HEIGHT, 8, 2, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + b"".join(
        (
            _chunk(b"IHDR", header),
            _chunk(b"IDAT", zlib.compress(bytes(rows), level=9)),
            _chunk(b"IEND", b""),
        )
    )


def write_fixture(path: Path) -> None:
    """Write the fixture, creating its parent directory when needed."""

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(build_png())


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output",
        type=Path,
        default=DEFAULT_OUTPUT,
        help="output PNG path (default: repository smoke fixture)",
    )
    arguments = parser.parse_args()
    write_fixture(arguments.output)
    print(arguments.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
