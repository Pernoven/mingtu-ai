"""Resolve an Ollama model reference to its local GGUF blob without mutation.

Ollama remains the authority for resolving its model reference.  This module
invokes the documented ``ollama show --modelfile MODEL`` command with a fixed
argument vector, then parses the generated Modelfile as plain text.  It never
evaluates shell syntax and never copies, downloads, or changes the blob.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
from typing import Mapping, Sequence, TextIO


GGUF_MAGIC = b"GGUF"
_DESTINATION_UNSAFE = re.compile(r"[^A-Za-z0-9._-]+")


class OllamaSourceError(RuntimeError):
    """An Ollama reference could not be resolved safely and read-only."""


@dataclass(frozen=True, slots=True)
class OllamaModelReport:
    """Inspection result for one path returned by Ollama."""

    model_ref: str
    resolved_blob_path: Path
    readable: bool
    size_bytes: int | None
    gguf_magic: bool | None
    suggested_destination: Path
    error: str | None = None

    @property
    def usable(self) -> bool:
        """Whether llama-server can safely be pointed at the inspected path."""

        return self.readable and self.gguf_magic is True and self.error is None

    def require_usable_path(self) -> Path:
        """Return the blob path or raise a concise configuration error."""

        if not self.readable:
            raise OllamaSourceError(self.error or "Ollama 模型 blob 無法讀取")
        if self.gguf_magic is not True:
            raise OllamaSourceError(self.error or "Ollama 模型 blob 不是 GGUF 檔案")
        if self.error is not None:
            raise OllamaSourceError(self.error)
        return self.resolved_blob_path

    def to_dict(self) -> dict[str, str | int | bool | None]:
        """Return stable JSON-compatible field names for scripts and support."""

        return {
            "model_ref": self.model_ref,
            "resolved_blob_path": os.fspath(self.resolved_blob_path),
            "readable": self.readable,
            "size_bytes": self.size_bytes,
            "gguf_magic": self.gguf_magic,
            "suggested_destination": os.fspath(self.suggested_destination),
            "error": self.error,
        }

    def render_text(self) -> str:
        if self.gguf_magic is True:
            magic = "可用"
        elif self.gguf_magic is False:
            magic = "不符合"
        else:
            magic = "無法檢查"
        size = str(self.size_bytes) if self.size_bytes is not None else "無法取得"
        lines = [
            f"Ollama 模型：{self.model_ref}",
            f"實際 blob：{self.resolved_blob_path}",
            f"可讀：{'yes' if self.readable else 'no'}",
            f"檔案大小（bytes）：{size}",
            f"GGUF magic：{magic}",
            f"建議穩定目的地：{self.suggested_destination}",
        ]
        if self.error is not None:
            lines.append(f"錯誤：{self.error}")
        return "\n".join(lines)


def _modelfile_instruction_lines(text: str) -> list[str]:
    """Return instruction lines while excluding comments and triple quotes.

    Generated Modelfiles include a commented replacement ``FROM`` and may have
    arbitrary template or license text inside triple-quoted blocks.  Neither is
    an instruction and therefore neither may influence blob selection.
    """

    instructions: list[str] = []
    in_multiline = False
    for raw_line in text.splitlines():
        line = raw_line.strip()
        quote_count = line.count('"""')
        if in_multiline:
            if quote_count % 2:
                in_multiline = False
            continue
        if not line or line.startswith("#"):
            continue
        instructions.append(line)
        if quote_count % 2:
            in_multiline = True
    return instructions


def parse_modelfile_blob_path(text: str) -> Path:
    """Extract the one absolute local path from a generated Modelfile.

    Model references, URLs, relative paths, empty ``FROM`` instructions, and
    multiple active ``FROM`` instructions are rejected.  The returned path is
    not resolved through the filesystem, so parsing itself remains read-only.
    """

    candidates: list[str] = []
    for line in _modelfile_instruction_lines(text):
        fields = line.split(maxsplit=1)
        if not fields or fields[0].casefold() != "from":
            continue
        candidates.append(fields[1].strip() if len(fields) == 2 else "")

    if not candidates:
        raise OllamaSourceError(
            "Ollama Modelfile 沒有可用的 FROM；模型可能尚未完成或不是本機 GGUF"
        )
    if len(candidates) != 1:
        raise OllamaSourceError("Ollama Modelfile 含有多重 FROM，無法唯一確定 blob")

    value = candidates[0]
    if not value:
        raise OllamaSourceError(
            "Ollama Modelfile 的 FROM 為空；模型可能尚未完成下載"
        )
    if "\x00" in value:
        raise OllamaSourceError("Ollama Modelfile 的 FROM 含有無效 NUL 字元")
    path = Path(value)
    if not path.is_absolute() or not value.startswith("/"):
        raise OllamaSourceError(
            "Ollama Modelfile 的 FROM 不是絕對本機路徑（URL 與相對路徑均不接受）"
        )
    return path


def _validate_model_ref(model_ref: str) -> str:
    reference = model_ref.strip()
    if not reference:
        raise OllamaSourceError("Ollama 模型 reference 不可為空")
    if reference.startswith("-"):
        raise OllamaSourceError("Ollama 模型 reference 不可以 '-' 開頭")
    if len(reference) > 512:
        raise OllamaSourceError("Ollama 模型 reference 過長")
    contains_unsafe_character = any(
        character.isspace() or ord(character) < 32 or ord(character) == 127
        for character in reference
    )
    if contains_unsafe_character:
        raise OllamaSourceError("Ollama 模型 reference 不可含空白或控制字元")
    return reference


def suggested_destination(
    model_ref: str,
    environ: Mapping[str, str] = os.environ,
) -> Path:
    """Return a stable, collision-resistant destination without creating it."""

    xdg_value = environ.get("XDG_DATA_HOME", "")
    xdg_path = Path(xdg_value) if xdg_value else None
    if xdg_path is not None and xdg_path.is_absolute():
        data_home = xdg_path
    else:
        home_value = environ.get("HOME", "")
        home = (
            Path(home_value)
            if home_value and Path(home_value).is_absolute()
            else Path.home()
        )
        data_home = home / ".local" / "share"

    slug = _DESTINATION_UNSAFE.sub("_", model_ref).strip("._-") or "ollama-model"
    slug = slug[:96].rstrip("._-") or "ollama-model"
    digest = hashlib.sha256(model_ref.encode("utf-8")).hexdigest()[:12]
    return data_home / "mingtu-ai" / "models" / f"{slug}-{digest}.gguf"


def _inspect_blob(
    model_ref: str,
    blob_path: Path,
    environ: Mapping[str, str],
) -> OllamaModelReport:
    destination = suggested_destination(model_ref, environ)
    size: int | None = None
    try:
        with blob_path.open("rb") as stream:
            file_status = os.fstat(stream.fileno())
            if not stat.S_ISREG(file_status.st_mode):
                raise OSError("路徑不是一般檔案")
            size = file_status.st_size
            magic = stream.read(len(GGUF_MAGIC))
    except OSError as exc:
        try:
            path_status = blob_path.stat()
            if stat.S_ISREG(path_status.st_mode):
                size = path_status.st_size
        except OSError:
            pass
        detail = exc.strerror or str(exc) or type(exc).__name__
        return OllamaModelReport(
            model_ref=model_ref,
            resolved_blob_path=blob_path,
            readable=False,
            size_bytes=size,
            gguf_magic=None,
            suggested_destination=destination,
            error=f"Ollama blob 無法讀取：{detail}",
        )

    valid_magic = magic == GGUF_MAGIC
    return OllamaModelReport(
        model_ref=model_ref,
        resolved_blob_path=blob_path,
        readable=True,
        size_bytes=size,
        gguf_magic=valid_magic,
        suggested_destination=destination,
        error=None if valid_magic else "Ollama blob 的檔頭不是 GGUF",
    )


def _command_failure(model_ref: str, stderr: str) -> OllamaSourceError:
    detail = stderr.strip() or "Ollama 未回傳錯誤訊息"
    lowered = detail.casefold()
    service_markers = (
        "could not connect",
        "connection refused",
        "server not responding",
    )
    if any(marker in lowered for marker in service_markers):
        return OllamaSourceError(f"無法連線 Ollama service：{detail}")
    if any(marker in lowered for marker in ("model not found", "manifest", "not found")):
        return OllamaSourceError(f"Ollama 模型 '{model_ref}' 不存在或尚未完成：{detail}")
    return OllamaSourceError(f"Ollama 無法顯示模型 '{model_ref}'：{detail}")


def resolve_ollama_model(
    model_ref: str,
    *,
    executable: str | os.PathLike[str] = "ollama",
    environ: Mapping[str, str] = os.environ,
    timeout_seconds: float = 20.0,
) -> OllamaModelReport:
    """Ask Ollama for one model's Modelfile and inspect its local blob.

    The subprocess uses no shell and receives a fixed command shape.  Ollama is
    only consulted during resolution; the resulting report contains a plain
    :class:`Path` and creates no runtime dependency on the Ollama service.
    """

    reference = _validate_model_ref(model_ref)
    command = [os.fspath(executable), "show", "--modelfile", reference]
    try:
        completed = subprocess.run(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            timeout=timeout_seconds,
            env=dict(environ),
        )
    except FileNotFoundError as exc:
        raise OllamaSourceError(
            "找不到 Ollama CLI；請先確認已安裝且 ollama 在 PATH 中"
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise OllamaSourceError(
            f"Ollama service 在 {timeout_seconds:g} 秒內未回應"
        ) from exc
    except OSError as exc:
        detail = exc.strerror or str(exc)
        raise OllamaSourceError(f"無法執行 Ollama CLI：{detail}") from exc

    if completed.returncode != 0:
        raise _command_failure(reference, completed.stderr)
    blob_path = parse_modelfile_blob_path(completed.stdout)
    return _inspect_blob(reference, blob_path, environ)


def resolve_ollama_path(
    model_ref: str,
    *,
    executable: str | os.PathLike[str] = "ollama",
    environ: Mapping[str, str] = os.environ,
    timeout_seconds: float = 20.0,
) -> Path:
    """Resolve a usable GGUF once, returning a service-independent path."""

    report = resolve_ollama_model(
        model_ref,
        executable=executable,
        environ=environ,
        timeout_seconds=timeout_seconds,
    )
    return report.require_usable_path()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m mingtu_ai.model_sources.ollama",
        description="唯讀解析 Ollama 模型的本機 GGUF blob 路徑",
    )
    parser.add_argument("model", metavar="MODEL", help="Ollama 模型 reference")
    parser.add_argument("--json", action="store_true", help="輸出 JSON")
    return parser


def main(
    argv: Sequence[str] | None = None,
    *,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
    environ: Mapping[str, str] = os.environ,
) -> int:
    out = stdout or sys.stdout
    err = stderr or sys.stderr
    arguments = build_parser().parse_args(argv)
    try:
        report = resolve_ollama_model(arguments.model, environ=environ)
    except OllamaSourceError as exc:
        if arguments.json:
            print(
                json.dumps(
                    {"model_ref": arguments.model, "error": str(exc)},
                    ensure_ascii=False,
                    indent=2,
                ),
                file=out,
            )
        else:
            print(f"Ollama 模型解析錯誤：{exc}", file=err)
        return 1

    if arguments.json:
        print(json.dumps(report.to_dict(), ensure_ascii=False, indent=2), file=out)
    else:
        print(report.render_text(), file=out)
    return 0 if report.usable else 1


if __name__ == "__main__":
    raise SystemExit(main())
