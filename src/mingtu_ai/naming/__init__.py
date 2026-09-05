"""Safe filename generation and conflict-free rename operations."""

from .files import (
    AtomicRenameUnavailableError,
    RenameError,
    numbered_destination,
    rename_noreplace,
    rename_unique,
    unique_destination,
)
from .prompt import GENERIC_FILENAME_RE, build_naming_prompt
from .sanitize import preserved_suffix, sanitize_stem

__all__ = [
    "AtomicRenameUnavailableError",
    "GENERIC_FILENAME_RE",
    "RenameError",
    "build_naming_prompt",
    "numbered_destination",
    "rename_noreplace",
    "preserved_suffix",
    "rename_unique",
    "sanitize_stem",
    "unique_destination",
]
