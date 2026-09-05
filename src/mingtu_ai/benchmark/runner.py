"""Batch benchmark runner built on the production adapter/provider/queue path."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
import errno
import math
from pathlib import Path
import platform
import socket
import statistics
import time
from typing import Any, Protocol
import unicodedata

from mingtu_ai.adapters import AdaptedFile, adapt_file
from mingtu_ai.models import AnalysisInput, NamingSuggestion
from mingtu_ai.queue import QueueTask, SingleWorkerQueue, TaskState

from .manifest import BenchmarkCase, BenchmarkManifest
from .models import BenchmarkReport, CaseResult, LifecycleResult
from .provenance import sha256_file
from .sampling import ResourceSample, ResourceSampler


class Runtime(Protocol):
    @property
    def is_running(self) -> bool: ...

    def ensure_started(self) -> None: ...

    def shutdown(self) -> None: ...


class Provider(Protocol):
    def analyze(self, item: AnalysisInput) -> NamingSuggestion: ...

    def close(self) -> None: ...


RuntimeFactory = Callable[[], Runtime]
ProviderFactory = Callable[[Runtime], Provider]
AdapterFactory = Callable[[Path], Any]
PortProbe = Callable[[], tuple[str, str | None]]


@dataclass(frozen=True, slots=True)
class _Session:
    runtime: Runtime
    provider: Provider


def _milliseconds(started: float, ended: float) -> float:
    return round((ended - started) * 1000.0, 3)


def _safe_error(error: BaseException) -> str:
    text = " ".join(str(error).split())
    if len(text) > 300:
        text = text[:300]
    return f"{type(error).__name__}: {text}" if text else type(error).__name__


def _normalized(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value).casefold()
    return " ".join(normalized.replace("_", " ").replace("-", " ").split())


def _coverage(expected: tuple[str, ...], stem: str) -> dict[str, Any]:
    haystack = _normalized(stem)
    matched = [item for item in expected if _normalized(item) in haystack]
    missing = [item for item in expected if item not in matched]
    score = None if not expected else round(len(matched) / len(expected), 4)
    return {
        "mode": "automatic_substring",
        "expected": list(expected),
        "matched": matched,
        "missing": missing,
        "score": score,
    }


def _match(expected: str | None, actual: str | None) -> dict[str, Any]:
    return {
        "expected": expected,
        "actual": actual,
        "score": None if expected is None else float(expected == actual),
    }


def probe_port_3001() -> tuple[str, str | None]:
    """Check only loopback port 3001, without stopping its current owner."""

    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        probe.bind(("127.0.0.1", 3001))
    except OSError as error:
        if error.errno == errno.EADDRINUSE:
            return "occupied", "127.0.0.1:3001 is still occupied"
        if error.errno in {errno.EACCES, errno.EPERM}:
            return "inconclusive", f"port probe was denied: {type(error).__name__}"
        return "inconclusive", f"port probe failed: {type(error).__name__}"
    finally:
        probe.close()
    return "released", None


class BenchmarkRunner:
    """Run all manifest cases through one production-style queued runtime batch."""

    def __init__(
        self,
        manifest: BenchmarkManifest,
        *,
        runtime_factory: RuntimeFactory,
        provider_factory: ProviderFactory,
        adapter_factory: AdapterFactory = adapt_file,
        sampler: ResourceSampler | None = None,
        port_probe: PortProbe = probe_port_3001,
        clock: Callable[[], float] = time.perf_counter,
        run_metadata: dict[str, Any] | None = None,
    ) -> None:
        self.manifest = manifest
        self.runtime_factory = runtime_factory
        self.provider_factory = provider_factory
        self.adapter_factory = adapter_factory
        self.sampler = sampler or ResourceSampler()
        self.port_probe = port_probe
        self.clock = clock
        self.run_metadata = dict(run_metadata or {})
        self.lifecycle = LifecycleResult()
        self._samples: list[ResourceSample] = []
        self._case_by_path = {case.source_path: case for case in manifest.cases}

    def run(self) -> BenchmarkReport:
        if not self.manifest.cases:
            raise ValueError("benchmark manifest has no cases; use --validate-only for an empty dataset")

        reproducibility = dict(self.run_metadata.get("reproducibility", {}))
        reproducibility.setdefault("hash_algorithm", "sha256")
        reproducibility["dataset_cases"] = [
            {
                "status": "hashed",
                "case_id": case.id,
                "file": case.relative_file.as_posix(),
                "sha256": sha256_file(case.source_path),
            }
            for case in self.manifest.cases
        ]
        self._sample("baseline")
        queue: SingleWorkerQueue[_Session, CaseResult] = SingleWorkerQueue(
            runtime_factory=self._runtime_session,
            handler=self._run_path,
        )
        tasks = [queue.submit(case.source_path) for case in self.manifest.cases]
        queue.run_until_idle()
        self.lifecycle.queue_drained = queue.pending_count == 0 and not queue.is_active
        self.lifecycle.port_3001_status, self.lifecycle.port_3001_detail = self.port_probe()

        results = tuple(
            task.result
            if task.state is TaskState.SUCCEEDED and task.result is not None
            else self._queue_failure(case, task)
            for case, task in zip(self.manifest.cases, tasks, strict=True)
        )
        summary = self._summary(results)
        return BenchmarkReport(
            report_schema_version=1,
            created_at=datetime.now(timezone.utc).isoformat(),
            benchmark_id=self.manifest.benchmark_id,
            model_id=self.manifest.model_id,
            manifest_path=str(self.manifest.source_path),
            manifest_sha256=self.manifest.sha256,
            run_metadata={
                "python": platform.python_version(),
                "platform": platform.platform(),
                **self.run_metadata,
                "reproducibility": reproducibility,
            },
            privacy={
                "file_content_recorded": False,
                "data_urls_recorded": False,
                "recorded_model_output_fields": [
                    "suggested_stem",
                    "detected_language",
                    "category",
                    "confidence",
                ],
            },
            summary=summary,
            lifecycle=self.lifecycle,
            resource_samples=tuple(self._samples),
            cases=results,
        )

    @contextmanager
    def _runtime_session(self) -> Iterator[_Session]:
        self.lifecycle.shared_runtime_sessions += 1
        runtime: Runtime | None = None
        provider: Provider | None = None
        try:
            runtime = self.runtime_factory()
            started = self.clock()
            runtime.ensure_started()
            self.lifecycle.cold_start_ms = _milliseconds(started, self.clock())
            self.lifecycle.runtime_started = True
            self._sample("after_cold_start")
            provider = self.provider_factory(runtime)
            yield _Session(runtime=runtime, provider=provider)
        except Exception as error:
            if self.lifecycle.runtime_error is None:
                self.lifecycle.runtime_error = _safe_error(error)
            raise
        finally:
            if provider is not None:
                try:
                    provider.close()
                except Exception as error:
                    self.lifecycle.runtime_error = (
                        self.lifecycle.runtime_error or f"provider close failed: {_safe_error(error)}"
                    )
            if runtime is not None:
                shutdown_started = self.clock()
                try:
                    runtime.shutdown()
                    self.lifecycle.shutdown_completed = True
                except Exception as error:
                    self.lifecycle.runtime_error = (
                        self.lifecycle.runtime_error or f"runtime shutdown failed: {_safe_error(error)}"
                    )
                self.lifecycle.shutdown_ms = _milliseconds(shutdown_started, self.clock())
                try:
                    self.lifecycle.owned_process_exited = not runtime.is_running
                except Exception:
                    self.lifecycle.owned_process_exited = None
            self._sample("after_shutdown")

    def _run_path(self, path: Path, session: _Session) -> CaseResult:
        case = self._case_by_path[path]
        total_started = self.clock()
        adapter_ms: float | None = None
        inference_ms: float | None = None
        adapter_kind: str | None = None
        stage = "adapter"
        try:
            adapter_started = self.clock()
            with self.adapter_factory(path) as adapted:
                if not isinstance(adapted, AdaptedFile):
                    raise TypeError("adapter_factory must yield AdaptedFile")
                adapter_ms = _milliseconds(adapter_started, self.clock())
                adapter_kind = adapted.kind.value
                request = AnalysisInput(
                    source_path=adapted.source_path,
                    mime_type=adapted.mime_type,
                    kind=adapter_kind,
                    text=adapted.text,
                    image_paths=adapted.image_paths,
                    image_file_descriptors=adapted.image_file_descriptors,
                    metadata=adapted.metadata,
                )
                stage = "inference"
                inference_started = self.clock()
                suggestion = session.provider.analyze(request)
                inference_ms = _milliseconds(inference_started, self.clock())
                stage = "adapter_cleanup"
            total_ended = self.clock()
            self._sample(f"case:{case.id}:after_inference")
        except Exception as error:
            total_ended = self.clock()
            self._sample(f"case:{case.id}:after_failure")
            return CaseResult(
                case_id=case.id,
                file=case.relative_file.as_posix(),
                success=False,
                failure_stage=stage,
                error=_safe_error(error),
                adapter_kind=adapter_kind,
                generated_stem=None,
                detected_language=None,
                category=None,
                confidence=None,
                adapter_ms=adapter_ms,
                inference_ms=inference_ms,
                total_ms=_milliseconds(total_started, total_ended),
                automatic_scores={},
                manual_scores=case.manual_scores,
                manual_notes=case.manual_notes,
                tags=case.tags,
            )

        automatic_scores = {
            "term_retention": _coverage(case.expected_terms, suggestion.suggested_stem),
            "stem_token_coverage": _coverage(
                case.expected_stem_tokens, suggestion.suggested_stem
            ),
            "language_match": _match(case.expected_language, suggestion.detected_language),
            "category_match": _match(case.expected_category, suggestion.category),
            "adapter_kind_match": _match(case.expected_adapter_kind, adapter_kind),
        }
        return CaseResult(
            case_id=case.id,
            file=case.relative_file.as_posix(),
            success=True,
            failure_stage=None,
            error=None,
            adapter_kind=adapter_kind,
            generated_stem=suggestion.suggested_stem,
            detected_language=suggestion.detected_language,
            category=suggestion.category,
            confidence=suggestion.confidence,
            adapter_ms=adapter_ms,
            inference_ms=inference_ms,
            total_ms=_milliseconds(total_started, total_ended),
            automatic_scores=automatic_scores,
            manual_scores=case.manual_scores,
            manual_notes=case.manual_notes,
            tags=case.tags,
        )

    def _queue_failure(
        self,
        case: BenchmarkCase,
        task: QueueTask[CaseResult],
    ) -> CaseResult:
        return CaseResult(
            case_id=case.id,
            file=case.relative_file.as_posix(),
            success=False,
            failure_stage="runtime",
            error=task.error or "queue task did not produce a result",
            adapter_kind=None,
            generated_stem=None,
            detected_language=None,
            category=None,
            confidence=None,
            adapter_ms=None,
            inference_ms=None,
            total_ms=0.0,
            automatic_scores={},
            manual_scores=case.manual_scores,
            manual_notes=case.manual_notes,
            tags=case.tags,
        )

    def _sample(self, label: str) -> None:
        try:
            self._samples.append(self.sampler.sample(label))
        except Exception:
            # Sampling is deliberately non-authoritative and must not make the
            # model benchmark fail in a restricted desktop or sandbox session.
            return

    def _summary(self, results: tuple[CaseResult, ...]) -> dict[str, Any]:
        successful = [item for item in results if item.success]
        term_scores = [
            item.automatic_scores["term_retention"]["score"]
            for item in successful
            if item.automatic_scores.get("term_retention", {}).get("score") is not None
        ]
        if (
            self.lifecycle.runtime_error
            or not self.lifecycle.runtime_started
            or not self.lifecycle.shutdown_completed
            or not self.lifecycle.queue_drained
            or self.lifecycle.owned_process_exited is False
        ):
            lifecycle_status = "failed"
        elif self.lifecycle.port_3001_status == "occupied":
            lifecycle_status = "failed"
        elif (
            self.lifecycle.owned_process_exited is None
            or self.lifecycle.port_3001_status != "released"
        ):
            lifecycle_status = "inconclusive"
        else:
            lifecycle_status = "verified"
        case_status = "passed" if len(successful) == len(results) else "failed"
        overall_status = (
            "failed"
            if case_status == "failed" or lifecycle_status == "failed"
            else "inconclusive"
            if lifecycle_status == "inconclusive"
            else "passed"
        )
        return {
            "case_count": len(results),
            "successful_cases": len(successful),
            "failed_cases": len(results) - len(successful),
            "all_cases_succeeded": len(successful) == len(results),
            "lifecycle_status": lifecycle_status,
            "overall_status": overall_status,
            "mean_term_retention": (
                round(statistics.fmean(term_scores), 4) if term_scores else None
            ),
            "latency_ms": {
                "adapter": self._latency_stats(
                    [item.adapter_ms for item in successful if item.adapter_ms is not None]
                ),
                "inference": self._latency_stats(
                    [item.inference_ms for item in successful if item.inference_ms is not None]
                ),
                "total": self._latency_stats([item.total_ms for item in successful]),
            },
            "resources": self._resource_summary(),
        }

    def _resource_summary(self) -> dict[str, Any]:
        process_peaks = [
            sample.process_peak_ram_mib
            for sample in self._samples
            if sample.process_peak_ram_mib is not None
        ]
        child_peaks = [
            sample.finished_children_peak_ram_mib
            for sample in self._samples
            if sample.finished_children_peak_ram_mib is not None
        ]
        gpu_values: dict[str, list[float]] = {}
        gpu_baseline = {
            reading.uuid: reading.memory_used_mib
            for reading in self._samples[0].nvidia.readings
            if reading.memory_used_mib is not None
        } if self._samples else {}
        for sample in self._samples:
            for reading in sample.nvidia.readings:
                if reading.memory_used_mib is not None:
                    gpu_values.setdefault(reading.uuid, []).append(reading.memory_used_mib)
        gpu_peaks = {uuid: max(values) for uuid, values in gpu_values.items()}
        return {
            "process_peak_ram_mib": max(process_peaks) if process_peaks else None,
            "finished_children_peak_ram_mib": max(child_peaks) if child_peaks else None,
            "gpu_baseline_memory_used_mib": gpu_baseline,
            "gpu_peak_memory_used_mib": gpu_peaks,
            "gpu_peak_delta_from_baseline_mib": {
                uuid: round(peak - gpu_baseline[uuid], 3)
                for uuid, peak in gpu_peaks.items()
                if uuid in gpu_baseline
            },
            "nvidia_sample_statuses": [sample.nvidia.status for sample in self._samples],
        }

    @staticmethod
    def _latency_stats(values: list[float]) -> dict[str, float | None]:
        if not values:
            return {"min": None, "median": None, "p95": None, "max": None}
        ordered = sorted(values)
        p95_index = max(0, math.ceil(0.95 * len(ordered)) - 1)
        return {
            "min": round(ordered[0], 3),
            "median": round(statistics.median(ordered), 3),
            "p95": round(ordered[p95_index], 3),
            "max": round(ordered[-1], 3),
        }
