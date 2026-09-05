from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory
import tempfile
import threading
import unittest

from mingtu_ai import AppConfig, NamingSuggestion
from mingtu_ai.adapters import AdaptedFile, AdapterKind
from mingtu_ai.app import ApplicationCoordinator
from mingtu_ai.history import EventType, HistoryStore
from mingtu_ai.notifications import NotificationReceipt, NotificationService
from mingtu_ai.queue import TaskState
from mingtu_ai.watcher import (
    FileEventMonitor,
    InotifyWatcher,
    OwnedMoveSuppressor,
    WatchInvalidatedError,
    WatchOverflowError,
)


class SilentBackend:
    def __init__(self) -> None:
        self.notifications = []

    def send(self, _notification, _on_action=None):
        self.notifications.append(_notification)
        return NotificationReceipt(True, "silent")

    def close(self):
        return None


class RuntimeTracker:
    def __init__(self) -> None:
        self.batch_entries = 0
        self.starts = 0
        self.jobs = 0
        self.shutdowns = 0
        self.running = False

    @contextmanager
    def batch(self):
        self.batch_entries += 1
        try:
            yield self
        finally:
            if self.running:
                self.shutdowns += 1
                self.running = False

    @contextmanager
    def job(self):
        self.jobs += 1
        if not self.running:
            self.starts += 1
            self.running = True
        yield


class ReadingProvider:
    def __init__(self, runtime: RuntimeTracker, reads: list[bytes]) -> None:
        self.runtime = runtime
        self.reads = reads

    def analyze(self, item):
        with self.runtime.job():
            self.reads.extend(path.read_bytes() for path in item.image_paths)
        return NamingSuggestion(
            suggested_stem="圖片_內容",
            detected_language="zh-Hant",
            category="image",
            keywords=["圖片"],
            confidence=0.8,
        )


class IntegrationTests(unittest.TestCase):
    def test_admission_storm_records_failures_without_crashing_monitor(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            stop_event = threading.Event()

            class StormMonitor:
                polls = 0

                def poll(self, timeout=None):
                    self.polls += 1
                    stop_event.set()
                    return [root / f"capture-{index}.png" for index in range(20)]

            monitor = StormMonitor()

            @contextmanager
            def monitor_factory(_directories):
                yield monitor

            store = HistoryStore(root / "history.sqlite3")
            backend = SilentBackend()
            with ApplicationCoordinator(
                config=AppConfig(max_pending_tasks=0),
                watch_directories=[root],
                history=store,
                notifications=NotificationService(backend),
                runtime_factory=RuntimeTracker().batch,
                provider_factory=lambda runtime: ReadingProvider(runtime, []),
                monitor_factory=monitor_factory,
            ) as coordinator:
                coordinator.run_forever(stop_event)

            failures = [
                event
                for event in store.list_recent(limit=50)
                if event.event_type is EventType.FAILURE
            ]
            self.assertEqual(monitor.polls, 1)
            self.assertEqual(len(failures), 20)
            self.assertTrue(
                all("QueueCapacityError" in (event.message or "") for event in failures)
            )
            self.assertEqual(len(backend.notifications), 20)

    def test_fatal_monitor_state_stops_coordinator_instead_of_losing_events(self) -> None:
        for failure in (
            WatchOverflowError("inotify event queue overflowed"),
            WatchInvalidatedError("inotify directory watch was removed"),
        ):
            with (
                self.subTest(error_type=type(failure).__name__),
                tempfile.TemporaryDirectory() as temporary,
            ):
                root = Path(temporary)

                class FailingMonitor:
                    def poll(self, timeout=None):
                        raise failure

                @contextmanager
                def monitor_factory(_directories):
                    yield FailingMonitor()

                with ApplicationCoordinator(
                    config=AppConfig(),
                    watch_directories=[root],
                    history=HistoryStore(root / "history.sqlite3"),
                    notifications=NotificationService(SilentBackend()),
                    runtime_factory=RuntimeTracker().batch,
                    provider_factory=lambda runtime: ReadingProvider(runtime, []),
                    monitor_factory=monitor_factory,
                ) as coordinator:
                    with self.assertRaises(type(failure)):
                        coordinator.run_forever()

    def test_batch_reuses_runtime_and_provider_reads_before_temp_cleanup(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            sources = [root / "one.png", root / "two.png"]
            for source in sources:
                source.write_bytes(b"original")

            derived_paths: list[Path] = []

            @contextmanager
            def adapter(source: Path):
                with TemporaryDirectory() as derived_directory:
                    image = Path(derived_directory) / "page.png"
                    image.write_bytes(source.name.encode("utf-8"))
                    derived_paths.append(image)
                    yield AdaptedFile(
                        source_path=source,
                        mime_type="image/png",
                        kind=AdapterKind.IMAGE,
                        image_paths=(image,),
                    )

            tracker = RuntimeTracker()
            reads: list[bytes] = []
            store = HistoryStore(root / "history.sqlite3")
            with ApplicationCoordinator(
                config=AppConfig(),
                watch_directories=[root],
                history=store,
                notifications=NotificationService(SilentBackend()),
                runtime_factory=tracker.batch,
                provider_factory=lambda runtime: ReadingProvider(runtime, reads),
                adapter_factory=adapter,
            ) as coordinator:
                tasks = [coordinator._queue.submit(source) for source in sources]
                coordinator._queue.run_until_idle()

            self.assertTrue(all(task.state is TaskState.SUCCEEDED for task in tasks))
            self.assertEqual(tracker.batch_entries, 1)
            self.assertEqual(tracker.starts, 1)
            self.assertEqual(tracker.jobs, 2)
            self.assertEqual(tracker.shutdowns, 1)
            self.assertEqual(reads, [b"one.png", b"two.png"])
            self.assertTrue(all(not path.exists() for path in derived_paths))
            self.assertEqual(
                len(
                    [
                        event
                        for event in store.list_recent()
                        if event.event_type is EventType.SUGGESTION
                    ]
                ),
                2,
            )

    def test_accept_and_undo_move_events_are_not_reanalyzed(self) -> None:
        with (
            tempfile.TemporaryDirectory() as temporary,
            tempfile.TemporaryDirectory() as state_directory,
        ):
            root = Path(temporary)
            tracker = RuntimeTracker()
            reads: list[bytes] = []
            store = HistoryStore(Path(state_directory) / "history.sqlite3")
            suppressor = OwnedMoveSuppressor()

            with InotifyWatcher([root]) as watcher:
                monitor = FileEventMonitor(
                    watcher,
                    stable_interval=0.001,
                    stable_rounds=1,
                    stable_timeout=0.5,
                    owned_moves=suppressor,
                )
                with ApplicationCoordinator(
                    config=AppConfig(),
                    watch_directories=[root],
                    history=store,
                    notifications=NotificationService(SilentBackend()),
                    runtime_factory=tracker.batch,
                    provider_factory=lambda runtime: ReadingProvider(runtime, reads),
                    owned_moves=suppressor,
                ) as coordinator:
                    source = root / "capture.png"
                    source.write_bytes(b"image")
                    self.assertEqual(monitor.poll(1), [source])

                    task = coordinator._queue.submit(source)
                    coordinator._queue.run_until_idle()
                    self.assertIs(task.state, TaskState.SUCCEEDED)
                    assert task.result is not None
                    rename = coordinator.accept(task.result)
                    self.assertIsNotNone(rename)

                    # The accepted rename produces IN_MOVED_TO, but it belongs
                    # to this coordinator and must not enter the queue again.
                    self.assertEqual(monitor.poll(1), [])
                    assert rename is not None
                    undo = coordinator.undo(rename.id)
                    self.assertIsNotNone(undo)
                    self.assertEqual(monitor.poll(1), [])
                    self.assertEqual(tracker.jobs, 1)


if __name__ == "__main__":
    unittest.main()
