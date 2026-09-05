from __future__ import annotations

import io
from pathlib import Path
import sys
import tarfile
from tempfile import TemporaryDirectory
import time
import unittest
from unittest.mock import patch
import zipfile

from mingtu_ai.adapters import (
    AdapterError,
    AdapterKind,
    AdapterLimits,
    FileAdapterRegistry,
    adapt_file,
    detect_mime_type,
)
from mingtu_ai.adapters.secure_input import read_bounded as real_read_bounded
from mingtu_ai.adapters.subprocesses import (
    BoundedProcessResult,
    ProcessCaptureTimeout,
    ProcessOutputLimitError,
    run_bounded_process,
)


class BoundedSubprocessTests(unittest.TestCase):
    def test_child_reads_inherited_descriptor_after_path_swap(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "input.bin"
            source.write_bytes(b"opened inode")
            replacement = root / "replacement.bin"
            replacement.write_bytes(b"replacement")
            moved = root / "moved.bin"
            descriptor = source.open("rb")
            try:
                source.rename(moved)
                source.symlink_to(replacement)
                result = run_bounded_process(
                    [
                        sys.executable,
                        "-c",
                        "import pathlib,sys; sys.stdout.buffer.write(pathlib.Path(sys.argv[1]).read_bytes())",
                        f"/proc/self/fd/{descriptor.fileno()}",
                    ],
                    timeout=2,
                    stdout_limit=64,
                    stderr_limit=64,
                    pass_fds=(descriptor.fileno(),),
                )
            finally:
                descriptor.close()

            self.assertEqual(result.returncode, 0)
            self.assertEqual(result.stdout, b"opened inode")

    def test_small_stdout_and_stderr_are_captured_concurrently(self) -> None:
        result = run_bounded_process(
            [
                sys.executable,
                "-c",
                "import os; os.write(1, b'output'); os.write(2, b'detail')",
            ],
            timeout=2,
            stdout_limit=32,
            stderr_limit=32,
        )
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout, b"output")
        self.assertEqual(result.stderr, b"detail")

    def test_large_stdout_is_stopped_during_generation(self) -> None:
        command = [
            sys.executable,
            "-c",
            "import os\nwhile True: os.write(1, b'x' * 65536)",
        ]
        started = time.monotonic()
        with self.assertRaises(ProcessOutputLimitError) as caught:
            run_bounded_process(
                command,
                timeout=5,
                stdout_limit=4096,
                stderr_limit=4096,
            )
        self.assertEqual(caught.exception.stream, "stdout")
        self.assertLess(time.monotonic() - started, 2)

    def test_large_stderr_is_stopped_during_generation(self) -> None:
        command = [
            sys.executable,
            "-c",
            "import os\nwhile True: os.write(2, b'x' * 65536)",
        ]
        with self.assertRaises(ProcessOutputLimitError) as caught:
            run_bounded_process(
                command,
                timeout=5,
                stdout_limit=4096,
                stderr_limit=2048,
            )
        self.assertEqual(caught.exception.stream, "stderr")

    def test_silent_child_is_stopped_at_wall_clock_timeout(self) -> None:
        started = time.monotonic()
        with self.assertRaises(ProcessCaptureTimeout):
            run_bounded_process(
                [sys.executable, "-c", "import time; time.sleep(60)"],
                timeout=0.05,
                stdout_limit=32,
                stderr_limit=32,
            )
        self.assertLess(time.monotonic() - started, 2)


class AdapterTests(unittest.TestCase):
    def test_symlink_input_is_never_followed(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "private.txt"
            target.write_text("must not be read", encoding="utf-8")
            link = root / "notes.txt"
            link.symlink_to(target)

            with self.assertRaises(AdapterError):
                with adapt_file(link):
                    self.fail("a symlink must not enter the adapter scope")

    def test_symlink_swap_reads_only_the_already_open_descriptor(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "notes.txt"
            source.write_text("original", encoding="utf-8")
            replacement = root / "replacement.txt"
            replacement.write_text("secret replacement", encoding="utf-8")
            moved = root / "opened.txt"
            reads: list[bytes] = []
            calls = 0

            def swap_then_read(descriptor: int, limit: int):
                nonlocal calls
                calls += 1
                if calls == 2:
                    source.rename(moved)
                    source.symlink_to(replacement)
                result = real_read_bounded(descriptor, limit)
                reads.append(result[0])
                return result

            with (
                patch("mingtu_ai.adapters.registry.read_bounded", side_effect=swap_then_read),
                self.assertRaisesRegex(AdapterError, "changed while it was being adapted"),
            ):
                with adapt_file(source):
                    self.fail("the swapped pathname must be rejected before yielding")

            self.assertEqual(reads[-1], b"original")
            self.assertNotIn(b"secret replacement", reads)

    def test_text_and_csv_are_bounded(self) -> None:
        limits = AdapterLimits(max_input_bytes=32, max_text_chars=12)
        registry = FileAdapterRegistry(limits)
        with TemporaryDirectory() as directory:
            root = Path(directory)
            text_path = root / "notes.txt"
            text_path.write_text("abcdefghijklmnopqrstuvwxyz", encoding="utf-8")
            with registry.open(text_path) as adapted:
                self.assertEqual(adapted.kind, AdapterKind.TEXT)
                self.assertEqual(len(adapted.text or ""), 12)
                self.assertTrue(adapted.metadata["content_truncated"])

            csv_path = root / "table.csv"
            csv_path.write_text("name,value\nalpha,1\n", encoding="utf-8")
            with adapt_file(csv_path) as adapted:
                self.assertIn("name | value", adapted.text or "")
                self.assertEqual(adapted.metadata["preview_rows"], 2)

    def test_docx_reads_selected_xml_without_extracting(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            document = root / "sample.docx"
            xml = b'''<?xml version="1.0"?>
                <w:document xmlns:w="urn:test"><w:p><w:r><w:t>Hello</w:t></w:r>
                <w:r><w:t>world</w:t></w:r></w:p></w:document>'''
            with zipfile.ZipFile(document, "w", zipfile.ZIP_DEFLATED) as archive:
                archive.writestr("word/document.xml", xml)
                archive.writestr("ignored.bin", b"never read")

            with adapt_file(document) as adapted:
                self.assertEqual(adapted.kind, AdapterKind.OFFICE)
                self.assertEqual(adapted.text, "Hello\nworld")
            self.assertFalse((root / "word").exists())

    def test_office_rejects_dtd_entities(self) -> None:
        with TemporaryDirectory() as directory:
            document = Path(directory) / "unsafe.docx"
            xml = b'''<!DOCTYPE x [<!ENTITY x "expanded">]>
                <w:document xmlns:w="urn:test"><w:t>&x;</w:t></w:document>'''
            with zipfile.ZipFile(document, "w") as archive:
                archive.writestr("word/document.xml", xml)
            with adapt_file(document) as adapted:
                self.assertIsNone(adapted.text)
                self.assertTrue(any("rejected" in item for item in adapted.warnings))

    def test_zip_and_tar_only_list_members(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            zip_path = root / "files.zip"
            with zipfile.ZipFile(zip_path, "w") as archive:
                archive.writestr("../escape.txt", "must not extract")
            with adapt_file(zip_path) as adapted:
                self.assertEqual(adapted.kind, AdapterKind.ARCHIVE)
                self.assertIn("../escape.txt", adapted.text or "")
            self.assertFalse((root.parent / "escape.txt").exists())

            tar_path = root / "files.tar"
            with tarfile.open(tar_path, "w") as archive:
                info = tarfile.TarInfo("folder/report.txt")
                content = b"not extracted"
                info.size = len(content)
                archive.addfile(info, io.BytesIO(content))
            with adapt_file(tar_path) as adapted:
                self.assertEqual(adapted.kind, AdapterKind.ARCHIVE)
                self.assertIn("folder/report.txt", adapted.text or "")
            self.assertFalse((root / "folder").exists())

    def test_pdf_derived_page_is_deleted_after_context(self) -> None:
        with TemporaryDirectory() as directory:
            pdf = Path(directory) / "sample.pdf"
            pdf.write_bytes(b"%PDF-1.4\n")
            registry = FileAdapterRegistry()

            def fake_capture(
                command: list[str],
                *,
                stdout_limit: int | None,
                pass_fds: tuple[int, ...] = (),
            ):
                self.assertEqual(Path(command[0]).name, "pdftotext")
                self.assertEqual(command[-1], "-")
                self.assertEqual(command[-2], f"/proc/self/fd/{pass_fds[0]}")
                self.assertEqual(stdout_limit, registry.limits.max_input_bytes)
                return BoundedProcessResult(0, b"First page", b"")

            def fake_run(
                command: list[str], *, pass_fds: tuple[int, ...] = ()
            ) -> str | None:
                self.assertTrue(pass_fds)
                Path(command[-1] + ".png").write_bytes(b"png")
                return None

            with (
                patch(
                    "mingtu_ai.adapters.registry.shutil.which",
                    side_effect=lambda tool: f"/usr/bin/{tool}",
                ),
                patch.object(registry, "_capture", side_effect=fake_capture),
                patch.object(registry, "_run", side_effect=fake_run),
            ):
                with registry.open(pdf) as adapted:
                    self.assertEqual(adapted.kind, AdapterKind.PDF)
                    self.assertEqual(adapted.text, "First page")
                    derived = adapted.image_paths[0]
                    self.assertTrue(derived.exists())
                self.assertFalse(derived.exists())

    def test_video_probe_and_representative_frame(self) -> None:
        with TemporaryDirectory() as directory:
            video = Path(directory) / "clip.mp4"
            video.write_bytes(b"video")
            registry = FileAdapterRegistry()
            probe_json = (
                b'{"streams":[{"codec_type":"video","width":640,'
                b'"height":360}],"format":{"duration":"2.0"}}'
            )

            def fake_run(
                command: list[str], *, pass_fds: tuple[int, ...] = ()
            ) -> str | None:
                self.assertTrue(pass_fds)
                Path(command[-1]).write_bytes(b"png")
                return None

            completed = BoundedProcessResult(0, probe_json, b"")
            with (
                patch(
                    "mingtu_ai.adapters.registry.shutil.which",
                    side_effect=lambda tool: f"/usr/bin/{tool}",
                ),
                patch.object(registry, "_capture", return_value=completed),
                patch.object(registry, "_run", side_effect=fake_run),
            ):
                with registry.open(video) as adapted:
                    self.assertEqual(adapted.kind, AdapterKind.VIDEO)
                    self.assertEqual(adapted.metadata["media"]["streams"][0]["width"], 640)
                    self.assertTrue(adapted.image_paths[0].exists())
                    frame = adapted.image_paths[0]
                self.assertFalse(frame.exists())

    def test_pdf_and_ffprobe_report_generation_time_output_limits(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            pdf = root / "sample.pdf"
            pdf.write_bytes(b"%PDF-1.4\n")
            media = root / "clip.mp4"
            media.write_bytes(b"media")
            limits = AdapterLimits(max_input_bytes=32)
            registry = FileAdapterRegistry(limits)

            with patch(
                "mingtu_ai.adapters.registry.shutil.which",
                side_effect=lambda tool: (
                    f"/usr/bin/{tool}" if tool in {"pdftotext", "ffprobe"} else None
                ),
            ), patch.object(
                registry,
                "_capture",
                side_effect=ProcessOutputLimitError("stdout", limits.max_input_bytes),
            ):
                with registry.open(pdf) as adapted:
                    self.assertIsNone(adapted.text)
                    self.assertTrue(adapted.metadata["content_truncated"])
                    self.assertIn("pdftotext output exceeded", " ".join(adapted.warnings))

                with registry.open(media) as adapted:
                    self.assertIn("ffprobe stdout exceeded", " ".join(adapted.warnings))

    def test_image_and_unknown_metadata_fallback(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            image = root / "capture.bin"
            image.write_bytes(b"\x89PNG\r\n\x1a\n" + b"x" * 8)
            self.assertEqual(detect_mime_type(image), "image/png")
            with adapt_file(image) as adapted:
                self.assertEqual(adapted.kind, AdapterKind.IMAGE)
                self.assertEqual(adapted.image_paths, (image,))

            unknown = root / "weights.unknown"
            unknown.write_bytes(b"\x00\x01\x02")
            with adapt_file(unknown) as adapted:
                self.assertEqual(adapted.kind, AdapterKind.METADATA)
                self.assertIn("size_bytes", adapted.text or "")


if __name__ == "__main__":
    unittest.main()
