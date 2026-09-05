"""Command-line interface for reproducible local M0 benchmarks."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from typing import Sequence, TextIO

from mingtu_ai.config import LlamaServerConfig, ProviderConfig
from mingtu_ai.providers import LocalGemmaProvider
from mingtu_ai.runtime import LlamaServerRuntime

from .manifest import ManifestValidationError, load_manifest
from .provenance import collect_runtime_provenance
from .report import write_report
from .runner import BenchmarkRunner


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m mingtu_ai.benchmark",
        description="Mingtu AI M0 local model feasibility benchmark",
    )
    parser.add_argument("manifest", type=Path, help="validated JSON dataset manifest")
    parser.add_argument(
        "--validate-only",
        action="store_true",
        help="validate the manifest and referenced files without starting a model",
    )
    parser.add_argument("--model", type=Path, help="local GGUF model path")
    parser.add_argument("--mmproj", type=Path, help="optional local multimodal projector")
    parser.add_argument(
        "--llama-server",
        type=Path,
        default=Path("/usr/bin/llama-server"),
        help="local llama-server executable",
    )
    parser.add_argument("--context-size", type=int, default=8192)
    parser.add_argument("--n-gpu-layers", type=int, default=99)
    parser.add_argument(
        "--output-dir",
        type=Path,
        help="report directory; defaults to a results folder beside the manifest",
    )
    parser.add_argument("--report-name", help="stable filename stem for JSON and Markdown")
    return parser


def main(
    argv: Sequence[str] | None = None,
    *,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
) -> int:
    out = stdout or sys.stdout
    err = stderr or sys.stderr
    args = build_parser().parse_args(argv)
    try:
        manifest = load_manifest(args.manifest)
    except ManifestValidationError as error:
        print(str(error), file=err)
        return 2

    if args.validate_only:
        print(
            json.dumps(
                {
                    "valid": True,
                    "schema_version": manifest.schema_version,
                    "benchmark_id": manifest.benchmark_id,
                    "model_id": manifest.model_id,
                    "case_count": len(manifest.cases),
                    "files_verified": len(manifest.cases),
                    "manifest_sha256": manifest.sha256,
                    "model_started": False,
                },
                ensure_ascii=False,
                indent=2,
            ),
            file=out,
        )
        return 0

    if args.model is None:
        print("benchmark error: --model is required unless --validate-only is used", file=err)
        return 2

    try:
        llama_config = LlamaServerConfig(
            binary=args.llama_server.expanduser().absolute(),
            model_path=args.model.expanduser().absolute(),
            mmproj_path=args.mmproj.expanduser().absolute() if args.mmproj else None,
            context_size=args.context_size,
            n_gpu_layers=args.n_gpu_layers,
        )
        provider_config = ProviderConfig()
        runner = BenchmarkRunner(
            manifest,
            runtime_factory=lambda: LlamaServerRuntime(llama_config),
            provider_factory=lambda runtime: LocalGemmaProvider(runtime, provider_config),
            run_metadata={
                "llama_server": str(llama_config.binary),
                "model_path": str(llama_config.model_path),
                "mmproj_path": str(llama_config.mmproj_path) if llama_config.mmproj_path else None,
                "host": "127.0.0.1",
                "port": 3001,
                "context_size": llama_config.context_size,
                "n_gpu_layers": llama_config.n_gpu_layers,
                "reproducibility": collect_runtime_provenance(
                    model_path=llama_config.model_path,
                    mmproj_path=llama_config.mmproj_path,
                    llama_server_path=llama_config.binary,
                ),
            },
        )
        report = runner.run()
        output_directory = args.output_dir or manifest.source_path.parent / "results"
        paths = write_report(report, output_directory, report_name=args.report_name)
    except (OSError, RuntimeError, ValueError) as error:
        print(f"benchmark error: {type(error).__name__}: {error}", file=err)
        return 2

    print(
        json.dumps(
            {
                "overall_status": report.summary["overall_status"],
                "successful_cases": report.summary["successful_cases"],
                "failed_cases": report.summary["failed_cases"],
                "json_report": str(paths.json),
                "markdown_report": str(paths.markdown),
            },
            ensure_ascii=False,
            indent=2,
        ),
        file=out,
    )
    return 0 if report.summary["overall_status"] == "passed" else 1
