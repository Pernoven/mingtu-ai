#!/usr/bin/env python3
"""Source-checkout entry point; the desktop entry pins the Python executable."""

from pathlib import Path
import signal
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from mingtu_ai.cli import main

if __name__ == "__main__":
    # KDE logout sends SIGTERM. Reuse the normal interrupt/Qt shutdown path so
    # the model child and notification helpers are reaped before exiting.
    signal.signal(signal.SIGTERM, signal.default_int_handler)
    raise SystemExit(main(["--single-instance", "--start-minimized", *sys.argv[1:]]))
