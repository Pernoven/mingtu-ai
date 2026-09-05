"""Privacy-bounded JSON and Markdown benchmark reports."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import stat
import tempfile

from .models import BenchmarkReport, CaseResult


_REPORT_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")


@dataclass(frozen=True, slots=True)
class ReportPaths:
    json: Path
    markdown: Path


def _regular_file_identity(path: Path) -> tuple[int, int] | None:
    """Return device/inode identity without following a symbolic link."""

    try:
        info = path.lstat()
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(info.st_mode):
        return None
    return info.st_dev, info.st_ino


def _rollback_unchanged_publication(target: Path, identity: tuple[int, int]) -> None:
    """Remove a published link only while it still names this call's inode."""

    if _regular_file_identity(target) != identity:
        return
    try:
        target.unlink()
    except FileNotFoundError:
        pass


def _cell(value: object) -> str:
    if value is None:
        return "-"
    return str(value).replace("|", "\\|").replace("\r", " ").replace("\n", " ")


def _latency(value: float | None) -> str:
    return "-" if value is None else f"{value:.3f}"


def _score(case: CaseResult, key: str) -> str:
    value = case.automatic_scores.get(key, {}).get("score")
    return "-" if value is None else f"{float(value):.3f}"


def render_markdown(report: BenchmarkReport) -> str:
    """Render a compact review document without source text or image data."""

    summary = report.summary
    lifecycle = report.lifecycle
    lines = [
        f"# Mingtu AI M0 Benchmark: {_cell(report.benchmark_id)}",
        "",
        f"- Created: `{_cell(report.created_at)}`",
        f"- Model metadata: `{_cell(report.model_id)}`",
        f"- Manifest SHA-256: `{_cell(report.manifest_sha256)}`",
        f"- Cases: {summary['case_count']} total, {summary['successful_cases']} succeeded, {summary['failed_cases']} failed",
        f"- Overall status: `{_cell(summary.get('overall_status'))}`",
        "- Privacy: source file contents and image/data URLs are not stored in this report.",
        "",
        "## Runtime lifecycle",
        "",
        f"- Shared runtime sessions: {lifecycle.shared_runtime_sessions}",
        f"- Cold start: {_latency(lifecycle.cold_start_ms)} ms",
        f"- Shutdown: {_latency(lifecycle.shutdown_ms)} ms",
        f"- Queue drained: {lifecycle.queue_drained}",
        f"- Owned process exited: {lifecycle.owned_process_exited}",
        f"- Port 3001: `{_cell(lifecycle.port_3001_status)}`",
    ]
    if lifecycle.runtime_error:
        lines.append(f"- Runtime error: {_cell(lifecycle.runtime_error)}")
    if lifecycle.port_3001_detail:
        lines.append(f"- Port detail: {_cell(lifecycle.port_3001_detail)}")

    reproducibility = report.run_metadata.get("reproducibility", {})
    lines.extend(["", "## Reproducibility fingerprints", ""])
    for label in ("model", "mmproj", "llama_server"):
        artifact = reproducibility.get(label, {})
        lines.append(
            f"- {label}: status `{_cell(artifact.get('status'))}`, "
            f"SHA-256 `{_cell(artifact.get('sha256'))}`"
        )
        if label == "llama_server" and artifact.get("version"):
            version = artifact["version"]
            lines.append(
                f"  - `--version`: status `{_cell(version.get('status'))}`, "
                f"output `{_cell(version.get('output'))}`"
            )
    dataset_cases = reproducibility.get("dataset_cases", [])
    lines.extend(["", "| Dataset case | File | SHA-256 |", "|---|---|---|"])
    for item in dataset_cases:
        lines.append(
            f"| {_cell(item.get('case_id'))} | {_cell(item.get('file'))} | "
            f"`{_cell(item.get('sha256'))}` |"
        )

    lines.extend(
        [
            "",
            "## Cases",
            "",
            "| Case | File | Result | Adapter ms | Inference ms | Total ms | Suggested stem | Term retention | Stem tokens | Manual scores | Manual notes | Error |",
            "|---|---|---:|---:|---:|---:|---|---:|---:|---|---|---|",
        ]
    )
    for case in report.cases:
        lines.append(
            "| "
            + " | ".join(
                (
                    _cell(case.case_id),
                    _cell(case.file),
                    "pass" if case.success else "fail",
                    _latency(case.adapter_ms),
                    _latency(case.inference_ms),
                    _latency(case.total_ms),
                    _cell(case.generated_stem),
                    _score(case, "term_retention"),
                    _score(case, "stem_token_coverage"),
                    _cell(
                        json.dumps(case.manual_scores, ensure_ascii=False, sort_keys=True)
                        if case.manual_scores is not None
                        else None
                    ),
                    _cell(case.manual_notes),
                    _cell(case.error),
                )
            )
            + " |"
        )

    lines.extend(
        [
            "",
            "## Resource samples",
            "",
            "RAM values are Python `resource.getrusage` high-water marks. NVIDIA VRAM uses the official `nvidia-smi --query-gpu ... --format=csv,noheader,nounits` interface. An unavailable/error sample is inconclusive, not evidence that no GPU exists.",
            "",
            "| Label | Process peak RAM MiB | Finished-child peak RAM MiB | NVIDIA status | GPU used MiB |",
            "|---|---:|---:|---|---|",
        ]
    )
    for sample in report.resource_samples:
        used = ", ".join(
            f"{reading.uuid}: {reading.memory_used_mib}"
            for reading in sample.nvidia.readings
        ) or "-"
        lines.append(
            "| "
            + " | ".join(
                (
                    _cell(sample.label),
                    _cell(sample.process_peak_ram_mib),
                    _cell(sample.finished_children_peak_ram_mib),
                    _cell(sample.nvidia.status),
                    _cell(used),
                )
            )
            + " |"
        )

    lines.extend(
        [
            "",
            "## Interpretation limits",
            "",
            "- `ru_maxrss` is a cumulative high-water mark, not an instantaneous allocation trace.",
            "- NVIDIA total framebuffer use can include other local processes; compare baseline and later samples.",
            "- Manual scores and notes come from the manifest and are not model-generated measurements.",
            "- A restricted-session `nvidia-smi` error is retained as inconclusive evidence.",
            "",
        ]
    )
    return "\n".join(lines)


def write_report(
    report: BenchmarkReport,
    output_directory: Path | str,
    *,
    report_name: str | None = None,
) -> ReportPaths:
    """Publish one private JSON/Markdown pair without replacing any target.

    Files are fully written to private temporary files before hard-link
    publication. Existing paths, including symlinks and non-regular files, are
    never followed, changed, or overwritten. If publishing the second member
    fails, the first member is removed only if its device/inode still identifies
    the regular staging file created by this call.
    """

    directory = Path(output_directory).expanduser().absolute()
    created_directory = False
    try:
        directory.mkdir(parents=True, mode=0o700)
        created_directory = True
    except FileExistsError:
        pass
    directory_info = directory.lstat()
    if not stat.S_ISDIR(directory_info.st_mode):
        raise ValueError("report output directory must be a directory, not a symlink or file")
    directory_mode = stat.S_IMODE(directory_info.st_mode)
    if created_directory:
        os.chmod(directory, 0o700, follow_symlinks=False)
    elif directory_mode & 0o077:
        raise PermissionError(
            "existing report output directory permits group/world access; "
            "choose a dedicated directory with mode 0700"
        )
    selected_name = report_name or datetime.now(timezone.utc).strftime("benchmark-%Y%m%dT%H%M%SZ")
    if not _REPORT_NAME.fullmatch(selected_name):
        raise ValueError("report_name may use only letters, numbers, dot, underscore, and hyphen")
    json_target = directory / f"{selected_name}.json"
    markdown_target = directory / f"{selected_name}.md"
    json_text = json.dumps(report.to_dict(), ensure_ascii=False, indent=2) + "\n"
    markdown_text = render_markdown(report)

    temp_paths: list[Path] = []
    published: list[tuple[Path, tuple[int, int]]] = []
    try:
        for suffix, text in (("json", json_text), ("md", markdown_text)):
            descriptor, temp_name = tempfile.mkstemp(
                prefix=f".{selected_name}.", suffix=f".{suffix}.tmp", dir=directory
            )
            temp_path = Path(temp_name)
            temp_paths.append(temp_path)
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                stream.write(text)
                stream.flush()
                os.fsync(stream.fileno())

        for temp_path, target in zip(
            temp_paths, (json_target, markdown_target), strict=True
        ):
            identity = _regular_file_identity(temp_path)
            if identity is None:
                raise RuntimeError("private report staging file is no longer a regular file")
            try:
                os.link(temp_path, target, follow_symlinks=False)
            except FileExistsError as error:
                raise FileExistsError(
                    f"benchmark report target already exists; choose another --report-name: {target}"
                ) from error
            published.append((target, identity))
        directory_descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory_descriptor)
        finally:
            os.close(directory_descriptor)
    except Exception:
        for target, identity in reversed(published):
            _rollback_unchanged_publication(target, identity)
        raise
    finally:
        for temp_path in temp_paths:
            try:
                temp_path.unlink()
            except FileNotFoundError:
                pass
    return ReportPaths(json=json_target, markdown=markdown_target)
