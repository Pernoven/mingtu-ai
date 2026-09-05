"""Resolve KDE desktop paths without executing shell configuration files."""

from __future__ import annotations

from collections.abc import Sequence
import configparser
import json
import os
from pathlib import Path
import re
from urllib.parse import unquote, urlparse


_XDG_PICTURES_ASSIGNMENT = re.compile(
    r"^\s*XDG_PICTURES_DIR\s*=\s*(?:"
    r'"(?P<double>(?:\\.|[^"\\])*)"'
    r"|'(?P<single>[^']*)'"
    r"|(?P<plain>[^#\s]+)"
    r")\s*(?:#.*)?$"
)
_DOUBLE_QUOTE_ESCAPE = re.compile(r"\\([\\\"$`])")


def home_directory(environ: os._Environ[str] | dict[str, str]) -> Path:
    """Return an absolute home directory, honoring an injected environment."""

    configured = environ.get("HOME")
    if configured:
        candidate = Path(configured)
        if candidate.is_absolute():
            return candidate
    return Path.home()


def xdg_config_home(environ: os._Environ[str] | dict[str, str]) -> Path:
    """Apply the XDG_CONFIG_HOME absolute-path rule and standard fallback."""

    home = home_directory(environ)
    configured = environ.get("XDG_CONFIG_HOME")
    if configured:
        candidate = Path(configured)
        if candidate.is_absolute():
            return candidate
    return home / ".config"


def pictures_directory(environ: os._Environ[str] | dict[str, str]) -> Path:
    """Read XDG_PICTURES_DIR safely without sourcing its shell-format file."""

    home = home_directory(environ)
    config_path = xdg_config_home(environ) / "user-dirs.dirs"
    try:
        contents = config_path.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return home / "Pictures"

    for line in contents.splitlines():
        match = _XDG_PICTURES_ASSIGNMENT.fullmatch(line)
        if match is None:
            continue
        value = match.group("double", "single", "plain")
        raw_value = next(item for item in value if item is not None)
        if match.group("double") is not None:
            raw_value = _DOUBLE_QUOTE_ESCAPE.sub(r"\1", raw_value)
        expanded = _expand_home_only(raw_value, home)
        if expanded is None:
            break
        candidate = Path(expanded)
        return candidate if candidate.is_absolute() else home / candidate
    return home / "Pictures"


def spectacle_screenshot_directory(
    environ: os._Environ[str] | dict[str, str],
) -> Path:
    """Resolve Spectacle's configured image-save directory.

    ``lastImageSaveLocation`` is the strongest evidence of the active default
    folder. The translated screenshot folder is the deterministic fallback
    below the XDG Pictures directory.
    """

    pictures = pictures_directory(environ)
    parser = configparser.ConfigParser(interpolation=None, strict=False)
    parser.optionxform = str
    try:
        with (xdg_config_home(environ) / "spectaclerc").open(
            "r", encoding="utf-8"
        ) as config_file:
            parser.read_file(config_file)
    except (OSError, UnicodeError, configparser.Error):
        return pictures

    if not parser.has_section("ImageSave"):
        return pictures

    last_location = parser.get("ImageSave", "lastImageSaveLocation", fallback="").strip()
    last_directory = _parent_from_file_location(last_location)
    translated = parser.get(
        "ImageSave", "translatedScreenshotsFolder", fallback=""
    ).strip()
    translated_directory = _translated_directory(translated, pictures)

    for candidate in (last_directory, translated_directory):
        if candidate is not None and candidate.is_dir():
            return candidate
    if translated_directory is not None:
        return translated_directory
    if last_directory is not None:
        return last_directory
    return pictures


def _expand_home_only(value: str, home: Path) -> str | None:
    """Expand only the HOME syntax emitted by xdg-user-dirs."""

    expanded = value.replace("${HOME}", os.fspath(home))
    expanded = re.sub(r"\$HOME(?=/|$)", os.fspath(home), expanded)
    if "$" in expanded or "`" in expanded:
        return None
    return expanded


def _parent_from_file_location(value: str) -> Path | None:
    if not value:
        return None
    parsed = urlparse(value)
    if parsed.scheme and parsed.scheme != "file":
        return None
    if parsed.scheme == "file" and parsed.netloc not in {"", "localhost"}:
        return None
    raw_path = unquote(parsed.path) if parsed.scheme else value
    if not raw_path:
        return None
    location = Path(raw_path)
    if not location.is_absolute():
        return None
    return location if value.endswith("/") else location.parent


def _translated_directory(value: str, pictures: Path) -> Path | None:
    if not value or "\x00" in value:
        return None
    candidate = Path(value)
    return candidate if candidate.is_absolute() else pictures / candidate


def user_config_path(environ: os._Environ[str] | dict[str, str] | None = None) -> Path:
    """Return path to user's config.json under XDG_CONFIG_HOME."""
    env = os.environ if environ is None else environ
    return xdg_config_home(env) / "mingtu-ai" / "config.json"


def load_user_watch_directories(
    environ: os._Environ[str] | dict[str, str] | None = None,
) -> list[Path]:
    """Load previously saved user watch directories if valid and non-empty."""
    path = user_config_path(environ)
    if not path.is_file():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        raw_dirs = data.get("watch_directories", [])
        if not isinstance(raw_dirs, list):
            return []
        result: list[Path] = []
        for item in raw_dirs:
            if isinstance(item, str) and item.strip():
                candidate = Path(item).expanduser().absolute()
                if candidate.is_dir() and not candidate.is_symlink():
                    if candidate not in result:
                        result.append(candidate)
        return result
    except (OSError, json.JSONDecodeError):
        return []


def save_user_watch_directories(
    directories: Sequence[Path | str],
    environ: os._Environ[str] | dict[str, str] | None = None,
) -> None:
    """Persist user watch directories list safely to config.json."""
    path = user_config_path(environ)
    path.parent.mkdir(parents=True, exist_ok=True)
    paths_str = [
        str(Path(d).expanduser().absolute())
        for d in directories
        if str(d).strip()
    ]
    payload = {"watch_directories": paths_str}
    temp_path = path.with_suffix(".json.tmp")
    temp_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    temp_path.replace(path)

