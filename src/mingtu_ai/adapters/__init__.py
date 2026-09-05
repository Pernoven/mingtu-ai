"""Bounded local file-understanding adapters."""

from .models import AdaptedFile, AdapterError, AdapterKind, AdapterLimits
from .registry import FileAdapterRegistry, adapt_file, detect_mime_type

__all__ = [
    "AdaptedFile",
    "AdapterError",
    "AdapterKind",
    "AdapterLimits",
    "FileAdapterRegistry",
    "adapt_file",
    "detect_mime_type",
]
