# Mingtu AI native shell

`mingtu_native.cpp` is a CPython 3.13 extension linked against Qt6 Widgets and
Qt6 D-Bus.
It does not start a helper daemon or sidecar: `QApplication`, the Python monitor,
the queue, and `llama-server` ownership all remain under the one `mingtu-ai`
process.

The release package uses setuptools through the declared PEP 517 backend. Every
wheel build compiles this source and produces a CPython-3.13/platform-specific
wheel. A wheel build fails clearly when the active interpreter is not Python
3.13 or when `pkg-config`, Python headers, Qt >= 6.5 development metadata, or a
C++20 compiler are unavailable. The build never installs or downloads them.

Build an sdist and a native wheel with the standard frontend when it is already
installed:

```fish
python -m build --no-isolation
```

`--no-isolation` makes the no-download boundary explicit: the active Python
environment must already satisfy the build requirements in `pyproject.toml`.

The sdist includes this C++ source, `_mingtu_build.py`, and the packaging
configuration needed to reproduce the wheel. The compiled `.so` is not source
distribution content and must never be copied from another Python/Qt ABI.

Build it with the repository's existing Python interpreter:

```fish
python scripts/build_native_ui.py
```

This developer helper invokes the same setuptools `build_ext` implementation as
the PEP 517 wheel build. Its generated extension is placed in
`src/mingtu_ai/ui/` and remains ignored because it is only an in-place build
artifact. A release wheel gets its own freshly compiled copy from a clean sdist.

Qt's `QApplication` event loop must still be started by the Python main thread.
`--headless` skips desktop startup at runtime, but it does not turn the release
artifact into a pure-Python wheel.
