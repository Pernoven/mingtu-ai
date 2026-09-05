from __future__ import annotations

import os
import sqlite3
import tempfile
from pathlib import Path
import unittest
from unittest.mock import patch

from mingtu_ai.history import (
    EventStatus,
    EventType,
    HistoryStore,
    RenamePostCommitError,
    RenameRecoveryRequiredError,
    safe_move_no_replace,
)


class HistoryStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        self.store = HistoryStore(self.root / "state" / "history.sqlite3")

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def test_records_all_non_file_events_without_content(self) -> None:
        source = self.root / "Screenshot.png"
        suggestion_id = self.store.record_suggestion(
            source,
            "CUDA_錯誤.png",
            metadata={"confidence": 0.9},
        )
        self.store.record_ignored(source, "user")
        self.store.record_failure(source, "model unavailable")

        suggestion = self.store.get(suggestion_id)
        self.assertIsNotNone(suggestion)
        assert suggestion is not None
        self.assertEqual(suggestion.event_type, EventType.SUGGESTION)
        self.assertEqual(suggestion.status, EventStatus.PENDING)
        self.assertEqual(suggestion.metadata, {"confidence": 0.9})
        self.assertEqual(len(self.store.list_recent()), 3)

    def test_rename_adds_suffix_and_never_overwrites(self) -> None:
        source = self.root / "Screenshot.png"
        occupied = self.root / "CUDA_錯誤.png"
        source.write_bytes(b"new screenshot")
        occupied.write_bytes(b"existing file")

        event = self.store.rename_file(source, occupied.name)

        self.assertEqual(event.event_type, EventType.RENAME)
        self.assertEqual(event.status, EventStatus.SUCCESS)
        self.assertEqual(event.target_path, self.root / "CUDA_錯誤_2.png")
        self.assertFalse(source.exists())
        self.assertEqual(occupied.read_bytes(), b"existing file")
        assert event.target_path is not None
        self.assertEqual(event.target_path.read_bytes(), b"new screenshot")

    def test_undo_restores_original_name(self) -> None:
        source = self.root / "Screenshot.png"
        source.write_text("payload", encoding="utf-8")
        rename = self.store.rename_file(source, "searchable.png")

        undo = self.store.undo_rename(rename.id)

        self.assertEqual(undo.event_type, EventType.UNDO)
        self.assertEqual(undo.status, EventStatus.SUCCESS)
        self.assertEqual(source.read_text(encoding="utf-8"), "payload")
        assert rename.target_path is not None
        self.assertFalse(rename.target_path.exists())
        with self.assertRaises(ValueError):
            self.store.undo_rename(rename.id)

    def test_undo_conflict_does_not_overwrite_and_is_recorded(self) -> None:
        source = self.root / "Screenshot.png"
        source.write_text("renamed payload", encoding="utf-8")
        rename = self.store.rename_file(source, "searchable.png")
        source.write_text("new occupant", encoding="utf-8")

        with self.assertRaises(FileExistsError):
            self.store.undo_rename(rename.id)

        self.assertEqual(source.read_text(encoding="utf-8"), "new occupant")
        assert rename.target_path is not None
        self.assertEqual(rename.target_path.read_text(encoding="utf-8"), "renamed payload")
        undo = self.store.list_recent(limit=1)[0]
        self.assertEqual(undo.event_type, EventType.UNDO)
        self.assertEqual(undo.status, EventStatus.FAILED)

    def test_undo_refuses_a_replacement_at_the_renamed_path(self) -> None:
        source = self.root / "Screenshot.png"
        source.write_text("original payload", encoding="utf-8")
        rename = self.store.rename_file(source, "searchable.png")
        assert rename.target_path is not None
        rename.target_path.unlink()
        rename.target_path.write_text("replacement payload", encoding="utf-8")

        with self.assertRaises(ValueError):
            self.store.undo_rename(rename.id)

        self.assertFalse(source.exists())
        self.assertEqual(
            rename.target_path.read_text(encoding="utf-8"),
            "replacement payload",
        )
        undo = self.store.list_recent(limit=1)[0]
        self.assertEqual(undo.event_type, EventType.UNDO)
        self.assertEqual(undo.status, EventStatus.FAILED)

    def test_compound_extension_collision_puts_counter_before_extension(self) -> None:
        source = self.root / "input.tar.gz"
        occupied = self.root / "archive.tar.gz"
        source.write_bytes(b"new")
        occupied.write_bytes(b"old")

        event = self.store.rename_file(source, occupied.name)

        self.assertEqual(event.target_path, self.root / "archive_2.tar.gz")
        self.assertEqual(occupied.read_bytes(), b"old")

    def test_long_unicode_collision_stays_within_name_max(self) -> None:
        source = self.root / "input.tar.gz"
        source.write_bytes(b"new")
        requested = ("圖" * 82) + "a.tar.gz"
        occupied = self.root / requested
        occupied.write_bytes(b"old")

        event = self.store.rename_file(source, requested)

        assert event.target_path is not None
        self.assertLessEqual(
            len(os.fsencode(event.target_path.name)),
            os.pathconf(self.root, "PC_NAME_MAX"),
        )
        self.assertTrue(event.target_path.name.endswith("_2.tar.gz"))
        self.assertEqual(event.target_path.read_bytes(), b"new")
        self.assertEqual(occupied.read_bytes(), b"old")

    def test_tiny_name_max_collision_leaves_source_unchanged(self) -> None:
        source = self.root / "input.tar.gz"
        occupied = self.root / "a.tar.gz"
        source.write_bytes(b"new")
        occupied.write_bytes(b"old")

        with (
            patch("mingtu_ai.naming.files.os.pathconf", return_value=8),
            self.assertRaisesRegex(RuntimeError, "extension leaves no room"),
        ):
            self.store.rename_file(source, occupied.name)

        self.assertEqual(source.read_bytes(), b"new")
        self.assertEqual(occupied.read_bytes(), b"old")

    def test_undo_eligibility_checks_ledger_beyond_recent_history(self) -> None:
        source = self.root / "original.txt"
        source.write_text("payload")
        rename = self.store.rename_file(source, "renamed.txt")
        self.assertTrue(self.store.can_undo_rename(rename.id))
        self.store.undo_rename(rename.id)
        # Restore the filesystem layout of an undoable rename. The complete
        # ledger must still prevent a second undo after it leaves the UI window.
        source.rename(rename.target_path)
        for index in range(110):
            self.store.record_failure(self.root / f"failure-{index}.txt", "test")
        self.assertFalse(self.store.can_undo_rename(rename.id))

    def test_undo_eligibility_is_read_only_for_committed_pending_rename(self) -> None:
        source = self.root / "original.txt"
        source.write_text("payload")
        with patch.object(self.store, "_set_status", side_effect=sqlite3.OperationalError("busy")):
            with self.assertRaises(RenamePostCommitError) as raised:
                self.store.rename_file(source, "renamed.txt")
        event_id = raised.exception.event_id
        self.assertTrue(self.store.can_undo_rename(event_id))
        self.assertEqual(self.store.get(event_id).status, EventStatus.PENDING)
        self.assertFalse(source.exists())

    def test_undo_eligibility_rejects_occupied_original_and_replaced_target(self) -> None:
        source = self.root / "original.txt"
        source.write_text("payload")
        rename = self.store.rename_file(source, "renamed.txt")
        source.symlink_to(self.root / "missing")
        self.assertFalse(self.store.can_undo_rename(rename.id))
        source.unlink()
        self.assertTrue(self.store.can_undo_rename(rename.id))
        replacement = self.root / "replacement.txt"
        replacement.write_text("replacement")
        replacement.replace(rename.target_path)
        self.assertFalse(self.store.can_undo_rename(rename.id))
        self.assertFalse(self.store.can_undo_rename(-1))

    def test_rejects_path_traversal_as_a_filename(self) -> None:
        source = self.root / "source.txt"
        source.write_text("safe", encoding="utf-8")

        with self.assertRaises(ValueError):
            self.store.rename_file(source, "../escape.txt")

        self.assertTrue(source.exists())
        self.assertFalse((self.root.parent / "escape.txt").exists())

        with self.assertRaises(ValueError):
            self.store.rename_file(source, "folder\\escape.txt")

    def test_rename_ledger_insert_failure_moves_nothing(self) -> None:
        source = self.root / "Screenshot.png"
        target = self.root / "searchable.png"
        source.write_bytes(b"must remain at the original name")

        with (
            patch.object(
                self.store,
                "_insert",
                side_effect=sqlite3.OperationalError("injected insert failure"),
            ),
            self.assertRaises(sqlite3.OperationalError),
        ):
            self.store.rename_file(source, target.name)

        self.assertEqual(source.read_bytes(), b"must remain at the original name")
        self.assertFalse(target.exists())

    def test_status_failure_leaves_recoverable_pending_rename(self) -> None:
        source = self.root / "Screenshot.png"
        target = self.root / "searchable.png"
        source.write_bytes(b"moved payload")

        with (
            patch.object(
                self.store,
                "_set_status",
                side_effect=sqlite3.OperationalError("injected finalization failure"),
            ),
            self.assertRaises(RenamePostCommitError) as raised,
        ):
            self.store.rename_file(source, target.name)

        self.assertFalse(source.exists())
        self.assertEqual(target.read_bytes(), b"moved payload")
        pending = self.store.get(raised.exception.event_id)
        self.assertIsNotNone(pending)
        assert pending is not None
        self.assertEqual(pending.status, EventStatus.PENDING)
        self.assertEqual(pending.source_path, source)
        self.assertEqual(pending.target_path, target)
        target_info = os.stat(target)
        self.assertEqual(
            pending.metadata["target_identity"],
            [target_info.st_dev, target_info.st_ino],
        )

        # A later call can prove that the target is the recorded inode, mark
        # the rename successful, and then perform the normal no-clobber undo.
        undo = self.store.undo_rename(pending.id)
        self.assertEqual(undo.status, EventStatus.SUCCESS)
        self.assertEqual(source.read_bytes(), b"moved payload")
        self.assertFalse(target.exists())

    def test_expected_identity_mismatch_removes_claim_and_keeps_source(self) -> None:
        source = self.root / "Screenshot.png"
        target = self.root / "searchable.png"
        source.write_bytes(b"replacement")

        with self.assertRaises(ValueError):
            self.store.rename_file(
                source,
                target.name,
                expected_source_identity=(0, 0, 0, 0),
            )

        self.assertEqual(source.read_bytes(), b"replacement")
        self.assertFalse(target.exists())
        self.assertFalse(
            any(event.event_type is EventType.RENAME for event in self.store.list_recent())
        )

    def test_source_replaced_during_ledger_write_is_not_moved_or_deleted(self) -> None:
        source = self.root / "source.txt"
        target = self.root / "target.txt"
        replacement = self.root / "replacement.txt"
        source.write_bytes(b"original")
        replacement.write_bytes(b"replacement")
        real_insert = self.store._insert

        def replace_after_insert(*args, **kwargs):
            event_id = real_insert(*args, **kwargs)
            replacement.replace(source)
            return event_id

        with patch.object(self.store, "_insert", side_effect=replace_after_insert):
            with self.assertRaisesRegex(ValueError, "source changed"):
                self.store.rename_file(source, target.name)

        self.assertEqual(source.read_bytes(), b"replacement")
        self.assertFalse(target.exists())
        event = self.store.list_recent(1)[0]
        self.assertEqual(event.status, EventStatus.FAILED)
        self.assertEqual(event.metadata["move_strategy"], "renameat2_noreplace")
        self.assertFalse(self.store.can_undo_rename(event.id))

    def test_source_disappears_during_ledger_write_moves_nothing(self) -> None:
        source = self.root / "source.txt"
        target = self.root / "target.txt"
        source.write_bytes(b"original")
        real_insert = self.store._insert

        def unlink_after_insert(*args, **kwargs):
            event_id = real_insert(*args, **kwargs)
            source.unlink()
            return event_id

        with patch.object(self.store, "_insert", side_effect=unlink_after_insert):
            with self.assertRaises(FileNotFoundError):
                self.store.rename_file(source, target.name)

        self.assertFalse(source.exists())
        self.assertFalse(target.exists())
        event = self.store.list_recent(1)[0]
        self.assertEqual(event.status, EventStatus.FAILED)
        self.assertFalse(self.store.can_undo_rename(event.id))

    def test_target_collision_during_ledger_write_advances_atomically(self) -> None:
        source = self.root / "source.txt"
        target = self.root / "target.txt"
        replacement = self.root / "replacement.txt"
        source.write_bytes(b"original")
        replacement.write_bytes(b"replacement")
        original_inode = source.stat().st_ino
        real_insert = self.store._insert

        inserted = 0

        def replace_after_insert(*args, **kwargs):
            nonlocal inserted
            event_id = real_insert(*args, **kwargs)
            inserted += 1
            if inserted == 1:
                replacement.replace(target)
            return event_id

        with patch.object(self.store, "_insert", side_effect=replace_after_insert):
            event = self.store.rename_file(source, target.name)

        self.assertFalse(source.exists())
        self.assertEqual(target.read_bytes(), b"replacement")
        self.assertEqual(event.target_path, self.root / "target_2.txt")
        self.assertEqual(event.target_path.read_bytes(), b"original")
        self.assertEqual(event.metadata["target_identity"][1], original_inode)
        attempts = [item for item in self.store.list_recent() if item.event_type is EventType.RENAME]
        self.assertEqual([item.status for item in attempts], [EventStatus.SUCCESS, EventStatus.FAILED])

    def test_failed_ledger_write_preserves_replacement_at_either_path(self) -> None:
        for replace_source in (False, True):
            with self.subTest(replace_source=replace_source):
                source = self.root / f"source-{replace_source}.txt"
                target = self.root / f"target-{replace_source}.txt"
                replacement = self.root / "replacement.txt"
                source.write_bytes(b"original")
                replacement.write_bytes(b"replacement")

                def replace_then_fail(*args, **kwargs):
                    replacement.replace(source if replace_source else target)
                    raise sqlite3.OperationalError("injected insert failure")

                with patch.object(self.store, "_insert", side_effect=replace_then_fail):
                    with self.assertRaises(sqlite3.OperationalError):
                        self.store.rename_file(source, target.name)

                self.assertEqual(source.read_bytes(), b"replacement" if replace_source else b"original")
                if replace_source:
                    self.assertFalse(target.exists())
                else:
                    self.assertEqual(target.read_bytes(), b"replacement")
                self.assertFalse(self.store.list_recent())

    def test_production_rename_never_uses_check_then_unlink(self) -> None:
        source = self.root / "source.txt"
        target = self.root / "target.txt"
        source.write_bytes(b"original")

        with patch("mingtu_ai.history.store.os.unlink", side_effect=AssertionError("unsafe unlink")):
            event = self.store.rename_file(source, target.name)

        self.assertEqual(event.target_path, target)
        self.assertFalse(source.exists())
        self.assertEqual(target.read_bytes(), b"original")

    def test_atomic_rename_unavailable_fails_and_closes_pending_ledger(self) -> None:
        from mingtu_ai.naming import AtomicRenameUnavailableError

        source = self.root / "source.txt"
        target = self.root / "target.txt"
        source.write_bytes(b"original")

        with (
            patch(
                "mingtu_ai.history.store.rename_noreplace",
                side_effect=AtomicRenameUnavailableError("unsupported"),
            ),
            self.assertRaisesRegex(AtomicRenameUnavailableError, "unsupported"),
        ):
            self.store.rename_file(source, target.name)

        self.assertEqual(source.read_bytes(), b"original")
        self.assertFalse(target.exists())
        event = self.store.list_recent(1)[0]
        self.assertEqual(event.status, EventStatus.FAILED)
        self.assertIn("unsupported", event.message)

    def test_atomic_undo_unavailable_moves_nothing_and_records_failure(self) -> None:
        from mingtu_ai.naming import AtomicRenameUnavailableError

        source = self.root / "source.txt"
        source.write_bytes(b"original")
        rename = self.store.rename_file(source, "target.txt")
        assert rename.target_path is not None

        with (
            patch(
                "mingtu_ai.history.store.rename_noreplace",
                side_effect=AtomicRenameUnavailableError("unsupported"),
            ),
            self.assertRaisesRegex(AtomicRenameUnavailableError, "unsupported"),
        ):
            self.store.undo_rename(rename.id)

        self.assertFalse(source.exists())
        self.assertEqual(rename.target_path.read_bytes(), b"original")
        undo = self.store.list_recent(1)[0]
        self.assertEqual(undo.event_type, EventType.UNDO)
        self.assertEqual(undo.status, EventStatus.FAILED)

    def test_atomic_pending_collision_reconciles_without_touching_paths(self) -> None:
        source = self.root / "source.txt"
        target = self.root / "target.txt"
        source.write_bytes(b"original")
        target.write_bytes(b"occupant")
        info = source.stat()
        event_id = self.store._insert(
            EventType.RENAME,
            EventStatus.PENDING,
            source,
            target_path=target,
            metadata={
                "target_identity": [info.st_dev, info.st_ino],
                "move_strategy": "renameat2_noreplace",
            },
        )

        event = self.store.reconcile_pending_rename(event_id)

        self.assertEqual(event.status, EventStatus.FAILED)
        self.assertEqual(source.read_bytes(), b"original")
        self.assertEqual(target.read_bytes(), b"occupant")

    def test_completed_pending_undo_reconciles_successfully(self) -> None:
        source = self.root / "source.txt"
        source.write_bytes(b"original")
        rename = self.store.rename_file(source, "target.txt")
        real_set_status = self.store._set_status
        failed_once = False

        def fail_undo_finalization(event_id, status, *args, **kwargs):
            nonlocal failed_once
            event = self.store.get(event_id)
            if (
                not failed_once
                and event is not None
                and event.event_type is EventType.UNDO
                and status is EventStatus.SUCCESS
            ):
                failed_once = True
                raise sqlite3.OperationalError("injected undo finalization failure")
            return real_set_status(event_id, status, *args, **kwargs)

        with patch.object(self.store, "_set_status", side_effect=fail_undo_finalization):
            with self.assertRaises(RenamePostCommitError) as raised:
                self.store.undo_rename(rename.id)

        pending = self.store.get(raised.exception.event_id)
        self.assertEqual(pending.status, EventStatus.PENDING)
        self.assertEqual(source.read_bytes(), b"original")
        assert rename.target_path is not None
        self.assertFalse(rename.target_path.exists())

        resolved = self.store.reconcile_pending_undo(pending.id)
        self.assertEqual(resolved.status, EventStatus.SUCCESS)
        self.assertTrue(self.store._successful_undo_exists(rename.id))

    def test_not_moved_pending_undo_reconciles_failed_without_changes(self) -> None:
        source = self.root / "source.txt"
        source.write_bytes(b"original")
        rename = self.store.rename_file(source, "target.txt")
        assert rename.target_path is not None
        event_id = self.store._insert(
            EventType.UNDO,
            EventStatus.PENDING,
            rename.target_path,
            target_path=source,
            parent_event_id=rename.id,
            metadata={
                "move_strategy": "renameat2_noreplace",
                "target_identity": rename.metadata["target_identity"],
            },
        )

        resolved = self.store.reconcile_pending_undo(event_id)

        self.assertEqual(resolved.status, EventStatus.FAILED)
        self.assertFalse(source.exists())
        self.assertEqual(rename.target_path.read_bytes(), b"original")

    def test_ambiguous_pending_undo_preserves_paths_and_stays_manual(self) -> None:
        source = self.root / "source.txt"
        source.write_bytes(b"original")
        rename = self.store.rename_file(source, "target.txt")
        assert rename.target_path is not None
        event_id = self.store._insert(
            EventType.UNDO,
            EventStatus.PENDING,
            rename.target_path,
            target_path=source,
            parent_event_id=rename.id,
            metadata={
                "move_strategy": "renameat2_noreplace",
                "target_identity": rename.metadata["target_identity"],
            },
        )
        preserved = self.root / "preserved-original.txt"
        rename.target_path.rename(preserved)
        rename.target_path.write_bytes(b"replacement-current")
        source.write_bytes(b"replacement-original")

        with self.assertRaisesRegex(ValueError, "ambiguous"):
            self.store.reconcile_pending_undo(event_id)

        self.assertEqual(rename.target_path.read_bytes(), b"replacement-current")
        self.assertEqual(source.read_bytes(), b"replacement-original")
        self.assertEqual(preserved.read_bytes(), b"original")
        pending = self.store.get(event_id)
        self.assertEqual(pending.status, EventStatus.PENDING)
        self.assertTrue(pending.metadata["requires_manual_recovery"])
        with self.assertRaisesRegex(ValueError, "manual inspection"):
            self.store.reconcile_pending_undo(event_id)

    def test_safe_move_detects_a_source_swap_without_deleting_either_file(self) -> None:
        source = self.root / "source.txt"
        target = self.root / "target.txt"
        replacement = self.root / "replacement.txt"
        preserved_original = self.root / "preserved-original.txt"
        source.write_bytes(b"original")
        replacement.write_bytes(b"replacement")
        from mingtu_ai.naming import rename_noreplace as real_rename_noreplace

        def replace_before_atomic_move(move_source, move_target):
            source.rename(preserved_original)
            replacement.rename(source)
            return real_rename_noreplace(move_source, move_target)

        with patch(
            "mingtu_ai.history.store.rename_noreplace",
            side_effect=replace_before_atomic_move,
        ):
            with self.assertRaises(RenameRecoveryRequiredError):
                safe_move_no_replace(source, target)

        self.assertEqual(preserved_original.read_bytes(), b"original")
        self.assertEqual(target.read_bytes(), b"replacement")

    def test_atomic_rename_source_swap_preserves_files_and_marks_manual_recovery(self) -> None:
        source = self.root / "source.txt"
        target = self.root / "target.txt"
        replacement = self.root / "replacement.txt"
        preserved_original = self.root / "preserved-original.txt"
        source.write_bytes(b"original")
        replacement.write_bytes(b"replacement")
        from mingtu_ai.naming import rename_noreplace as real_rename_noreplace

        def replace_before_atomic_move(move_source, move_target):
            source.rename(preserved_original)
            replacement.rename(source)
            return real_rename_noreplace(move_source, move_target)

        with patch(
            "mingtu_ai.history.store.rename_noreplace",
            side_effect=replace_before_atomic_move,
        ):
            with self.assertRaises(RenameRecoveryRequiredError) as raised:
                self.store.rename_file(source, target.name)

        self.assertEqual(preserved_original.read_bytes(), b"original")
        self.assertEqual(target.read_bytes(), b"replacement")
        event = self.store.get(raised.exception.event_id)
        self.assertEqual(event.status, EventStatus.PENDING)
        self.assertTrue(event.metadata["requires_manual_recovery"])

    def test_reconcile_legacy_pending_rename_never_unlinks_either_path(self) -> None:
        source = self.root / "source.txt"
        target = self.root / "target.txt"
        source.write_bytes(b"original")
        os.link(source, target)
        info = source.stat()
        event_id = self.store._insert(
            EventType.RENAME, EventStatus.PENDING, source, target_path=target,
            metadata={"target_identity": [info.st_dev, info.st_ino]},
        )
        with self.assertRaisesRegex(ValueError, "manual inspection"):
            self.store.reconcile_pending_rename(event_id)
        self.assertEqual(source.read_bytes(), b"original")
        self.assertEqual(target.read_bytes(), b"original")
        event = self.store.get(event_id)
        self.assertEqual(event.status, EventStatus.PENDING)
        self.assertTrue(event.metadata["requires_manual_recovery"])

    def test_reconcile_legacy_pending_rename_stays_manual_on_retry(self) -> None:
        source = self.root / "source.txt"
        target = self.root / "target.txt"
        source.write_bytes(b"original")
        os.link(source, target)
        info = source.stat()
        event_id = self.store._insert(
            EventType.RENAME, EventStatus.PENDING, source, target_path=target,
            metadata={"target_identity": [info.st_dev, info.st_ino]},
        )
        with self.assertRaisesRegex(ValueError, "manual inspection"):
            self.store.reconcile_pending_rename(event_id)
        with self.assertRaisesRegex(ValueError, "manual inspection"):
            self.store.reconcile_pending_rename(event_id)
        self.assertEqual(source.read_bytes(), b"original")
        self.assertEqual(target.read_bytes(), b"original")

    def test_existing_parent_permissions_are_not_changed(self) -> None:
        shared_parent = self.root / "shared-state"
        shared_parent.mkdir(mode=0o755)

        store = HistoryStore(shared_parent / "history.sqlite3")

        self.assertIsNotNone(store)
        self.assertEqual(shared_parent.stat().st_mode & 0o777, 0o755)
        self.assertEqual(store.database_path.stat().st_mode & 0o777, 0o600)

    def test_existing_database_symlink_is_rejected_without_changing_target(self) -> None:
        victim = self.root / "victim.txt"
        victim.write_text("not a database", encoding="utf-8")
        victim.chmod(0o644)
        database_link = self.root / "linked-history.sqlite3"
        database_link.symlink_to(victim)

        with self.assertRaises((OSError, ValueError)):
            HistoryStore(database_link)

        self.assertEqual(victim.read_text(encoding="utf-8"), "not a database")
        self.assertEqual(victim.stat().st_mode & 0o777, 0o644)

    def test_symlink_history_parent_is_rejected_without_changing_target(self) -> None:
        real_parent = self.root / "shared"
        real_parent.mkdir(mode=0o755)
        linked_parent = self.root / "state-link"
        linked_parent.symlink_to(real_parent, target_is_directory=True)

        with self.assertRaises((OSError, ValueError)):
            HistoryStore(linked_parent / "history.sqlite3")

        self.assertEqual(real_parent.stat().st_mode & 0o777, 0o755)
        self.assertFalse((real_parent / "history.sqlite3").exists())


if __name__ == "__main__":
    unittest.main()
