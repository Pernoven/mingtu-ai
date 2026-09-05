"""Programmatic native-extension configuration for the PEP 517 backend."""

from pathlib import Path
import sys

from setuptools import setup


PROJECT_ROOT = Path(__file__).resolve().parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from _mingtu_build import MingtuBuildExt, native_extension


setup(
    ext_modules=[native_extension()],
    cmdclass={"build_ext": MingtuBuildExt},
)
