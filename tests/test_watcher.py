from __future__ import annotations

from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import threading
import unittest

from mingtu_ai.watcher import (
    EventDeduplicator,
    FileEventMonitor,
    InotifyEvent,
    InotifyWatcher,
    OwnedMoveSuppressor,
    WatchInvalidatedError,
    WatchOverflowError,
    should_ignore,
    wait_until_stable,
)
from mingtu_ai.watcher.inotify import IN_IGNORED, IN_Q_OVERFLOW


class WatchPolicyTests(unittest.TestCase):
    def test_ignores_non_files_hidden_locks_downloads_and_symlinks(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            normal = root / "capture.png"
            normal.write_bytes(b"png")
            self.assertFalse(should_ignore(normal))

            for name in (".hidden", "busy.lock", "video.part", "swap.swp", "backup~"):
                candidate = root / name
                candidate.write_text("x")
                self.assertTrue(should_ignore(candidate), name)

            folder = root / "folder"
            folder.mkdir()
            link = root / "linked.png"
            link.symlink_to(normal)
            self.assertTrue(should_ignore(folder))
            self.assertTrue(should_ignore(link))

    def test_wait_until_stable_and_deduplicate_by_snapshot(self) -> None:
        with TemporaryDirectory() as directory:
            path = Path(directory) / "stable.txt"
            path.write_text("ready")
            self.assertTrue(wait_until_stable(path, interval=0, stable_rounds=1, timeout=1))

            deduplicator = EventDeduplicator(ttl=2)
            self.assertFalse(deduplicator.is_duplicate(path, now=1))
            self.assertTrue(deduplicator.is_duplicate(path, now=2))
            self.assertFalse(deduplicator.is_duplicate(path, now=4.1))

    def test_owned_move_correlations_are_capacity_and_ttl_bounded(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            now = [0.0]
            suppressor = OwnedMoveSuppressor(
                ttl=5,
                capacity=1,
                clock=lambda: now[0],
            )
            first = root / "first-target.txt"
            second = root / "second-target.txt"
            first.write_text("first", encoding="utf-8")
            second.write_text("second", encoding="utf-8")
            self.assertTrue(suppressor.register(root / "first.txt", first))
            suppressor.observe_moved_from(root / "first.txt", 1)
            self.assertTrue(suppressor.register(root / "second.txt", second))
            self.assertFalse(suppressor.consume_moved_to(first, 1))

            now[0] = 6.0
            suppressor.observe_moved_from(root / "second.txt", 2)
            self.assertFalse(suppressor.consume_moved_to(second, 2))

    def test_monitor_stops_on_overflow_or_invalidated_watch(self) -> None:
        root = Path("/watch")

        class EventWatcher:
            def __init__(self, event: InotifyEvent) -> None:
                self.event = event

            def read_events(self, _timeout=None):
                return [self.event]

        cases = (
            (
                InotifyEvent(None, "", IN_Q_OVERFLOW, 0),
                WatchOverflowError,
                "overflowed",
            ),
            (
                InotifyEvent(root, "", IN_IGNORED, 0),
                WatchInvalidatedError,
                str(root),
            ),
        )
        for event, error_type, expected in cases:
            with self.subTest(error_type=error_type.__name__):
                monitor = FileEventMonitor(EventWatcher(event))
                with self.assertRaises(error_type) as caught:
                    monitor.poll(0)
                self.assertIn(expected, str(caught.exception))


@unittest.skipUnless(sys.platform.startswith("linux"), "Linux inotify test")
class InotifyIntegrationTests(unittest.TestCase):
    def test_deleted_directory_invalidates_monitor(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory) / "watched"
            root.mkdir()
            with InotifyWatcher([root]) as watcher:
                monitor = FileEventMonitor(watcher)
                root.rmdir()
                with self.assertRaises(WatchInvalidatedError) as caught:
                    monitor.poll(1)
                self.assertIn(str(root), str(caught.exception))

    def test_close_write_becomes_one_stable_path(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            with InotifyWatcher([root]) as watcher:
                monitor = FileEventMonitor(
                    watcher,
                    stable_interval=0.001,
                    stable_rounds=1,
                    stable_timeout=0.5,
                )
                created = root / "capture.png"
                created.write_bytes(b"complete")
                self.assertEqual(monitor.poll(1), [created])

    def test_moved_to_is_seen_but_hidden_file_is_ignored(self) -> None:
        with TemporaryDirectory() as watched, TemporaryDirectory() as staging:
            root = Path(watched)
            with InotifyWatcher([root]) as watcher:
                monitor = FileEventMonitor(
                    watcher,
                    stable_interval=0.001,
                    stable_rounds=1,
                    stable_timeout=0.5,
                )
                source = Path(staging) / "incoming.pdf"
                source.write_bytes(b"%PDF-1.4")
                destination = root / source.name
                source.replace(destination)
                self.assertEqual(monitor.poll(1), [destination])

                (root / ".hidden").write_text("ignored")
                self.assertEqual(monitor.poll(1), [])

    def test_owned_move_is_suppressed_but_external_same_path_and_inode_is_seen(self) -> None:
        with TemporaryDirectory() as watched, TemporaryDirectory() as staging:
            root = Path(watched)
            outside = Path(staging)
            moved_from_seen = threading.Event()

            class SignalingSuppressor(OwnedMoveSuppressor):
                def observe_moved_from(self, source, cookie):
                    moved_from_seen.set()
                    super().observe_moved_from(source, cookie)

            suppressor = SignalingSuppressor(ttl=10, capacity=4)
            with InotifyWatcher([root]) as watcher:
                monitor = FileEventMonitor(
                    watcher,
                    stable_interval=0.001,
                    stable_rounds=1,
                    stable_timeout=0.5,
                    owned_moves=suppressor,
                )
                source = root / "capture.txt"
                source.write_text("same inode", encoding="utf-8")
                self.assertEqual(monitor.poll(1), [source])

                destination = root / "named.txt"
                polled: list[list[Path]] = []
                with suppressor.move_guard():
                    source.rename(destination)
                    poll_thread = threading.Thread(
                        target=lambda: polled.append(monitor.poll(1)),
                    )
                    poll_thread.start()
                    self.assertTrue(moved_from_seen.wait(1))
                    self.assertTrue(suppressor.register(source, destination))
                poll_thread.join(1)
                self.assertFalse(poll_thread.is_alive())
                self.assertEqual(polled, [[]])

                # Reuse both the destination name and inode from outside the
                # watched directory.  Its new move cookie must remain visible.
                external_link = outside / "same-inode.txt"
                external_link.hardlink_to(destination)
                destination.unlink()
                external_link.rename(destination)
                self.assertEqual(monitor.poll(1), [destination])


if __name__ == "__main__":
    unittest.main()
