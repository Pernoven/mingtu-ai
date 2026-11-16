"""Public data contract shared by adapters and the inference coordinator."""

from __future__ import annotations

from dataclasses import dataclass, field, fields
from enum import StrEnum
from pathlib import Path
from typing import Any


class AdapterKind(StrEnum):
    IMAGE = "image"
    TEXT = "text"
    PDF = "pdf"
    OFFICE = "office"
    VIDEO = "video"
    AUDIO = "audio"
    ARCHIVE = "archive"
    METADATA = "metadata"


@dataclass(frozen=True, slots=True)
class AdapterLimits:
    max_input_bytes: int = 256 * 1024
    max_text_chars: int = 16_000
    max_pdf_pages: int = 12
    max_archive_members: int = 200
    max_member_name_chars: int = 300
    max_office_xml_members: int = 128
    max_office_xml_bytes: int = 2 * 1024 * 1024
    max_office_total_xml_bytes: int = 8 * 1024 * 1024
    max_container_index_bytes: int = 2 * 1024 * 1024
    max_zip_compression_ratio: int = 200
    command_timeout: float = 30.0

    def __post_init__(self) -> None:
        for definition in fields(self):
            name = definition.name
            value = getattr(self, name)
            if value <= 0:
                raise ValueError(f"{name} must be positive")


@dataclass(frozen=True, slots=True)
class AdaptedFile:
    """Content made safe and small enough for a local model request.

    Paths in ``image_paths`` that point into a temporary directory are valid
    only while the ``adapt_file`` context manager is active. Non-``None``
    ``image_file_descriptors`` are borrowed and have the same lifetime; each
    entry aligns with the image path at the same index.
    """

    source_path: Path
    mime_type: str
    kind: AdapterKind
    text: str | None = None
    image_paths: tuple[Path, ...] = ()
    metadata: dict[str, Any] = field(default_factory=dict)
    warnings: tuple[str, ...] = ()
    source_identity: tuple[int, int, int, int] | None = None
    image_file_descriptors: tuple[int | None, ...] = ()
    unreadable_office_container: bool = False


class AdapterError(RuntimeError):
    """A local input could not be adapted within the security boundaries."""


class FileChangedError(AdapterError):
    """The input snapshot became invalid before analysis finished."""
