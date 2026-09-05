"""Serializable benchmark report contracts."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

from .sampling import ResourceSample


@dataclass(slots=True)
class LifecycleResult:
    shared_runtime_sessions: int = 0
    cold_start_ms: float | None = None
    shutdown_ms: float | None = None
    runtime_started: bool = False
    shutdown_completed: bool = False
    queue_drained: bool = False
    owned_process_exited: bool | None = None
    port_3001_status: str = "not_checked"
    port_3001_detail: str | None = None
    runtime_error: str | None = None


@dataclass(frozen=True, slots=True)
class CaseResult:
    case_id: str
    file: str
    success: bool
    failure_stage: str | None
    error: str | None
    adapter_kind: str | None
    generated_stem: str | None
    detected_language: str | None
    category: str | None
    confidence: float | None
    adapter_ms: float | None
    inference_ms: float | None
    total_ms: float
    automatic_scores: dict[str, Any]
    manual_scores: dict[str, float] | None
    manual_notes: str | None
    tags: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class BenchmarkReport:
    report_schema_version: int
    created_at: str
    benchmark_id: str
    model_id: str | None
    manifest_path: str
    manifest_sha256: str
    run_metadata: dict[str, Any]
    privacy: dict[str, Any]
    summary: dict[str, Any]
    lifecycle: LifecycleResult
    resource_samples: tuple[ResourceSample, ...]
    cases: tuple[CaseResult, ...]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
