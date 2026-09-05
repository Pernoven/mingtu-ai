"""Strict, path-safe JSON manifest loading for benchmark datasets."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import re
import stat
from typing import Any


SCHEMA_VERSION = 1
MAX_MANIFEST_BYTES = 1024 * 1024
_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,79}$")
_MANUAL_SCORE_KEYS = frozenset(
    {"content_understanding", "stem_quality", "filename_searchability", "ocr_quality"}
)
_ROOT_KEYS = frozenset(
    {"schema_version", "benchmark_id", "model_id", "description", "cases"}
)
_CASE_KEYS = frozenset(
    {
        "id",
        "file",
        "expected_terms",
        "expected_stem_tokens",
        "expected_language",
        "expected_category",
        "expected_adapter_kind",
        "manual_scores",
        "manual_notes",
        "tags",
    }
)


class ManifestValidationError(ValueError):
    """One or more manifest fields or dataset paths are invalid."""

    def __init__(self, errors: list[str] | tuple[str, ...]) -> None:
        self.errors = tuple(errors)
        detail = "\n".join(f"- {item}" for item in self.errors)
        super().__init__(f"benchmark manifest is invalid:\n{detail}")


@dataclass(frozen=True, slots=True)
class BenchmarkCase:
    id: str
    relative_file: Path
    source_path: Path
    expected_terms: tuple[str, ...] = ()
    expected_stem_tokens: tuple[str, ...] = ()
    expected_language: str | None = None
    expected_category: str | None = None
    expected_adapter_kind: str | None = None
    manual_scores: dict[str, float] | None = None
    manual_notes: str | None = None
    tags: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class BenchmarkManifest:
    source_path: Path
    sha256: str
    schema_version: int
    benchmark_id: str
    model_id: str | None
    description: str
    cases: tuple[BenchmarkCase, ...]


def _string(
    value: Any,
    field: str,
    errors: list[str],
    *,
    required: bool = False,
    max_length: int = 500,
) -> str | None:
    if value is None and not required:
        return None
    if not isinstance(value, str) or not value.strip():
        errors.append(f"{field} must be a non-empty string")
        return None
    if value != value.strip():
        errors.append(f"{field} must not have surrounding whitespace")
    if len(value) > max_length:
        errors.append(f"{field} exceeds {max_length} characters")
    return value.strip()[:max_length]


def _string_list(value: Any, field: str, errors: list[str]) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list):
        errors.append(f"{field} must be an array of strings")
        return ()
    result: list[str] = []
    for index, item in enumerate(value):
        parsed = _string(item, f"{field}[{index}]", errors, required=True, max_length=120)
        if parsed is not None:
            result.append(parsed)
    if len(result) != len(set(result)):
        errors.append(f"{field} must not contain duplicates")
    if len(result) > 30:
        errors.append(f"{field} may contain at most 30 values")
    return tuple(dict.fromkeys(result[:30]))


def _manual_scores(value: Any, field: str, errors: list[str]) -> dict[str, float] | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        errors.append(f"{field} must be an object")
        return None
    unknown = sorted(set(value) - _MANUAL_SCORE_KEYS)
    if unknown:
        errors.append(f"{field} contains unknown keys: {', '.join(unknown)}")
    parsed: dict[str, float] = {}
    for key in _MANUAL_SCORE_KEYS:
        score = value.get(key)
        if score is None:
            continue
        if isinstance(score, bool) or not isinstance(score, int | float):
            errors.append(f"{field}.{key} must be a number from 0 to 5")
            continue
        numeric = float(score)
        if not 0.0 <= numeric <= 5.0:
            errors.append(f"{field}.{key} must be from 0 to 5")
            continue
        parsed[key] = numeric
    return parsed


def load_manifest(path: Path | str) -> BenchmarkManifest:
    """Load and validate a manifest and all referenced regular files."""

    source = Path(path).expanduser().absolute()
    try:
        info = source.lstat()
    except OSError as error:
        raise ManifestValidationError([f"cannot inspect manifest: {type(error).__name__}"]) from error
    if not stat.S_ISREG(info.st_mode):
        raise ManifestValidationError(["manifest must be a regular file, not a symlink or directory"])
    if info.st_size > MAX_MANIFEST_BYTES:
        raise ManifestValidationError([f"manifest exceeds {MAX_MANIFEST_BYTES} bytes"])
    try:
        payload = source.read_bytes()
        document = json.loads(payload)
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ManifestValidationError([f"manifest is not valid UTF-8 JSON: {type(error).__name__}"]) from error

    errors: list[str] = []
    if not isinstance(document, dict):
        raise ManifestValidationError(["manifest root must be a JSON object"])
    unknown_root = sorted(set(document) - _ROOT_KEYS)
    if unknown_root:
        errors.append(f"manifest contains unknown keys: {', '.join(unknown_root)}")

    schema_version = document.get("schema_version")
    if (
        not isinstance(schema_version, int)
        or isinstance(schema_version, bool)
        or schema_version != SCHEMA_VERSION
    ):
        errors.append(f"schema_version must be the integer {SCHEMA_VERSION}")
    benchmark_id = _string(
        document.get("benchmark_id"),
        "benchmark_id",
        errors,
        required=True,
        max_length=80,
    )
    if benchmark_id and not _ID_PATTERN.fullmatch(benchmark_id):
        errors.append("benchmark_id may use only letters, numbers, dot, underscore, and hyphen")
    model_id = _string(document.get("model_id"), "model_id", errors, max_length=200)
    description = _string(document.get("description"), "description", errors, max_length=2000) or ""

    raw_cases = document.get("cases")
    if not isinstance(raw_cases, list):
        errors.append("cases must be an array")
        raw_cases = []
    if len(raw_cases) > 1000:
        errors.append("cases may contain at most 1000 entries")

    cases: list[BenchmarkCase] = []
    seen_ids: set[str] = set()
    seen_paths: set[Path] = set()
    root = source.parent.resolve()
    for index, raw_case in enumerate(raw_cases[:1000]):
        prefix = f"cases[{index}]"
        if not isinstance(raw_case, dict):
            errors.append(f"{prefix} must be an object")
            continue
        unknown_case = sorted(set(raw_case) - _CASE_KEYS)
        if unknown_case:
            errors.append(f"{prefix} contains unknown keys: {', '.join(unknown_case)}")
        case_id = _string(raw_case.get("id"), f"{prefix}.id", errors, required=True, max_length=80)
        if case_id and not _ID_PATTERN.fullmatch(case_id):
            errors.append(f"{prefix}.id has unsupported characters")
        if case_id in seen_ids:
            errors.append(f"{prefix}.id duplicates {case_id!r}")
        elif case_id:
            seen_ids.add(case_id)

        file_value = _string(raw_case.get("file"), f"{prefix}.file", errors, required=True, max_length=500)
        relative = Path(file_value or ".")
        lexical = root / relative
        if relative.is_absolute() or ".." in relative.parts or file_value in {None, ".", ""}:
            errors.append(f"{prefix}.file must be a relative path without '..'")
            resolved = root
        else:
            resolved = lexical.resolve()
            if not resolved.is_relative_to(root):
                errors.append(f"{prefix}.file resolves outside the manifest directory")
        if resolved in seen_paths:
            errors.append(f"{prefix}.file duplicates another case path")
        else:
            seen_paths.add(resolved)
        try:
            file_mode = lexical.lstat().st_mode
        except OSError:
            errors.append(f"{prefix}.file does not exist: {relative.as_posix()}")
        else:
            if not stat.S_ISREG(file_mode):
                errors.append(f"{prefix}.file must be a regular file, not a symlink or directory")

        expected_language = _string(
            raw_case.get("expected_language"),
            f"{prefix}.expected_language",
            errors,
            max_length=35,
        )
        expected_category = _string(
            raw_case.get("expected_category"),
            f"{prefix}.expected_category",
            errors,
            max_length=80,
        )
        expected_adapter_kind = _string(
            raw_case.get("expected_adapter_kind"),
            f"{prefix}.expected_adapter_kind",
            errors,
            max_length=40,
        )
        manual_notes = _string(
            raw_case.get("manual_notes"),
            f"{prefix}.manual_notes",
            errors,
            max_length=2000,
        )
        tags = _string_list(raw_case.get("tags"), f"{prefix}.tags", errors)
        expected_terms = _string_list(
            raw_case.get("expected_terms"), f"{prefix}.expected_terms", errors
        )
        expected_stem_tokens = _string_list(
            raw_case.get("expected_stem_tokens"),
            f"{prefix}.expected_stem_tokens",
            errors,
        )
        scores = _manual_scores(raw_case.get("manual_scores"), f"{prefix}.manual_scores", errors)
        if case_id and file_value:
            cases.append(
                BenchmarkCase(
                    id=case_id,
                    relative_file=relative,
                    source_path=resolved,
                    expected_terms=expected_terms,
                    expected_stem_tokens=expected_stem_tokens,
                    expected_language=expected_language,
                    expected_category=expected_category,
                    expected_adapter_kind=expected_adapter_kind,
                    manual_scores=scores,
                    manual_notes=manual_notes,
                    tags=tags,
                )
            )

    if errors:
        raise ManifestValidationError(errors)
    assert benchmark_id is not None
    return BenchmarkManifest(
        source_path=source,
        sha256=hashlib.sha256(payload).hexdigest(),
        schema_version=SCHEMA_VERSION,
        benchmark_id=benchmark_id,
        model_id=model_id,
        description=description,
        cases=tuple(cases),
    )
