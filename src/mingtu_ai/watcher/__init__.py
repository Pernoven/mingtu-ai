"""Linux file watching with conservative filtering and stability checks."""

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

__all__ = [
    "EventDeduplicator",
    "FileEventMonitor",
    "InotifyEvent",
    "InotifyUnavailableError",
    "InotifyWatcher",
    "OwnedMoveSuppressor",
    "WatchInvalidatedError",
    "WatchOverflowError",
    "should_ignore",
    "wait_until_stable",
]
