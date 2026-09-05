"""Unicode-aware filename sanitization."""

from __future__ import annotations

import unicodedata
from pathlib import Path


_SAFE_SYMBOLS = frozenset({"+", "#"})
_COMPOUND_SUFFIXES = (
    ".tar.gz",
    ".tar.bz2",
    ".tar.xz",
    ".tar.zst",
    ".nii.gz",
)


def sanitize_stem(value: str, *, fallback: str = "untitled", max_chars: int = 120) -> str:
    """Return a safe, searchable filename stem.

    Letters, numbers, combining marks, ``+`` and ``#`` are retained. Runs of
    whitespace, path separators, punctuation, controls, and other symbols are
    collapsed to a single underscore. The result never starts with a dot.
    """

    if max_chars < 1:
        raise ValueError("max_chars must be positive")

    normalized = unicodedata.normalize("NFKC", value)
    output: list[str] = []
    separator_pending = False

    for character in normalized:
        category = unicodedata.category(character)
        if category[0] in {"L", "N", "M"} or character in _SAFE_SYMBOLS:
            if separator_pending and output and output[-1] != "_":
                output.append("_")
            output.append(character)
            separator_pending = False
        elif character == "_":
            separator_pending = bool(output)
        else:
            separator_pending = bool(output)

    result = "".join(output).strip("_")
    if not result:
        if fallback == value:
            fallback = "untitled"
        return sanitize_stem(fallback, fallback="untitled", max_chars=max_chars)

    result = result[:max_chars].rstrip("_")
    return result or "untitled"


def preserved_suffix(path: Path) -> str:
    """Return the original extension, including known compound extensions."""

    lowercase_name = path.name.lower()
    for suffix in _COMPOUND_SUFFIXES:
        if lowercase_name.endswith(suffix):
            return path.name[-len(suffix) :]
    return path.suffix
