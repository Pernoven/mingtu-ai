"""Race-resistant primitives for reading untrusted local input files.

The pathname is resolved exactly once with ``O_NOFOLLOW`` when the platform
provides it.  All content reads and parser subprocesses then use the resulting
regular-file descriptor, so replacing the directory entry cannot redirect an
in-flight analysis to another file.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
import errno
import os
from pathlib import Path
import stat
from typing import BinaryIO

from .models import AdapterError, FileChangedError


FileIdentity = tuple[int, int, int, int]


def identity_from_stat(info: os.stat_result) -> FileIdentity:
    """Return the stable fields used to detect replacement or modification."""

    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns)


@dataclass(frozen=True, slots=True)
class OpenedRegularFile:
    """An owned descriptor bound to one regular file for an adapter scope."""

    path: Path
    fd: int
    identity: FileIdentity

    def current_identity(self) -> FileIdentity:
        """Read the identity of the file still referenced by this descriptor."""

        return identity_from_stat(os.fstat(self.fd))

    def proc_path(self) -> str:
        """Return a child-visible Linux path for this inherited descriptor."""

        proc_directory = Path("/proc/self/fd")
        if not proc_directory.is_dir():
            raise AdapterError(
                "secure external parsing requires Linux /proc/self/fd support"
            )
        return f"/proc/self/fd/{self.fd}"

    def path_matches_identity(self) -> bool:
        """Return whether the directory entry still names this exact file."""

        try:
            info = self.path.stat(follow_symlinks=False)
        except OSError:
            return False
        return stat.S_ISREG(info.st_mode) and identity_from_stat(info) == self.identity


class BoundedReadProxy:
    """Proxy a seekable file while rejecting any single unbounded library read.

    ZIP readers request their complete central directory in one read. Capping
    that request before allocation bounds parser metadata memory while member
    payloads retain their separate adapter limits.
    """

    def __init__(self, stream: BinaryIO, max_read_bytes: int) -> None:
        if max_read_bytes <= 0:
            raise ValueError("max_read_bytes must be positive")
        self._stream = stream
        self._max_read_bytes = max_read_bytes

    def read(self, size: int = -1) -> bytes:
        """Read no more than the configured per-call allocation boundary."""

        if size < 0 or size > self._max_read_bytes:
            raise OSError("container parser requested an oversized read")
        return self._stream.read(size)

    def seek(self, offset: int, whence: int = os.SEEK_SET) -> int:
        """Delegate bounded-parser seeking to the descriptor-backed stream."""

        return self._stream.seek(offset, whence)

    def tell(self) -> int:
        """Return the current parser stream offset."""

        return self._stream.tell()

    def seekable(self) -> bool:
        """Report the seek capability required by ZIP and TAR parsers."""

        return self._stream.seekable()

    def readable(self) -> bool:
        """Report the read capability required by ZIP and TAR parsers."""

        return self._stream.readable()


@contextmanager
def open_regular_file(path: Path | str) -> Iterator[OpenedRegularFile]:
    """Open *path* without following a final symlink and require a regular file.

    On platforms without ``O_NOFOLLOW``, a pre-open ``lstat`` plus post-open
    descriptor identity comparison is used.  That fallback detects ordinary
    swaps but cannot close every hostile same-user race, so Mingtu's supported
    Linux deployment should retain ``O_NOFOLLOW`` support.
    """

    source = Path(path)
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    before: os.stat_result | None = None
    if nofollow:
        flags |= nofollow
    else:
        try:
            before = source.lstat()
        except FileNotFoundError as error:
            raise FileChangedError(f"cannot inspect file: {error}") from error
        except OSError as error:
            raise AdapterError(f"cannot inspect file: {error}") from error
        if not stat.S_ISREG(before.st_mode):
            raise AdapterError("adapters accept regular files only")

    try:
        descriptor = os.open(source, flags)
    except FileNotFoundError as error:
        raise FileChangedError(f"cannot open file: {error}") from error
    except OSError as error:
        if error.errno == errno.ELOOP:
            raise AdapterError("adapters do not follow symbolic links") from error
        raise AdapterError(f"cannot open file: {error}") from error

    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode):
            raise AdapterError("adapters accept regular files only")
        identity = identity_from_stat(info)
        if before is not None and identity_from_stat(before) != identity:
            raise FileChangedError("file changed while it was being opened")
        yield OpenedRegularFile(source, descriptor, identity)
    finally:
        os.close(descriptor)


def read_bounded(descriptor: int, limit: int) -> tuple[bytes, bool]:
    """Read at most *limit* bytes plus one overflow sentinel from a descriptor."""

    if limit < 0:
        raise ValueError("limit must be non-negative")
    payload = bytearray()
    offset = 0
    target = limit + 1
    while len(payload) < target:
        amount = min(64 * 1024, target - len(payload))
        try:
            chunk = os.pread(descriptor, amount, offset)
        except InterruptedError:
            continue
        if not chunk:
            break
        payload.extend(chunk)
        offset += len(chunk)
    truncated = len(payload) > limit
    if truncated:
        del payload[limit:]
    return bytes(payload), truncated


@contextmanager
def duplicated_binary_stream(descriptor: int) -> Iterator[BinaryIO]:
    """Yield an independent binary stream for libraries that require file objects."""

    duplicate = os.dup(descriptor)
    with os.fdopen(duplicate, "rb", closefd=True) as stream:
        yield stream
