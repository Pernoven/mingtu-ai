"""Batch folder scanning, candidate filtering, and preset formatting utilities."""

from __future__ import annotations

from datetime import datetime, timezone
import os
from pathlib import Path
import stat

from mingtu_ai.models import BatchPreset
from mingtu_ai.watcher.policy import should_ignore


def scan_batch_candidates(
    directory: Path | str,
    *,
    recursive: bool = False,
    history_path: Path | None = None,
) -> list[Path]:
    """Scan a target directory and return a list of eligible files for renaming.

    Filters out hidden files, symlinks, non-regular files, temporary files,
    and history database sidecars.
    """
    root = Path(directory).expanduser().absolute()
    if not root.exists():
        raise FileNotFoundError(f"目錄不存在：{root}")
    if not root.is_dir():
        raise NotADirectoryError(f"指定路徑不是目錄：{root}")

    canonical_history = history_path.resolve(strict=False) if history_path else None
    candidates: list[Path] = []

    def process_dir(current_dir: Path) -> None:
        try:
            entries = sorted(current_dir.iterdir(), key=lambda p: p.name.lower())
        except OSError:
            return

        for path in entries:
            try:
                # Do not traverse or inspect symlinks
                path_stat = path.lstat()
            except OSError:
                continue

            if stat.S_ISLNK(path_stat.st_mode):
                continue

            if stat.S_ISDIR(path_stat.st_mode):
                if recursive and not path.name.startswith("."):
                    process_dir(path)
                continue

            if not stat.S_ISREG(path_stat.st_mode):
                continue

            # Check ignore policy (hidden files, locks, downloads, temp files)
            if should_ignore(path):
                continue

            # Check if this file is the history DB or sidecar
            if canonical_history:
                resolved = path.resolve(strict=False)
                if resolved == canonical_history or str(resolved).startswith(str(canonical_history)):
                    continue

            candidates.append(path)

    process_dir(root)
    return candidates


def extract_file_timestamp_prefix(path: Path) -> str:
    """Extract a formatted date prefix (YYYY-MM-DD) for a file.

    Checks file metadata / stat time as a reliable local timestamp fallback.
    """
    try:
        info = path.stat(follow_symlinks=False)
        mtime = info.st_mtime
        dt = datetime.fromtimestamp(mtime, tz=timezone.utc)
        return dt.strftime("%Y-%m-%d")
    except Exception:
        return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def apply_naming_preset(
    stem: str,
    path: Path,
    preset: BatchPreset | str = BatchPreset.DEFAULT,
    category: str | None = None,
) -> str:
    """Apply preset formatting rules (e.g. photo date prefix, category prefix) to a stem."""
    preset_str = str(preset).lower()

    if preset_str == BatchPreset.PHOTO_DATE.value:
        date_prefix = extract_file_timestamp_prefix(path)
        # Avoid duplicate date prefix if AI stem already starts with date
        if stem.startswith(date_prefix) or (len(stem) >= 10 and stem[:10].replace("-", "").isdigit()):
            return stem
        return f"{date_prefix}_{stem}"

    if preset_str == BatchPreset.CATEGORY_AI.value:
        cat_prefix = (category or "doc").lower().replace(" ", "_")
        if stem.lower().startswith(cat_prefix):
            return stem
        return f"{cat_prefix}_{stem}"

    return stem
