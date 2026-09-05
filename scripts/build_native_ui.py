#!/usr/bin/env python3
"""Build the packaged Qt6 CPython extension in place without downloads."""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import sys
import tempfile

from setuptools import Distribution
from setuptools.errors import SetupError


ROOT = Path(__file__).resolve().parents[1]
if os.fspath(ROOT) not in sys.path:
    sys.path.insert(0, os.fspath(ROOT))

from _mingtu_build import MingtuBuildExt, NATIVE_MODULE, native_extension


def build(*, cxx: str, verbose: bool = False) -> Path:
    """Use the same setuptools build_ext command as wheel builds."""

    distribution = Distribution(
        {
            "name": "mingtu-ai",
            "package_dir": {"": "src"},
            "ext_modules": [native_extension()],
            "cmdclass": {"build_ext": MingtuBuildExt},
        }
    )
    distribution.script_name = os.fspath(ROOT / "setup.py")
    distribution.verbose = 2 if verbose else 1
    command = distribution.get_command_obj("build_ext")
    command.inplace = True
    command.force = True

    previous_cxx = os.environ.get("CXX")
    previous_directory = Path.cwd()
    try:
        os.environ["CXX"] = cxx
        os.chdir(ROOT)
        with tempfile.TemporaryDirectory(prefix="mingtu-native-build-") as temporary:
            command.build_temp = os.fspath(Path(temporary) / "temp")
            command.build_lib = os.fspath(Path(temporary) / "lib")
            distribution.run_command("build_ext")
            destination = Path(command.get_ext_fullpath(NATIVE_MODULE)).resolve()
    finally:
        os.chdir(previous_directory)
        if previous_cxx is None:
            os.environ.pop("CXX", None)
        else:
            os.environ["CXX"] = previous_cxx
    if not destination.is_file():
        raise RuntimeError(f"native build did not create the expected extension: {destination}")
    return destination


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cxx", default=os.environ.get("CXX", "g++"))
    parser.add_argument("--verbose", action="store_true")
    arguments = parser.parse_args()
    try:
        output = build(cxx=arguments.cxx, verbose=arguments.verbose)
    except (OSError, RuntimeError, SetupError) as exc:
        parser.exit(1, f"build-native-ui: {exc}\n")
    print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
