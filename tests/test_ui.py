from __future__ import annotations

import os
import json
import queue
from pathlib import Path
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from mingtu_ai import AppConfig
from mingtu_ai.ui.native import (
    NativeUiController,
    NativeUiUnavailable,
    _load_native_module,
    run_native_ui,
)


class FakeHistory:
    def list_recent(self, limit=100):
        return []


class FakeCoordinator:
    def __init__(self) -> None:
        self.config = AppConfig()
        self._watch_directories = [Path("/tmp/screenshots")]
        self.history = FakeHistory()
        self.pending_count = 0
        self.is_active = False
        self.is_paused = False
        self.started = threading.Event()
        self.closed = 0
        self.actions = []

    @property
    def watch_directories(self):
        return tuple(self._watch_directories)

    def add_watch_directory(self, directory):
        candidate = Path(directory).expanduser().absolute()
        if candidate not in self._watch_directories:
            self._watch_directories.append(candidate)
        return tuple(self._watch_directories)

    def remove_watch_directory(self, directory):
        candidate = Path(directory).expanduser().absolute()
        if candidate in self._watch_directories:
            self._watch_directories.remove(candidate)
        return tuple(self._watch_directories)

    def run_forever(self, stop_event):
        self.started.set()
        stop_event.wait()

    def toggle_paused(self):
        self.is_paused = not self.is_paused
        return self.is_paused

    def list_pending_proposals(self):
        return ()

    def accept_proposal(self, event_id, *, edited_stem=None):
        self.actions.append(("accept", event_id, edited_stem, threading.current_thread().name))
        return object()

    def ignore_proposal(self, event_id):
        self.actions.append(("ignore", event_id, None, threading.current_thread().name))
        return None

    def undo(self, event_id):
        self.actions.append(("undo", event_id, None, threading.current_thread().name))
        return object()

    def close(self):
        self.closed += 1


class RecordingBridge:
    def __init__(self) -> None:
        self.pending: list[int] = []
        self.model_statuses: list[str] = []
        self.paused: list[bool] = []
        self.history: list[str] = []
        self.settings: list[str] = []
        self.watch_dirs: list[str] = []
        self.errors: list[str] = []
        self.proposals = []
        self.busy = []
        self.messages = []

    def set_pending_count(self, count):
        self.pending.append(count)

    def set_model_status(self, status):
        self.model_statuses.append(status)

    def set_paused(self, paused):
        self.paused.append(paused)

    def set_history_json(self, text):
        self.history.append(text)

    def set_watch_directories_json(self, text):
        self.watch_dirs.append(text)

    def set_proposals_json(self, text):
        self.proposals.append(text)

    def set_action_busy(self, busy):
        self.busy.append(busy)

    def set_action_message(self, message):
        self.messages.append(message)

    def set_settings_text(self, text):
        self.settings.append(text)

    def set_error(self, message):
        self.errors.append(message)


class NativeUiTests(unittest.TestCase):
    def test_controller_starts_monitor_updates_and_stops_idempotently(self) -> None:
        coordinator = FakeCoordinator()
        controller = NativeUiController(coordinator)
        bridge = RecordingBridge()

        controller.start(bridge)
        self.assertTrue(coordinator.started.wait(1.0))
        self.assertTrue(controller.toggle_pause())
        time.sleep(0.01)
        controller.stop()
        controller.stop()

        self.assertEqual(coordinator.closed, 1)
        self.assertIn(True, bridge.paused)
        self.assertIn("待命（有工作時按需載入）", bridge.model_statuses)
        self.assertTrue(bridge.settings)
        self.assertIn("127.0.0.1:3001", bridge.settings[0])
        assert controller._monitor_thread is not None
        assert controller._status_thread is not None
        self.assertFalse(controller._monitor_thread.is_alive())
        self.assertFalse(controller._status_thread.is_alive())

    def test_actions_are_bounded_off_gui_and_shutdown_drains(self) -> None:
        coordinator = FakeCoordinator()
        entered = threading.Event()
        release = threading.Event()
        original = coordinator.accept_proposal

        def blocking_accept(event_id, *, edited_stem=None):
            entered.set()
            release.wait(2)
            return original(event_id, edited_stem=edited_stem)

        coordinator.accept_proposal = blocking_accept
        controller = NativeUiController(coordinator)
        bridge = RecordingBridge()
        controller.start(bridge)
        try:
            self.assertTrue(controller.request_action("edit", 42, "新名稱"))
            self.assertTrue(entered.wait(1))
            self.assertFalse(controller.request_action("ignore", 43))
            self.assertTrue(bridge.busy[-1])
        finally:
            release.set()
            controller.stop()
        self.assertEqual(coordinator.actions[0][:3], ("accept", 42, "新名稱"))
        self.assertEqual(coordinator.actions[0][3], "mingtu-ai-native-actions")
        self.assertFalse(controller._action_thread.is_alive())
        self.assertFalse(controller.request_action("accept", 99))
        self.assertIn("操作完成。", bridge.messages)

    def test_shutdown_cannot_overtake_action_admission(self) -> None:
        coordinator = FakeCoordinator()
        controller = NativeUiController(coordinator)
        entered = threading.Event()
        release = threading.Event()

        class PausingQueue(queue.Queue):
            def put_nowait(self, item):
                entered.set()
                release.wait(2)
                return super().put_nowait(item)

        controller._actions = PausingQueue(maxsize=1)
        controller.start(RecordingBridge())
        admitted = []
        submit = threading.Thread(target=lambda: admitted.append(controller.request_action("accept", 19)))
        stopper = threading.Thread(target=controller.stop)
        try:
            submit.start()
            self.assertTrue(entered.wait(1))
            stopper.start()
            # Shutdown must wait for the admission lock, leaving the worker
            # alive until insertion has completed.
            self.assertFalse(controller._stop_event.wait(0.15))
        finally:
            release.set()
            submit.join(3)
            stopper.join(3)
            controller.stop()
        self.assertEqual(admitted, [True])
        self.assertEqual(coordinator.actions[0][:2], ("accept", 19))
        self.assertFalse(stopper.is_alive())
        self.assertFalse(controller._action_thread.is_alive())

    def test_monitor_failure_cannot_overtake_action_admission(self) -> None:
        coordinator = FakeCoordinator()
        controller = NativeUiController(coordinator)
        entered = threading.Event()
        release = threading.Event()
        fail_monitor = threading.Event()
        reported = threading.Event()
        bridge = RecordingBridge()
        bridge.set_error = lambda message: reported.set()

        def monitor(_stop):
            fail_monitor.wait(2)
            raise OSError("synthetic lost watch")

        class PausingQueue(queue.Queue):
            def put_nowait(self, item):
                entered.set()
                release.wait(2)
                return super().put_nowait(item)

        coordinator.run_forever = monitor
        controller._actions = PausingQueue(maxsize=1)
        controller.start(bridge)
        admitted = []
        submit = threading.Thread(target=lambda: admitted.append(controller.request_action("accept", 20)))
        try:
            submit.start()
            self.assertTrue(entered.wait(1))
            fail_monitor.set()
            self.assertTrue(reported.wait(1))
            self.assertFalse(controller._stop_event.wait(0.15))
        finally:
            release.set()
            fail_monitor.set()
            submit.join(3)
            controller.stop()
        self.assertEqual(admitted, [True])
        self.assertEqual(coordinator.actions[0][:2], ("accept", 20))
        self.assertFalse(controller._action_thread.is_alive())

    def test_partial_thread_start_failure_can_be_stopped(self) -> None:
        coordinator = FakeCoordinator()
        controller = NativeUiController(coordinator)
        original_start = threading.Thread.start

        def start(thread):
            if thread.name == "mingtu-ai-native-monitor":
                raise RuntimeError("synthetic thread exhaustion")
            original_start(thread)

        with patch.object(threading.Thread, "start", start):
            with self.assertRaisesRegex(RuntimeError, "exhaustion"):
                controller.start(RecordingBridge())
        controller.stop()
        self.assertFalse(controller._action_thread.is_alive())
        self.assertEqual(coordinator.closed, 1)

    def test_noop_is_not_reported_as_success(self) -> None:
        coordinator = FakeCoordinator()
        controller = NativeUiController(coordinator)
        bridge = RecordingBridge()
        controller.start(bridge)
        controller.request_action("ignore", 3)
        controller.stop()
        self.assertTrue(any("操作未完成" in message for message in bridge.messages))
        self.assertNotIn("操作完成。", bridge.messages)

    def test_history_failure_is_visible_and_refresh_recovers(self) -> None:
        coordinator = FakeCoordinator()
        controller = NativeUiController(coordinator)
        bridge = RecordingBridge()
        attempts = []

        def history(limit=100):
            attempts.append(1)
            if len(attempts) == 1:
                raise OSError("synthetic database unavailable")
            controller._stop_event.set()
            return []

        coordinator.history.list_recent = history
        controller._bridge = bridge
        controller._publish_status()
        self.assertTrue(any("synthetic database unavailable" in message for message in bridge.messages))
        self.assertEqual(json.loads(bridge.history[-1]), [])
        self.assertEqual(len(attempts), 2)

    def test_history_eligibility_is_cached_until_events_change(self) -> None:
        from mingtu_ai.history import EventStatus, EventType
        event = SimpleNamespace(id=8, status=EventStatus.SUCCESS, updated_at="1", created_at="2026-09-05T00:00:00+00:00", event_type=EventType.RENAME, source_path=Path("/tmp/a.txt"), target_path=Path("/tmp/b.txt"), suggested_name=None, message=None)
        coordinator = FakeCoordinator()
        coordinator.history.list_recent = lambda limit=100: [event]
        checks = []
        coordinator.history.can_undo_rename = lambda event_id: checks.append(event_id) or True
        controller = NativeUiController(coordinator)
        controller._history_json()
        controller._history_json()
        self.assertEqual(checks, [8])
        event.updated_at = "2"
        controller._history_json()
        self.assertEqual(checks, [8, 8])

    def test_run_native_ui_passes_one_controller_and_always_stops(self) -> None:
        coordinator = FakeCoordinator()
        bridge = RecordingBridge()
        calls: list[dict[str, object]] = []

        def run(controller, **kwargs):
            calls.append(kwargs)
            controller.start(bridge)
            self.assertTrue(coordinator.started.wait(1.0))
            controller.stop()
            return 7

        module = SimpleNamespace(run=run)
        with patch.dict(os.environ, {"QT_QPA_PLATFORM": "offscreen"}):
            result = run_native_ui(
                coordinator,
                native_module=module,
                quit_after_ms=25,
                show_windows=False,
            )

        self.assertEqual(result, 7)
        self.assertEqual(coordinator.closed, 1)
        self.assertEqual(calls[0]["quit_after_ms"], 25)
        self.assertFalse(calls[0]["show_windows"])

    def test_missing_extension_has_build_instruction(self) -> None:
        with patch(
            "mingtu_ai.ui.native.importlib.import_module",
            side_effect=ImportError("missing"),
        ):
            with self.assertRaisesRegex(NativeUiUnavailable, "build_native_ui"):
                _load_native_module()

    def test_native_ui_rejects_background_thread(self) -> None:
        errors: list[BaseException] = []

        def target():
            try:
                run_native_ui(FakeCoordinator(), native_module=SimpleNamespace(run=lambda *_a, **_k: 0))
            except BaseException as exc:
                errors.append(exc)

        with patch.dict(os.environ, {"QT_QPA_PLATFORM": "offscreen"}):
            thread = threading.Thread(target=target)
            thread.start()
            thread.join()

        self.assertEqual(len(errors), 1)
        self.assertIsInstance(errors[0], NativeUiUnavailable)
        self.assertIn("主執行緒", str(errors[0]))

    def test_no_desktop_session_degrades_before_loading_qt(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(NativeUiUnavailable, "session"):
                run_native_ui(
                    FakeCoordinator(),
                    native_module=SimpleNamespace(run=lambda *_a, **_k: 0),
                    show_windows=False,
                )

    def test_add_and_remove_watch_directory(self) -> None:
        coordinator = FakeCoordinator()
        controller = NativeUiController(coordinator)
        bridge = RecordingBridge()
        controller.start(bridge)
        try:
            self.assertTrue(coordinator.started.wait(1.0))
            dirs_after_add = controller.add_watch_directory("/tmp/new_watch")
            self.assertIn("/tmp/new_watch", dirs_after_add)
            self.assertIn("/tmp/new_watch", bridge.watch_dirs[-1])

            dirs_after_remove = controller.remove_watch_directory("/tmp/new_watch")
            self.assertNotIn("/tmp/new_watch", dirs_after_remove)
            self.assertNotIn("/tmp/new_watch", bridge.watch_dirs[-1])
        finally:
            controller.stop()


if __name__ == "__main__":
    unittest.main()
