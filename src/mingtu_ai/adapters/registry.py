"""Dispatch common file formats to bounded, local-only adapters."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
import csv
import io
import json
import mimetypes
import os
from pathlib import Path
import re
import shutil
import tarfile
from tempfile import TemporaryDirectory
import zipfile
import xml.etree.ElementTree as ET

from .models import AdaptedFile, AdapterError, AdapterKind, AdapterLimits, FileChangedError
from .secure_input import (
    BoundedReadProxy,
    OpenedRegularFile,
    duplicated_binary_stream,
    open_regular_file,
    read_bounded,
)
from .subprocesses import (
    BoundedProcessError,
    BoundedProcessResult,
    ProcessOutputLimitError,
    run_bounded_process,
)


_IMAGE_SUFFIXES = {".avif", ".bmp", ".gif", ".heic", ".heif", ".jpeg", ".jpg", ".png", ".tif", ".tiff", ".webp"}
_TEXT_SUFFIXES = {
    ".c", ".cc", ".conf", ".cpp", ".css", ".csv", ".fish", ".go",
    ".h", ".hpp", ".html", ".ini", ".java", ".js", ".json", ".jsx",
    ".log", ".md", ".py", ".rs", ".sh", ".sql", ".toml", ".ts",
    ".tsx", ".txt", ".xml", ".yaml", ".yml",
}
_OFFICE_SUFFIXES = {".docx", ".odp", ".ods", ".odt", ".pptx", ".xlsx"}
_VIDEO_SUFFIXES = {".avi", ".m4v", ".mkv", ".mov", ".mp4", ".mpeg", ".mpg", ".webm"}
_AUDIO_SUFFIXES = {".aac", ".flac", ".m4a", ".mp3", ".ogg", ".opus", ".wav", ".wma"}
_TAR_SUFFIXES = (".tar", ".tar.bz2", ".tar.gz", ".tar.lzma", ".tar.xz", ".tbz", ".tbz2", ".tgz", ".txz")
_SLIDE_RE = re.compile(r"^ppt/slides/slide\d+\.xml$")
_SHEET_RE = re.compile(r"^xl/worksheets/sheet\d+\.xml$")


def detect_mime_type(path: Path | str) -> str:
    """Detect a regular file using a no-follow descriptor and bounded signature."""

    candidate = Path(path)
    try:
        with open_regular_file(candidate) as opened:
            signature, _truncated = read_bounded(opened.fd, 32)
    except (OSError, AdapterError):
        signature = b""

    return _mime_type_from_signature(candidate, signature)


def _mime_type_from_opened(opened: OpenedRegularFile) -> str:
    signature, _truncated = read_bounded(opened.fd, 32)
    return _mime_type_from_signature(opened.path, signature)


def _mime_type_from_signature(candidate: Path, signature: bytes) -> str:
    """Classify a filename and its already-bounded leading bytes."""

    if signature.startswith(b"%PDF-"):
        return "application/pdf"
    if signature.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if signature.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if signature.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if signature.startswith(b"BM"):
        return "image/bmp"
    if signature.startswith((b"II*\x00", b"MM\x00*")):
        return "image/tiff"
    if signature.startswith(b"RIFF") and signature[8:12] == b"WEBP":
        return "image/webp"
    if signature.startswith(b"PK\x03\x04") and candidate.suffix.casefold() not in _OFFICE_SUFFIXES:
        return "application/zip"

    guessed, _encoding = mimetypes.guess_type(candidate.name, strict=False)
    return guessed or "application/octet-stream"


def _base_metadata(opened: OpenedRegularFile) -> dict[str, object]:
    info = os.fstat(opened.fd)
    return {
        "original_name": opened.path.name,
        "extension": opened.path.suffix,
        "size_bytes": info.st_size,
        "modified_ns": info.st_mtime_ns,
    }


def _bounded_text(text: str, limit: int) -> tuple[str, bool]:
    if len(text) <= limit:
        return text, False
    return text[:limit], True


def _decode_bytes(payload: bytes) -> str:
    if payload.startswith((b"\xff\xfe", b"\xfe\xff")):
        return payload.decode("utf-16", errors="replace")
    return payload.decode("utf-8-sig", errors="replace")


def _read_text_file(opened: OpenedRegularFile, limits: AdapterLimits) -> tuple[str, bool]:
    payload, byte_truncated = read_bounded(opened.fd, limits.max_input_bytes)
    text = _decode_bytes(payload)
    text, char_truncated = _bounded_text(text, limits.max_text_chars)
    return text, byte_truncated or char_truncated


def _safe_error(error: BaseException) -> str:
    message = str(error).strip().replace("\n", " ")
    if len(message) > 240:
        message = message[:240]
    return f"{type(error).__name__}: {message}" if message else type(error).__name__


class FileAdapterRegistry:
    """Select and execute an adapter without uploading file contents."""

    def __init__(self, limits: AdapterLimits | None = None) -> None:
        self.limits = limits or AdapterLimits()

    @contextmanager
    def open(self, path: Path | str) -> Iterator[AdaptedFile]:
        source = Path(path).expanduser().absolute()
        try:
            with open_regular_file(source) as opened:
                with TemporaryDirectory(prefix="mingtu-ai-") as temp_name:
                    adapted = self.adapt(source, Path(temp_name), opened=opened)
                    if (
                        opened.current_identity() != opened.identity
                        or not opened.path_matches_identity()
                    ):
                        raise FileChangedError("file changed while it was being adapted")
                    yield adapted
                    if (
                        opened.current_identity() != opened.identity
                        or not opened.path_matches_identity()
                    ):
                        raise FileChangedError("file changed while it was being analyzed")
        except AdapterError:
            raise
        except OSError as error:
            raise AdapterError(f"secure file access failed: {_safe_error(error)}") from error

    def adapt(
        self,
        source: Path,
        temp_directory: Path,
        *,
        opened: OpenedRegularFile,
    ) -> AdaptedFile:
        """Adapt one descriptor-bound source using caller-owned temp storage."""

        mime_type = _mime_type_from_opened(opened)
        suffix = source.suffix.casefold()
        lower_name = source.name.casefold()
        if suffix in _IMAGE_SUFFIXES or mime_type.startswith("image/"):
            return self._adapt_image(opened, mime_type)
        if suffix == ".pdf" or mime_type == "application/pdf":
            return self._adapt_pdf(opened, mime_type, temp_directory)
        if suffix in _OFFICE_SUFFIXES:
            return self._adapt_office(opened, mime_type)
        if suffix in _TEXT_SUFFIXES or mime_type.startswith("text/"):
            return self._adapt_text(opened, mime_type)
        if suffix in _VIDEO_SUFFIXES or mime_type.startswith("video/"):
            return self._adapt_media(opened, mime_type, temp_directory, video=True)
        if suffix in _AUDIO_SUFFIXES or mime_type.startswith("audio/"):
            return self._adapt_media(opened, mime_type, temp_directory, video=False)
        if suffix == ".zip" or mime_type == "application/zip":
            return self._adapt_zip_archive(opened, mime_type)
        if any(lower_name.endswith(item) for item in _TAR_SUFFIXES):
            return self._adapt_tar_archive(opened, mime_type)
        return self._adapt_metadata(opened, mime_type)

    def _adapt_image(self, opened: OpenedRegularFile, mime_type: str) -> AdaptedFile:
        return AdaptedFile(
            source_path=opened.path,
            mime_type=mime_type,
            kind=AdapterKind.IMAGE,
            image_paths=(opened.path,),
            metadata=_base_metadata(opened),
            source_identity=opened.identity,
            image_file_descriptors=(opened.fd,),
        )

    def _adapt_text(self, opened: OpenedRegularFile, mime_type: str) -> AdaptedFile:
        source = opened.path
        text, truncated = _read_text_file(opened, self.limits)
        metadata = _base_metadata(opened)
        warnings: list[str] = []
        suffix = source.suffix.casefold()

        if suffix == ".json" and not truncated:
            try:
                parsed = json.loads(text)
                metadata["json_root_type"] = type(parsed).__name__
            except (json.JSONDecodeError, UnicodeError):
                warnings.append("JSON could not be parsed; using bounded raw text")
        elif suffix == ".csv":
            try:
                rows: list[str] = []
                for index, row in enumerate(csv.reader(io.StringIO(text))):
                    if index >= 100:
                        warnings.append("CSV row preview was limited to 100 rows")
                        break
                    rows.append(" | ".join(cell[:500] for cell in row[:50]))
                text, csv_truncated = _bounded_text("\n".join(rows), self.limits.max_text_chars)
                truncated = truncated or csv_truncated
                metadata["preview_rows"] = len(rows)
            except csv.Error:
                warnings.append("CSV could not be parsed; using bounded raw text")

        if truncated:
            metadata["content_truncated"] = True
            warnings.append("Text preview was truncated")
        return AdaptedFile(
            source_path=source,
            mime_type=mime_type,
            kind=AdapterKind.TEXT,
            text=text,
            metadata=metadata,
            warnings=tuple(warnings),
            source_identity=opened.identity,
        )

    def _adapt_pdf(
        self,
        opened: OpenedRegularFile,
        mime_type: str,
        temp_directory: Path,
    ) -> AdaptedFile:
        source = opened.path
        metadata = _base_metadata(opened)
        warnings: list[str] = []
        text: str | None = None
        images: list[Path] = []
        inherited_path = opened.proc_path()

        pdftotext = shutil.which("pdftotext")
        if pdftotext:
            try:
                completed = self._capture(
                    [
                        pdftotext, "-f", "1", "-l", str(self.limits.max_pdf_pages),
                        "-enc", "UTF-8", "-nopgbrk", inherited_path, "-",
                    ],
                    stdout_limit=self.limits.max_input_bytes,
                    pass_fds=(opened.fd,),
                )
            except ProcessOutputLimitError as error:
                if error.stream == "stdout":
                    metadata["content_truncated"] = True
                    warnings.append("pdftotext output exceeded the byte limit")
                else:
                    warnings.append(f"pdftotext failed: {_safe_error(error)}")
            except (OSError, BoundedProcessError) as error:
                warnings.append(f"pdftotext failed: {_safe_error(error)}")
            else:
                if completed.returncode == 0:
                    text = _decode_bytes(completed.stdout)
                    text, truncated = _bounded_text(text, self.limits.max_text_chars)
                    if truncated:
                        metadata["content_truncated"] = True
                        warnings.append("PDF text preview was truncated")
                else:
                    detail = _decode_bytes(completed.stderr).strip()
                    warnings.append(
                        f"pdftotext exited with {completed.returncode}: {detail[:240]}"
                    )
        else:
            warnings.append("pdftotext is unavailable")

        pdftoppm = shutil.which("pdftoppm")
        if pdftoppm:
            prefix = temp_directory / "representative-page"
            error = self._run(
                [
                    pdftoppm, "-f", "1", "-l", "1", "-singlefile",
                    "-scale-to", "1600", "-png", inherited_path, os.fspath(prefix),
                ],
                pass_fds=(opened.fd,),
            )
            rendered = prefix.with_suffix(".png")
            if error is None and rendered.is_file():
                images.append(rendered)
            else:
                warnings.append(error or "pdftoppm produced no representative page")
        else:
            warnings.append("pdftoppm is unavailable")

        return AdaptedFile(
            source_path=source,
            mime_type=mime_type,
            kind=AdapterKind.PDF,
            text=text,
            image_paths=tuple(images),
            metadata=metadata,
            warnings=tuple(warnings),
            source_identity=opened.identity,
            image_file_descriptors=tuple(None for _item in images),
        )

    def _adapt_office(self, opened: OpenedRegularFile, mime_type: str) -> AdaptedFile:
        source = opened.path
        metadata = _base_metadata(opened)
        warnings: list[str] = []
        suffix = source.suffix.casefold()
        fragments: list[str] = []
        total_read = 0
        unreadable_container = False

        try:
            with duplicated_binary_stream(opened.fd) as stream:
                bounded_stream = BoundedReadProxy(
                    stream, self.limits.max_container_index_bytes
                )
                archive = zipfile.ZipFile(bounded_stream)
                with archive:
                    selected = [
                        info for info in archive.infolist()
                        if self._is_office_xml_member(suffix, info.filename)
                    ]
                    if len(selected) > self.limits.max_office_xml_members:
                        selected = selected[: self.limits.max_office_xml_members]
                        warnings.append("Office XML member count was limited")

                    for info in selected:
                        remaining = self.limits.max_office_total_xml_bytes - total_read
                        if remaining <= 0:
                            warnings.append("Office XML total byte limit was reached")
                            break
                        payload, warning = self._read_zip_member_bounded(archive, info, remaining)
                        if warning:
                            warnings.append(warning)
                            continue
                        total_read += len(payload)
                        fragment, warning = self._extract_xml_text(payload, odf=suffix.startswith(".od"))
                        if warning:
                            warnings.append(f"{info.filename}: {warning}")
                        elif fragment:
                            fragments.append(fragment)
        except (OSError, zipfile.BadZipFile, NotImplementedError) as error:
            unreadable_container = isinstance(error, zipfile.BadZipFile)
            warnings.append(f"Office container could not be read: {_safe_error(error)}")

        text, truncated = _bounded_text("\n".join(fragments), self.limits.max_text_chars)
        if truncated:
            metadata["content_truncated"] = True
            warnings.append("Office text preview was truncated")
        metadata["xml_bytes_read"] = total_read
        return AdaptedFile(
            source_path=source,
            mime_type=mime_type,
            kind=AdapterKind.OFFICE,
            text=text or None,
            metadata=metadata,
            warnings=tuple(dict.fromkeys(warnings)),
            source_identity=opened.identity,
            unreadable_office_container=unreadable_container,
        )

    @staticmethod
    def _is_office_xml_member(suffix: str, name: str) -> bool:
        if suffix == ".docx":
            return name == "word/document.xml" or bool(
                re.match(r"^word/(?:header|footer|footnotes|endnotes)\d*\.xml$", name)
            )
        if suffix == ".pptx":
            return bool(_SLIDE_RE.match(name))
        if suffix == ".xlsx":
            return name == "xl/sharedStrings.xml" or bool(_SHEET_RE.match(name))
        return name in {"content.xml", "meta.xml"}

    def _read_zip_member_bounded(
        self,
        archive: zipfile.ZipFile,
        info: zipfile.ZipInfo,
        remaining: int,
    ) -> tuple[bytes, str | None]:
        limit = min(self.limits.max_office_xml_bytes, remaining)
        if info.is_dir():
            return b"", None
        if info.flag_bits & 0x1:
            return b"", f"{info.filename}: encrypted member was skipped"
        if info.file_size > limit:
            return b"", f"{info.filename}: XML member exceeds byte limit"
        if info.compress_size == 0 and info.file_size:
            return b"", f"{info.filename}: invalid compression size"
        if info.compress_size and info.file_size / info.compress_size > self.limits.max_zip_compression_ratio:
            return b"", f"{info.filename}: compression ratio exceeds safety limit"
        try:
            with archive.open(info, "r") as stream:
                payload = stream.read(limit + 1)
        except (OSError, RuntimeError, zipfile.BadZipFile, NotImplementedError) as error:
            return b"", f"{info.filename}: {_safe_error(error)}"
        if len(payload) > limit:
            return b"", f"{info.filename}: XML member exceeds byte limit"
        return payload, None

    @staticmethod
    def _extract_xml_text(payload: bytes, *, odf: bool) -> tuple[str, str | None]:
        upper = payload.upper()
        if b"<!DOCTYPE" in upper or b"<!ENTITY" in upper:
            return "", "DTD or entity declaration was rejected"
        try:
            root = ET.fromstring(payload)
        except ET.ParseError as error:
            return "", _safe_error(error)

        if odf:
            parts = [
                "".join(element.itertext()).strip()
                for element in root.iter()
                if element.tag.rsplit("}", 1)[-1] in {"h", "p"}
            ]
        else:
            parts = [
                element.text.strip()
                for element in root.iter()
                if element.tag.rsplit("}", 1)[-1] in {"t", "v"}
                and element.text and element.text.strip()
            ]
        return "\n".join(part for part in parts if part), None

    def _adapt_media(
        self,
        opened: OpenedRegularFile,
        mime_type: str,
        temp_directory: Path,
        *,
        video: bool,
    ) -> AdaptedFile:
        source = opened.path
        metadata = _base_metadata(opened)
        warnings: list[str] = []
        probe = self._probe_media(opened)
        if isinstance(probe, str):
            warnings.append(probe)
        else:
            metadata["media"] = probe

        images: list[Path] = []
        if video:
            ffmpeg = shutil.which("ffmpeg")
            if ffmpeg:
                frame = temp_directory / "representative-frame.png"
                error = self._run(
                    [
                        ffmpeg, "-v", "error", "-i", opened.proc_path(),
                        "-vf", "thumbnail=60,scale=1600:1600:force_original_aspect_ratio=decrease",
                        "-frames:v", "1", "-y", os.fspath(frame),
                    ],
                    pass_fds=(opened.fd,),
                )
                if error is None and frame.is_file():
                    images.append(frame)
                else:
                    warnings.append(error or "ffmpeg produced no representative frame")
            else:
                warnings.append("ffmpeg is unavailable")

        text = json.dumps(metadata.get("media", {}), ensure_ascii=False, separators=(",", ":"))
        text, _truncated = _bounded_text(text, self.limits.max_text_chars)
        return AdaptedFile(
            source_path=source,
            mime_type=mime_type,
            kind=AdapterKind.VIDEO if video else AdapterKind.AUDIO,
            text=text,
            image_paths=tuple(images),
            metadata=metadata,
            warnings=tuple(warnings),
            source_identity=opened.identity,
            image_file_descriptors=tuple(None for _item in images),
        )

    def _probe_media(self, opened: OpenedRegularFile) -> dict[str, object] | str:
        ffprobe = shutil.which("ffprobe")
        if not ffprobe:
            return "ffprobe is unavailable"
        command = [
            ffprobe, "-v", "error",
            "-show_entries",
            "format=format_name,duration,size,bit_rate:format_tags=title,artist,album,comment,date:stream=index,codec_type,codec_name,width,height,duration:stream_tags=title,language",
            "-of", "json", opened.proc_path(),
        ]
        try:
            completed = self._capture(
                command,
                stdout_limit=self.limits.max_input_bytes,
                pass_fds=(opened.fd,),
            )
        except ProcessOutputLimitError as error:
            return f"ffprobe {error.stream} exceeded the byte limit"
        except (OSError, BoundedProcessError) as error:
            return f"ffprobe failed: {_safe_error(error)}"
        if completed.returncode != 0:
            detail = _decode_bytes(completed.stderr).strip()
            return f"ffprobe exited with {completed.returncode}: {detail[:240]}"
        try:
            parsed = json.loads(completed.stdout)
        except (json.JSONDecodeError, UnicodeError) as error:
            return f"ffprobe returned invalid JSON: {_safe_error(error)}"
        return parsed if isinstance(parsed, dict) else {"value": parsed}

    def _adapt_zip_archive(self, opened: OpenedRegularFile, mime_type: str) -> AdaptedFile:
        source = opened.path
        metadata = _base_metadata(opened)
        warnings: list[str] = []
        entries: list[dict[str, object]] = []
        try:
            with duplicated_binary_stream(opened.fd) as stream:
                bounded_stream = BoundedReadProxy(
                    stream, self.limits.max_container_index_bytes
                )
                with zipfile.ZipFile(bounded_stream) as archive:
                    for index, info in enumerate(archive.infolist()):
                        if index >= self.limits.max_archive_members:
                            warnings.append("Archive member list was truncated")
                            break
                        entries.append(
                            {
                                "name": info.filename[: self.limits.max_member_name_chars],
                                "size": info.file_size,
                                "directory": info.is_dir(),
                                "encrypted": bool(info.flag_bits & 0x1),
                            }
                        )
        except (OSError, zipfile.BadZipFile, NotImplementedError) as error:
            warnings.append(f"ZIP member list failed: {_safe_error(error)}")
        metadata["members"] = entries
        return self._archive_result(
            source, mime_type, metadata, warnings, source_identity=opened.identity
        )

    def _adapt_tar_archive(self, opened: OpenedRegularFile, mime_type: str) -> AdaptedFile:
        source = opened.path
        metadata = _base_metadata(opened)
        warnings: list[str] = []
        entries: list[dict[str, object]] = []
        try:
            with duplicated_binary_stream(opened.fd) as stream:
                bounded_stream = BoundedReadProxy(
                    stream, self.limits.max_container_index_bytes
                )
                with tarfile.open(fileobj=bounded_stream, mode="r:*") as archive:
                    for index, info in enumerate(archive):
                        if index >= self.limits.max_archive_members:
                            warnings.append("Archive member list was truncated")
                            break
                        entries.append(
                            {
                                "name": info.name[: self.limits.max_member_name_chars],
                                "size": info.size,
                                "directory": info.isdir(),
                                "link": info.issym() or info.islnk(),
                            }
                        )
        except (OSError, tarfile.TarError) as error:
            warnings.append(f"TAR member list failed: {_safe_error(error)}")
        metadata["members"] = entries
        return self._archive_result(
            source, mime_type, metadata, warnings, source_identity=opened.identity
        )

    def _archive_result(
        self,
        source: Path,
        mime_type: str,
        metadata: dict[str, object],
        warnings: list[str],
        *,
        source_identity: tuple[int, int, int, int],
    ) -> AdaptedFile:
        entries = metadata.get("members", [])
        lines = [
            f"{entry.get('name')} ({entry.get('size')} bytes)"
            for entry in entries
            if isinstance(entry, dict)
        ]
        text, truncated = _bounded_text("Archive members:\n" + "\n".join(lines), self.limits.max_text_chars)
        if truncated:
            warnings.append("Archive member preview was truncated")
        return AdaptedFile(
            source_path=source,
            mime_type=mime_type,
            kind=AdapterKind.ARCHIVE,
            text=text,
            metadata=metadata,
            warnings=tuple(warnings),
            source_identity=source_identity,
        )

    def _adapt_metadata(self, opened: OpenedRegularFile, mime_type: str) -> AdaptedFile:
        source = opened.path
        metadata = _base_metadata(opened)
        metadata["mime_type"] = mime_type
        text = "\n".join(f"{key}: {value}" for key, value in metadata.items())
        return AdaptedFile(
            source_path=source,
            mime_type=mime_type,
            kind=AdapterKind.METADATA,
            text=text,
            metadata=metadata,
            warnings=("No content adapter matched; metadata fallback was used",),
            source_identity=opened.identity,
        )

    def _run(self, command: list[str], *, pass_fds: tuple[int, ...] = ()) -> str | None:
        try:
            completed = self._capture(
                command,
                stdout_limit=None,
                pass_fds=pass_fds,
            )
        except (OSError, BoundedProcessError) as error:
            return f"External tool failed: {_safe_error(error)}"
        if completed.returncode == 0:
            return None
        detail = _decode_bytes(completed.stderr).strip()
        return f"External tool exited with {completed.returncode}: {detail[:240]}"

    def _capture(
        self,
        command: list[str],
        *,
        stdout_limit: int | None,
        pass_fds: tuple[int, ...] = (),
    ) -> BoundedProcessResult:
        """Run one external parser with generation-time output boundaries."""

        return run_bounded_process(
            command,
            timeout=self.limits.command_timeout,
            stdout_limit=stdout_limit,
            stderr_limit=4096,
            pass_fds=pass_fds,
        )


@contextmanager
def adapt_file(
    path: Path | str,
    *,
    registry: FileAdapterRegistry | None = None,
) -> Iterator[AdaptedFile]:
    """Adapt one file and always delete derived pages/frames afterward."""

    selected = registry or FileAdapterRegistry()
    with selected.open(path) as adapted:
        yield adapted
