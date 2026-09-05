"""Shared desktop/notification decisions with real local files and SQLite."""

from contextlib import contextmanager
from dataclasses import FrozenInstanceError
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest
from unittest.mock import patch

from mingtu_ai import AppConfig, NamingSuggestion
from mingtu_ai.app import ApplicationCoordinator, ProcessingError
from mingtu_ai.history import EventType, HistoryStore
from mingtu_ai.notifications import NotificationReceipt, NotificationService


class RecordingBackend:
    def __init__(self):
        self.sent = []

    def send(self, notification, on_action=None):
        self.sent.append(notification)
        return NotificationReceipt(True, "test")

    def close(self):
        pass


@contextmanager
def runtime_scope():
    yield None


class Provider:
    def analyze(self, _item):
        return NamingSuggestion(
            suggested_stem="searchable", detected_language="en",
            category="document", keywords=["document"], confidence=0.8,
        )


class ProposalTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.source = self.root / "original.txt"
        self.source.write_text("test document", encoding="utf-8")
        self.store = HistoryStore(self.root / "state" / "history.sqlite3")
        self.backend = RecordingBackend()
        self.coordinator = ApplicationCoordinator(
            config=AppConfig(), watch_directories=[self.root], history=self.store,
            notifications=NotificationService(self.backend),
            runtime_factory=runtime_scope, provider_factory=lambda _: Provider(),
        )

    def tearDown(self):
        self.coordinator.close()
        self.temporary.cleanup()

    def analyze(self):
        return self.coordinator.analyze_once(self.source)

    def test_snapshots_are_immutable_and_public_proposal_cannot_change_decision(self):
        proposal = self.analyze()
        snapshot, = self.coordinator.list_pending_proposals()
        self.assertEqual(snapshot.suggested_stem, "searchable")
        with self.assertRaises(FrozenInstanceError):
            snapshot.suggested_filename = "mutated.txt"
        proposal.suggestion.suggested_stem = "mutated"
        event = self.coordinator.accept_proposal(snapshot.history_event_id)
        self.assertEqual(event.target_path.name, "searchable.txt")
        self.assertEqual(self.coordinator.list_pending_proposals(), ())

    def test_ignore_from_ui_disables_stale_notification_accept(self):
        proposal = self.analyze()
        notification = self.backend.sent[-1]
        self.assertIsNotNone(self.coordinator.ignore_proposal(proposal.history_event_id))
        next(action for action in notification.actions if action.key == "accept").callback()
        self.assertTrue(self.source.exists())
        self.assertFalse((self.root / "searchable.txt").exists())
        self.assertIsNone(self.coordinator.ignore(proposal))
        self.assertEqual(
            [event.event_type for event in self.store.list_recent()],
            [EventType.IGNORED, EventType.SUGGESTION],
        )

    def test_accept_and_ignore_callbacks_are_serialized(self):
        proposal = self.analyze()
        entered = threading.Event()
        release = threading.Event()
        real_rename = self.store.rename_file
        results = []

        def slow_rename(*args, **kwargs):
            entered.set()
            if not release.wait(5):
                raise RuntimeError("test did not release rename")
            return real_rename(*args, **kwargs)

        accept = threading.Thread(target=lambda: results.append(self.coordinator.accept(proposal)))
        ignore = threading.Thread(target=lambda: results.append(self.coordinator.ignore(proposal)))
        with patch.object(self.store, "rename_file", side_effect=slow_rename):
            accept.start()
            self.assertTrue(entered.wait(5))
            ignore.start()
            release.set()
            accept.join(5)
            ignore.join(5)
        self.assertFalse(accept.is_alive())
        self.assertFalse(ignore.is_alive())
        self.assertEqual(sum(result is not None for result in results), 1)
        self.assertEqual(
            [event.event_type for event in self.store.list_recent()],
            [EventType.RENAME, EventType.SUGGESTION],
        )

    def test_ignore_while_edit_dialog_is_open_prevents_rename(self):
        proposal = self.analyze()

        def edit_and_ignore(_proposal):
            self.coordinator.ignore_proposal(proposal.history_event_id)
            return "edited"

        self.coordinator._edit_name = edit_and_ignore
        self.assertIsNone(self.coordinator.edit(proposal))
        self.assertTrue(self.source.exists())
        self.assertFalse((self.root / "edited.txt").exists())

    def test_failed_database_write_retains_pending_for_retry(self):
        proposal = self.analyze()
        with patch.object(self.store, "rename_file", side_effect=sqlite3.OperationalError("busy")):
            self.assertIsNone(self.coordinator.accept(proposal))
        self.assertEqual(len(self.coordinator.list_pending_proposals()), 1)
        self.assertTrue(self.source.exists())
        self.assertIsNotNone(self.coordinator.accept_proposal(proposal.history_event_id))

    def test_replaced_source_is_terminal_and_never_renamed(self):
        proposal = self.analyze()
        replacement = self.root / "replacement.txt"
        replacement.write_text("replacement", encoding="utf-8")
        replacement.replace(self.source)
        self.assertIsNone(self.coordinator.accept(proposal))
        self.assertEqual(self.coordinator.list_pending_proposals(), ())
        self.assertEqual(self.source.read_text(), "replacement")
        self.assertEqual(self.store.list_recent(1)[0].event_type, EventType.FAILURE)

    def test_replaced_source_before_atomic_rename_consumes_proposal_without_move(self):
        proposal = self.analyze()
        replacement = self.root / "replacement.txt"
        replacement.write_text("replacement", encoding="utf-8")
        real_insert = self.store._insert

        def replace_during_rename(event_type, *args, **kwargs):
            event_id = real_insert(event_type, *args, **kwargs)
            if event_type is EventType.RENAME:
                replacement.replace(self.source)
            return event_id

        with (
            patch.object(self.store, "_insert", side_effect=replace_during_rename),
            patch.object(self.coordinator._owned_moves, "register") as register,
        ):
            self.assertIsNone(self.coordinator.accept(proposal))
        register.assert_not_called()
        self.assertEqual(self.coordinator.list_pending_proposals(), ())
        self.assertEqual(self.source.read_text(), "replacement")
        self.assertFalse((self.root / "searchable.txt").exists())
        failure = self.store.list_recent(1)[0]
        self.assertEqual(failure.event_type, EventType.FAILURE)
        self.assertIn("source changed", failure.message)

    def test_capacity_records_rejection_without_evicting_undecided_suggestions(self):
        with patch("mingtu_ai.app.coordinator.MAX_PENDING_PROPOSALS", 1):
            first = self.analyze()
            with self.assertRaises(ProcessingError):
                self.analyze()
            snapshot, = self.coordinator.list_pending_proposals()
            self.assertEqual(snapshot.history_event_id, first.history_event_id)
            self.assertEqual(self.store.list_recent(1)[0].event_type, EventType.FAILURE)
            self.assertIn("待決建議已達 1", self.backend.sent[-1].body)
            self.coordinator.ignore(first)
            self.assertIsNotNone(self.analyze())

    def test_unchanged_edit_is_recorded_as_ignored_and_consumed(self):
        proposal = self.analyze()
        self.assertIsNone(self.coordinator.accept_proposal(
            proposal.history_event_id, edited_stem=self.source.stem,
        ))
        self.assertEqual(self.coordinator.list_pending_proposals(), ())
        self.assertEqual(self.store.list_recent(1)[0].event_type, EventType.IGNORED)
        self.assertTrue(self.source.exists())


if __name__ == "__main__":
    unittest.main()
