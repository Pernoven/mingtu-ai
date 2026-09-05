from __future__ import annotations

import errno
import io
import os
import subprocess
import tempfile
from pathlib import Path
import unittest
from unittest.mock import patch

from mingtu_ai.cli import main
from mingtu_ai.diagnostics import (
    CheckResult,
    CheckStatus,
    check_mmproj,
    check_model,
    check_port,
    run_doctor,
)


class FakeSystem:
    def __init__(self, outputs=None, paths=None) -> None:
        self.outputs = outputs or {}
        self.paths = paths or {}
        self.commands: list[tuple[str, ...]] = []

    def which(self, name: str) -> str | None:
        return self.paths.get(name)

    def run(self, command, timeout):
        self.commands.append(tuple(command))
        key = Path(command[0]).name
        return self.outputs.get(key, subprocess.CompletedProcess(command, 0, "ok\n", ""))


class DiagnosticsTests(unittest.TestCase):
    def test_complete_healthy_report(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            model = root / "gemma.gguf"
            model.write_bytes(b"GGUF")
            mmproj = root / "mmproj.gguf"
            mmproj.write_bytes(b"GGUF")
            system = FakeSystem(
                paths={
                    "nvidia-smi": "/usr/bin/nvidia-smi",
                    "llama-server": "/usr/bin/llama-server",
                    "notify-send": "/usr/bin/notify-send",
                    "qdbus6": "/usr/bin/qdbus6",
                    "ffmpeg": "/usr/bin/ffmpeg",
                },
                outputs={
                    "nvidia-smi": subprocess.CompletedProcess([], 0, "NVIDIA RTX, 999, 8192\n", ""),
                    "llama-server": subprocess.CompletedProcess([], 0, "version 1\n", ""),
                    "qdbus6": subprocess.CompletedProcess([], 0, "actions\nbody\n", ""),
                    "ffmpeg": subprocess.CompletedProcess([], 0, "ffmpeg version 7\n", ""),
                },
            )

            report = run_doctor(
                model_path=model,
                mmproj_path=mmproj,
                watch_directories=[root],
                runner=system.run,
                which=system.which,
                environ={
                    "DBUS_SESSION_BUS_ADDRESS": "unix:path=/tmp/bus",
                    "WAYLAND_DISPLAY": "wayland-0",
                },
                port_probe=lambda _host, _port: errno.ECONNREFUSED,
            )

            self.assertTrue(report.healthy)
            self.assertEqual(report.exit_code, 0)
            self.assertTrue(all(check.status is CheckStatus.PASS for check in report.checks))
            self.assertIn("多模態 projector", {check.name for check in report.checks})
            self.assertFalse(any(root.glob(".mingtu-ai-doctor-*")))

    def test_port_occupied_is_failure_and_never_runs_kill_command(self) -> None:
        calls: list[tuple[str, int]] = []

        result = check_port(
            host="127.0.0.1",
            port=3001,
            probe=lambda host, port: calls.append((host, port)) or 0,
        )

        self.assertEqual(result.status, CheckStatus.FAIL)
        self.assertEqual(calls, [("127.0.0.1", 3001)])
        self.assertIn("不會終止", result.summary)

    def test_port_permission_denied_is_warn_not_false_free(self) -> None:
        result = check_port(
            host="127.0.0.1",
            port=3001,
            probe=lambda _host, _port: errno.EPERM,
        )
        self.assertEqual(result.status, CheckStatus.WARN)
        self.assertIn("不誤判", result.summary)

    def test_dbus_access_denied_is_inconclusive_warning(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            model = root / "model.gguf"
            model.touch()
            system = FakeSystem(
                paths={
                    "nvidia-smi": "/usr/bin/nvidia-smi",
                    "llama-server": "/usr/bin/llama-server",
                    "notify-send": "/usr/bin/notify-send",
                    "qdbus6": "/usr/bin/qdbus6",
                    "ffmpeg": "/usr/bin/ffmpeg",
                },
                outputs={
                    "qdbus6": subprocess.CompletedProcess([], 1, "", "Operation not permitted"),
                },
            )

            report = run_doctor(
                model_path=model,
                watch_directories=[root],
                runner=system.run,
                which=system.which,
                environ={
                    "DBUS_SESSION_BUS_ADDRESS": "unix:path=/run/user/1000/bus",
                    "WAYLAND_DISPLAY": "wayland-0",
                },
                port_probe=lambda _host, _port: errno.ECONNREFUSED,
            )

            notification = next(check for check in report.checks if check.name == "KDE 通知")
            self.assertEqual(notification.status, CheckStatus.WARN)
            self.assertIn("不判定", notification.summary)

    def test_missing_required_tools_and_model_fail(self) -> None:
        system = FakeSystem(paths={})
        report = run_doctor(
            model_path=None,
            runner=system.run,
            which=system.which,
            environ={},
            port_probe=lambda _host, _port: errno.ECONNREFUSED,
        )
        self.assertFalse(report.healthy)
        failed_names = {check.name for check in report.checks if check.status is CheckStatus.FAIL}
        self.assertTrue({"GPU", "llama-server", "模型", "KDE 通知", "ffmpeg"} <= failed_names)

    def test_model_path_operations_become_explicit_failures(self) -> None:
        model = Path("/model/gemma.gguf")
        cases = (
            ("exists", PermissionError(errno.EACCES, "blocked"), "無權檢查模型路徑"),
            ("is_file", OSError(errno.EIO, "metadata failed"), "無法判定模型路徑類型"),
            ("stat", PermissionError(errno.EACCES, "blocked"), "無權讀取模型檔資訊"),
            ("open", PermissionError(errno.EACCES, "blocked"), "無權讀取模型檔頭"),
        )

        for operation, error, expected in cases:
            with self.subTest(operation=operation):
                patches = [
                    patch.object(Path, "exists", return_value=True),
                    patch.object(Path, "is_file", return_value=True),
                    patch.object(Path, "stat", return_value=os.stat_result((0,) * 10)),
                    patch.object(Path, "open"),
                    patch("mingtu_ai.diagnostics.doctor.os.access", return_value=True),
                ]
                operation_index = {"exists": 0, "is_file": 1, "stat": 2, "open": 3}[operation]
                patches[operation_index] = patch.object(Path, operation, side_effect=error)
                entered = [manager.start() for manager in patches]
                if operation != "open":
                    entered[3].return_value.__enter__.return_value.read.return_value = b"GGUF"
                try:
                    result = check_model(model)
                finally:
                    for manager in reversed(patches):
                        manager.stop()

                self.assertEqual(result.status, CheckStatus.FAIL)
                self.assertIn(expected, result.summary)
                self.assertNotIn("不存在", result.summary)

    def test_model_access_probe_oserror_is_caught(self) -> None:
        model = Path("/model/gemma.gguf")
        with (
            patch.object(Path, "exists", return_value=True),
            patch.object(Path, "is_file", return_value=True),
            patch(
                "mingtu_ai.diagnostics.doctor.os.access",
                side_effect=PermissionError(errno.EACCES, "blocked"),
            ),
        ):
            result = check_model(model)

        self.assertEqual(result.status, CheckStatus.FAIL)
        self.assertIn("無權檢查模型檔讀取權限", result.summary)
        self.assertNotIn("不存在", result.summary)

    def test_mmproj_requires_a_readable_regular_gguf_file(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            valid = root / "valid-projector.gguf"
            valid.write_bytes(b"GGUFprojector")
            invalid_header = root / "invalid-projector.gguf"
            invalid_header.write_bytes(b"NOPE")

            self.assertEqual(check_mmproj(valid).status, CheckStatus.PASS)
            self.assertEqual(check_mmproj(root).status, CheckStatus.FAIL)
            self.assertIn("不是一般檔案", check_mmproj(root).summary)
            self.assertEqual(check_mmproj(invalid_header).status, CheckStatus.FAIL)
            self.assertIn("GGUF 檔頭無效", check_mmproj(invalid_header).summary)

            with patch(
                "mingtu_ai.diagnostics.doctor.os.access",
                return_value=False,
            ):
                unreadable = check_mmproj(valid)
            self.assertEqual(unreadable.status, CheckStatus.FAIL)
            self.assertIn("無法讀取", unreadable.summary)

    def test_mmproj_permission_error_is_an_explicit_failure(self) -> None:
        projector = Path("/model/mmproj.gguf")
        with (
            patch.object(Path, "exists", return_value=True),
            patch.object(Path, "is_file", return_value=True),
            patch.object(Path, "stat", return_value=os.stat_result((0,) * 10)),
            patch.object(
                Path,
                "open",
                side_effect=PermissionError(errno.EACCES, "blocked"),
            ),
            patch("mingtu_ai.diagnostics.doctor.os.access", return_value=True),
        ):
            result = check_mmproj(projector)

        self.assertEqual(result.status, CheckStatus.FAIL)
        self.assertIn("無權讀取多模態投影模型檔頭", result.summary)
        self.assertIn("PermissionError", result.summary)

    def test_configured_mmproj_failure_affects_doctor_exit_code(self) -> None:
        passing = CheckResult("stub", CheckStatus.PASS, "ok")
        with tempfile.TemporaryDirectory() as temporary:
            invalid = Path(temporary) / "mmproj.gguf"
            invalid.write_bytes(b"NOPE")
            with (
                patch("mingtu_ai.diagnostics.doctor.check_gpu", return_value=passing),
                patch(
                    "mingtu_ai.diagnostics.doctor.check_llama_server",
                    return_value=passing,
                ),
                patch("mingtu_ai.diagnostics.doctor.check_model", return_value=passing),
                patch(
                    "mingtu_ai.diagnostics.doctor.check_notifications",
                    return_value=passing,
                ),
                patch("mingtu_ai.diagnostics.doctor.check_port", return_value=passing),
                patch("mingtu_ai.diagnostics.doctor.check_ffmpeg", return_value=passing),
            ):
                report = run_doctor(mmproj_path=invalid)

        self.assertFalse(report.healthy)
        self.assertEqual(report.exit_code, 1)
        projector = next(
            check for check in report.checks if check.name == "多模態 projector"
        )
        self.assertEqual(projector.status, CheckStatus.FAIL)

    def test_cli_doctor_permission_error_has_no_traceback(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            model = root / "service-owned-model.gguf"
            original_exists = Path.exists

            def exists_or_denied(path: Path) -> bool:
                if path == model:
                    raise PermissionError(errno.EACCES, "service-owned cache")
                return original_exists(path)

            stdout = io.StringIO()
            stderr = io.StringIO()
            with patch.object(Path, "exists", exists_or_denied):
                exit_code = main(
                    ["--doctor", "--model", str(model), "--watch", str(root)],
                    stdout=stdout,
                    stderr=stderr,
                    environ={},
                )

        rendered = stdout.getvalue()
        self.assertEqual(exit_code, 1)
        self.assertIn("[FAIL] 模型: 無權檢查模型路徑", rendered)
        self.assertNotIn("模型檔不存在", rendered)
        self.assertNotIn("Traceback", rendered + stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
