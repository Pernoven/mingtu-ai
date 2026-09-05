from __future__ import annotations

import io
import json
import hashlib
import os
from pathlib import Path
import stat
import subprocess
from tempfile import TemporaryDirectory
from concurrent.futures import ThreadPoolExecutor
import unittest
from unittest import mock

from mingtu_ai.benchmark.cli import main
from mingtu_ai.benchmark.manifest import ManifestValidationError, load_manifest
from mingtu_ai.benchmark.models import BenchmarkReport, LifecycleResult
from mingtu_ai.benchmark.provenance import (
    bounded_version_output,
    collect_runtime_provenance,
    sha256_file,
)
from mingtu_ai.benchmark.report import write_report
from mingtu_ai.benchmark.runner import BenchmarkRunner
from mingtu_ai.benchmark.sampling import NvidiaSmiSampler, ResourceSampler
from mingtu_ai.models import AnalysisInput, NamingSuggestion


def _write_manifest(root: Path, cases: list[dict[str, object]]) -> Path:
    path = root / "manifest.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "benchmark_id": "m0-test",
                "model_id": "google/gemma-4-E2B-it-qat-q4_0-gguf",
                "description": "test dataset",
                "cases": cases,
            }
        ),
        encoding="utf-8",
    )
    return path


def _minimal_report(identifier: str) -> BenchmarkReport:
    return BenchmarkReport(
        report_schema_version=1,
        created_at="2026-09-04T00:00:00+00:00",
        benchmark_id=identifier,
        model_id="test-model",
        manifest_path="manifest.json",
        manifest_sha256="0" * 64,
        run_metadata={
            "reproducibility": {
                "model": {"status": "hashed", "sha256": "1" * 64},
                "mmproj": {"status": "skipped", "sha256": None},
                "llama_server": {
                    "status": "hashed",
                    "sha256": "2" * 64,
                    "version": {"status": "ok", "output": "build 1"},
                },
                "dataset_cases": [],
            }
        },
        privacy={},
        summary={
            "case_count": 0,
            "successful_cases": 0,
            "failed_cases": 0,
            "overall_status": "passed",
        },
        lifecycle=LifecycleResult(),
        resource_samples=(),
        cases=(),
    )


class FakeRuntime:
    def __init__(self, *, fail_start: bool = False) -> None:
        self.fail_start = fail_start
        self.start_count = 0
        self.shutdown_count = 0
        self._running = False

    @property
    def is_running(self) -> bool:
        return self._running

    def ensure_started(self) -> None:
        self.start_count += 1
        if self.fail_start:
            raise RuntimeError("fake runtime start failed")
        self._running = True

    def shutdown(self) -> None:
        self.shutdown_count += 1
        self._running = False


class FakeProvider:
    def __init__(self) -> None:
        self.calls: list[str] = []
        self.close_count = 0

    def analyze(self, item: AnalysisInput) -> NamingSuggestion:
        self.calls.append(item.source_path.name)
        return NamingSuggestion(
            suggested_stem=f"CUDA_錯誤_{item.source_path.stem}",
            detected_language="zh-Hant",
            category="software_error",
            keywords=["CUDA"],
            confidence=0.9,
        )

    def close(self) -> None:
        self.close_count += 1


class ManifestTests(unittest.TestCase):
    def test_valid_manifest_resolves_regular_files(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "dataset" / "capture.txt"
            source.parent.mkdir()
            source.write_text("sample", encoding="utf-8")
            manifest_path = _write_manifest(
                root,
                [
                    {
                        "id": "capture",
                        "file": "dataset/capture.txt",
                        "expected_terms": ["CUDA"],
                        "manual_scores": {"stem_quality": 4.5},
                    }
                ],
            )

            manifest = load_manifest(manifest_path)

            self.assertEqual(manifest.cases[0].source_path, source)
            self.assertEqual(manifest.cases[0].manual_scores, {"stem_quality": 4.5})
            self.assertEqual(len(manifest.sha256), 64)

    def test_rejects_traversal_symlink_duplicate_and_unknown_key(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            outside = root.parent / f"{root.name}-outside.txt"
            outside.write_text("outside", encoding="utf-8")
            link = root / "link.txt"
            link.symlink_to(outside)
            local = root / "local.txt"
            local.write_text("local", encoding="utf-8")
            manifest_path = _write_manifest(
                root,
                [
                    {"id": "one", "file": "../outside.txt", "surprise": True},
                    {"id": "two", "file": "link.txt"},
                    {"id": "three", "file": "local.txt"},
                    {"id": "four", "file": "local.txt"},
                ],
            )
            try:
                with self.assertRaises(ManifestValidationError) as caught:
                    load_manifest(manifest_path)
                message = str(caught.exception)
                self.assertIn("unknown keys", message)
                self.assertIn("without '..'", message)
                self.assertIn("symlink", message)
                self.assertIn("duplicates another case path", message)
            finally:
                outside.unlink()


class SamplingTests(unittest.TestCase):
    def test_nvidia_selective_query_parses_csv(self) -> None:
        completed = subprocess.CompletedProcess(
            [],
            0,
            stdout=b"0, GPU-abc, NVIDIA RTX 5060, 123, 8192\n",
            stderr=b"",
        )
        sampler = NvidiaSmiSampler(
            "/usr/bin/nvidia-smi",
            discover=False,
            command_runner=lambda command, **kwargs: completed,
        )

        sample = sampler.sample()

        self.assertEqual(sample.status, "ok")
        self.assertEqual(sample.readings[0].memory_used_mib, 123.0)
        self.assertEqual(sample.readings[0].memory_total_mib, 8192.0)

    def test_nvidia_failure_is_inconclusive_data_not_exception(self) -> None:
        completed = subprocess.CompletedProcess([], 9, stdout=b"", stderr=b"access denied")
        sampler = NvidiaSmiSampler(
            "/usr/bin/nvidia-smi",
            discover=False,
            command_runner=lambda command, **kwargs: completed,
        )

        sample = sampler.sample()

        self.assertEqual(sample.status, "error")
        self.assertIn("unknown", sample.detail or "")


class ProvenanceTests(unittest.TestCase):
    def test_file_hashing_is_incremental_and_runtime_artifacts_are_explicit(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            model = root / "model.gguf"
            server = root / "llama-server"
            model.write_bytes(b"large-model-fixture")
            server.write_text("#!/bin/sh\nprintf 'llama.cpp build 123\\n'\n", encoding="utf-8")
            server.chmod(0o700)

            self.assertEqual(
                sha256_file(model, chunk_bytes=3),
                hashlib.sha256(b"large-model-fixture").hexdigest(),
            )
            provenance = collect_runtime_provenance(
                model_path=model,
                mmproj_path=None,
                llama_server_path=server,
            )

            self.assertEqual(provenance["model"]["status"], "hashed")
            self.assertEqual(provenance["mmproj"]["status"], "skipped")
            self.assertIsNone(provenance["mmproj"]["sha256"])
            self.assertEqual(provenance["llama_server"]["version"]["status"], "ok")
            self.assertIn("build 123", provenance["llama_server"]["version"]["output"])

    def test_version_capture_stops_owned_child_at_output_bound(self) -> None:
        with TemporaryDirectory() as directory:
            executable = Path(directory) / "noisy-server"
            executable.write_text(
                "#!/bin/sh\nwhile true; do printf '1234567890'; done\n",
                encoding="utf-8",
            )
            executable.chmod(0o700)

            result = bounded_version_output(
                executable,
                max_bytes=128,
                timeout_seconds=2.0,
            )

            self.assertEqual(result["status"], "truncated")
            self.assertTrue(result["truncated"])
            self.assertLessEqual(len((result["output"] or "").encode()), 128)


class ReportPublishingTests(unittest.TestCase):
    def test_private_modes_and_existing_report_are_preserved(self) -> None:
        with TemporaryDirectory() as directory:
            output = Path(directory) / "reports"
            first = write_report(_minimal_report("first"), output, report_name="same")

            self.assertEqual(stat.S_IMODE(output.stat().st_mode), 0o700)
            self.assertEqual(stat.S_IMODE(first.json.stat().st_mode), 0o600)
            self.assertEqual(stat.S_IMODE(first.markdown.stat().st_mode), 0o600)
            before_json = first.json.read_bytes()
            before_markdown = first.markdown.read_bytes()
            with self.assertRaisesRegex(FileExistsError, "already exists"):
                write_report(_minimal_report("second"), output, report_name="same")
            self.assertEqual(first.json.read_bytes(), before_json)
            self.assertEqual(first.markdown.read_bytes(), before_markdown)

    def test_existing_shared_output_directory_is_rejected_without_chmod(self) -> None:
        with TemporaryDirectory() as directory:
            output = Path(directory) / "shared"
            output.mkdir(mode=0o777)
            os.chmod(output, 0o777)

            with self.assertRaisesRegex(PermissionError, "group/world"):
                write_report(_minimal_report("first"), output, report_name="fixed")

            self.assertEqual(stat.S_IMODE(output.stat().st_mode), 0o777)
            self.assertEqual(list(output.iterdir()), [])

    def test_symlink_directory_and_target_fail_closed(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            real_output = root / "real"
            real_output.mkdir()
            real_output.chmod(0o700)
            linked_output = root / "linked"
            linked_output.symlink_to(real_output, target_is_directory=True)
            with self.assertRaisesRegex(ValueError, "not a symlink"):
                write_report(_minimal_report("one"), linked_output, report_name="linked")
            self.assertEqual(list(real_output.iterdir()), [])

            victim = root / "victim.txt"
            victim.write_text("do not change", encoding="utf-8")
            markdown_link = real_output / "fixed.md"
            markdown_link.symlink_to(victim)
            with self.assertRaisesRegex(FileExistsError, "already exists"):
                write_report(_minimal_report("two"), real_output, report_name="fixed")
            self.assertEqual(victim.read_text(encoding="utf-8"), "do not change")
            self.assertTrue(markdown_link.is_symlink())
            self.assertFalse((real_output / "fixed.json").exists())

    def test_same_name_race_publishes_one_matching_pair(self) -> None:
        with TemporaryDirectory() as directory:
            output = Path(directory) / "reports"

            def publish(identifier: str) -> str:
                write_report(_minimal_report(identifier), output, report_name="race")
                return identifier

            with ThreadPoolExecutor(max_workers=2) as pool:
                futures = [pool.submit(publish, identifier) for identifier in ("one", "two")]
            succeeded = [future.result() for future in futures if future.exception() is None]
            failed = [future.exception() for future in futures if future.exception() is not None]
            self.assertEqual(len(succeeded), 1)
            self.assertEqual(len(failed), 1)
            self.assertIsInstance(failed[0], FileExistsError)
            payload = json.loads((output / "race.json").read_text(encoding="utf-8"))
            markdown = (output / "race.md").read_text(encoding="utf-8")
            self.assertEqual(payload["benchmark_id"], succeeded[0])
            self.assertIn(f"Benchmark: {succeeded[0]}", markdown)

    def test_rollback_does_not_delete_a_replacement_with_another_inode(self) -> None:
        with TemporaryDirectory() as directory:
            output = Path(directory) / "reports"
            replacement = b"replacement owned by another writer"
            real_link = os.link
            link_calls = 0

            def replace_then_fail(
                source: str | bytes | os.PathLike[str] | os.PathLike[bytes],
                destination: str | bytes | os.PathLike[str] | os.PathLike[bytes],
                *,
                follow_symlinks: bool = True,
            ) -> None:
                nonlocal link_calls
                link_calls += 1
                if link_calls == 2:
                    json_target = output / "race.json"
                    json_target.unlink()
                    json_target.write_bytes(replacement)
                    raise FileExistsError("injected second-target collision")
                real_link(source, destination, follow_symlinks=follow_symlinks)

            with mock.patch("mingtu_ai.benchmark.report.os.link", replace_then_fail):
                with self.assertRaisesRegex(FileExistsError, "already exists"):
                    write_report(_minimal_report("one"), output, report_name="race")

            self.assertEqual((output / "race.json").read_bytes(), replacement)
            self.assertFalse((output / "race.md").exists())


class RunnerTests(unittest.TestCase):
    def test_batch_uses_one_runtime_scores_terms_and_writes_private_reports(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            secret = "TOP SECRET SOURCE CONTENT THAT MUST NOT ENTER REPORTS"
            for name in ("one.txt", "two.txt"):
                (root / name).write_text(secret, encoding="utf-8")
            manifest = load_manifest(
                _write_manifest(
                    root,
                    [
                        {
                            "id": "one",
                            "file": "one.txt",
                            "expected_terms": ["CUDA"],
                            "expected_stem_tokens": ["錯誤"],
                            "expected_language": "zh-Hant",
                            "expected_adapter_kind": "text",
                            "manual_scores": {"stem_quality": 4},
                            "manual_notes": "human review complete",
                        },
                        {"id": "two", "file": "two.txt", "expected_terms": ["CUDA"]},
                    ],
                )
            )
            runtime = FakeRuntime()
            provider = FakeProvider()
            sampler = ResourceSampler(NvidiaSmiSampler(discover=False))
            runner = BenchmarkRunner(
                manifest,
                runtime_factory=lambda: runtime,
                provider_factory=lambda _runtime: provider,
                sampler=sampler,
                port_probe=lambda: ("released", None),
            )

            report = runner.run()

            self.assertEqual(runtime.start_count, 1)
            self.assertEqual(runtime.shutdown_count, 1)
            self.assertEqual(provider.close_count, 1)
            self.assertEqual(provider.calls, ["one.txt", "two.txt"])
            self.assertEqual(report.summary["overall_status"], "passed")
            self.assertEqual(report.lifecycle.shared_runtime_sessions, 1)
            self.assertTrue(report.lifecycle.queue_drained)
            self.assertTrue(report.lifecycle.owned_process_exited)
            self.assertEqual(
                report.cases[0].automatic_scores["term_retention"]["score"], 1.0
            )
            self.assertGreaterEqual(report.cases[0].total_ms, 0.0)
            dataset_fingerprints = report.run_metadata["reproducibility"]["dataset_cases"]
            self.assertEqual([item["case_id"] for item in dataset_fingerprints], ["one", "two"])
            self.assertEqual(
                dataset_fingerprints[0]["sha256"],
                hashlib.sha256(secret.encode()).hexdigest(),
            )

            paths = write_report(report, root / "results", report_name="fixed")
            json_text = paths.json.read_text(encoding="utf-8")
            markdown_text = paths.markdown.read_text(encoding="utf-8")
            self.assertNotIn(secret, json_text)
            self.assertNotIn(secret, markdown_text)
            self.assertNotIn("data:image", json_text)
            self.assertIn("CUDA_錯誤_one", markdown_text)
            self.assertIn("stem_quality", markdown_text)
            self.assertIn("human review complete", markdown_text)
            self.assertIn("Reproducibility fingerprints", markdown_text)

    def test_runtime_start_failure_becomes_reported_case_failure(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "one.txt").write_text("sample", encoding="utf-8")
            manifest = load_manifest(
                _write_manifest(root, [{"id": "one", "file": "one.txt"}])
            )
            runtime = FakeRuntime(fail_start=True)
            runner = BenchmarkRunner(
                manifest,
                runtime_factory=lambda: runtime,
                provider_factory=lambda _runtime: FakeProvider(),
                sampler=ResourceSampler(NvidiaSmiSampler(discover=False)),
                port_probe=lambda: ("released", None),
            )

            report = runner.run()

            self.assertEqual(report.summary["overall_status"], "failed")
            self.assertEqual(report.cases[0].failure_stage, "runtime")
            self.assertIn("fake runtime start failed", report.cases[0].error or "")
            self.assertEqual(runtime.shutdown_count, 1)
            self.assertTrue(report.lifecycle.owned_process_exited)


class BenchmarkCliTests(unittest.TestCase):
    def test_validate_only_needs_no_model_and_starts_nothing(self) -> None:
        with TemporaryDirectory() as directory:
            manifest = _write_manifest(Path(directory), [])
            stdout = io.StringIO()
            stderr = io.StringIO()

            code = main([str(manifest), "--validate-only"], stdout=stdout, stderr=stderr)

            self.assertEqual(code, 0)
            payload = json.loads(stdout.getvalue())
            self.assertTrue(payload["valid"])
            self.assertEqual(payload["case_count"], 0)
            self.assertFalse(payload["model_started"])
            self.assertEqual(stderr.getvalue(), "")


if __name__ == "__main__":
    unittest.main()
