#!/usr/bin/env python3
"""Measure burst settling and exercise the real Qt/inotify/local-model path.

Uses only generated inputs and temporary history. No downloads or user files.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
import traceback

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from mingtu_ai import AppConfig, LlamaServerConfig
from mingtu_ai.app import ApplicationCoordinator
from mingtu_ai.history import HistoryStore
from mingtu_ai.notifications import NotificationReceipt, NotificationService
from mingtu_ai.runtime import LlamaServerRuntime
from mingtu_ai.ui.native import NativeUiController, _load_native_module
from mingtu_ai.watcher import FileEventMonitor, InotifyWatcher, wait_until_stable


class QuietBackend:
    def send(self, notification, on_action=None):
        return NotificationReceipt(True, "resident-validation")

    def close(self):
        pass


def burst_benchmark(root):
    files = [root / f"capture-{i}.txt" for i in range(32)]
    with InotifyWatcher([root]) as watcher:
        for path in files:
            path.write_text("Synthetic completed file")
        started = time.monotonic()
        ready = set()
        monitor = FileEventMonitor(watcher)
        while len(ready) < len(files) and time.monotonic() - started < 10:
            ready.update(monitor.poll(0.5))
        together = time.monotonic() - started
        assert ready == set(files)
    started = time.monotonic()
    # Reference to the original serial stability loop, using identical limits.
    for path in files:
        assert wait_until_stable(path)
    serial = time.monotonic() - started
    return {"files": len(files), "serial_reference_seconds": serial, "optimized_seconds": together}


class ValidationController(NativeUiController):
    def __init__(self, coordinator, source, payload, runtimes):
        super().__init__(coordinator)
        self.source, self.payload, self.runtimes = source, payload, runtimes
        self.result, self.failures = {}, []
        self.driver = None
        self.history_reads = 0
        original = coordinator.history.list_recent

        def counted(*args, **kwargs):
            self.history_reads += 1
            return original(*args, **kwargs)

        coordinator.history.list_recent = counted

    def start(self, bridge):
        super().start(bridge)
        self.driver = threading.Thread(target=self.exercise, name="resident-validation")
        self.driver.start()

    def until(self, predicate, timeout=120):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return
            if self._stop_event.wait(0.05):
                raise AssertionError("monitor stopped unexpectedly")
        raise AssertionError("timed out waiting for resident path")

    def exercise(self):
        try:
            self.until(lambda: self.coordinator._active_monitor is not None, timeout=5)
            cpu = time.process_time()
            reads = self.history_reads
            started = time.monotonic()
            self._stop_event.wait(6)
            self.result.update(idle_seconds=time.monotonic() - started,
                               idle_cpu_seconds=time.process_time() - cpu,
                               idle_history_queries=self.history_reads - reads,
                               idle_model_instances=len(self.runtimes))
            assert not self.runtimes, "model was loaded during idle"
            self.source.write_bytes(self.payload)
            started = time.monotonic()
            def analyzed():
                if self.coordinator.list_pending_proposals():
                    return True
                failures = [event for event in self.coordinator.history.list_recent() if event.event_type.value == "failure"]
                if failures:
                    raise AssertionError(failures[-1].message)
                return False
            self.until(analyzed)
            self.until(lambda: not self.coordinator.is_active)
            proposal, = self.coordinator.list_pending_proposals()
            assert self.source.read_bytes() == self.payload
            assert all(not runtime.is_running for runtime in self.runtimes)
            self.result.update(suggestion_seconds=time.monotonic() - started,
                               suggested_filename=proposal.suggested_filename,
                               source_sha256=hashlib.sha256(self.payload).hexdigest(),
                               original_unchanged=True, model_released=True)
            assert self.request_action("ignore", proposal.history_event_id)
            self.until(lambda: not self._action_busy, timeout=5)
            assert not self.coordinator.list_pending_proposals()
        except BaseException:
            self.failures.append(traceback.format_exc())
        finally:
            self._bridge._finish_smoke()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", type=Path, required=True)
    parser.add_argument("--mmproj", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="mingtu-resident-") as temporary:
        root = Path(temporary)
        burst = root / "burst"
        burst.mkdir()
        result = {"burst": burst_benchmark(burst)}
        watched = root / "watched"
        watched.mkdir()
        config = AppConfig(llama=LlamaServerConfig(model_path=args.model, mmproj_path=args.mmproj))
        runtimes = []

        @contextmanager
        def runtime_factory():
            runtime = LlamaServerRuntime(config.llama)
            runtimes.append(runtime)
            try:
                yield runtime
            finally:
                runtime.shutdown()

        payload = (ROOT / "benchmark/dataset/generated/gemma4-smoke-shapes.png").read_bytes()
        with ApplicationCoordinator(config=config, watch_directories=[watched],
                                    history=HistoryStore(root / "state/history.sqlite3"),
                                    notifications=NotificationService(QuietBackend()),
                                    runtime_factory=runtime_factory) as coordinator:
            controller = ValidationController(coordinator, watched / "synthetic.png", payload, runtimes)
            try:
                exit_code = _load_native_module().run(controller, show_windows=False, quit_after_ms=150_000)
            finally:
                controller.stop()
                if controller.driver:
                    controller.driver.join(5)
            if controller.failures:
                raise AssertionError("\n".join(controller.failures))
            assert exit_code == 0
            assert all(not thread.is_alive() for thread in (controller._action_thread, controller._status_thread, controller._monitor_thread))
            result.update(status="pass", real_model=True, native_ui=True, monitor="inotify", **controller.result)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
