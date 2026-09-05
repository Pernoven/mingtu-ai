from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from mingtu_ai.desktop_paths import (
    pictures_directory,
    spectacle_screenshot_directory,
    xdg_config_home,
)


class DesktopPathTests(unittest.TestCase):
    def test_localized_xdg_and_spectacle_folder_are_resolved(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            config = home / ".config"
            screenshots = home / "圖片" / "螢幕截圖"
            screenshots.mkdir(parents=True)
            config.mkdir()
            (config / "user-dirs.dirs").write_text(
                'XDG_PICTURES_DIR="$HOME/圖片"\n', encoding="utf-8"
            )
            (config / "spectaclerc").write_text(
                "[ImageSave]\n"
                f"lastImageSaveLocation={screenshots.as_uri()}/capture.png\n"
                "translatedScreenshotsFolder=螢幕截圖\n",
                encoding="utf-8",
            )
            environ = {"HOME": str(home)}

            self.assertEqual(pictures_directory(environ), home / "圖片")
            self.assertEqual(spectacle_screenshot_directory(environ), screenshots)

    def test_translated_folder_falls_back_below_xdg_pictures(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            config = home / "custom-config"
            screenshots = home / "Pictures Local" / "Screenshots Local"
            screenshots.mkdir(parents=True)
            config.mkdir()
            (config / "user-dirs.dirs").write_text(
                'XDG_PICTURES_DIR="$HOME/Pictures Local"\n', encoding="utf-8"
            )
            (config / "spectaclerc").write_text(
                "[ImageSave]\n"
                "lastImageSaveLocation=file:///missing/location/old.png\n"
                "translatedScreenshotsFolder=Screenshots Local\n",
                encoding="utf-8",
            )
            environ = {"HOME": str(home), "XDG_CONFIG_HOME": str(config)}

            self.assertEqual(xdg_config_home(environ), config)
            self.assertEqual(spectacle_screenshot_directory(environ), screenshots)

    def test_shell_substitution_is_never_evaluated(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            config = home / ".config"
            config.mkdir()
            marker = home / "must-not-exist"
            (config / "user-dirs.dirs").write_text(
                f'XDG_PICTURES_DIR="$(touch {marker})"\n', encoding="utf-8"
            )

            self.assertEqual(pictures_directory({"HOME": str(home)}), home / "Pictures")
            self.assertFalse(marker.exists())

    def test_non_file_spectacle_url_is_ignored(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            config = home / ".config"
            pictures = home / "Pictures"
            pictures.mkdir()
            config.mkdir()
            (config / "spectaclerc").write_text(
                "[ImageSave]\nlastImageSaveLocation=https://example.test/capture.png\n",
                encoding="utf-8",
            )
            self.assertEqual(
                spectacle_screenshot_directory({"HOME": str(home)}), pictures
            )


    def test_user_watch_directories_persistence(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            config = home / ".config"
            watch_dir = home / "Pictures" / "MyScreenshots"
            watch_dir.mkdir(parents=True)
            environ = {"HOME": str(home)}

            from mingtu_ai.desktop_paths import (
                load_user_watch_directories,
                save_user_watch_directories,
                user_config_path,
            )

            self.assertEqual(load_user_watch_directories(environ), [])
            save_user_watch_directories([watch_dir], environ)

            self.assertTrue(user_config_path(environ).is_file())
            loaded = load_user_watch_directories(environ)
            self.assertEqual(loaded, [watch_dir.absolute()])


if __name__ == "__main__":
    unittest.main()

