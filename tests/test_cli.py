from __future__ import annotations

import io
import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from mingtu_ai import AppConfig, NamingSuggestion
from mingtu_ai.app import ApplicationCoordinator, ProcessingError, SuggestionProposal
from mingtu_ai.cli import _watch_directories, build_parser, main
from mingtu_ai.ui import NativeUiUnavailable
from mingtu_ai.diagnostics import CheckResult, CheckStatus, DoctorReport
from mingtu_ai.history import EventStatus, EventType, HistoryStore
from mingtu_ai.notifications import NotificationReceipt, NotificationService


class FakeCoordinator:
    proposal: SuggestionProposal | None = None
    error: Exception | None = None
    ran_forever = False
    closed = False

    def __enter__(self):
        return self

    def __exit__(self, *_exc_info):
        type(self).closed = True

    def analyze_once(self, _path):
        if type(self).error:
            raise type(self).error
        assert type(self).proposal is not None
        return type(self).proposal

    def run_forever(self):
        type(self).ran_forever = True
        raise KeyboardInterrupt


class RecordingNotificationBackend:
    def __init__(self) -> None:
        self.notifications = []
        self.callbacks = []
        self.closed = False

    def send(self, notification, on_action=None):
        self.notifications.append(notification)
        self.callbacks.append(on_action)
        return NotificationReceipt(True, "recording")

    def close(self):
        self.closed = True


class StaticProvider:
    def analyze(self, _request):
        return NamingSuggestion(
            suggested_stem="CUDA_錯誤",
            detected_language="zh-Hant",
            category="software_error",
            keywords=["CUDA"],
            confidence=0.9,
        )


@contextmanager
def fake_runtime():
    yield object()


class CliTests(unittest.TestCase):
    def setUp(self) -> None:
        FakeCoordinator.proposal = None
        FakeCoordinator.error = None
        FakeCoordinator.ran_forever = False
        FakeCoordinator.closed = False

    def test_once_outputs_validated_suggestion_json(self) -> None:
        suggestion = NamingSuggestion(
            suggested_stem="CUDA_錯誤",
            detected_language="zh-Hant",
            category="software_error",
            keywords=["CUDA"],
            confidence=0.9,
        )
        FakeCoordinator.proposal = SuggestionProposal(
            Path("/tmp/input.png"),
            "CUDA_錯誤.png",
            suggestion,
            7,
        )
        output = io.StringIO()
        error = io.StringIO()
        with patch("mingtu_ai.cli.create_coordinator", return_value=FakeCoordinator()):
            code = main(["--once", "/tmp/input.png", "--json"], stdout=output, stderr=error)

        self.assertEqual(code, 0)
        payload = json.loads(output.getvalue())
        self.assertEqual(payload["suggested_filename"], "CUDA_錯誤.png")
        self.assertEqual(payload["history_event_id"], 7)
        self.assertTrue(FakeCoordinator.closed)
        self.assertEqual(error.getvalue(), "")

    def test_once_failure_is_reported_without_traceback(self) -> None:
        FakeCoordinator.error = ProcessingError("model failed safely")
        output = io.StringIO()
        error = io.StringIO()
        with patch("mingtu_ai.cli.create_coordinator", return_value=FakeCoordinator()):
            code = main(["--once", "/tmp/input.png"], stdout=output, stderr=error)

        self.assertEqual(code, 1)
        self.assertIn("model failed safely", error.getvalue())
        self.assertNotIn("Traceback", error.getvalue())

    def test_doctor_uses_only_fixed_loopback_port_3001(self) -> None:
        report = DoctorReport((CheckResult("port", CheckStatus.PASS, "free"),))
        output = io.StringIO()
        with patch("mingtu_ai.cli.run_doctor", return_value=report) as doctor:
            code = main(
                [
                    "--doctor",
                    "--json",
                    "--model",
                    "/tmp/model.gguf",
                    "--mmproj",
                    "/tmp/mmproj.gguf",
                    "--watch",
                    "/tmp",
                ],
                stdout=output,
                environ={},
            )

        self.assertEqual(code, 0)
        self.assertTrue(json.loads(output.getvalue())["healthy"])
        kwargs = doctor.call_args.kwargs
        self.assertEqual((kwargs["host"], kwargs["port"]), ("127.0.0.1", 3001))
        self.assertEqual(kwargs["model_path"], Path("/tmp/model.gguf"))
        self.assertEqual(kwargs["mmproj_path"], Path("/tmp/mmproj.gguf"))

    def test_resolve_ollama_is_a_main_cli_mode(self) -> None:
        output = io.StringIO()
        error = io.StringIO()
        environment = {"XDG_DATA_HOME": "/tmp/mingtu-test-data"}
        with patch(
            "mingtu_ai.model_sources.ollama.main",
            return_value=7,
        ) as resolver:
            code = main(
                ["--resolve-ollama", "gemma:latest", "--json"],
                stdout=output,
                stderr=error,
                environ=environment,
            )

        self.assertEqual(code, 7)
        resolver.assert_called_once_with(
            ["gemma:latest", "--json"],
            stdout=output,
            stderr=error,
            environ=environment,
        )

    def test_daemon_mode_dispatches_monitor_and_closes(self) -> None:
        output = io.StringIO()
        with patch("mingtu_ai.cli.create_coordinator", return_value=FakeCoordinator()):
            code = main(["--watch", "/tmp", "--headless"], stdout=output, environ={})

        self.assertEqual(code, 0)
        self.assertTrue(FakeCoordinator.ran_forever)
        self.assertTrue(FakeCoordinator.closed)
        self.assertIn("/tmp", output.getvalue())

    def test_daemon_mode_prefers_native_ui_and_preserves_its_exit_code(self) -> None:
        coordinator = FakeCoordinator()
        with patch("mingtu_ai.cli.create_coordinator", return_value=coordinator):
            with patch("mingtu_ai.cli.run_native_ui", return_value=9) as native:
                code = main(["--watch", "/tmp"], environ={})

        self.assertEqual(code, 9)
        native.assert_called_once_with(coordinator)
        self.assertFalse(FakeCoordinator.ran_forever)
        self.assertTrue(FakeCoordinator.closed)

    def test_missing_native_ui_falls_back_to_headless(self) -> None:
        output = io.StringIO()
        error = io.StringIO()
        with patch("mingtu_ai.cli.create_coordinator", return_value=FakeCoordinator()):
            with patch(
                "mingtu_ai.cli.run_native_ui",
                side_effect=NativeUiUnavailable("module missing"),
            ):
                code = main(["--watch", "/tmp"], stdout=output, stderr=error, environ={})

        self.assertEqual(code, 0)
        self.assertTrue(FakeCoordinator.ran_forever)
        self.assertIn("headless", error.getvalue())

    def test_watch_precedence_is_cli_then_environment_then_spectacle(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            config = home / ".config"
            spectacle_directory = home / "圖片" / "螢幕截圖"
            spectacle_directory.mkdir(parents=True)
            config.mkdir()
            (config / "user-dirs.dirs").write_text(
                'XDG_PICTURES_DIR="$HOME/圖片"\n', encoding="utf-8"
            )
            (config / "spectaclerc").write_text(
                "[ImageSave]\ntranslatedScreenshotsFolder=螢幕截圖\n",
                encoding="utf-8",
            )

            base_environment = {"HOME": str(home)}
            parser = build_parser(base_environment)
            default_args = parser.parse_args([])
            self.assertEqual(
                _watch_directories(default_args, base_environment),
                [spectacle_directory],
            )

            environment_directory = home / "env-captures"
            environment = {
                **base_environment,
                "MINGTU_AI_WATCH_DIRS": str(environment_directory),
            }
            environment_args = build_parser(environment).parse_args([])
            self.assertEqual(
                _watch_directories(environment_args, environment),
                [environment_directory],
            )

            cli_directory = home / "cli-captures"
            cli_args = build_parser(environment).parse_args(
                ["--watch", str(cli_directory)]
            )
            self.assertEqual(
                _watch_directories(cli_args, environment),
                [cli_directory],
            )

    def test_once_and_doctor_are_mutually_exclusive(self) -> None:
        parser = build_parser()
        with self.assertRaises(SystemExit):
            parser.parse_args(["--once", "file", "--doctor"])

        with self.assertRaises(SystemExit):
            parser.parse_args(["--doctor", "--resolve-ollama", "gemma"])

    def test_real_coordinator_once_accept_and_undo(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "Screenshot.txt"
            source.write_text("CUDA out of memory", encoding="utf-8")
            occupied = root / "CUDA_錯誤.txt"
            occupied.write_text("must remain", encoding="utf-8")
            store = HistoryStore(root / "history.sqlite3")
            backend = RecordingNotificationBackend()

            with ApplicationCoordinator(
                config=AppConfig(),
                watch_directories=[root],
                history=store,
                notifications=NotificationService(backend),
                runtime_factory=fake_runtime,
                provider_factory=lambda _runtime: StaticProvider(),
            ) as coordinator:
                proposal = coordinator.analyze_once(source)
                self.assertEqual(proposal.suggested_filename, "CUDA_錯誤_2.txt")
                rename = coordinator.accept(proposal)
                self.assertIsNotNone(rename)
                assert rename is not None and rename.target_path is not None
                self.assertFalse(source.exists())
                self.assertTrue(rename.target_path.exists())
                self.assertEqual(rename.target_path.name, "CUDA_錯誤_2.txt")
                self.assertEqual(occupied.read_text(encoding="utf-8"), "must remain")

                undo = coordinator.undo(rename.id)
                self.assertIsNotNone(undo)
                self.assertTrue(source.exists())
                self.assertEqual(source.read_text(encoding="utf-8"), "CUDA out of memory")
                self.assertEqual(occupied.read_text(encoding="utf-8"), "must remain")

            event_types = [event.event_type for event in store.list_recent()]
            self.assertIn(EventType.SUGGESTION, event_types)
            self.assertIn(EventType.RENAME, event_types)
            self.assertIn(EventType.UNDO, event_types)
            self.assertTrue(backend.closed)

    def test_real_coordinator_daemon_submits_monitor_events(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "Screenshot.txt"
            source.write_text("CUDA", encoding="utf-8")
            store = HistoryStore(root / "history.sqlite3")
            backend = RecordingNotificationBackend()
            stopper = threading.Event()

            class OneEventMonitor:
                def poll(self, timeout=None):
                    stopper.set()
                    return [source]

            @contextmanager
            def monitor_factory(_directories):
                yield OneEventMonitor()

            with ApplicationCoordinator(
                config=AppConfig(),
                watch_directories=[root],
                history=store,
                notifications=NotificationService(backend),
                runtime_factory=fake_runtime,
                provider_factory=lambda _runtime: StaticProvider(),
                monitor_factory=monitor_factory,
            ) as coordinator:
                coordinator.run_forever(stopper)

            suggestions = [
                event for event in store.list_recent() if event.event_type is EventType.SUGGESTION
            ]
            self.assertEqual(len(suggestions), 1)

    def test_paused_daemon_discards_monitor_events_without_stopping_queue(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "Screenshot.txt"
            source.write_text("CUDA", encoding="utf-8")
            store = HistoryStore(root / "history.sqlite3")
            backend = RecordingNotificationBackend()
            stopper = threading.Event()

            class OnePausedEventMonitor:
                def poll(self, timeout=None):
                    stopper.set()
                    return [source]

            @contextmanager
            def monitor_factory(_directories):
                yield OnePausedEventMonitor()

            with ApplicationCoordinator(
                config=AppConfig(),
                watch_directories=[root],
                history=store,
                notifications=NotificationService(backend),
                runtime_factory=fake_runtime,
                provider_factory=lambda _runtime: StaticProvider(),
                monitor_factory=monitor_factory,
            ) as coordinator:
                coordinator.pause()
                coordinator.run_forever(stopper)
                self.assertTrue(coordinator.is_paused)
                self.assertFalse(coordinator.toggle_paused())

            suggestions = [
                event for event in store.list_recent() if event.event_type is EventType.SUGGESTION
            ]
            self.assertEqual(suggestions, [])

    def test_accept_refuses_a_file_replaced_after_analysis(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "Screenshot.txt"
            source.write_text("original CUDA error", encoding="utf-8")
            store = HistoryStore(root / "history.sqlite3")
            backend = RecordingNotificationBackend()

            with ApplicationCoordinator(
                config=AppConfig(),
                watch_directories=[root],
                history=store,
                notifications=NotificationService(backend),
                runtime_factory=fake_runtime,
                provider_factory=lambda _runtime: StaticProvider(),
            ) as coordinator:
                proposal = coordinator.analyze_once(source)
                source.unlink()
                source.write_text("replacement must remain untouched", encoding="utf-8")
                rename = coordinator.accept(proposal)

            self.assertIsNone(rename)
            self.assertEqual(
                source.read_text(encoding="utf-8"),
                "replacement must remain untouched",
            )
            failures = [
                event for event in store.list_recent() if event.event_type is EventType.FAILURE
            ]
            self.assertTrue(failures)

    def test_accept_ledger_insert_failure_cannot_leave_an_untracked_move(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "Screenshot.txt"
            source.write_text("original", encoding="utf-8")
            store = HistoryStore(root / "history.sqlite3")
            backend = RecordingNotificationBackend()

            with ApplicationCoordinator(
                config=AppConfig(),
                watch_directories=[root],
                history=store,
                notifications=NotificationService(backend),
                runtime_factory=fake_runtime,
                provider_factory=lambda _runtime: StaticProvider(),
            ) as coordinator:
                proposal = coordinator.analyze_once(source)
                original_insert = store._insert

                def fail_rename_insert(event_type, *args, **kwargs):
                    if event_type is EventType.RENAME:
                        raise sqlite3.OperationalError("injected rename insert failure")
                    return original_insert(event_type, *args, **kwargs)

                with patch.object(store, "_insert", side_effect=fail_rename_insert):
                    result = coordinator.accept(proposal)

            self.assertIsNone(result)
            self.assertEqual(source.read_text(encoding="utf-8"), "original")
            self.assertFalse((root / "CUDA_錯誤.txt").exists())
            self.assertFalse(
                any(event.event_type is EventType.RENAME for event in store.list_recent())
            )

    def test_accept_finalization_failure_keeps_pending_ledger_and_owned_move(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "Screenshot.txt"
            source.write_text("original", encoding="utf-8")
            store = HistoryStore(root / "history.sqlite3")
            backend = RecordingNotificationBackend()

            with ApplicationCoordinator(
                config=AppConfig(),
                watch_directories=[root],
                history=store,
                notifications=NotificationService(backend),
                runtime_factory=fake_runtime,
                provider_factory=lambda _runtime: StaticProvider(),
            ) as coordinator:
                proposal = coordinator.analyze_once(source)
                register = coordinator._owned_moves.register
                with (
                    patch.object(
                        store,
                        "_set_status",
                        side_effect=sqlite3.OperationalError("injected finalization failure"),
                    ),
                    patch.object(
                        store,
                        "record_failure",
                        side_effect=sqlite3.OperationalError("database still unavailable"),
                    ),
                    patch.object(
                        coordinator._owned_moves,
                        "register",
                        wraps=register,
                    ) as registered,
                ):
                    result = coordinator.accept(proposal)

            self.assertIsNone(result)
            target = root / "CUDA_錯誤.txt"
            self.assertFalse(source.exists())
            self.assertEqual(target.read_text(encoding="utf-8"), "original")
            registered.assert_called_once_with(source, target)
            rename_events = [
                event
                for event in store.list_recent()
                if event.event_type is EventType.RENAME
            ]
            self.assertEqual(len(rename_events), 1)
            self.assertEqual(rename_events[0].status, EventStatus.PENDING)
            self.assertEqual(rename_events[0].target_path, target)
            self.assertEqual(backend.notifications[-1].summary, "命圖 AI 處理失敗")
            self.assertIn("history logging also failed", backend.notifications[-1].body)


if __name__ == "__main__":
    unittest.main()
