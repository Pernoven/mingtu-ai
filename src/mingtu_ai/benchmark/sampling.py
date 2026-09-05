"""Best-effort RAM and NVIDIA VRAM measurements.

Sampling failures are report data, never proof that a GPU is absent or a
benchmark failure.  This matters in sandboxes where device access can be
blocked even when the host has a working NVIDIA GPU.
"""

from __future__ import annotations

import csv
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import io
from pathlib import Path
import resource
import shutil
import subprocess
import sys
from typing import Any, Protocol


class CommandRunner(Protocol):
    def __call__(self, command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[bytes]: ...


@dataclass(frozen=True, slots=True)
class GpuReading:
    index: int | None
    uuid: str
    name: str
    memory_used_mib: float | None
    memory_total_mib: float | None


@dataclass(frozen=True, slots=True)
class NvidiaSample:
    status: str
    readings: tuple[GpuReading, ...] = ()
    detail: str | None = None


@dataclass(frozen=True, slots=True)
class ResourceSample:
    label: str
    measured_at: str
    process_peak_ram_mib: float | None
    finished_children_peak_ram_mib: float | None
    ram_measurement: str
    nvidia: NvidiaSample

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _safe_detail(payload: bytes | str) -> str:
    if isinstance(payload, bytes):
        text = payload.decode("utf-8", errors="replace")
    else:
        text = payload
    return " ".join(text.split())[:240]


def _number(value: str) -> float | None:
    normalized = value.strip()
    if normalized in {"", "N/A", "[N/A]", "Not Supported"}:
        return None
    try:
        return float(normalized)
    except ValueError:
        return None


class NvidiaSmiSampler:
    """Query framebuffer memory through NVIDIA's selective query interface."""

    def __init__(
        self,
        executable: Path | str | None = None,
        *,
        discover: bool = True,
        command_runner: CommandRunner = subprocess.run,
        timeout: float = 5.0,
    ) -> None:
        selected = executable
        if selected is None and discover:
            selected = shutil.which("nvidia-smi")
        self.executable = Path(selected) if selected is not None else None
        self._command_runner = command_runner
        self.timeout = timeout

    def sample(self) -> NvidiaSample:
        if self.executable is None:
            return NvidiaSample(
                status="unavailable",
                detail="nvidia-smi was not found; GPU state is unknown",
            )
        command = [
            str(self.executable),
            "--query-gpu=index,uuid,name,memory.used,memory.total",
            "--format=csv,noheader,nounits",
        ]
        try:
            completed = self._command_runner(
                command,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=self.timeout,
                check=False,
            )
        except (OSError, subprocess.SubprocessError) as error:
            return NvidiaSample(
                status="error",
                detail=f"nvidia-smi query could not run: {type(error).__name__}",
            )
        if completed.returncode != 0:
            detail = _safe_detail(completed.stderr) or _safe_detail(completed.stdout)
            return NvidiaSample(
                status="error",
                detail=(
                    f"nvidia-smi exited with {completed.returncode}"
                    + (f": {detail}" if detail else "")
                    + "; GPU state remains unknown"
                ),
            )

        readings: list[GpuReading] = []
        try:
            rows = csv.reader(io.StringIO(completed.stdout.decode("utf-8")))
            for row in rows:
                if not row:
                    continue
                if len(row) != 5:
                    return NvidiaSample(
                        status="error",
                        detail="nvidia-smi returned an unexpected column count; GPU state remains unknown",
                    )
                readings.append(
                    GpuReading(
                        index=int(row[0].strip()) if row[0].strip().isdigit() else None,
                        uuid=row[1].strip(),
                        name=row[2].strip(),
                        memory_used_mib=_number(row[3]),
                        memory_total_mib=_number(row[4]),
                    )
                )
        except (UnicodeError, csv.Error) as error:
            return NvidiaSample(
                status="error",
                detail=f"nvidia-smi output could not be parsed: {type(error).__name__}",
            )
        if not readings:
            return NvidiaSample(
                status="unavailable",
                detail="nvidia-smi returned no GPU rows; GPU state is unknown",
            )
        return NvidiaSample(status="ok", readings=tuple(readings))


class ResourceSampler:
    """Capture process RAM high-water marks and best-effort NVIDIA VRAM."""

    def __init__(self, nvidia: NvidiaSmiSampler | None = None) -> None:
        self.nvidia = nvidia or NvidiaSmiSampler()

    def sample(self, label: str) -> ResourceSample:
        process_peak = self._peak_mib(resource.RUSAGE_SELF)
        children_peak = self._peak_mib(resource.RUSAGE_CHILDREN)
        return ResourceSample(
            label=label,
            measured_at=datetime.now(timezone.utc).isoformat(),
            process_peak_ram_mib=process_peak,
            finished_children_peak_ram_mib=children_peak,
            ram_measurement=(
                "Python resource.getrusage ru_maxrss high-water mark; child value covers "
                "children that have already exited and is cumulative for this process"
            ),
            nvidia=self.nvidia.sample(),
        )

    @staticmethod
    def _peak_mib(who: int) -> float | None:
        try:
            raw = float(resource.getrusage(who).ru_maxrss)
        except (OSError, ValueError):
            return None
        # Linux reports KiB; macOS reports bytes.  The target platform is Linux,
        # while this branch keeps unit conversion honest in portable tests.
        divisor = 1024.0 if sys.platform.startswith("linux") else 1024.0 * 1024.0
        return round(raw / divisor, 3)
