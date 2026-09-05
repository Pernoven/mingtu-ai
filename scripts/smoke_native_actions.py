#!/usr/bin/env python3
"""Exercise real Qt buttons on generated files, without downloads or private input.

Default: synthetic provider, temporary SQLite/files, offscreen-compatible Qt.
Optional --model / --mmproj: analyze one public generated PNG using the existing
local model before exercising the same native accept / undo controls.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
import traceback

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, os.fspath(ROOT / "src"))

from mingtu_ai import AppConfig, NamingSuggestion
from mingtu_ai.app import ApplicationCoordinator
from mingtu_ai.history import EventStatus, EventType, HistoryStore
from mingtu_ai.notifications import NotificationReceipt, NotificationService
from mingtu_ai.providers import LocalGemmaProvider
from mingtu_ai.ui.native import NativeUiController, _load_native_module


class QuietBackend:
    """Record notification delivery locally; this smoke tests Qt buttons."""
    def __init__(self):
        self.closed = False

    def send(self, notification, on_action=None):
        return NotificationReceipt(True, "synthetic-smoke")

    def close(self):
        self.closed = True


class StaticProvider:
    def analyze(self, request):
        return NamingSuggestion(
            suggested_stem=f"建議_{request.source_path.stem}",
            detected_language="zh-Hant", category="synthetic", keywords=["CUDA"], confidence=0.9,
        )


class QuietMonitor:
    def poll(self, timeout=0.5):
        time.sleep(0.02)
        return []


@contextmanager
def quiet_monitor(_directories):
    yield QuietMonitor()


@contextmanager
def fake_runtime():
    yield object()


class SmokeController(NativeUiController):
    """Drive only this smoke's IDs through real C++ selection and click slots."""
    def __init__(self, coordinator, proposals, originals, screenshot):
        super().__init__(coordinator)
        self.proposals = proposals
        self.originals = originals
        self.screenshot = screenshot
        self.history_screenshot = screenshot.with_stem(screenshot.stem + "-history")
        self.driver = None
        self.failures = []
        self.checks = []
        self.requests = []

    def start(self, bridge):
        super().start(bridge)
        self.driver = threading.Thread(target=self._exercise, name="mingtu-native-smoke-driver", daemon=False)
        self.driver.start()

    def request_action(self, action, event_id, edited_stem=None):
        accepted = super().request_action(action, event_id, edited_stem)
        self.requests.append((action, event_id, edited_stem, accepted))
        return accepted

    def wait_for(self, predicate, label, timeout=8):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return
            if self._stop_event.is_set():
                raise AssertionError(f"UI stopped before {label}")
            time.sleep(0.02)
        raise AssertionError(f"timed out waiting for {label}; callbacks={self.requests!r}")

    def refresh(self):
        self._bridge.set_proposals_json(self._proposals_json())
        self._bridge.set_history_json(self._history_json())

    def click(self, action, event_id, stem=""):
        self.refresh()
        before = len(self.requests)
        self._bridge._test_action(action, event_id, stem)
        self.wait_for(lambda: len(self.requests) > before, f"{action} native callback")
        self.wait_for(lambda: not self._action_busy, f"{action} worker completion")
        assert self.requests[-1][3], f"{action} was not admitted"

    def rename_for(self, proposal):
        matches = [event for event in self.coordinator.history.list_recent() if event.event_type is EventType.RENAME and event.parent_event_id == proposal.history_event_id and event.status is EventStatus.SUCCESS]
        assert len(matches) == 1, f"expected one successful rename: {matches}"
        return matches[0]

    def _exercise(self):
        try:
            self.refresh()
            self._bridge._save_screenshot(os.fspath(self.screenshot))
            self.wait_for(self.screenshot.is_file, "app screenshot")
            first, edited, ignored, cancelled = self.proposals
            # ACCEPT: real C++ button -> Python action worker -> SQLite + file.
            self.click("accept", first.history_event_id)
            rename = self.rename_for(first)
            assert not first.source_path.exists()
            assert rename.target_path.read_bytes() == self.originals[first.source_path]
            self.checks.append("native accept preserves file bytes")
            self.click("undo", rename.id)
            assert first.source_path.read_bytes() == self.originals[first.source_path]
            assert not rename.target_path.exists()
            self.checks.append("native undo restores original name and bytes")
            # EDIT: QInputDialog input -> same real Edit button callback.
            self.click("edit", edited.history_event_id, "CUDA_修改後的檔名")
            edit_event = self.rename_for(edited)
            assert edit_event.target_path.name == "CUDA_修改後的檔名.txt"
            assert edit_event.target_path.read_bytes() == self.originals[edited.source_path]
            self.checks.append("native edit dialog preserves extension and bytes")
            # UI eligibility is advisory. Force a name conflict immediately
            # after an undoable snapshot to verify the click-time safety guard.
            self.refresh()
            edited.source_path.write_bytes(b"synthetic conflict occupant")
            self.click("undo", edit_event.id)
            assert edited.source_path.read_bytes() == b"synthetic conflict occupant"
            assert edit_event.target_path.read_bytes() == self.originals[edited.source_path]
            assert any(event.event_type is EventType.FAILURE for event in self.coordinator.history.list_recent())
            self.checks.append("native undo conflict preserves both files and records failure")
            edited.source_path.unlink()  # Only the exact synthetic occupant above.
            self._history_cache_key = None
            self.click("undo", edit_event.id)
            assert edited.source_path.read_bytes() == self.originals[edited.source_path]
            self.checks.append("native undo succeeds after synthetic conflict is removed")
            self.click("ignore", ignored.history_event_id)
            assert ignored.source_path.read_bytes() == self.originals[ignored.source_path]
            assert all(item.history_event_id != ignored.history_event_id for item in self.coordinator.list_pending_proposals())
            self.checks.append("native ignore leaves source unchanged")
            self.refresh()
            before = len(self.requests)
            self._bridge._test_action("cancel_edit", cancelled.history_event_id, "不應使用")
            time.sleep(0.4)
            assert len(self.requests) == before, "cancelled dialog submitted an action"
            assert cancelled.source_path.read_bytes() == self.originals[cancelled.source_path]
            assert any(item.history_event_id == cancelled.history_event_id for item in self.coordinator.list_pending_proposals())
            self.checks.append("native edit cancellation keeps proposal and source")
            self.refresh()
            self._bridge._save_history_screenshot(os.fspath(self.history_screenshot))
            self.wait_for(self.history_screenshot.is_file, "app history screenshot")
        except BaseException:
            self.failures.append(traceback.format_exc())
        finally:
            try:
                self._bridge._finish_smoke()
            except RuntimeError:
                # The watchdog may already have completed normal native quit.
                # Preserve the failure above without a second thread exception.
                if not self._stop_event.is_set():
                    self.failures.append(traceback.format_exc())


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--screenshot", type=Path, default=Path("/tmp/mingtu-native-actions.png"))
    parser.add_argument("--model", type=Path)
    parser.add_argument("--mmproj", type=Path)
    args = parser.parse_args()
    if args.model is not None and args.mmproj is None:
        parser.error("--model requires --mmproj for this synthetic image smoke")
    if args.mmproj is not None and args.model is None:
        parser.error("--mmproj requires --model")
    screenshot = args.screenshot.expanduser().absolute()
    if screenshot.exists():
        parser.error(f"refusing to overwrite screenshot: {screenshot}")
    history_screenshot = screenshot.with_stem(screenshot.stem + "-history")
    if history_screenshot.exists():
        parser.error(f"refusing to overwrite screenshot: {history_screenshot}")
    screenshot.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="mingtu-native-actions-") as directory:
        root = Path(directory)
        config = AppConfig()
        if args.model:
            config.llama.model_path = args.model.expanduser().absolute()
            config.llama.mmproj_path = args.mmproj.expanduser().absolute()
        backend = QuietBackend()
        coordinator = ApplicationCoordinator(
            config=config, watch_directories=[root], history=HistoryStore(root / "state/history.sqlite3"),
            notifications=NotificationService(backend), monitor_factory=quiet_monitor,
            runtime_factory=None if args.model else fake_runtime,
            provider_factory=lambda runtime: LocalGemmaProvider(runtime, config.provider) if args.model else StaticProvider(),
        )
        # A provider switch allows exactly one real local-image inference. The
        # remaining text cases deliberately exercise deterministic UI behavior.
        originals = {}
        proposals = []
        try:
            for index, name in enumerate(("原始截圖", "編輯測試", "忽略測試", "取消測試")):
                if index == 0 and args.model:
                    path = root / "original-synthetic-image.png"
                    path.write_bytes((ROOT / "benchmark/dataset/generated/gemma4-smoke-shapes.png").read_bytes())
                else:
                    path = root / f"{name}.txt"
                    path.write_text(f"Synthetic UI fixture {index}: CUDA English 中文\n", encoding="utf-8")
                originals[path] = path.read_bytes()
                proposals.append(coordinator.analyze_once(path))
                coordinator._provider_factory = lambda _runtime: StaticProvider()
            controller = SmokeController(coordinator, proposals, originals, screenshot)
            native = _load_native_module()
            exit_code = native.run(controller, app_name="命圖 AI｜合成按鈕驗證", quit_after_ms=40_000, show_windows=True)
            controller.stop()
            controller.driver.join(timeout=2)
            assert not controller.driver.is_alive(), "smoke driver did not stop"
            assert not any(thread.is_alive() for thread in (controller._action_thread, controller._monitor_thread, controller._status_thread)), "owned UI worker leaked"
            assert backend.closed, "notification backend did not close"
            if controller.failures:
                raise AssertionError("\n".join(controller.failures))
            assert exit_code == 0
            controller.checks.append("normal native quit joins all owned UI workers")
            print(json.dumps({"status": "pass", "real_model": bool(args.model), "checks": controller.checks, "native_callbacks": len(controller.requests), "screenshot": os.fspath(screenshot), "history_screenshot": os.fspath(history_screenshot)}, ensure_ascii=False, indent=2))
        finally:
            coordinator.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
