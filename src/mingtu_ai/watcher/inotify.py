"""Small stdlib-only wrapper around Linux inotify."""

from __future__ import annotations

from collections.abc import Iterable, Iterator
import ctypes
from dataclasses import dataclass
import errno
import os
from pathlib import Path
import select
import struct
import sys
from types import TracebackType

from .policy import EventDeduplicator, OwnedMoveSuppressor, should_ignore, wait_until_stable


IN_CLOSE_WRITE = 0x00000008
IN_MOVED_FROM = 0x00000040
IN_MOVED_TO = 0x00000080
IN_Q_OVERFLOW = 0x00004000
IN_IGNORED = 0x00008000
IN_ISDIR = 0x40000000
IN_ONLYDIR = 0x01000000
IN_DONT_FOLLOW = 0x02000000
IN_EXCL_UNLINK = 0x04000000

WATCH_MASK = (
    IN_CLOSE_WRITE
    | IN_MOVED_FROM
    | IN_MOVED_TO
    | IN_ONLYDIR
    | IN_DONT_FOLLOW
    | IN_EXCL_UNLINK
)
_EVENT_HEADER = struct.Struct("=iIII")


class WatchOverflowError(RuntimeError):
    """Raised when the kernel reports an inotify queue overflow."""


class WatchInvalidatedError(RuntimeError):
    """Raised when the kernel removes a configured directory watch."""


class InotifyUnavailableError(RuntimeError):
    """Raised when this process cannot initialize Linux inotify."""


class _InotifyApi:
    def __init__(self) -> None:
        if not sys.platform.startswith("linux"):
            raise InotifyUnavailableError("inotify is available only on Linux")
        libc = ctypes.CDLL(None, use_errno=True)
        self._init1 = libc.inotify_init1
        self._init1.argtypes = [ctypes.c_int]
        self._init1.restype = ctypes.c_int
        self._add_watch = libc.inotify_add_watch
        self._add_watch.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_uint32]
        self._add_watch.restype = ctypes.c_int
        self._rm_watch = libc.inotify_rm_watch
        self._rm_watch.argtypes = [ctypes.c_int, ctypes.c_int]
        self._rm_watch.restype = ctypes.c_int

    def init(self) -> int:
        fd = self._init1(os.O_NONBLOCK | os.O_CLOEXEC)
        if fd < 0:
            error = ctypes.get_errno()
            raise InotifyUnavailableError(os.strerror(error))
        return fd

    def add_watch(self, fd: int, path: Path) -> int:
        wd = self._add_watch(fd, os.fsencode(path), WATCH_MASK)
        if wd < 0:
            error = ctypes.get_errno()
            raise OSError(error, os.strerror(error), path)
        return wd

    def remove_watch(self, fd: int, wd: int) -> None:
        if self._rm_watch(fd, wd) < 0:
            error = ctypes.get_errno()
            if error not in (errno.EBADF, errno.EINVAL):
                raise OSError(error, os.strerror(error))


@dataclass(frozen=True, slots=True)
class InotifyEvent:
    watch_directory: Path | None
    name: str
    mask: int
    cookie: int

    @property
    def path(self) -> Path | None:
        if self.watch_directory is None or not self.name:
            return None
        return self.watch_directory / self.name

    @property
    def is_ready_file(self) -> bool:
        return not self.mask & IN_ISDIR and bool(self.mask & (IN_CLOSE_WRITE | IN_MOVED_TO))


class InotifyWatcher:
    """Monitor one or more directories for completed or moved-in files."""

    def __init__(self, directories: Iterable[Path | str]) -> None:
        self._api = _InotifyApi()
        self._fd = self._api.init()
        self._watches: dict[int, Path] = {}
        try:
            for directory in directories:
                self.add_directory(directory)
        except BaseException:
            self.close()
            raise
        if not self._watches:
            self.close()
            raise ValueError("at least one watch directory is required")

    def add_directory(self, directory: Path | str) -> None:
        candidate = Path(directory).expanduser().absolute()
        if candidate.is_symlink() or not candidate.is_dir():
            raise NotADirectoryError(candidate)
        wd = self._api.add_watch(self._require_fd(), candidate)
        self._watches[wd] = candidate

    def remove_directory(self, directory: Path | str) -> None:
        candidate = Path(directory).expanduser().absolute()
        removed_wds = [wd for wd, path in self._watches.items() if path == candidate]
        if not removed_wds:
            raise KeyError(f"directory is not currently watched: {candidate}")
        fd = self._require_fd()
        for wd in removed_wds:
            try:
                self._api.remove_watch(fd, wd)
            except OSError:
                pass
            self._watches.pop(wd, None)

    def fileno(self) -> int:
        return self._require_fd()

    def read_events(self, timeout: float | None = None) -> list[InotifyEvent]:
        fd = self._require_fd()
        if timeout is not None and timeout < 0:
            raise ValueError("timeout must be non-negative or None")
        readable, _, _ = select.select([fd], [], [], timeout)
        if not readable:
            return []

        payload = bytearray()
        while True:
            try:
                chunk = os.read(fd, 64 * 1024)
            except BlockingIOError:
                break
            if not chunk:
                break
            payload.extend(chunk)
            if len(chunk) < 64 * 1024:
                break

        events: list[InotifyEvent] = []
        offset = 0
        while offset + _EVENT_HEADER.size <= len(payload):
            wd, mask, cookie, name_length = _EVENT_HEADER.unpack_from(payload, offset)
            offset += _EVENT_HEADER.size
            end = offset + name_length
            if end > len(payload):
                break
            raw_name = bytes(payload[offset:end]).split(b"\0", 1)[0]
            offset = end
            event = InotifyEvent(
                watch_directory=self._watches.get(wd),
                name=os.fsdecode(raw_name),
                mask=mask,
                cookie=cookie,
            )
            if mask & IN_IGNORED:
                self._watches.pop(wd, None)
            events.append(event)
        return events

    def iter_events(self, *, timeout: float | None = None) -> Iterator[InotifyEvent]:
        while self._fd is not None:
            yield from self.read_events(timeout)

    def close(self) -> None:
        if self._fd is None:
            return
        fd = self._fd
        self._fd = None
        watches = tuple(self._watches)
        self._watches.clear()
        try:
            for wd in watches:
                self._api.remove_watch(fd, wd)
        finally:
            os.close(fd)

    def _require_fd(self) -> int:
        if self._fd is None:
            raise ValueError("inotify watcher is closed")
        return self._fd

    def __enter__(self) -> InotifyWatcher:
        self._require_fd()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()


class FileEventMonitor:
    """Turn low-level events into stable, unique, eligible file paths."""

    def __init__(
        self,
        watcher: InotifyWatcher,
        *,
        stable_interval: float = 0.15,
        stable_rounds: int = 2,
        stable_timeout: float = 5.0,
        deduplicator: EventDeduplicator | None = None,
        owned_moves: OwnedMoveSuppressor | None = None,
    ) -> None:
        self.watcher = watcher
        self.stable_interval = stable_interval
        self.stable_rounds = stable_rounds
        self.stable_timeout = stable_timeout
        self.deduplicator = deduplicator or EventDeduplicator()
        self.owned_moves = owned_moves or OwnedMoveSuppressor()

    def poll(self, timeout: float | None = None) -> list[Path]:
        ready: list[Path] = []
        for event in self.watcher.read_events(timeout):
            if event.mask & IN_Q_OVERFLOW:
                self.owned_moves.clear()
                raise WatchOverflowError("inotify event queue overflowed")
            if event.mask & IN_IGNORED:
                self.owned_moves.clear()
                directory = event.watch_directory
                detail = f": {directory}" if directory is not None else ""
                raise WatchInvalidatedError(f"inotify directory watch was removed{detail}")
            path = event.path
            if event.mask & IN_MOVED_FROM:
                if path is not None and not event.mask & IN_ISDIR:
                    self.owned_moves.observe_moved_from(path, event.cookie)
                continue
            if (
                event.mask & IN_MOVED_TO
                and path is not None
                and self.owned_moves.consume_moved_to(path, event.cookie)
            ):
                continue
            if not event.is_ready_file or path is None or should_ignore(path):
                continue
            if not wait_until_stable(
                path,
                interval=self.stable_interval,
                stable_rounds=self.stable_rounds,
                timeout=self.stable_timeout,
            ):
                continue
            if self.deduplicator.is_duplicate(path):
                continue
            ready.append(path)
        return ready
