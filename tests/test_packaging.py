from __future__ import annotations

import importlib.util
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tarfile
from tempfile import TemporaryDirectory
import tomllib
import unittest
from unittest.mock import patch
import zipfile

from setuptools.errors import SetupError

from _mingtu_build import _pkg_config


ROOT = Path(__file__).resolve().parents[1]
OWNED_DOCUMENTS = (
    ROOT / "README.md",
    ROOT / "CONTRIBUTING.md",
    ROOT / "SECURITY.md",
    *(ROOT / "docs").glob("*.md"),
)
PACKAGING_FILES = (
    ROOT / "packaging/systemd/mingtu-ai.service",
    ROOT / "packaging/systemd/environment.example",
    ROOT / "packaging/autostart/org.permind.MingtuAI.desktop",
)
SCRIPTS = (
    ROOT / "scripts/dev-run.fish",
    ROOT / "scripts/doctor.fish",
)
NATIVE_BUILD_FILES = (
    ROOT / "_mingtu_build.py",
    ROOT / "MANIFEST.in",
    ROOT / "setup.py",
    ROOT / "native/mingtu_native.cpp",
    ROOT / "native/README.md",
)


def _has_native_build_toolchain() -> bool:
    if sys.version_info[:2] != (3, 13):
        return False
    if importlib.util.find_spec("setuptools") is None:
        return False
    if importlib.util.find_spec("wheel") is None:
        return False
    if importlib.util.find_spec("pip") is None:
        return False
    if shutil.which("pkg-config") is None or shutil.which("g++") is None:
        return False
    completed = subprocess.run(
        ["pkg-config", "--atleast-version=6.5", "Qt6Widgets", "Qt6DBus"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    return completed.returncode == 0


def _clean_build_environment() -> dict[str, str]:
    environment = os.environ.copy()
    environment.pop("PYTHONPATH", None)
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    environment["PIP_DISABLE_PIP_VERSION_CHECK"] = "1"
    return environment


def _copy_clean_source(destination: Path) -> None:
    ignored_names = {
        ".git",
        ".pytest_cache",
        ".mypy_cache",
        ".ruff_cache",
        ".venv",
        "__pycache__",
        "build",
        "dist",
        "htmlcov",
        "venv",
    }

    def ignore(directory: str, names: list[str]) -> set[str]:
        ignored = {name for name in names if name in ignored_names}
        ignored.update(name for name in names if name.endswith((".pyc", ".so")))
        if Path(directory).name == "benchmark":
            ignored.add("results")
        ignored.update(name for name in names if name.endswith(".egg-info"))
        return ignored

    shutil.copytree(ROOT, destination, ignore=ignore)


class PackagingTests(unittest.TestCase):
    def test_delivery_files_exist(self) -> None:
        for path in (*OWNED_DOCUMENTS, *PACKAGING_FILES, *SCRIPTS, *NATIVE_BUILD_FILES):
            with self.subTest(path=path.relative_to(ROOT)):
                self.assertTrue(path.is_file())

    def test_documentation_links_resolve(self) -> None:
        pattern = re.compile(r"(?<!!)\[[^\]]+\]\(([^)]+)\)")
        for document in OWNED_DOCUMENTS:
            text = document.read_text(encoding="utf-8")
            for target in pattern.findall(text):
                clean_target = target.strip().split("#", 1)[0]
                if not clean_target or clean_target.startswith(("http://", "https://", "mailto:")):
                    continue
                resolved = (document.parent / clean_target).resolve()
                with self.subTest(document=document.name, target=target):
                    self.assertTrue(resolved.exists(), f"broken local link: {target}")

    def test_assets_do_not_embed_machine_or_secret_data(self) -> None:
        forbidden = (
            "/home/",
            "BEGIN PRIVATE KEY",
        )
        for path in (*OWNED_DOCUMENTS, *PACKAGING_FILES, *SCRIPTS):
            text = path.read_text(encoding="utf-8")
            for value in forbidden:
                with self.subTest(path=path.name, value=value):
                    self.assertNotIn(value, text)

    def test_launch_assets_never_target_reserved_port(self) -> None:
        for path in (*PACKAGING_FILES, *SCRIPTS):
            text = path.read_text(encoding="utf-8")
            with self.subTest(path=path.name):
                self.assertNotIn("localhost:3000", text)
                self.assertNotIn("127.0.0.1:3000", text)

    def test_launch_scripts_never_install_or_download(self) -> None:
        forbidden = ("pacman", "pip install", "curl ", "wget ", "systemctl --user enable")
        for script in SCRIPTS:
            text = script.read_text(encoding="utf-8")
            for value in forbidden:
                with self.subTest(script=script.name, value=value):
                    self.assertNotIn(value, text)

    def test_pep517_metadata_requires_cpython_313_native_build(self) -> None:
        metadata = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
        self.assertEqual(metadata["build-system"]["build-backend"], "setuptools.build_meta")
        self.assertEqual(metadata["project"]["requires-python"], ">=3.13,<3.14")
        self.assertFalse(metadata["tool"]["setuptools"]["include-package-data"])
        setup_text = (ROOT / "setup.py").read_text(encoding="utf-8")
        self.assertIn("ext_modules=[native_extension()]", setup_text)
        self.assertIn('NATIVE_MODULE = "mingtu_ai.ui._native_ui"', NATIVE_BUILD_FILES[0].read_text())

    def test_missing_pkg_config_error_is_actionable_and_offline(self) -> None:
        with patch("_mingtu_build.subprocess.run", side_effect=FileNotFoundError):
            with self.assertRaisesRegex(SetupError, "requires the system 'pkg-config'"):
                _pkg_config("--cflags", "Qt6Widgets", "Qt6DBus")

    def test_missing_qt_development_metadata_error_is_actionable(self) -> None:
        missing_qt = subprocess.CompletedProcess(
            args=["pkg-config"],
            returncode=1,
            stdout="",
            stderr="Package Qt6Widgets was not found",
        )
        with patch("_mingtu_build.subprocess.run", return_value=missing_qt):
            with self.assertRaisesRegex(
                SetupError,
                "requires Qt >= 6.5 development metadata.*Qt6Widgets",
            ):
                _pkg_config("--cflags", "Qt6Widgets", "Qt6DBus")

    @unittest.skipUnless(
        _has_native_build_toolchain(),
        "native gate requires existing setuptools/wheel/pip, g++, pkg-config, and Qt >= 6.5",
    )
    def test_clean_sdist_builds_native_wheel_and_installed_imports(self) -> None:
        self.assertEqual(sys.version_info[:2], (3, 13))
        environment = _clean_build_environment()
        with TemporaryDirectory(prefix="mingtu-package-gate-") as directory:
            temporary = Path(directory)
            source = temporary / "source"
            _copy_clean_source(source)
            self.assertFalse(any(source.rglob("*.so")))

            sdist_build = subprocess.run(
                [
                    sys.executable,
                    "-c",
                    "from setuptools.build_meta import build_sdist; build_sdist('dist')",
                ],
                cwd=source,
                env=environment,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(
                sdist_build.returncode,
                0,
                f"sdist build failed:\n{sdist_build.stdout}\n{sdist_build.stderr}",
            )
            sdists = list((source / "dist").glob("*.tar.gz"))
            self.assertEqual(len(sdists), 1)

            with tarfile.open(sdists[0], "r:gz") as archive:
                sdist_members = set(archive.getnames())
            prefix = "mingtu_ai-0.1.0/"
            expected_sdist_members = {
                f"{prefix}_mingtu_build.py",
                f"{prefix}setup.py",
                f"{prefix}pyproject.toml",
                f"{prefix}native/mingtu_native.cpp",
                f"{prefix}native/README.md",
                f"{prefix}docs/packaging.md",
                f"{prefix}packaging/systemd/mingtu-ai.service",
                f"{prefix}tests/test_packaging.py",
            }
            self.assertTrue(expected_sdist_members.issubset(sdist_members))
            self.assertFalse(any(name.endswith((".so", ".pyc")) for name in sdist_members))
            self.assertFalse(any("benchmark/results/" in name for name in sdist_members))

            wheel_directory = temporary / "wheelhouse"
            wheel_directory.mkdir()
            wheel_build = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "pip",
                    "wheel",
                    "--no-index",
                    "--no-deps",
                    "--no-build-isolation",
                    "--wheel-dir",
                    os.fspath(wheel_directory),
                    os.fspath(sdists[0]),
                ],
                cwd=temporary,
                env=environment,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(
                wheel_build.returncode,
                0,
                f"wheel build failed:\n{wheel_build.stdout}\n{wheel_build.stderr}",
            )
            wheels = list(wheel_directory.glob("*.whl"))
            self.assertEqual(len(wheels), 1)
            self.assertIn("-cp313-cp313-", wheels[0].name)
            self.assertNotIn("-py3-none-any.whl", wheels[0].name)

            with zipfile.ZipFile(wheels[0]) as archive:
                wheel_members = set(archive.namelist())
            extensions = [
                name
                for name in wheel_members
                if name.startswith("mingtu_ai/ui/_native_ui") and name.endswith(".so")
            ]
            self.assertEqual(len(extensions), 1)
            self.assertNotIn("native/mingtu_native.cpp", wheel_members)
            self.assertFalse(any(name.endswith(".pyc") for name in wheel_members))

            installation = temporary / "installed"
            install = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "pip",
                    "install",
                    "--no-index",
                    "--no-deps",
                    "--no-compile",
                    "--target",
                    os.fspath(installation),
                    os.fspath(wheels[0]),
                ],
                cwd=temporary,
                env=environment,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(
                install.returncode,
                0,
                f"wheel install failed:\n{install.stdout}\n{install.stderr}",
            )
            import_environment = environment.copy()
            import_environment["PYTHONPATH"] = os.fspath(installation)
            imported = subprocess.run(
                [
                    sys.executable,
                    "-c",
                    (
                        "from pathlib import Path; import sys; "
                        "import mingtu_ai; import mingtu_ai.ui._native_ui as native; "
                        "root=Path(sys.argv[1]).resolve(); "
                        "assert Path(mingtu_ai.__file__).resolve().is_relative_to(root); "
                        "assert callable(native.run); print(native.__file__)"
                    ),
                    os.fspath(installation),
                ],
                cwd=temporary,
                env=import_environment,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(
                imported.returncode,
                0,
                f"installed import failed:\n{imported.stdout}\n{imported.stderr}",
            )

    @unittest.skipUnless(shutil.which("git"), "git is unavailable")
    def test_private_benchmark_results_are_ignored_but_fixture_is_public(self) -> None:
        gitignore = (ROOT / ".gitignore").read_text(encoding="utf-8")
        with TemporaryDirectory() as directory:
            checkout = Path(directory)
            (checkout / ".gitignore").write_text(gitignore, encoding="utf-8")
            initialized = subprocess.run(
                ["git", "init", "--quiet"],
                cwd=checkout,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(initialized.returncode, 0, initialized.stderr)

            private_result = subprocess.run(
                [
                    "git",
                    "check-ignore",
                    "--quiet",
                    "--no-index",
                    "benchmark/results/private.json",
                ],
                cwd=checkout,
                check=False,
            )
            public_fixture = subprocess.run(
                [
                    "git",
                    "check-ignore",
                    "--quiet",
                    "--no-index",
                    "benchmark/dataset/generated/gemma4-smoke-shapes.png",
                ],
                cwd=checkout,
                check=False,
            )

            self.assertEqual(private_result.returncode, 0)
            self.assertEqual(public_fixture.returncode, 1)

    def test_systemd_asset_is_manual_and_graphical(self) -> None:
        text = PACKAGING_FILES[0].read_text(encoding="utf-8")
        self.assertIn("EnvironmentFile=-%h/.config/mingtu-ai/environment", text)
        self.assertIn("ExecStart=/usr/bin/env mingtu-ai", text)
        self.assertIn("WantedBy=graphical-session.target", text)
        self.assertNotIn("systemctl", text)

    def test_autostart_asset_is_disabled_by_default(self) -> None:
        text = PACKAGING_FILES[2].read_text(encoding="utf-8")
        self.assertIn("TryExec=mingtu-ai", text)
        self.assertIn("OnlyShowIn=KDE;", text)
        self.assertIn("Hidden=true", text)

    @unittest.skipUnless(shutil.which("fish"), "fish is unavailable")
    def test_fish_scripts_parse(self) -> None:
        completed = subprocess.run(
            ["fish", "-n", *(str(path) for path in SCRIPTS)],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)

    @unittest.skipUnless(shutil.which("desktop-file-validate"), "desktop validator is unavailable")
    def test_desktop_asset_validates(self) -> None:
        completed = subprocess.run(
            ["desktop-file-validate", str(PACKAGING_FILES[2])],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)


if __name__ == "__main__":
    unittest.main()
