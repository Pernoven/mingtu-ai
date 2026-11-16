"""Bounded local file-understanding adapters."""

from .models import AdaptedFile, AdapterError, AdapterKind, AdapterLimits, FileChangedError
from .registry import FileAdapterRegistry, adapt_file, detect_mime_type

__all__ = [
    "AdaptedFile",
    "AdapterError",
    "AdapterKind",
    "AdapterLimits",
    "FileChangedError",
    "FileAdapterRegistry",
    "adapt_file",
    "detect_mime_type",
]
