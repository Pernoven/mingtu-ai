"""Snapshot-based polled directory watcher for Windows and non-inotify hosts."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
import sys
import time
from types import TracebackType

from .policy import should_ignore


# Emulate inotify constant bitmasks for compatibility with FileEventMonitor
IN_CLOSE_WRITE = 0x00000008
IN_MOVED_FROM = 0x00000040
IN_MOVED_TO = 0x00000080
IN_Q_OVERFLOW = 0x00004000
IN_IGNORED = 0x00008000
IN_ISDIR = 0x40000000


@dataclass(frozen=True, slots=True)
class RawPolledEvent:
    mask: int
    watch_directory: Path
    path: Path | None = None
    cookie: int = 0

    @property
    def is_ready_file(self) -> bool:
        return bool(self.mask & (IN_CLOSE_WRITE | IN_MOVED_TO))


@dataclass
class _FileSnapshot:
    mtime: float
    size: int


class PolledWatcher:
    """Stdlib-only directory watcher using periodic snapshot polling."""

    def __init__(
        self,
        watch_directories: Iterable[Path | str] = (),
        *,
        poll_interval: float = 0.5,
    ) -> None:
        self.poll_interval = poll_interval
        self._directories: dict[Path, dict[Path, _FileSnapshot]] = {}
        self._closed = False
        for directory in watch_directories:
            self.add_watch_directory(directory)

    @property
    def watch_directories(self) -> tuple[Path, ...]:
        return tuple(self._directories.keys())

    def add_watch_directory(self, directory: Path | str) -> None:
        if self._closed:
            raise ValueError("watcher is closed")
        path = Path(directory).expanduser().absolute()
        if not path.is_dir():
            raise FileNotFoundError(path)
        if path not in self._directories:
            self._directories[path] = self._snapshot_directory(path)

    def remove_watch_directory(self, directory: Path | str) -> None:
        if self._closed:
            raise ValueError("watcher is closed")
        path = Path(directory).expanduser().absolute()
        self._directories.pop(path, None)

    def _snapshot_directory(self, directory: Path) -> dict[Path, _FileSnapshot]:
        snapshot: dict[Path, _FileSnapshot] = {}
        try:
            for entry in directory.iterdir():
                if entry.is_file() and not should_ignore(entry):
                    try:
                        stat = entry.stat()
                        snapshot[entry] = _FileSnapshot(mtime=stat.st_mtime, size=stat.st_size)
                    except OSError:
                        continue
        except OSError:
            pass
        return snapshot

    def read_events(self, timeout: float | None = None) -> list[RawPolledEvent]:
        if self._closed:
            raise ValueError("watcher is closed")

        wait_time = timeout if timeout is not None else self.poll_interval
        if wait_time > 0:
            time.sleep(min(wait_time, self.poll_interval))

        events: list[RawPolledEvent] = []
        for directory in tuple(self._directories.keys()):
            if not directory.exists():
                self._directories.pop(directory, None)
                events.append(
                    RawPolledEvent(
                        mask=IN_IGNORED,
                        watch_directory=directory,
                    )
                )
                continue

            current_snapshot = self._snapshot_directory(directory)
            old_snapshot = self._directories.get(directory, {})

            for path, current_meta in current_snapshot.items():
                if path not in old_snapshot:
                    events.append(
                        RawPolledEvent(
                            mask=IN_CLOSE_WRITE,
                            watch_directory=directory,
                            path=path,
                        )
                    )
                else:
                    old_meta = old_snapshot[path]
                    if (
                        current_meta.mtime > old_meta.mtime
                        or current_meta.size != old_meta.size
                    ):
                        events.append(
                            RawPolledEvent(
                                mask=IN_CLOSE_WRITE,
                                watch_directory=directory,
                                path=path,
                            )
                        )

            self._directories[directory] = current_snapshot

        return events

    def close(self) -> None:
        self._closed = True
        self._directories.clear()

    def __enter__(self) -> PolledWatcher:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()
