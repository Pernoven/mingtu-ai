"""Unit and integration tests for folder selection and batch file renaming features."""

from __future__ import annotations

from contextlib import contextmanager
import json
import io
from pathlib import Path
import tempfile
import unittest

from mingtu_ai import (
    AppConfig, BatchAnalysisResult, BatchItemProposal, BatchPreset, NamingSuggestion,
)
from mingtu_ai.app import ApplicationCoordinator
from mingtu_ai.batch import (
    apply_naming_preset, extract_file_timestamp_prefix, scan_batch_candidates,
)
from mingtu_ai.cli import main as cli_main
from mingtu_ai.history import HistoryStore
from mingtu_ai.notifications import NotificationService


class DummyProvider:
    def analyze(self, item):
        stem = item.source_path.stem.lower()
        if "photo" in stem or item.source_path.suffix in (".jpg", ".png"):
            return NamingSuggestion(
                suggested_stem="sunset_beach",
                detected_language="zh-Hant",
                category="photo",
                keywords=["sunset", "beach"],
                confidence=0.95,
            )
        elif "doc" in stem or item.source_path.suffix == ".pdf":
            return NamingSuggestion(
                suggested_stem="financial_report",
                detected_language="zh-Hant",
                category="document",
                keywords=["financial", "report"],
                confidence=0.90,
            )
        return NamingSuggestion(
            suggested_stem="processed_file",
            detected_language="zh-Hant",
            category="general",
            keywords=["general"],
            confidence=0.85,
        )


class DummyRuntimeTracker:
    @contextmanager
    def batch(self):
        yield self

    @contextmanager
    def job(self):
        yield


class BatchRenamingTests(unittest.TestCase):
    def test_scan_batch_candidates_filtering(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            file1 = root / "image1.jpg"
            file2 = root / "doc1.pdf"
            hidden = root / ".hidden.txt"
            temp_file = root / "draft.tmp"
            sub_dir = root / "subdir"
            sub_dir.mkdir()
            sub_file = sub_dir / "sub_image.png"

            file1.write_text("img1")
            file2.write_text("doc1")
            hidden.write_text("hidden")
            temp_file.write_text("temp")
            sub_file.write_text("sub")

            # Non-recursive scan
            flat = scan_batch_candidates(root, recursive=False)
            flat_names = [f.name for f in flat]
            self.assertIn("image1.jpg", flat_names)
            self.assertIn("doc1.pdf", flat_names)
            self.assertNotIn(".hidden.txt", flat_names)
            self.assertNotIn("draft.tmp", flat_names)
            self.assertNotIn("sub_image.png", flat_names)

            # Recursive scan
            recursive_files = scan_batch_candidates(root, recursive=True)
            rec_names = [f.name for f in recursive_files]
            self.assertIn("image1.jpg", rec_names)
            self.assertIn("doc1.pdf", rec_names)
            self.assertIn("sub_image.png", rec_names)
            self.assertNotIn(".hidden.txt", rec_names)

    def test_apply_naming_preset(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            sample = Path(temporary) / "photo.jpg"
            sample.write_text("data")
            date_str = extract_file_timestamp_prefix(sample)

            # DEFAULT preset
            stem_def = apply_naming_preset("sunset_beach", sample, preset=BatchPreset.DEFAULT)
            self.assertEqual(stem_def, "sunset_beach")

            # PHOTO_DATE preset
            stem_date = apply_naming_preset("sunset_beach", sample, preset=BatchPreset.PHOTO_DATE)
            self.assertEqual(stem_date, f"{date_str}_sunset_beach")

            # CATEGORY_AI preset
            stem_cat = apply_naming_preset(
                "sunset_beach", sample, preset=BatchPreset.CATEGORY_AI, category="Photo"
            )
            self.assertEqual(stem_cat, "photo_sunset_beach")

    def test_coordinator_analyze_accept_and_undo_batch(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            watch_dir = root / "watch"
            watch_dir.mkdir()
            db_path = root / "state" / "history.sqlite3"

            f1 = watch_dir / "IMG_0001.jpg"
            f2 = watch_dir / "DOC_0002.pdf"
            f1.write_bytes(b"photo content")
            f2.write_bytes(b"doc content")

            config = AppConfig()
            history = HistoryStore(db_path)
            notifications = NotificationService()
            runtime = DummyRuntimeTracker()

            coordinator = ApplicationCoordinator(
                config=config,
                watch_directories=[watch_dir],
                history=history,
                notifications=notifications,
                runtime_factory=lambda: runtime.batch(),
                provider_factory=lambda _rt: DummyProvider(),
            )

            # Analyze batch
            result = coordinator.analyze_batch(watch_dir, preset=BatchPreset.DEFAULT)
            self.assertEqual(result.total_files, 2)
            self.assertEqual(result.processed_files, 2)
            self.assertEqual(result.failed_files, 0)
            self.assertTrue(result.batch_id.startswith("batch_"))

            # Check generated proposals
            self.assertEqual(len(result.items), 2)
            suggested_names = {item.source_path.name: item.suggested_filename for item in result.items}
            self.assertEqual(suggested_names["IMG_0001.jpg"], "sunset_beach.jpg")
            self.assertEqual(suggested_names["DOC_0002.pdf"], "financial_report.pdf")

            # Accept batch (One-click batch rename)
            renamed_events = coordinator.accept_batch(result.batch_id)
            self.assertEqual(len(renamed_events), 2)

            # Check filesystem after rename
            self.assertFalse(f1.exists())
            self.assertFalse(f2.exists())
            self.assertTrue((watch_dir / "sunset_beach.jpg").exists())
            self.assertTrue((watch_dir / "financial_report.pdf").exists())

            # Undo batch (One-click batch restore)
            undo_events = coordinator.undo_batch(result.batch_id)
            self.assertEqual(len(undo_events), 2)

            # Check filesystem after undo
            self.assertTrue(f1.exists())
            self.assertTrue(f2.exists())
            self.assertFalse((watch_dir / "sunset_beach.jpg").exists())
            self.assertFalse((watch_dir / "financial_report.pdf").exists())

    def test_batch_notification_quietness(self) -> None:
        """Verify batch operations send a single summary notification instead of a notification storm."""
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            watch_dir = root / "watch"
            watch_dir.mkdir()
            db_path = root / "state" / "history.sqlite3"

            for i in range(10):
                (watch_dir / f"file_{i}.jpg").write_bytes(b"data")

            sent_notifications = []

            class RecordingBackend:
                def send(self, notification, on_action=None):
                    sent_notifications.append(notification)
                    from mingtu_ai.notifications import NotificationReceipt
                    return NotificationReceipt(True, "recording")

                def close(self):
                    pass

            config = AppConfig()
            history = HistoryStore(db_path)
            notifications = NotificationService(backend=RecordingBackend())
            runtime = DummyRuntimeTracker()

            coordinator = ApplicationCoordinator(
                config=config,
                watch_directories=[watch_dir],
                history=history,
                notifications=notifications,
                runtime_factory=lambda: runtime.batch(),
                provider_factory=lambda _rt: DummyProvider(),
            )

            # Analyze batch of 10 files
            result = coordinator.analyze_batch(watch_dir)
            self.assertEqual(result.total_files, 10)
            # Should have exactly ONE summary notification for analysis (not 10!)
            self.assertEqual(len(sent_notifications), 1)
            self.assertEqual(sent_notifications[0].summary, "資料夾批次分析完成")

            sent_notifications.clear()

            # Accept batch of 10 files
            renamed = coordinator.accept_batch(result.batch_id)
            self.assertEqual(len(renamed), 10)
            # Should have exactly ONE summary notification for rename (not 10!)
            self.assertEqual(len(sent_notifications), 1)
            self.assertEqual(sent_notifications[0].summary, "批次命名完成")

            sent_notifications.clear()

            # Undo batch
            undone = coordinator.undo_batch(result.batch_id)
            self.assertEqual(len(undone), 10)
            # Should have exactly ONE summary notification for undo (not 10!)
            self.assertEqual(len(sent_notifications), 1)
            self.assertEqual(sent_notifications[0].summary, "批次復原完成")

    def test_cli_batch_commands(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            target_dir = root / "photos"
            target_dir.mkdir()
            f1 = target_dir / "IMG_1001.jpg"
            f2 = target_dir / "IMG_1002.jpg"
            f1.write_bytes(b"data1")
            f2.write_bytes(b"data2")
            db_path = root / "history.sqlite3"

            from unittest.mock import patch

            def make_test_coordinator(args, environ=None):
                return ApplicationCoordinator(
                    config=AppConfig(),
                    watch_directories=[target_dir],
                    history=HistoryStore(db_path),
                    notifications=NotificationService(),
                    runtime_factory=lambda: DummyRuntimeTracker().batch(),
                    provider_factory=lambda _rt: DummyProvider(),
                )

            # 1. CLI batch --dry-run
            stdout = io.StringIO()
            stderr = io.StringIO()
            with patch("mingtu_ai.cli.create_coordinator", side_effect=make_test_coordinator):
                exit_code = cli_main(
                    ["--batch", str(target_dir), "--dry-run", "--history-db", str(db_path)],
                    stdout=stdout,
                    stderr=stderr,
                )
            self.assertEqual(exit_code, 0)
            output = stdout.getvalue()
            self.assertIn("資料夾批次分析完成", output)
            self.assertIn("Dry Run", output)
            # Files should NOT be renamed
            self.assertTrue(f1.exists())
            self.assertTrue(f2.exists())

            # 2. CLI batch -y (One-click rename)
            stdout = io.StringIO()
            stderr = io.StringIO()
            with patch("mingtu_ai.cli.create_coordinator", side_effect=make_test_coordinator):
                exit_code = cli_main(
                    ["--batch", str(target_dir), "-y", "--preset", "photo_date", "--history-db", str(db_path)],
                    stdout=stdout,
                    stderr=stderr,
                )
            self.assertEqual(exit_code, 0)
            output = stdout.getvalue()
            self.assertIn("[一鍵改名]", output)
            # Original names should be renamed
            self.assertFalse(f1.exists())
            self.assertFalse(f2.exists())
            renamed_files = list(target_dir.glob("*.jpg"))
            self.assertEqual(len(renamed_files), 2)


if __name__ == "__main__":
    unittest.main()
