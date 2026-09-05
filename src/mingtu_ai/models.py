"""Typed data exchanged by adapters, providers, and the application shell."""

from __future__ import annotations

import math
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator


class AnalysisInput(BaseModel):
    """A local adapter result ready to send to the model provider.

    ``image_paths`` are read only while :meth:`LocalGemmaProvider.analyze` is
    executing. ``image_file_descriptors`` aligns with those paths and lets the
    provider read the exact descriptor-bound inode selected by the adapter;
    ``None`` means the provider must securely open that derived path itself.
    """

    model_config = ConfigDict(extra="forbid", arbitrary_types_allowed=True)

    source_path: Path
    mime_type: str = Field(min_length=1, max_length=255)
    kind: str = Field(min_length=1, max_length=80)
    text: str | None = None
    image_paths: tuple[Path, ...] = ()
    image_file_descriptors: tuple[int | None, ...] = ()
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("metadata")
    @classmethod
    def metadata_must_be_json_compatible(cls, value: dict[str, Any]) -> dict[str, Any]:
        def check(item: Any) -> None:
            if isinstance(item, float) and not math.isfinite(item):
                raise ValueError("metadata numbers must be finite")
            if item is None or isinstance(item, str | int | float | bool):
                return
            if isinstance(item, list | tuple):
                for child in item:
                    check(child)
                return
            if isinstance(item, dict):
                for key, child in item.items():
                    if not isinstance(key, str):
                        raise ValueError("metadata object keys must be strings")
                    check(child)
                return
            raise ValueError("metadata values must be JSON-compatible")

        check(value)
        return value


class NamingSuggestion(BaseModel):
    """The complete, strict JSON contract returned by the naming model."""

    model_config = ConfigDict(extra="forbid", strict=True)

    suggested_stem: str = Field(min_length=1, max_length=120)
    detected_language: str = Field(
        min_length=2,
        max_length=35,
        pattern=r"^[A-Za-z]{2,3}(?:-[A-Za-z0-9]{2,8})*$",
    )
    category: str = Field(min_length=1, max_length=80, pattern=r"^[a-z0-9_]+$")
    keywords: list[str] = Field(min_length=1, max_length=12)
    confidence: float = Field(ge=0.0, le=1.0)

    @field_validator("suggested_stem")
    @classmethod
    def stem_must_not_contain_paths_or_controls(cls, value: str) -> str:
        if value != value.strip():
            raise ValueError("suggested_stem must not have surrounding whitespace")
        if any(character in value for character in ("/", "\\", "\x00")):
            raise ValueError("suggested_stem must not contain path separators")
        if any(ord(character) < 32 or ord(character) == 127 for character in value):
            raise ValueError("suggested_stem must not contain control characters")
        if value in {".", ".."}:
            raise ValueError("suggested_stem must be a filename stem")
        return value

    @field_validator("keywords")
    @classmethod
    def keywords_are_clean_and_unique(cls, values: list[str]) -> list[str]:
        if any(not value.strip() or value != value.strip() for value in values):
            raise ValueError("keywords must be non-empty without surrounding whitespace")
        if len(set(values)) != len(values):
            raise ValueError("keywords must be unique")
        return values
