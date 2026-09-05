from __future__ import annotations

import binascii
import importlib.util
from pathlib import Path
import struct
import tempfile
from types import ModuleType
import unittest
import zlib

from mingtu_ai.benchmark.manifest import load_manifest


ROOT = Path(__file__).resolve().parents[1]
GENERATOR = ROOT / "benchmark/generate_smoke_fixture.py"
FIXTURE = ROOT / "benchmark/dataset/generated/gemma4-smoke-shapes.png"
MANIFEST = ROOT / "benchmark/manifest.e2b-smoke.json"


def _load_generator() -> ModuleType:
    spec = importlib.util.spec_from_file_location("generate_smoke_fixture", GENERATOR)
    if spec is None or spec.loader is None:
        raise RuntimeError("could not load the smoke fixture generator")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _png_chunks(payload: bytes) -> list[tuple[bytes, bytes]]:
    if not payload.startswith(b"\x89PNG\r\n\x1a\n"):
        raise ValueError("fixture is not a PNG")
    chunks: list[tuple[bytes, bytes]] = []
    offset = 8
    while offset < len(payload):
        length = struct.unpack_from(">I", payload, offset)[0]
        kind = payload[offset + 4 : offset + 8]
        data_start = offset + 8
        data_end = data_start + length
        data = payload[data_start:data_end]
        expected_crc = struct.unpack_from(">I", payload, data_end)[0]
        actual_crc = binascii.crc32(kind + data) & 0xFFFFFFFF
        if expected_crc != actual_crc:
            raise ValueError(f"invalid CRC for {kind!r}")
        chunks.append((kind, data))
        offset = data_end + 4
    if offset != len(payload):
        raise ValueError("fixture has trailing or truncated bytes")
    return chunks


class E2BSmokeFixtureTests(unittest.TestCase):
    def test_committed_fixture_is_reproducible(self) -> None:
        generator = _load_generator()
        with tempfile.TemporaryDirectory() as directory:
            rebuilt = Path(directory) / "fixture.png"
            generator.write_fixture(rebuilt)
            self.assertEqual(rebuilt.read_bytes(), FIXTURE.read_bytes())

    def test_fixture_is_well_formed_rgb_png(self) -> None:
        chunks = _png_chunks(FIXTURE.read_bytes())
        self.assertEqual(
            [kind for kind, _data in chunks],
            [b"IHDR", b"IDAT", b"IEND"],
        )

        width, height, depth, color_type, compression, filter_method, interlace = (
            struct.unpack(">IIBBBBB", chunks[0][1])
        )
        self.assertEqual((width, height), (640, 400))
        self.assertEqual((depth, color_type), (8, 2))
        self.assertEqual((compression, filter_method, interlace), (0, 0, 0))

        pixels = zlib.decompress(chunks[1][1])
        self.assertEqual(len(pixels), height * (1 + width * 3))
        self.assertTrue(all(pixels[row * (1 + width * 3)] == 0 for row in range(height)))

    def test_manifest_references_only_the_public_fixture(self) -> None:
        manifest = load_manifest(MANIFEST)
        self.assertEqual(manifest.benchmark_id, "mingtu-e2b-multimodal-smoke-v1")
        self.assertEqual(manifest.model_id, "gemma-4-E2B-it-UD-Q4_K_XL.gguf")
        self.assertEqual(len(manifest.cases), 1)
        self.assertEqual(manifest.cases[0].source_path, FIXTURE)
        self.assertEqual(manifest.cases[0].expected_adapter_kind, "image")


if __name__ == "__main__":
    unittest.main()
