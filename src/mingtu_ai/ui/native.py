"""Python-side lifecycle controller for the optional Qt extension.

Qt owns the main thread while the native event loop is running. The filesystem
monitor and model queue remain ordinary Python threads in the same process.
"""

from __future__ import annotations

from datetime import datetime
import importlib
import json
import queue
import os
import threading
import time
from types import ModuleType
from typing import Protocol

from mingtu_ai.app import ApplicationCoordinator
from mingtu_ai.notifications import has_desktop_session
from mingtu_ai.history import EventType


class NativeUiUnavailable(RuntimeError):
    """The optional compiled Qt extension cannot be loaded safely."""


class NativeBridge(Protocol):
    def set_pending_count(self, count: int) -> None: ...

    def set_model_status(self, status: str) -> None: ...

    def set_paused(self, paused: bool) -> None: ...

    def set_proposals_json(self, text: str) -> None: ...

    def set_history_json(self, text: str) -> None: ...

    def set_watch_directories_json(self, text: str) -> None: ...

    def set_action_busy(self, busy: bool) -> None: ...

    def set_action_message(self, message: str) -> None: ...

    def set_settings_text(self, text: str) -> None: ...

    def set_error(self, message: str) -> None: ...


class NativeUiController:
    """Coordinate one native UI with one owned ApplicationCoordinator."""

    def __init__(self, coordinator: ApplicationCoordinator) -> None:
        self.coordinator = coordinator
        self._bridge: NativeBridge | None = None
        self._stop_event = threading.Event()
        self._monitor_thread: threading.Thread | None = None
        self._status_thread: threading.Thread | None = None
        self._stop_lock = threading.Lock()
        self._stopped = False
        self._action_thread: threading.Thread | None = None
        self._actions: queue.Queue[tuple | None] = queue.Queue(maxsize=1)
        self._action_lock = threading.Lock()
        self._action_busy = False
        self._history_cache_key: tuple[object, ...] | None = None
        self._history_cache_json = "[]"
        self._history_cache_time = 0.0
        self._refresh_event = threading.Event()

    def start(self, bridge: NativeBridge) -> None:
        """Called once by Qt before entering its main-thread event loop."""

        if self._monitor_thread is not None:
            raise RuntimeError("native UI controller was already started")
        self._bridge = bridge
        bridge.set_settings_text(self._settings_text())
        bridge.set_watch_directories_json(self._watch_directories_json())
        bridge.set_paused(self.coordinator.is_paused)
        bridge.set_model_status("待命（有工作時按需載入）")
        bridge.set_pending_count(self.coordinator.pending_count)
        self._monitor_thread = threading.Thread(
            target=self._run_monitor,
            name="mingtu-ai-native-monitor",
            daemon=False,
        )
        self._status_thread = threading.Thread(
            target=self._publish_status,
            name="mingtu-ai-native-status",
            daemon=False,
        )
        self._action_thread = threading.Thread(
            target=self._run_actions, name="mingtu-ai-native-actions", daemon=False,
        )
        self._action_thread.start()
        self._monitor_thread.start()
        self._status_thread.start()

    def add_watch_directory(self, path: str) -> list[str]:
        try:
            dirs = self.coordinator.add_watch_directory(path)
            if self._bridge is not None:
                try:
                    self._bridge.set_watch_directories_json(self._watch_directories_json())
                    self._bridge.set_settings_text(self._settings_text())
                    self._bridge.set_action_message(f"已新增監控資料夾：{path}")
                except RuntimeError:
                    pass
            return [str(p) for p in dirs]
        except Exception as exc:
            if self._bridge is not None:
                try:
                    self._bridge.set_action_message(f"無法新增監控資料夾：{exc}")
                except RuntimeError:
                    pass
            raise

    def remove_watch_directory(self, path: str) -> list[str]:
        try:
            dirs = self.coordinator.remove_watch_directory(path)
            if self._bridge is not None:
                try:
                    self._bridge.set_watch_directories_json(self._watch_directories_json())
                    self._bridge.set_settings_text(self._settings_text())
                    self._bridge.set_action_message(f"已移除監控資料夾：{path}")
                except RuntimeError:
                    pass
            return [str(p) for p in dirs]
        except Exception as exc:
            if self._bridge is not None:
                try:
                    self._bridge.set_action_message(f"無法移除監控資料夾：{exc}")
                except RuntimeError:
                    pass
            raise

    def toggle_pause(self) -> bool:
        paused = self.coordinator.toggle_paused()
        if self._bridge is not None:
            self._bridge.set_paused(paused)
        return paused

    def stop(self) -> None:
        """Stop only monitor, queue/runtime, and helpers owned by this app."""

        with self._stop_lock:
            if self._stopped:
                return
            self._stopped = True
            self._signal_stop()
            monitor = self._monitor_thread
            status = self._status_thread
            action = self._action_thread

        current = threading.current_thread()
        if monitor is not None and monitor is not current and monitor.ident is not None:
            monitor.join()
        if status is not None and status is not current and status.ident is not None:
            status.join()
        if action is not None and action is not current and action.ident is not None:
            action.join()
        self.coordinator.close()

    def _signal_stop(self) -> None:
        """Serialize all stop signals with action admission."""
        with self._action_lock:
            self._stop_event.set()
            try:
                self._actions.put_nowait(None)
            except queue.Full:
                pass  # The admitted action will drain before the thread exits.

    def _run_monitor(self) -> None:
        try:
            self.coordinator.run_forever(self._stop_event)
        except Exception as exc:
            bridge = self._bridge
            try:
                if bridge is not None:
                    bridge.set_error(f"監控停止：{type(exc).__name__}: {exc}")
            except RuntimeError:
                pass  # The native event loop may already have invalidated it.
            finally:
                self._signal_stop()

    def request_action(self, action: str, event_id: int, edited_stem: str | None = None) -> bool:
        """Admit at most one user action; filesystem work never runs on Qt's thread."""

        if action not in {"accept", "edit", "ignore", "undo"}:
            raise ValueError("unknown native UI action")
        if type(event_id) is not int or event_id <= 0:
            raise ValueError("event ID must be a positive integer")
        if action == "edit" and (not isinstance(edited_stem, str) or not edited_stem.strip()):
            return False
        with self._action_lock:
            if self._action_thread is None or self._stop_event.is_set() or self._action_busy:
                return False
            self._action_busy = True
            self._actions.put_nowait((action, event_id, edited_stem, None))
            if self._bridge is not None:
                self._bridge.set_action_busy(True)
        return True

    def request_batch_analyze(
        self,
        directory: str,
        recursive: bool = False,
        preset: str = "default",
        hint: str | None = None,
        auto_accept: bool = False,
    ) -> bool:
        with self._action_lock:
            if self._action_thread is None or self._stop_event.is_set() or self._action_busy:
                return False
            self._action_busy = True
            payload = {
                "directory": directory,
                "recursive": recursive,
                "preset": preset,
                "hint": hint,
                "auto_accept": auto_accept,
            }
            self._actions.put_nowait(("batch_analyze", 0, None, payload))
            if self._bridge is not None:
                self._bridge.set_action_busy(True)
        return True

    def request_batch_accept(self, batch_id: str | None = None) -> bool:
        with self._action_lock:
            if self._action_thread is None or self._stop_event.is_set() or self._action_busy:
                return False
            self._action_busy = True
            payload = {"batch_id": batch_id}
            self._actions.put_nowait(("batch_accept", 0, None, payload))
            if self._bridge is not None:
                self._bridge.set_action_busy(True)
        return True

    def request_batch_undo(self, batch_id: str) -> bool:
        with self._action_lock:
            if self._action_thread is None or self._stop_event.is_set() or self._action_busy:
                return False
            self._action_busy = True
            payload = {"batch_id": batch_id}
            self._actions.put_nowait(("batch_undo", 0, None, payload))
            if self._bridge is not None:
                self._bridge.set_action_busy(True)
        return True

    def _run_actions(self) -> None:
        while not self._stop_event.is_set() or not self._actions.empty():
            item = self._actions.get()
            if item is None:
                self._actions.task_done()
                return
            action, event_id, edited_stem = item[0], item[1], item[2]
            extra = item[3] if len(item) > 3 else None
            try:
                if action == "batch_analyze" and extra:
                    batch_res = self.coordinator.analyze_batch(
                        extra["directory"],
                        recursive=extra.get("recursive", False),
                        preset=extra.get("preset", "default"),
                        hint=extra.get("hint"),
                    )
                    if extra.get("auto_accept", False) and batch_res.processed_files > 0:
                        renamed_events = self.coordinator.accept_batch(batch_res.batch_id)
                        message = (
                            f"一鍵資料夾批次改名完成 (Batch ID: {batch_res.batch_id})："
                            f"已成功重新命名 {len(renamed_events)} 個檔案。"
                        )
                    else:
                        message = (
                            f"資料夾批次分析完成 (Batch ID: {batch_res.batch_id})：共產生 "
                            f"{batch_res.processed_files}/{batch_res.total_files} 個改名建議。"
                        )
                elif action == "batch_accept" and extra:
                    events = self.coordinator.accept_batch(batch_id=extra.get("batch_id"))
                    message = f"一鍵批次改名完成：共成功改名 {len(events)} 個檔案。"
                elif action == "batch_undo" and extra:
                    events = self.coordinator.undo_batch(batch_id=extra["batch_id"])
                    message = f"一鍵批次復原完成：共恢復 {len(events)} 個檔案。"
                elif action in {"accept", "edit"}:
                    result = self.coordinator.accept_proposal(event_id, edited_stem=edited_stem)
                    message = (
                        "操作完成。" if result is not None else
                        "操作未完成或建議已處理；請查看歷史紀錄中的結果與錯誤。"
                    )
                elif action == "ignore":
                    result = self.coordinator.ignore_proposal(event_id)
                    message = (
                        "操作完成。" if result is not None else
                        "操作未完成或建議已處理；請查看歷史紀錄中的結果與錯誤。"
                    )
                else:
                    result = self.coordinator.undo(event_id)
                    message = (
                        "操作完成。" if result is not None else
                        "操作未完成或建議已處理；請查看歷史紀錄中的結果與錯誤。"
                    )
            except Exception as exc:
                message = f"操作失敗：{type(exc).__name__}: {exc}"
            finally:
                self._actions.task_done()
                self._refresh_event.set()
            bridge = self._bridge
            if bridge is not None:
                try:
                    bridge.set_action_message(message)
                except RuntimeError:
                    pass  # Qt has invalidated its bridge during shutdown.
            with self._action_lock:
                self._action_busy = False
                if bridge is not None:
                    try:
                        bridge.set_action_busy(False)
                    except RuntimeError:
                        pass

    def _publish_status(self) -> None:
        last_error: str | None = None
        published: dict[str, object] = {}
        history_due = 0.0
        while not self._stop_event.is_set():
            bridge = self._bridge
            active = self.coordinator.is_active
            if bridge is not None:
                try:
                    values = {
                        "pending_count": self.coordinator.pending_count,
                        "paused": self.coordinator.is_paused,
                        "model_status": "處理中" if active else "待命（有工作時按需載入）",
                        "proposals_json": self._proposals_json(),
                        "watch_directories_json": self._watch_directories_json(),
                    }
                    changed = any(published.get(key) != value for key, value in values.items())
                    refresh = self._refresh_event.is_set()
                    self._refresh_event.clear()
                    if changed or refresh or time.monotonic() >= history_due:
                        values["history_json"] = self._history_json()
                        history_due = time.monotonic() + 5.0
                    for key, value in values.items():
                        if published.get(key) != value:
                            getattr(bridge, f"set_{key}")(value)
                            published[key] = value
                    last_error = None
                except Exception as exc:
                    # A failed history query must remain visible, and must not
                    # permanently kill refresh. RuntimeError may also mean the
                    # native bridge was invalidated as the event loop exited.
                    message = f"狀態更新失敗：{type(exc).__name__}: {exc}"
                    if message != last_error:
                        try:
                            bridge.set_action_message(message)
                        except RuntimeError:
                            return
                    last_error = message
            self._stop_event.wait(0.25 if active else 1.0)

    def _proposals_json(self) -> str:
        return json.dumps([
            {
                "id": proposal.history_event_id,
                "source": proposal.source_path.name,
                "path": str(proposal.source_path),
                "suggested": proposal.suggested_filename,
                "stem": proposal.suggested_stem,
            }
            for proposal in self.coordinator.list_pending_proposals()
        ], ensure_ascii=False)

    def _watch_directories_json(self) -> str:
        return json.dumps([
            str(path) for path in self.coordinator.watch_directories
        ], ensure_ascii=False)

    def _history_json(self) -> str:
        events = self.coordinator.history.list_recent(limit=100)
        key = tuple((event.id, event.status, event.updated_at) for event in events)
        now = time.monotonic()
        if key == self._history_cache_key and now - self._history_cache_time < 5.0:
            return self._history_cache_json
        result = json.dumps([
            {
                "id": event.id,
                "time": datetime.fromisoformat(event.created_at).astimezone().strftime("%Y-%m-%d %H:%M:%S"),
                "state": {
                    "suggestion": "已產生建議", "rename": "改名", "undo": "復原",
                    "ignored": "已忽略", "failure": "處理失敗",
                }[event.event_type.value] + (
                    "" if event.event_type.value in {"suggestion", "ignored", "failure"}
                    else {"pending": "待確認", "success": "成功", "failed": "失敗"}[event.status.value]
                ),
                "type": event.event_type.value,
                "status": event.status.value,
                "source": event.source_path.name,
                "path": str(event.source_path),
                "target": event.target_path.name if event.target_path else event.suggested_name or "",
                "message": event.message or "",
                "undoable": (
                    event.event_type is EventType.RENAME
                    and self.coordinator.history.can_undo_rename(event.id)
                ),
            }
            for event in events
        ], ensure_ascii=False)
        self._history_cache_key = key
        self._history_cache_time = now
        self._history_cache_json = result
        return result

    def _settings_text(self) -> str:
        watches = "\n".join(f"• {path}" for path in self.coordinator.watch_directories)
        model = self.coordinator.config.llama.model_path
        model_text = str(model) if model is not None else "尚未設定"
        return (
            "監控目錄\n"
            f"{watches}\n\n"
            "本地模型\n"
            f"• {model_text}\n"
            "• 推論端點固定為 127.0.0.1:3001\n"
            "• 檔案內容預設不上傳"
        )


def _load_native_module() -> ModuleType:
    try:
        return importlib.import_module("mingtu_ai.ui._native_ui")
    except (ImportError, OSError) as exc:
        raise NativeUiUnavailable(
            "找不到可用的 Qt6 原生擴充模組；可先執行 "
            "`python scripts/build_native_ui.py`"
        ) from exc


def run_native_ui(
    coordinator: ApplicationCoordinator,
    *,
    native_module: ModuleType | None = None,
    quit_after_ms: int = 0,
    show_windows: bool = True,
) -> int:
    """Run QApplication on the Python main thread and return its exit code."""

    if threading.current_thread() is not threading.main_thread():
        raise NativeUiUnavailable("Qt 原生 UI 必須由 Python 主執行緒啟動")
    if (
        os.environ.get("QT_QPA_PLATFORM", "").casefold() != "offscreen"
        and not has_desktop_session()
    ):
        raise NativeUiUnavailable("目前沒有可用的 KDE/Wayland 桌面 session")
    module = native_module or _load_native_module()
    run = getattr(module, "run", None)
    if not callable(run):
        raise NativeUiUnavailable("Qt 原生擴充模組缺少 run() 介面")

    controller = NativeUiController(coordinator)
    try:
        return int(
            run(
                controller,
                app_name="命圖 AI",
                quit_after_ms=quit_after_ms,
                show_windows=show_windows,
            )
        )
    finally:
        controller.stop()
