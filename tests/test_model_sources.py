from __future__ import annotations

import io
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from mingtu_ai.model_sources.ollama import (
    OllamaModelReport,
    OllamaSourceError,
    main,
    parse_modelfile_blob_path,
    resolve_ollama_model,
)


class ModelfileParsingTests(unittest.TestCase):
    def test_comments_and_triple_quoted_content_are_not_instructions(self) -> None:
        modelfile = '''
# FROM /commented/blob
TEMPLATE """
FROM /inside/template
{{ .Prompt }}
"""
LICENSE """FROM /inside/single-line-license"""
FROM /ollama/models/blobs/sha256-real
'''

        self.assertEqual(
            parse_modelfile_blob_path(modelfile),
            Path("/ollama/models/blobs/sha256-real"),
        )

    def test_multiple_active_from_instructions_are_rejected(self) -> None:
        with self.assertRaisesRegex(OllamaSourceError, "多重 FROM"):
            parse_modelfile_blob_path("FROM /first\nFROM /second\n")

    def test_non_local_and_malformed_from_values_are_rejected(self) -> None:
        cases = {
            "URL": "FROM https://example.invalid/model.gguf\n",
            "relative": "FROM models/model.gguf\n",
            "NUL": "FROM /models/model\x00.gguf\n",
        }

        for label, modelfile in cases.items():
            with self.subTest(label=label):
                with self.assertRaises(OllamaSourceError):
                    parse_modelfile_blob_path(modelfile)

    def test_missing_from_describes_an_incomplete_or_unsupported_model(self) -> None:
        with self.assertRaisesRegex(OllamaSourceError, "尚未完成"):
            parse_modelfile_blob_path("PARAMETER temperature 0.2\n")


class OllamaResolutionTests(unittest.TestCase):
    def test_readable_gguf_uses_fixed_read_only_ollama_command(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            model = root / "model.gguf"
            model.write_bytes(b"GGUFpayload")
            completed = subprocess.CompletedProcess(
                args=[],
                returncode=0,
                stdout=f"FROM {model}\n",
                stderr="",
            )
            environment = {
                "PATH": "/usr/bin",
                "XDG_DATA_HOME": str(root / "data"),
            }

            with patch(
                "mingtu_ai.model_sources.ollama.subprocess.run",
                return_value=completed,
            ) as runner:
                report = resolve_ollama_model("gemma:latest", environ=environment)

            self.assertTrue(report.usable)
            self.assertEqual(report.require_usable_path(), model)
            self.assertEqual(report.size_bytes, len(b"GGUFpayload"))
            command = runner.call_args.args[0]
            options = runner.call_args.kwargs
            self.assertEqual(command, ["ollama", "show", "--modelfile", "gemma:latest"])
            self.assertNotIn("shell", options)
            self.assertIs(options["stdin"], subprocess.DEVNULL)

    def test_permission_error_is_a_report_instead_of_a_traceback(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            model = Path(temporary) / "model.gguf"
            model.write_bytes(b"GGUFpayload")
            completed = subprocess.CompletedProcess(
                args=[], returncode=0, stdout=f"FROM {model}\n", stderr=""
            )

            with patch(
                "mingtu_ai.model_sources.ollama.subprocess.run",
                return_value=completed,
            ), patch.object(
                Path,
                "open",
                side_effect=PermissionError(13, "Permission denied", str(model)),
            ):
                report = resolve_ollama_model("gemma")

            self.assertFalse(report.usable)
            self.assertFalse(report.readable)
            self.assertIsNone(report.gguf_magic)
            self.assertIn("Permission denied", report.error or "")
            with self.assertRaisesRegex(OllamaSourceError, "Permission denied"):
                report.require_usable_path()

    def test_unsafe_model_references_never_start_a_subprocess(self) -> None:
        unsafe_references = ["", "--help", "model name", "model\nname", "x" * 513]

        with patch("mingtu_ai.model_sources.ollama.subprocess.run") as runner:
            for model_ref in unsafe_references:
                with self.subTest(model_ref=repr(model_ref)):
                    with self.assertRaises(OllamaSourceError):
                        resolve_ollama_model(model_ref)

        runner.assert_not_called()


class OllamaCliTests(unittest.TestCase):
    def test_missing_cli_is_a_concise_text_error(self) -> None:
        output = io.StringIO()
        error = io.StringIO()
        with patch(
            "mingtu_ai.model_sources.ollama.subprocess.run",
            side_effect=FileNotFoundError,
        ):
            code = main(["gemma"], stdout=output, stderr=error)

        self.assertEqual(code, 1)
        self.assertEqual(output.getvalue(), "")
        self.assertIn("找不到 Ollama CLI", error.getvalue())
        self.assertNotIn("Traceback", error.getvalue())

    def test_json_error_is_machine_readable_and_exits_one(self) -> None:
        output = io.StringIO()
        error = io.StringIO()
        with patch(
            "mingtu_ai.model_sources.ollama.subprocess.run",
            side_effect=FileNotFoundError,
        ):
            code = main(["gemma", "--json"], stdout=output, stderr=error)

        self.assertEqual(code, 1)
        self.assertEqual(error.getvalue(), "")
        payload = json.loads(output.getvalue())
        self.assertEqual(payload["model_ref"], "gemma")
        self.assertIn("找不到 Ollama CLI", payload["error"])

    def test_unavailable_service_is_distinguished_from_missing_model(self) -> None:
        failures = (
            ("Error: could not connect to ollama app", "無法連線 Ollama service"),
            ("Error: model not found", "不存在或尚未完成"),
        )

        for stderr_text, expected in failures:
            with self.subTest(stderr=stderr_text):
                output = io.StringIO()
                error = io.StringIO()
                completed = subprocess.CompletedProcess(
                    args=[], returncode=1, stdout="", stderr=stderr_text
                )
                with patch(
                    "mingtu_ai.model_sources.ollama.subprocess.run",
                    return_value=completed,
                ):
                    code = main(["gemma"], stdout=output, stderr=error)

                self.assertEqual(code, 1)
                self.assertEqual(output.getvalue(), "")
                self.assertIn(expected, error.getvalue())

    def test_incomplete_model_without_from_is_reported(self) -> None:
        output = io.StringIO()
        error = io.StringIO()
        completed = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout="# generated Modelfile is incomplete\nPARAMETER temperature 0\n",
            stderr="",
        )
        with patch(
            "mingtu_ai.model_sources.ollama.subprocess.run",
            return_value=completed,
        ):
            code = main(["gemma"], stdout=output, stderr=error)

        self.assertEqual(code, 1)
        self.assertIn("沒有可用的 FROM", error.getvalue())

    def test_text_success_and_json_failure_have_stable_exit_codes(self) -> None:
        usable = OllamaModelReport(
            model_ref="gemma",
            resolved_blob_path=Path("/models/gemma.gguf"),
            readable=True,
            size_bytes=8,
            gguf_magic=True,
            suggested_destination=Path("/data/gemma.gguf"),
        )
        unusable = OllamaModelReport(
            model_ref="gemma",
            resolved_blob_path=Path("/models/gemma.gguf"),
            readable=True,
            size_bytes=8,
            gguf_magic=False,
            suggested_destination=Path("/data/gemma.gguf"),
            error="Ollama blob 的檔頭不是 GGUF",
        )

        text_output = io.StringIO()
        with patch(
            "mingtu_ai.model_sources.ollama.resolve_ollama_model",
            return_value=usable,
        ):
            text_code = main(["gemma"], stdout=text_output)

        json_output = io.StringIO()
        with patch(
            "mingtu_ai.model_sources.ollama.resolve_ollama_model",
            return_value=unusable,
        ):
            json_code = main(["gemma", "--json"], stdout=json_output)

        self.assertEqual(text_code, 0)
        self.assertIn("GGUF magic：可用", text_output.getvalue())
        self.assertEqual(json_code, 1)
        payload = json.loads(json_output.getvalue())
        self.assertFalse(payload["gguf_magic"])
        self.assertEqual(payload["error"], "Ollama blob 的檔頭不是 GGUF")


if __name__ == "__main__":
    unittest.main()
