"""Shared setuptools configuration for the Mingtu AI native Qt extension.

This module is imported by both the PEP 517 build and the developer in-place
build helper.  It performs no network access and discovers Qt exclusively via
the system ``pkg-config`` metadata.
"""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path
import shlex
import subprocess
import sys
import sysconfig

from setuptools import Extension
from setuptools.command.build_ext import build_ext
from setuptools.errors import SetupError


ROOT = Path(__file__).resolve().parent
NATIVE_SOURCE = "native/mingtu_native.cpp"
NATIVE_MODULE = "mingtu_ai.ui._native_ui"
QT_PACKAGES = ("Qt6Widgets", "Qt6DBus")


def native_extension() -> Extension:
    """Return the single CPython 3.13 extension built into platform wheels."""

    compile_args = (
        ["/std:c++20", "/O2", "/W4"]
        if sys.platform.startswith("win")
        else [
            "-std=c++20",
            "-O2",
            "-Wall",
            "-Wextra",
            "-Wpedantic",
            "-Wno-missing-field-initializers",
            "-Wno-cast-function-type",
        ]
    )
    return Extension(
        NATIVE_MODULE,
        sources=[NATIVE_SOURCE],
        language="c++",
        extra_compile_args=compile_args,
    )


def _pkg_config(*arguments: str) -> list[str]:
    command = ["pkg-config", *arguments]
    try:
        completed = subprocess.run(
            command,
            cwd=ROOT,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
        )
    except FileNotFoundError as exc:
        raise SetupError(
            "Mingtu AI native UI build requires the system 'pkg-config' "
            "executable; no package is downloaded automatically"
        ) from exc
    if completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip()
        suffix = f": {detail}" if detail else ""
        raise SetupError(
            "Mingtu AI native UI build requires Qt >= 6.5 development "
            f"metadata for Qt6Widgets and Qt6DBus{suffix}"
        )
    return shlex.split(completed.stdout)


def _append_unique(target: list[str], values: Iterable[str]) -> None:
    for value in values:
        if value not in target:
            target.append(value)


def _configure_qt(extension: Extension) -> None:
    _pkg_config("--atleast-version=6.5", *QT_PACKAGES)
    compile_tokens = _pkg_config("--cflags", *QT_PACKAGES)
    link_tokens = _pkg_config("--libs", *QT_PACKAGES)

    for token in compile_tokens:
        if token.startswith("-I") and len(token) > 2:
            _append_unique(extension.include_dirs, [token[2:]])
        elif token.startswith("-D") and len(token) > 2:
            definition = token[2:].split("=", 1)
            value = definition[1] if len(definition) == 2 else None
            macro = (definition[0], value)
            if macro not in extension.define_macros:
                extension.define_macros.append(macro)
        elif token.startswith("-U") and len(token) > 2:
            _append_unique(extension.undef_macros, [token[2:]])
        else:
            _append_unique(extension.extra_compile_args, [token])

    for token in link_tokens:
        if token.startswith("-L") and len(token) > 2:
            _append_unique(extension.library_dirs, [token[2:]])
        elif token.startswith("-l") and len(token) > 2:
            _append_unique(extension.libraries, [token[2:]])
        else:
            _append_unique(extension.extra_link_args, [token])


class MingtuBuildExt(build_ext):
    """Build the Qt shell from system headers for the active CPython ABI."""

    def build_extensions(self) -> None:
        if sys.version_info[:2] != (3, 13):
            raise SetupError(
                "Mingtu AI native wheels currently require CPython 3.13; "
                f"the active interpreter is {sys.version_info.major}."
                f"{sys.version_info.minor}"
            )
        python_include = sysconfig.get_path("include")
        if not python_include or not (Path(python_include) / "Python.h").is_file():
            raise SetupError(
                "Mingtu AI native UI build requires Python 3.13 development "
                "headers for the active interpreter"
            )
        if len(self.extensions) != 1 or self.extensions[0].name != NATIVE_MODULE:
            raise SetupError("unexpected Mingtu AI native extension configuration")
        _configure_qt(self.extensions[0])
        super().build_extensions()
