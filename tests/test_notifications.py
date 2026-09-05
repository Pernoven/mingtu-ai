from __future__ import annotations

import io
import subprocess
import threading
import time
import unittest
from unittest import mock

from mingtu_ai.notifications import (
    Notification,
    NotificationAction,
    NotificationReceipt,
    NotificationService,
    NotifySendBackend,
    has_desktop_session,
)


class FakeBackend:
    def __init__(self, *, delivered: bool = True, selected: str | None = None) -> None:
        self.delivered = delivered
        self.selected = selected
        self.seen: Notification | None = None
        self.closed = False

    def send(self, notification, on_action=None):
        self.seen = notification
        if self.selected and on_action:
            on_action(self.selected)
        return NotificationReceipt(
            self.delivered,
            "fake",
            error=None if self.delivered else "no session",
        )

    def close(self):
        self.closed = True


class BlockingProcess:
    """Small Popen test double whose lifetime is controlled by an event."""

    def __init__(self, *, ignore_terminate: bool = False) -> None:
        self.ignore_terminate = ignore_terminate
        self.started = threading.Event()
        self.release = threading.Event()
        self.returncode: int | None = None
        self.stdout_text = ""
        self.terminate_calls = 0
        self.kill_calls = 0
        self.wait_calls = 0

    def communicate(self, timeout=None):
        self.started.set()
        self.release.wait()
        self.returncode = 0
        return self.stdout_text, ""

    def poll(self):
        return self.returncode

    def terminate(self):
        self.terminate_calls += 1
        if not self.ignore_terminate:
            self.release.set()

    def kill(self):
        self.kill_calls += 1
        self.release.set()

    def wait(self, timeout=None):
        self.wait_calls += 1
        if not self.release.wait(timeout):
            raise subprocess.TimeoutExpired("notify-send", timeout)
        self.returncode = 0
        return 0


def wait_until(predicate, timeout: float = 1.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.005)
    return predicate()


class NotificationTests(unittest.TestCase):
    def test_suggestion_dispatches_named_action_callback(self) -> None:
        calls: list[str] = []
        backend = FakeBackend(selected="accept")
        service = NotificationService(backend)

        receipt = service.notify_suggestion(
            "CUDA_錯誤.png",
            on_accept=lambda: calls.append("accepted"),
            on_ignore=lambda: calls.append("ignored"),
        )

        self.assertTrue(receipt.delivered)
        self.assertEqual(calls, ["accepted"])
        assert backend.seen is not None
        self.assertEqual([action.key for action in backend.seen.actions], ["accept", "edit", "ignore"])

    def test_failed_delivery_has_graceful_text_fallback(self) -> None:
        stream = io.StringIO()
        service = NotificationService(FakeBackend(delivered=False), fallback_stream=stream)

        receipt = service.notify_error("D-Bus unavailable")

        self.assertFalse(receipt.delivered)
        self.assertIn("通知降級", stream.getvalue())
        self.assertIn("D-Bus unavailable", stream.getvalue())

    def test_no_desktop_session_is_unavailable_without_running_command(self) -> None:
        backend = NotifySendBackend(
            executable="/usr/bin/notify-send",
            environ={"PATH": "/usr/bin"},
        )

        receipt = backend.send(Notification("test"))

        self.assertFalse(receipt.delivered)
        self.assertIn("session", receipt.error or "")

    def test_notify_send_command_uses_argument_vector_and_action_names(self) -> None:
        env = {
            "PATH": "/usr/bin",
            "DBUS_SESSION_BUS_ADDRESS": "unix:path=/tmp/bus",
            "WAYLAND_DISPLAY": "wayland-0",
        }
        backend = NotifySendBackend(executable="/usr/bin/notify-send", environ=env)
        notification = Notification(
            "summary",
            "body; $(never-executed)",
            actions=(NotificationAction("accept", "接受"),),
        )

        command = backend.build_command(notification)

        self.assertEqual(command[0], "/usr/bin/notify-send")
        self.assertIn("--action=accept=接受", command)
        self.assertEqual(command[-1], "body; $(never-executed)")
        self.assertTrue(has_desktop_session(env))

    def test_action_keys_reject_notify_send_delimiter(self) -> None:
        with self.assertRaises(ValueError):
            NotificationAction("bad=key", "Bad")

    def test_action_helper_capacity_rejects_before_spawning(self) -> None:
        env = {
            "PATH": "/usr/bin",
            "DBUS_SESSION_BUS_ADDRESS": "unix:path=/tmp/bus",
            "WAYLAND_DISPLAY": "wayland-0",
        }
        processes = [BlockingProcess(), BlockingProcess()]
        backend = NotifySendBackend(
            executable="/usr/bin/notify-send",
            environ=env,
            max_action_helpers=2,
            close_timeout_seconds=0.2,
        )
        notification = Notification(
            "summary",
            actions=(NotificationAction("accept", "Accept"),),
            expire_ms=0,
        )

        with mock.patch(
            "mingtu_ai.notifications.service.subprocess.Popen",
            side_effect=processes,
        ) as popen:
            first = backend.send(notification)
            second = backend.send(notification)
            self.assertTrue(all(process.started.wait(1) for process in processes))
            rejected = backend.send(notification)

            self.assertTrue(first.delivered)
            self.assertTrue(second.delivered)
            self.assertFalse(rejected.delivered)
            self.assertIn("capacity 2", rejected.error or "")
            self.assertEqual(popen.call_count, 2)
            self.assertEqual(backend.active_action_process_count, 2)
            self.assertEqual(backend.active_action_thread_count, 2)

            for process in processes:
                process.release.set()
            self.assertTrue(
                wait_until(
                    lambda: backend.active_action_process_count == 0
                    and backend.active_action_thread_count == 0
                )
            )
        backend.close()

    def test_zero_action_capacity_and_invalid_limits(self) -> None:
        env = {
            "PATH": "/usr/bin",
            "DBUS_SESSION_BUS_ADDRESS": "unix:path=/tmp/bus",
            "WAYLAND_DISPLAY": "wayland-0",
        }
        notification = Notification(
            "summary",
            actions=(NotificationAction("accept", "Accept"),),
        )
        backend = NotifySendBackend(
            executable="/usr/bin/notify-send",
            environ=env,
            max_action_helpers=0,
        )
        with mock.patch("mingtu_ai.notifications.service.subprocess.Popen") as popen:
            receipt = backend.send(notification)
        self.assertFalse(receipt.delivered)
        popen.assert_not_called()

        with self.assertRaises(TypeError):
            NotifySendBackend(max_action_helpers=True)
        with self.assertRaises(TypeError):
            NotifySendBackend(max_action_helpers=1.5)
        with self.assertRaises(ValueError):
            NotifySendBackend(max_action_helpers=-1)
        with self.assertRaises(ValueError):
            NotifySendBackend(close_timeout_seconds=0)

    def test_running_callback_holds_thread_capacity_after_child_exits(self) -> None:
        env = {
            "PATH": "/usr/bin",
            "DBUS_SESSION_BUS_ADDRESS": "unix:path=/tmp/bus",
            "WAYLAND_DISPLAY": "wayland-0",
        }
        callback_started = threading.Event()
        release_callback = threading.Event()
        first_process = BlockingProcess()
        first_process.stdout_text = "accept\n"
        backend = NotifySendBackend(
            executable="/usr/bin/notify-send",
            environ=env,
            max_action_helpers=1,
            close_timeout_seconds=0.2,
        )
        notification = Notification(
            "summary",
            actions=(NotificationAction("accept", "Accept"),),
            expire_ms=0,
        )

        def block_callback(_selected: str) -> None:
            callback_started.set()
            release_callback.wait()

        with mock.patch(
            "mingtu_ai.notifications.service.subprocess.Popen",
            return_value=first_process,
        ) as popen:
            self.assertTrue(backend.send(notification, block_callback).delivered)
            first_process.release.set()
            self.assertTrue(callback_started.wait(1))
            self.assertTrue(
                wait_until(lambda: backend.active_action_process_count == 0)
            )
            self.assertEqual(backend.active_action_thread_count, 1)

            rejected = backend.send(notification)
            self.assertFalse(rejected.delivered)
            self.assertIn("capacity 1", rejected.error or "")
            self.assertEqual(popen.call_count, 1)

            release_callback.set()
            self.assertTrue(
                wait_until(lambda: backend.active_action_thread_count == 0)
            )
        backend.close()

    def test_close_is_bounded_and_reaps_owned_action_children(self) -> None:
        env = {
            "PATH": "/usr/bin",
            "DBUS_SESSION_BUS_ADDRESS": "unix:path=/tmp/bus",
            "WAYLAND_DISPLAY": "wayland-0",
        }
        process = BlockingProcess(ignore_terminate=True)
        backend = NotifySendBackend(
            executable="/usr/bin/notify-send",
            environ=env,
            max_action_helpers=1,
            close_timeout_seconds=0.1,
        )
        notification = Notification(
            "summary",
            actions=(NotificationAction("accept", "Accept"),),
            expire_ms=0,
        )

        with mock.patch(
            "mingtu_ai.notifications.service.subprocess.Popen",
            return_value=process,
        ):
            self.assertTrue(backend.send(notification).delivered)
            self.assertTrue(process.started.wait(1))
            started = time.monotonic()
            backend.close()
            elapsed = time.monotonic() - started

        self.assertLess(elapsed, 0.5)
        self.assertGreaterEqual(process.terminate_calls, 1)
        self.assertGreaterEqual(process.kill_calls, 1)
        self.assertGreaterEqual(process.wait_calls, 1)
        self.assertEqual(backend.active_action_process_count, 0)
        self.assertEqual(backend.active_action_thread_count, 0)
        self.assertFalse(backend.send(notification).delivered)


if __name__ == "__main__":
    unittest.main()
