"""Non-overwriting rename primitives for the Linux target platform."""

from __future__ import annotations

import ctypes
import errno
import os
from pathlib import Path

from .sanitize import preserved_suffix, sanitize_stem


AT_FDCWD = -100
RENAME_NOREPLACE = 1


class RenameError(RuntimeError):
    """Raised when a requested safe rename cannot be completed."""


class AtomicRenameUnavailableError(RenameError):
    """The host cannot provide Linux ``renameat2(RENAME_NOREPLACE)``."""


def unique_destination(source: Path, suggested_stem: str, *, index: int = 1) -> Path:
    """Build one candidate path without claiming it.

    Callers that actually rename a file should use :func:`rename_unique`, whose
    no-replace operation protects against races.
    """

    if index < 1:
        raise ValueError("index must be at least 1")
    suffix = preserved_suffix(source)
    original_stem = source.name[: -len(suffix)] if suffix else source.name
    safe_stem = sanitize_stem(suggested_stem, fallback=original_stem)
    return numbered_destination(source, f"{safe_stem}{suffix}", index=index)


def numbered_destination(
    source: Path, requested_filename: str, *, index: int = 1,
) -> Path:
    """Build a byte-bounded collision candidate for ``requested_filename``.

    The counter is inserted before a known compound suffix (or the ordinary
    final suffix). Space for both is reserved before the stem is truncated, so
    advancing from the previewed name to ``_2`` cannot exceed ``PC_NAME_MAX``.
    """

    if index < 1:
        raise ValueError("index must be at least 1")
    requested = Path(requested_filename)
    suffix = preserved_suffix(requested)
    stem = requested.name[: -len(suffix)] if suffix else requested.name
    counter = "" if index == 1 else f"_{index}"
    try:
        name_max = os.pathconf(source.parent, "PC_NAME_MAX")
    except OSError:
        name_max = 255
    if name_max < 1:
        name_max = 255
    stem_budget = name_max - len(os.fsencode(counter + suffix))
    stem = _truncate_to_byte_limit(stem, stem_budget)
    if not stem:
        raise RenameError("file extension leaves no room for a safe stem")
    return source.with_name(f"{stem}{counter}{suffix}")


def _truncate_to_byte_limit(value: str, byte_limit: int) -> str:
    if byte_limit < 1:
        return ""
    if len(os.fsencode(value)) <= byte_limit:
        return value

    lower = 0
    upper = len(value)
    while lower < upper:
        midpoint = (lower + upper + 1) // 2
        if len(os.fsencode(value[:midpoint])) <= byte_limit:
            lower = midpoint
        else:
            upper = midpoint - 1
    return value[:lower].rstrip("_")


def rename_noreplace(source: Path | str, destination: Path | str) -> Path:
    """Atomically rename one path without replacing an existing destination.

    This is deliberately Linux-only and fail-closed.  A link/unlink fallback
    has an unavoidable identity-check-to-unlink race, so callers handling user
    files must receive a clear error when ``renameat2(RENAME_NOREPLACE)`` is not
    available instead of silently weakening the operation.
    """

    source = Path(source)
    destination = Path(destination)
    libc = ctypes.CDLL(None, use_errno=True)
    renameat2 = getattr(libc, "renameat2", None)
    if renameat2 is None:
        raise AtomicRenameUnavailableError(
            "atomic no-replace rename is unavailable on this Linux host"
        )

    renameat2.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
    renameat2.restype = ctypes.c_int
    result = renameat2(
        AT_FDCWD,
        os.fsencode(source),
        AT_FDCWD,
        os.fsencode(destination),
        RENAME_NOREPLACE,
    )
    if result == 0:
        return destination

    error_number = ctypes.get_errno()
    if error_number in {errno.ENOSYS, errno.EINVAL}:
        raise AtomicRenameUnavailableError(
            "filesystem does not support atomic no-replace rename"
        )
    raise OSError(error_number, os.strerror(error_number), destination)


def rename_unique(source: Path, suggested_stem: str) -> Path:
    """Atomically rename ``source`` without ever replacing another path.

    Conflicts are retried as ``_2``, ``_3``, and so on. Linux ``renameat2``
    with ``RENAME_NOREPLACE`` performs the claim and rename in one syscall.
    """

    source = Path(source)
    if not source.exists():
        raise FileNotFoundError(source)
    if source.is_symlink() or not source.is_file():
        raise RenameError("only regular, non-symlink files may be renamed")

    for index in range(1, 1_000_000):
        destination = unique_destination(source, suggested_stem, index=index)
        if destination == source:
            return source
        try:
            rename_noreplace(source, destination)
        except FileExistsError:
            continue
        except OSError as exc:
            raise RenameError(f"could not safely rename {source.name}") from exc
        return destination
    raise RenameError("could not find an available filename")
