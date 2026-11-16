"""An optional process-lifetime lock for the Linux desktop resident."""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from contextlib import contextmanager
import errno
import os
from pathlib import Path
import stat
import sys


class ResidentAlreadyRunning(RuntimeError):
    pass


@contextmanager
def resident_lock(environ: Mapping[str, str]) -> Iterator[None]:
    if not sys.platform.startswith("linux"):
        yield
        return
    import fcntl

    home = Path(environ.get("HOME", str(Path.home())))
    state = Path(environ.get("XDG_STATE_HOME", str(home / ".local/state")))
    directory = state / "mingtu-ai"
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    if directory.is_symlink() or not directory.is_dir():
        raise ValueError(f"invalid resident state directory: {directory}")
    # Keep this inode after exit: unlinking a lock file allows two owners of
    # different inodes. The kernel releases flock even after an unclean exit.
    descriptor = os.open(directory / "resident.lock", os.O_CREAT | os.O_RDWR | os.O_CLOEXEC | os.O_NOFOLLOW, 0o600)
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
            raise ValueError("resident lock must be a regular file owned by this user")
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            if exc.errno in (errno.EACCES, errno.EAGAIN):
                raise ResidentAlreadyRunning("命圖 AI 監視程式已在執行。") from exc
            raise
        os.ftruncate(descriptor, 0)
        os.write(descriptor, f"{os.getpid()}\n".encode("ascii"))
        yield
    finally:
        os.close(descriptor)
