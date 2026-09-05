import sys
from collections.abc import Iterable
from pathlib import Path

from .inotify import (
    FileEventMonitor,
    InotifyEvent,
    InotifyUnavailableError,
    InotifyWatcher,
    WatchInvalidatedError,
    WatchOverflowError,
)
from .policy import (
    EventDeduplicator,
    OwnedMoveSuppressor,
    should_ignore,
    wait_until_stable,
)
from .polling import PolledWatcher


def create_watcher(watch_directories: Iterable[Path | str] = ()):
    """Return an appropriate watcher (InotifyWatcher on Linux, PolledWatcher fallback)."""
    if sys.platform.startswith("linux"):
        try:
            return InotifyWatcher(watch_directories)
        except InotifyUnavailableError:
            pass
    return PolledWatcher(watch_directories)


__all__ = [
    "EventDeduplicator",
    "FileEventMonitor",
    "InotifyEvent",
    "InotifyUnavailableError",
    "InotifyWatcher",
    "OwnedMoveSuppressor",
    "PolledWatcher",
    "WatchInvalidatedError",
    "WatchOverflowError",
    "create_watcher",
    "should_ignore",
    "wait_until_stable",
]
