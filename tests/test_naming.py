from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import errno
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from pydantic import ValidationError

from mingtu_ai.models import AnalysisInput, NamingSuggestion
from mingtu_ai.naming import (
    GENERIC_FILENAME_RE,
    AtomicRenameUnavailableError,
    RenameError,
    build_naming_prompt,
    preserved_suffix,
    rename_noreplace,
    rename_unique,
    sanitize_stem,
)


class PromptTests(unittest.TestCase):
    def test_generic_screenshot_pattern_matching(self) -> None:
        self.assertTrue(bool(GENERIC_FILENAME_RE.search("螢幕截圖_20260905_123456")))
        self.assertTrue(bool(GENERIC_FILENAME_RE.search("Screenshot_2026-09-05")))
        self.assertTrue(bool(GENERIC_FILENAME_RE.search("Selection_001")))
        self.assertFalse(bool(GENERIC_FILENAME_RE.search("PyTorch_CUDA_OOM_Report")))

    def test_build_naming_prompt_adds_generic_note_and_image_guidance(self) -> None:
        generic_item = AnalysisInput(
            source_path=Path("螢幕截圖_20260905_123456.png"),
            mime_type="image/png",
            kind="image",
            image_paths=(Path("螢幕截圖_20260905_123456.png"),),
        )
        prompt = build_naming_prompt(generic_item)
        self.assertIn("Uninformative generic screenshot", prompt)
        self.assertIn("Attached image payload provided for vision analysis", prompt)
        self.assertIn("NEVER return generic placeholder terms like \"螢幕截圖\"", prompt)


class SanitizingTests(unittest.TestCase):
    def test_unicode_terms_are_preserved_and_unsafe_runs_collapse(self) -> None:
        self.assertEqual(
            sanitize_stem("  PyTorch/CUDA:\x00  記憶體***不足  "),
            "PyTorch_CUDA_記憶體_不足",
        )
        self.assertEqual(sanitize_stem("C++ #error"), "C++_#error")

    def test_empty_result_uses_safe_fallback(self) -> None:
        self.assertEqual(sanitize_stem("///", fallback="old name"), "old_name")

    def test_compound_extension_fallback_does_not_duplicate_tar(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "archive.tar.gz"
            source.write_bytes(b"archive")
            result = rename_unique(source, "///")
            self.assertEqual(result.name, "archive.tar.gz")

    def test_compound_suffix_is_preserved(self) -> None:
        self.assertEqual(preserved_suffix(Path("archive.tar.gz")), ".tar.gz")
        self.assertEqual(preserved_suffix(Path("report.final.pdf")), ".pdf")


class RenameTests(unittest.TestCase):
    def test_atomic_primitive_fails_closed_without_renameat2(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.txt"
            target = root / "target.txt"
            source.write_bytes(b"original")

            with (
                patch("mingtu_ai.naming.files.ctypes.CDLL", return_value=SimpleNamespace()),
                self.assertRaisesRegex(AtomicRenameUnavailableError, "unavailable"),
            ):
                rename_noreplace(source, target)

            self.assertEqual(source.read_bytes(), b"original")
            self.assertFalse(target.exists())

    def test_atomic_primitive_refuses_enosys_and_einval_fallbacks(self) -> None:
        class UnsupportedRenameAt2:
            def __call__(self, *_args):
                return -1

        for error_number in (errno.ENOSYS, errno.EINVAL):
            with self.subTest(error_number=error_number), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                source = root / "source.txt"
                target = root / "target.txt"
                source.write_bytes(b"original")
                libc = SimpleNamespace(renameat2=UnsupportedRenameAt2())

                with (
                    patch("mingtu_ai.naming.files.ctypes.CDLL", return_value=libc),
                    patch("mingtu_ai.naming.files.ctypes.get_errno", return_value=error_number),
                    self.assertRaisesRegex(AtomicRenameUnavailableError, "does not support"),
                ):
                    rename_noreplace(source, target)

                self.assertEqual(source.read_bytes(), b"original")
                self.assertFalse(target.exists())

    def test_conflict_increments_without_overwriting(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "capture.png"
            occupied = root / "建議檔名.png"
            source.write_bytes(b"new")
            occupied.write_bytes(b"old")

            result = rename_unique(source, "建議檔名")

            self.assertEqual(result.name, "建議檔名_2.png")
            self.assertEqual(result.read_bytes(), b"new")
            self.assertEqual(occupied.read_bytes(), b"old")

    def test_concurrent_renames_claim_different_names(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            sources = [root / "one.png", root / "two.png"]
            for index, source in enumerate(sources):
                source.write_bytes(str(index).encode())

            with ThreadPoolExecutor(max_workers=2) as executor:
                results = list(executor.map(lambda path: rename_unique(path, "same"), sources))

            self.assertEqual({path.name for path in results}, {"same.png", "same_2.png"})
            self.assertEqual({path.read_bytes() for path in results}, {b"0", b"1"})

    def test_long_unicode_name_stays_within_filesystem_byte_limit(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "capture.png"
            source.write_bytes(b"image")
            result = rename_unique(source, "圖" * 120)
            self.assertLessEqual(len(result.name.encode("utf-8")), 255)
            self.assertEqual(result.suffix, ".png")

    def test_long_unicode_collision_reserves_bytes_for_counter(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "capture.tar.gz"
            source.write_bytes(b"new")
            first = root / (("圖" * 82) + "a.tar.gz")
            first.write_bytes(b"old")

            result = rename_unique(source, ("圖" * 82) + "a")

            self.assertLessEqual(len(os.fsencode(result.name)), os.pathconf(root, "PC_NAME_MAX"))
            self.assertTrue(result.name.endswith("_2.tar.gz"))
            self.assertEqual(result.read_bytes(), b"new")
            self.assertEqual(first.read_bytes(), b"old")

    def test_suffix_and_counter_that_exceed_name_max_fail_before_move(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "capture.tar.gz"
            occupied = root / "a.tar.gz"
            source.write_bytes(b"unchanged")
            occupied.write_bytes(b"occupied")

            with (
                patch("mingtu_ai.naming.files.os.pathconf", return_value=8),
                self.assertRaises(RenameError),
            ):
                rename_unique(source, "a")

            self.assertEqual(source.read_bytes(), b"unchanged")
            self.assertEqual(occupied.read_bytes(), b"occupied")


class SuggestionContractTests(unittest.TestCase):
    def test_valid_json_is_accepted(self) -> None:
        suggestion = NamingSuggestion.model_validate_json(
            '{"suggested_stem":"PyTorch_CUDA_記憶體不足","detected_language":"zh-Hant",'
            '"category":"software_error","keywords":["PyTorch","CUDA"],"confidence":0.92}'
        )
        self.assertEqual(suggestion.category, "software_error")

    def test_extra_or_coerced_fields_are_rejected(self) -> None:
        with self.assertRaises(ValidationError):
            NamingSuggestion.model_validate_json(
                '{"suggested_stem":"name","detected_language":"en",'
                '"category":"document","keywords":["name"],"confidence":"0.9"}'
            )
        with self.assertRaises(ValidationError):
            NamingSuggestion.model_validate_json(
                '{"suggested_stem":"name","detected_language":"en",'
                '"category":"document","keywords":["name"],"confidence":0.9,"extra":true}'
            )


if __name__ == "__main__":
    unittest.main()
