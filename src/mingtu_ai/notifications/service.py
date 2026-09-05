"""KDE/Freedesktop notifications with an optional action callback bridge."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
import os
import shutil
import subprocess
import sys
import threading
import time
from typing import Protocol, TextIO


ActionCallback = Callable[[], None]
DEFAULT_MAX_ACTION_HELPERS = 32
DEFAULT_CLOSE_TIMEOUT_SECONDS = 5.0


@dataclass(frozen=True, slots=True)
class NotificationAction:
    """Describe one optional desktop-notification action."""

    key: str
    label: str
    callback: ActionCallback | None = field(default=None, compare=False, repr=False)

    def __post_init__(self) -> None:
        if not self.key or "=" in self.key or "\x00" in self.key:
            raise ValueError("notification action key must be non-empty and contain no '='")
        if not self.label or "\x00" in self.label:
            raise ValueError("notification action label must be non-empty")


@dataclass(frozen=True, slots=True)
class Notification:
    """Describe a desktop notification independently of its backend."""

    summary: str
    body: str = ""
    actions: tuple[NotificationAction, ...] = ()
    urgency: str = "normal"
    expire_ms: int = 15_000
    icon: str = "folder"


@dataclass(frozen=True, slots=True)
class NotificationReceipt:
    """Report whether a backend accepted a notification for delivery."""

    delivered: bool
    backend: str
    notification_id: int | None = None
    error: str | None = None


class NotificationBackend(Protocol):
    """Protocol implemented by desktop notification backends."""

    def send(
        self,
        notification: Notification,
        on_action: Callable[[str], None] | None = None,
    ) -> NotificationReceipt: ...

    def close(self) -> None: ...


def has_desktop_session(environ: Mapping[str, str] | None = None) -> bool:
    """Return whether the environment identifies a graphical D-Bus session."""

    env = os.environ if environ is None else environ
    return bool(env.get("DBUS_SESSION_BUS_ADDRESS")) and bool(
        env.get("WAYLAND_DISPLAY") or env.get("DISPLAY") or env.get("KDE_FULL_SESSION")
    )


class NotifySendBackend:
    """Use libnotify's ``notify-send`` frontend without an extra Python package.

    Action notifications are collected in a background thread.  Only child
    processes created by this backend are ever terminated by ``close``.
    """

    name = "notify-send"

    def __init__(
        self,
        *,
        app_name: str = "命圖 AI",
        executable: str | None = None,
        environ: Mapping[str, str] | None = None,
        max_action_helpers: int = DEFAULT_MAX_ACTION_HELPERS,
        close_timeout_seconds: float = DEFAULT_CLOSE_TIMEOUT_SECONDS,
    ) -> None:
        if isinstance(max_action_helpers, bool) or not isinstance(max_action_helpers, int):
            raise TypeError("max_action_helpers must be an integer")
        if max_action_helpers < 0:
            raise ValueError("max_action_helpers must not be negative")
        if isinstance(close_timeout_seconds, bool) or not isinstance(
            close_timeout_seconds, (int, float)
        ):
            raise TypeError("close_timeout_seconds must be a number")
        if close_timeout_seconds <= 0:
            raise ValueError("close_timeout_seconds must be positive")
        self.app_name = app_name
        self.environ = dict(os.environ if environ is None else environ)
        self.executable = executable or shutil.which("notify-send", path=self.environ.get("PATH"))
        self._max_action_helpers = max_action_helpers
        self._close_timeout_seconds = float(close_timeout_seconds)
        self._children: set[subprocess.Popen[str]] = set()
        self._threads: set[threading.Thread] = set()
        self._lock = threading.Lock()
        self._closed = False

    @property
    def available(self) -> bool:
        """Return whether ``notify-send`` and a desktop session are available."""

        return bool(self.executable) and has_desktop_session(self.environ)

    @property
    def active_action_process_count(self) -> int:
        """Return the number of owned action processes awaiting cleanup."""

        with self._lock:
            return len(self._children)

    @property
    def active_action_thread_count(self) -> int:
        """Return the number of owned action collector threads still running."""

        with self._lock:
            return len(self._threads)

    def build_command(self, notification: Notification) -> list[str]:
        """Build an argument vector for one ``notify-send`` invocation."""

        if not self.executable:
            raise RuntimeError("notify-send is not installed")
        command = [
            self.executable,
            f"--app-name={self.app_name}",
            f"--urgency={notification.urgency}",
            f"--expire-time={max(0, notification.expire_ms)}",
            f"--icon={notification.icon}",
        ]
        for action in notification.actions:
            command.append(f"--action={action.key}={action.label}")
        command.append(notification.summary)
        if notification.body:
            command.append(notification.body)
        return command

    def send(
        self,
        notification: Notification,
        on_action: Callable[[str], None] | None = None,
    ) -> NotificationReceipt:
        """Deliver a notification or return a safe degradation receipt.

        Notifications with actions need one owned child and one collector
        thread.  Admission is atomic with respect to ``close`` and is rejected
        before spawning when either helper reaches the configured limit.
        """

        if not self.executable:
            return NotificationReceipt(False, self.name, error="notify-send not found")
        if not has_desktop_session(self.environ):
            return NotificationReceipt(False, self.name, error="desktop session bus unavailable")

        command = self.build_command(notification)
        if notification.actions:
            process: subprocess.Popen[str] | None = None
            try:
                with self._lock:
                    if self._closed:
                        return NotificationReceipt(
                            False,
                            self.name,
                            error="notification backend is closed",
                        )
                    if (
                        len(self._children) >= self._max_action_helpers
                        or len(self._threads) >= self._max_action_helpers
                    ):
                        return NotificationReceipt(
                            False,
                            self.name,
                            error=(
                                "action helper capacity "
                                f"{self._max_action_helpers} reached"
                            ),
                        )
                    process = subprocess.Popen(
                        command,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                        text=True,
                        env=self.environ,
                    )
                    thread = threading.Thread(
                        target=self._collect_action,
                        args=(process, notification.expire_ms, on_action),
                        name="mingtu-ai-notification-action",
                        daemon=True,
                    )
                    self._children.add(process)
                    self._threads.add(thread)
                    try:
                        thread.start()
                    except Exception:
                        self._children.discard(process)
                        self._threads.discard(thread)
                        raise
            except (OSError, RuntimeError) as exc:
                if process is not None:
                    self._stop_process(process, timeout=0.5)
                    self._close_process_pipes(process)
                return NotificationReceipt(False, self.name, error=str(exc))
            return NotificationReceipt(True, self.name)

        with self._lock:
            if self._closed:
                return NotificationReceipt(
                    False,
                    self.name,
                    error="notification backend is closed",
                )

        try:
            completed = subprocess.run(
                command,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                env=self.environ,
                timeout=5,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            return NotificationReceipt(False, self.name, error=str(exc))
        if completed.returncode != 0:
            error = completed.stderr.strip() or f"notify-send exited {completed.returncode}"
            return NotificationReceipt(False, self.name, error=error)
        return NotificationReceipt(True, self.name)

    def _collect_action(
        self,
        process: subprocess.Popen[str],
        expire_ms: int,
        on_action: Callable[[str], None] | None,
    ) -> None:
        # Plasma may keep a notification longer than the requested expiry.  Keep
        # a bounded helper lifetime and terminate only this owned notify-send.
        timeout = max(15.0, expire_ms / 1000 + 10.0) if expire_ms else 3600.0
        stdout = ""
        current_thread = threading.current_thread()
        try:
            try:
                stdout, _stderr = process.communicate(timeout=timeout)
            except subprocess.TimeoutExpired:
                self._stop_process(process, timeout=2.0)
            except (OSError, ValueError):
                # ``close`` may have already terminated and reaped this child.
                stdout = ""
            finally:
                with self._lock:
                    self._children.discard(process)

            selected = stdout.strip().splitlines()[0] if stdout.strip() else ""
            if selected and on_action is not None:
                try:
                    on_action(selected)
                except Exception:
                    # A UI callback must not take down the coordinator or its
                    # notification collector thread.
                    return
        finally:
            self._close_process_pipes(process)
            with self._lock:
                self._children.discard(process)
                self._threads.discard(current_thread)

    @staticmethod
    def _stop_process(process: subprocess.Popen[str], *, timeout: float) -> None:
        """Terminate, then kill if needed, and reap one owned child promptly."""

        if process.poll() is not None:
            return
        try:
            process.terminate()
        except OSError:
            pass
        try:
            process.wait(timeout=max(0.0, timeout / 2))
            return
        except (OSError, subprocess.TimeoutExpired):
            pass
        try:
            process.kill()
        except OSError:
            pass
        try:
            process.wait(timeout=max(0.0, timeout / 2))
        except (OSError, subprocess.TimeoutExpired):
            return

    @staticmethod
    def _close_process_pipes(process: subprocess.Popen[str]) -> None:
        """Close captured pipes after a child has exited or been reaped."""

        for stream in (getattr(process, "stdout", None), getattr(process, "stderr", None)):
            if stream is None:
                continue
            try:
                stream.close()
            except (OSError, ValueError):
                continue

    def close(self) -> None:
        """Close admission and boundedly terminate/reap all owned helpers."""

        deadline = time.monotonic() + self._close_timeout_seconds
        with self._lock:
            self._closed = True
            children = tuple(self._children)
            threads = tuple(self._threads)
        for process in children:
            if process.poll() is None:
                try:
                    process.terminate()
                except OSError:
                    pass

        # Give collectors a short opportunity to drain their pipes after the
        # terminate signal, while retaining time to kill and reap stragglers.
        grace_deadline = min(deadline, time.monotonic() + self._close_timeout_seconds / 2)
        while time.monotonic() < grace_deadline:
            if all(process.poll() is not None for process in children):
                break
            time.sleep(min(0.01, max(0.0, grace_deadline - time.monotonic())))

        for process in children:
            if process.poll() is None:
                try:
                    process.kill()
                except OSError:
                    pass
        for process in children:
            remaining = max(0.0, deadline - time.monotonic())
            try:
                process.wait(timeout=remaining)
            except (OSError, subprocess.TimeoutExpired):
                continue
        for thread in threads:
            if thread is threading.current_thread():
                continue
            remaining = max(0.0, deadline - time.monotonic())
            thread.join(remaining)

        with self._lock:
            reaped_children = tuple(
                process for process in self._children if process.poll() is not None
            )
            finished_threads = tuple(
                thread for thread in self._threads if not thread.is_alive()
            )
            self._children.difference_update(reaped_children)
            self._threads.difference_update(finished_threads)


class NotificationService:
    """Present notifications and provide a stderr fallback on degradation."""

    def __init__(
        self,
        backend: NotificationBackend | None = None,
        *,
        fallback_stream: TextIO | None = None,
    ) -> None:
        self.backend = backend or NotifySendBackend()
        self.fallback_stream = fallback_stream or sys.stderr

    def show(self, notification: Notification) -> NotificationReceipt:
        """Show one notification and print a fallback when delivery fails."""

        callbacks = {
            action.key: action.callback
            for action in notification.actions
            if action.callback is not None
        }

        def dispatch(action_key: str) -> None:
            callback = callbacks.get(action_key)
            if callback is not None:
                callback()

        receipt = self.backend.send(notification, dispatch)
        if not receipt.delivered:
            message = f"[命圖 AI] {notification.summary}"
            if notification.body:
                message += f": {notification.body}"
            if receipt.error:
                message += f" (通知降級: {receipt.error})"
            print(message, file=self.fallback_stream)
        return receipt

    def notify_suggestion(
        self,
        suggested_filename: str,
        *,
        on_accept: ActionCallback | None = None,
        on_edit: ActionCallback | None = None,
        on_ignore: ActionCallback | None = None,
    ) -> NotificationReceipt:
        """Show a filename suggestion with accept, edit, and ignore actions."""

        return self.show(
            Notification(
                summary="命圖 AI 檔名建議",
                body=f"建議檔名：{suggested_filename}",
                actions=(
                    NotificationAction("accept", "接受", on_accept),
                    NotificationAction("edit", "修改", on_edit),
                    NotificationAction("ignore", "忽略", on_ignore),
                ),
                expire_ms=0,
            )
        )

    def notify_renamed(
        self,
        filename: str,
        *,
        on_undo: ActionCallback | None = None,
        on_open_folder: ActionCallback | None = None,
    ) -> NotificationReceipt:
        """Show a successful rename with undo and open-folder actions."""

        return self.show(
            Notification(
                summary="命名完成",
                body=filename,
                actions=(
                    NotificationAction("undo", "復原", on_undo),
                    NotificationAction("open-folder", "開啟資料夾", on_open_folder),
                ),
                expire_ms=0,
            )
        )

    def notify_error(self, message: str) -> NotificationReceipt:
        """Show a critical processing error notification."""

        return self.show(
            Notification(
                summary="命圖 AI 處理失敗",
                body=message,
                urgency="critical",
                expire_ms=0,
            )
        )

    def notify_restored(self, filename: str) -> NotificationReceipt:
        """Show confirmation that an earlier rename was restored."""

        return self.show(
            Notification(
                summary="復原完成",
                body=filename,
            )
        )

    def close(self) -> None:
        """Close the configured backend and its owned helper resources."""

        self.backend.close()
