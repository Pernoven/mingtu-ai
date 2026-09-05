"""Policy helpers used after receiving an inotify event."""

from __future__ import annotations

from collections import OrderedDict
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
import os
from pathlib import Path
import stat
import threading
import time


_INCOMPLETE_SUFFIXES = (
    ".crdownload",
    ".download",
    ".filepart",
    ".part",
    ".partial",
    ".temp",
    ".tmp",
)
_LOCK_SUFFIXES = (".lock", ".lck", ".swp", ".swo")


def should_ignore(path: Path | str) -> bool:
    """Return whether *path* is ineligible for content analysis.

    This deliberately uses ``lstat`` so a symlink to a regular file never slips
    through the regular-file check.
    """

    candidate = Path(path)
    name = candidate.name
    lower_name = name.casefold()

    if not name or name.startswith(".") or name.endswith("~"):
        return True
    if name.startswith("#") and name.endswith("#"):
        return True
    if lower_name.endswith(_INCOMPLETE_SUFFIXES + _LOCK_SUFFIXES):
        return True

    try:
        mode = candidate.lstat().st_mode
    except (FileNotFoundError, OSError):
        return True
    return not stat.S_ISREG(mode)


def _snapshot(path: Path) -> tuple[int, int, int, int] | None:
    try:
        info = path.lstat()
    except (FileNotFoundError, OSError):
        return None
    if not stat.S_ISREG(info.st_mode):
        return None
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns)


def wait_until_stable(
    path: Path | str,
    *,
    interval: float = 0.15,
    stable_rounds: int = 2,
    timeout: float = 5.0,
    sleeper: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> bool:
    """Wait until size and modification time remain unchanged.

    A close-write event is a strong signal, but moved files and filesystem
    producers can still race with consumers.  Requiring repeated identical
    snapshots gives adapters a bounded, deterministic hand-off point.
    """

    if stable_rounds < 1:
        raise ValueError("stable_rounds must be at least 1")
    if interval < 0 or timeout < 0:
        raise ValueError("interval and timeout must be non-negative")

    candidate = Path(path)
    deadline = clock() + timeout
    previous: tuple[int, int, int, int] | None = None
    unchanged = 0

    while True:
        current = _snapshot(candidate)
        if current is None:
            return False
        if current == previous:
            unchanged += 1
            if unchanged >= stable_rounds:
                return True
        else:
            unchanged = 0
            previous = current

        now = clock()
        if now >= deadline:
            return False
        sleeper(min(interval, max(0.0, deadline - now)))


class EventDeduplicator:
    """Bounded TTL cache for identical filesystem snapshots."""

    def __init__(self, *, ttl: float = 2.0, capacity: int = 2048) -> None:
        if ttl < 0:
            raise ValueError("ttl must be non-negative")
        if capacity < 1:
            raise ValueError("capacity must be positive")
        self._ttl = ttl
        self._capacity = capacity
        self._seen: OrderedDict[tuple[str, int, int, int, int], float] = OrderedDict()

    def is_duplicate(
        self,
        path: Path | str,
        *,
        now: float | None = None,
    ) -> bool:
        candidate = Path(path)
        snapshot = _snapshot(candidate)
        if snapshot is None:
            return True

        timestamp = time.monotonic() if now is None else now
        self._prune(timestamp)
        key = (os.fspath(candidate.absolute()), *snapshot)
        previous = self._seen.get(key)
        self._seen[key] = timestamp
        self._seen.move_to_end(key)
        while len(self._seen) > self._capacity:
            self._seen.popitem(last=False)
        return previous is not None and timestamp - previous <= self._ttl

    def _prune(self, now: float) -> None:
        cutoff = now - self._ttl
        while self._seen:
            _key, seen_at = next(iter(self._seen.items()))
            if seen_at >= cutoff:
                break
            self._seen.popitem(last=False)


@dataclass(frozen=True, slots=True)
class _OwnedMove:
    source: str
    destination: str
    identity: tuple[int, int, int, int]
    registered_at: float


class OwnedMoveSuppressor:
    """Suppress only the ``IN_MOVED_TO`` event created by an owned rename.

    A destination path or inode alone is not enough evidence: either can be
    reused by a later user operation.  The suppressor therefore requires the
    matching ``IN_MOVED_FROM`` cookie, source path, destination path, and file
    identity.  Registrations and cookie associations are bounded by both a TTL
    and a fixed capacity.

    Callers must hold :meth:`move_guard` across their rename and subsequent
    :meth:`register` call.  Event handling uses the same lock, closing the race
    where inotify becomes readable before the rename callback can register the
    successful move.
    """

    def __init__(
        self,
        *,
        ttl: float = 10.0,
        capacity: int = 256,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if ttl < 0:
            raise ValueError("ttl must be non-negative")
        if capacity < 1:
            raise ValueError("capacity must be positive")
        self._ttl = ttl
        self._capacity = capacity
        self._clock = clock
        self._lock = threading.RLock()
        self._next_id = 1
        self._moves: OrderedDict[int, _OwnedMove] = OrderedDict()
        self._cookies: OrderedDict[int, int] = OrderedDict()

    @contextmanager
    def move_guard(self) -> Iterator[None]:
        """Serialize one owned move with event-to-registration matching."""

        with self._lock:
            yield

    def register(self, source: Path | str, destination: Path | str) -> bool:
        """Register a completed owned move, returning false if it vanished."""

        source_key = os.fspath(Path(source).absolute())
        destination_path = Path(destination).absolute()
        identity = _snapshot(destination_path)
        if identity is None:
            return False
        with self._lock:
            now = self._clock()
            self._prune_locked(now)
            move_id = self._next_id
            self._next_id += 1
            self._moves[move_id] = _OwnedMove(
                source=source_key,
                destination=os.fspath(destination_path),
                identity=identity,
                registered_at=now,
            )
            self._trim_locked()
        return True

    def observe_moved_from(self, source: Path | str, cookie: int) -> None:
        """Associate an inotify move cookie with an exact owned source path."""

        if cookie == 0:
            return
        source_key = os.fspath(Path(source).absolute())
        with self._lock:
            self._prune_locked(self._clock())
            already_assigned = set(self._cookies.values())
            for move_id, move in self._moves.items():
                if move_id not in already_assigned and move.source == source_key:
                    self._cookies[cookie] = move_id
                    self._cookies.move_to_end(cookie)
                    self._trim_locked()
                    return

    def consume_moved_to(self, destination: Path | str, cookie: int) -> bool:
        """Return true exactly once for a matching owned destination event."""

        if cookie == 0:
            return False
        destination_path = Path(destination).absolute()
        with self._lock:
            self._prune_locked(self._clock())
            move_id = self._cookies.pop(cookie, None)
            if move_id is None:
                return False
            move = self._moves.pop(move_id, None)
            if move is None:
                return False
            return (
                move.destination == os.fspath(destination_path)
                and _snapshot(destination_path) == move.identity
            )

    def clear(self) -> None:
        """Discard correlations after an inotify overflow or watcher reset."""

        with self._lock:
            self._moves.clear()
            self._cookies.clear()

    def _prune_locked(self, now: float) -> None:
        cutoff = now - self._ttl
        expired: set[int] = set()
        while self._moves:
            move_id, move = next(iter(self._moves.items()))
            if move.registered_at >= cutoff:
                break
            self._moves.popitem(last=False)
            expired.add(move_id)
        if expired:
            self._cookies = OrderedDict(
                (cookie, move_id)
                for cookie, move_id in self._cookies.items()
                if move_id not in expired
            )

    def _trim_locked(self) -> None:
        removed: set[int] = set()
        while len(self._moves) > self._capacity:
            move_id, _move = self._moves.popitem(last=False)
            removed.add(move_id)
        if removed:
            self._cookies = OrderedDict(
                (cookie, move_id)
                for cookie, move_id in self._cookies.items()
                if move_id not in removed
            )
        while len(self._cookies) > self._capacity:
            _cookie, move_id = self._cookies.popitem(last=False)
            self._moves.pop(move_id, None)
