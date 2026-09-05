"""Unit tests for Windows platform compatibility and fallbacks."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from mingtu_ai.desktop_paths import home_directory, xdg_config_home
from mingtu_ai.diagnostics.doctor import check_llama_server, check_notifications
from mingtu_ai.naming.files import (
    AtomicRenameUnavailableError,
    numbered_destination,
    rename_noreplace,
)
from mingtu_ai.watcher import PolledWatcher, create_watcher
from mingtu_ai.watcher.polling import IN_CLOSE_WRITE


class WindowsDesktopPathsTests(unittest.TestCase):
    def test_home_directory_honors_userprofile(self) -> None:
        fake_env = {"USERPROFILE": "/home/fake_user"}
        self.assertEqual(home_directory(fake_env), Path("/home/fake_user"))

    def test_xdg_config_home_honors_appdata(self) -> None:
        fake_env = {"USERPROFILE": "/home/fake_user", "APPDATA": "/home/fake_user/AppData/Roaming"}
        self.assertEqual(
            xdg_config_home(fake_env), Path("/home/fake_user/AppData/Roaming")
        )

    def test_xdg_config_home_windows_fallback(self) -> None:
        fake_env = {"USERPROFILE": "/home/fake_user"}
        with patch.object(sys, "platform", "win32"):
            self.assertEqual(
                xdg_config_home(fake_env), Path("/home/fake_user/AppData/Roaming")
            )

    def test_xdg_config_home_macos_fallback(self) -> None:
        fake_env = {"HOME": "/Users/fake_user"}
        with patch.object(sys, "platform", "darwin"):
            self.assertEqual(
                xdg_config_home(fake_env), Path("/Users/fake_user/Library/Application Support")
            )


class WindowsNamingFilesTests(unittest.TestCase):
    def test_numbered_destination_handles_missing_pathconf(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            source = Path(temp_dir) / "image.png"
            source.touch()
            with patch("os.pathconf", side_effect=AttributeError("no pathconf")):
                dest = numbered_destination(source, "suggested_name.png", index=1)
                self.assertEqual(dest.name, "suggested_name.png")

    def test_windows_rename_noreplace_prevents_overwrite(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            source = temp_path / "source.txt"
            source.write_text("source content", encoding="utf-8")
            dest = temp_path / "dest.txt"
            dest.write_text("existing content", encoding="utf-8")

            with patch.object(sys, "platform", "win32"):
                with self.assertRaises(FileExistsError):
                    rename_noreplace(source, dest)
                # Ensure destination was not overwritten
                self.assertEqual(dest.read_text(encoding="utf-8"), "existing content")

    def test_windows_rename_noreplace_succeeds_when_destination_free(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            source = temp_path / "source.txt"
            source.write_text("source content", encoding="utf-8")
            dest = temp_path / "dest.txt"

            with patch.object(sys, "platform", "win32"):
                result = rename_noreplace(source, dest)
                self.assertEqual(result, dest)
                self.assertTrue(dest.is_file())
                self.assertFalse(source.is_file())


class PolledWatcherTests(unittest.TestCase):
    def test_polled_watcher_detects_new_files(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir).resolve()
            watcher = PolledWatcher(watch_directories=[temp_path], poll_interval=0.01)

            # Initial scan should record existing snapshot
            events_before = watcher.read_events(timeout=0.01)
            self.assertEqual(len(events_before), 0)

            # Create a new file
            new_file = temp_path / "new_test_file.png"
            new_file.write_text("hello", encoding="utf-8")

            events_after = watcher.read_events(timeout=0.01)
            self.assertEqual(len(events_after), 1)
            self.assertEqual(events_after[0].path, new_file)
            self.assertTrue(events_after[0].is_ready_file)

            watcher.close()

    def test_create_watcher_factory_returns_polled_on_windows(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            with patch.object(sys, "platform", "win32"):
                watcher = create_watcher([temp_path])
                self.assertIsInstance(watcher, PolledWatcher)
                watcher.close()


class WindowsDiagnosticsTests(unittest.TestCase):
    def test_check_llama_server_finds_exe_binary(self) -> None:
        mock_runner = MagicMock()
        mock_runner.return_value.returncode = 0
        mock_runner.return_value.stdout = "version 1.0.0"
        mock_runner.return_value.stderr = ""

        def mock_which(name: str) -> str | None:
            if name == "llama-server.exe":
                return "C:\\llama-server.exe"
            return None

        with patch("os.name", "nt"), patch("pathlib.Path.is_file", return_value=True):
            result = check_llama_server(runner=mock_runner, which=mock_which)
            self.assertEqual(result.status.value, "pass")
            self.assertEqual(result.details.get("path"), "C:\\llama-server.exe")

    def test_check_notifications_warns_on_windows(self) -> None:
        mock_runner = MagicMock()
        mock_which = MagicMock(return_value=None)

        with patch.object(sys, "platform", "win32"):
            result = check_notifications(
                runner=mock_runner, which=mock_which, environ={}
            )
            self.assertEqual(result.status.value, "warn")
            self.assertIn("Windows", result.summary)


if __name__ == "__main__":
    unittest.main()
