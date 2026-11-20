from __future__ import annotations

import importlib.util
import io
import os
import shutil
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from mingtu_ai.cli import main
from mingtu_ai.runtime.instance import resident_lock


ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(sys.platform.startswith("linux"), "Linux desktop resident")
class ResidentTests(unittest.TestCase):
    def test_process_lock_blocks_duplicate_and_releases_after_exit(self):
        with tempfile.TemporaryDirectory() as temporary:
            environment = {**os.environ, "XDG_STATE_HOME": temporary, "PYTHONPATH": str(ROOT / "src")}
            code = (
                "import os; from mingtu_ai.runtime.instance import resident_lock; "
                "guard=resident_lock(os.environ); guard.__enter__()"
            )
            with resident_lock(environment):
                result = subprocess.run([sys.executable, "-c", code], env=environment, capture_output=True, text=True)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("ResidentAlreadyRunning", result.stderr)
                output = io.StringIO()
                with patch("mingtu_ai.cli.create_coordinator") as create:
                    self.assertEqual(main(["--single-instance", "--headless"], environ=environment, stdout=output), 0)
                    create.assert_not_called()
                self.assertIn("已在執行", output.getvalue())
            result = subprocess.run([sys.executable, "-c", code], env=environment, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)

    def test_start_minimized_preserves_native_tray(self):
        with patch("mingtu_ai.cli.create_coordinator") as create, patch("mingtu_ai.cli.run_native_ui", return_value=0) as native:
            self.assertEqual(main(["--start-minimized"], environ={}), 0)
            native.assert_called_once_with(create.return_value.__enter__.return_value, show_windows=False)

    def test_logout_signal_stops_resident_and_releases_lock(self):
        self._check_logout_signal(headless=True)

    def test_logout_signal_stops_qt_resident_and_releases_lock(self):
        if importlib.util.find_spec("mingtu_ai.ui._native_ui") is None:
            self.skipTest("native Qt extension required")
        self._check_logout_signal(headless=False)

    def test_lost_watch_exits_qt_resident_with_failure_for_supervisor(self):
        if importlib.util.find_spec("mingtu_ai.ui._native_ui") is None:
            self.skipTest("native Qt extension required")
        self._check_logout_signal(headless=False, invalidate_watch=True)

    def _check_logout_signal(self, *, headless, invalidate_watch=False):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            watched = root / "watched"
            watched.mkdir()
            environment = {
                **os.environ,
                "QT_QPA_PLATFORM": "offscreen",
                "XDG_STATE_HOME": str(root / "state"),
            }
            arguments = ["--headless"] if headless else []
            process = subprocess.Popen([
                sys.executable, str(ROOT / "scripts/start-resident.py"), *arguments,
                "--watch", str(watched), "--history-db", str(root / "history/db.sqlite3"),
            ], env=environment, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            try:
                deadline = time.monotonic() + 5
                lock = root / "state/mingtu-ai/resident.lock"
                fd_directory = Path("/proc") / str(process.pid) / "fd"
                while process.poll() is None and time.monotonic() < deadline:
                    try:
                        if any(fd.readlink() == Path("anon_inode:inotify") for fd in fd_directory.glob("*")):
                            break
                    except FileNotFoundError:
                        pass  # The process may close a descriptor during the scan.
                    time.sleep(0.02)
                else:
                    self.fail("resident did not start monitoring")
                self.assertTrue(lock.exists())
                if invalidate_watch:
                    watched.rmdir()
                else:
                    process.terminate()
                output, error = process.communicate(timeout=5)
                self.assertEqual(process.returncode, 1 if invalidate_watch else 0, error)
                if invalidate_watch:
                    self.assertIn("監控停止", error)
                elif headless:
                    self.assertIn("已停止", output)
                self.assertNotIn("Traceback", error)
                with resident_lock(environment):
                    pass
            finally:
                if process.poll() is None:
                    process.kill()
                    process.communicate()

    def test_restart_policy_install_remove_and_unmanaged_file_protection(self):
        spec = importlib.util.spec_from_file_location("mingtu_autostart", ROOT / "scripts/install-autostart.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        with tempfile.TemporaryDirectory() as temporary:
            config = Path(temporary)
            path = config / "systemd/user/app-org.permind.MingtuAI@autostart.service.d/50-mingtu-resident.conf"
            module.configure_restart_policy(config, remove=False, dry_run=True)
            self.assertFalse(path.exists())
            module.configure_restart_policy(config, remove=False, dry_run=False)
            self.assertIn("Restart=on-failure", path.read_text())
            module.configure_restart_policy(config, remove=True, dry_run=False)
            self.assertFalse(path.exists())
            path.write_text("# User-owned settings\n")
            with self.assertRaises(ValueError):
                module.configure_restart_policy(config, remove=False, dry_run=False)
            self.assertEqual(path.read_text(), "# User-owned settings\n")

    @unittest.skipUnless(shutil.which("desktop-file-validate"), "desktop-file-validate required")
    def test_generated_entry_quotes_paths_without_shell_interpretation(self):
        spec = importlib.util.spec_from_file_location("mingtu_autostart", ROOT / "scripts/install-autostart.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        entry = module.make_entry(Path('/python 3.13'), ROOT / "scripts/start-resident.py", Path('/models/a $b `c` 100% "x".gguf'), None)
        self.assertIn("Hidden=false", entry)
        self.assertIn('100%%', entry)
        self.assertIn('\\\\$', entry)
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "test.desktop"
            path.write_text(entry)
            result = subprocess.run(["desktop-file-validate", str(path)], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
