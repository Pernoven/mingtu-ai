from __future__ import annotations

from contextlib import contextmanager
import base64
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import httpx

from mingtu_ai.config import LlamaServerConfig, ProviderConfig
from mingtu_ai.models import AnalysisInput, NamingSuggestion
from mingtu_ai.providers import LocalGemmaProvider, ProviderError
from mingtu_ai.providers.local_gemma import ProviderInputError
from mingtu_ai.adapters.secure_input import read_bounded as real_read_bounded


VALID_CONTENT = {
    "suggested_stem": "PyTorch_CUDA_記憶體不足",
    "detected_language": "zh-Hant",
    "category": "software_error",
    "keywords": ["PyTorch", "CUDA", "out of memory"],
    "confidence": 0.92,
}
VALID_RESPONSE_SCHEMA = NamingSuggestion.model_json_schema()


class FakeRuntime:
    def __init__(self) -> None:
        self.config = LlamaServerConfig(model_path=Path("unused.gguf"))
        self.base_url = "http://127.0.0.1:3001"
        self.jobs = 0

    @contextmanager
    def job(self):
        self.jobs += 1
        yield


class ProviderTests(unittest.TestCase):
    def test_provider_uses_bound_image_descriptor_after_symlink_swap(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            image = root / "capture.png"
            image.write_bytes(b"original image")
            moved = root / "opened.png"
            replacement = root / "replacement.png"
            replacement.write_bytes(b"secret replacement")
            descriptor = os.open(
                image,
                os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
            )
            try:
                image.rename(moved)
                image.symlink_to(replacement)
                provider = LocalGemmaProvider(
                    FakeRuntime(),
                    client=httpx.Client(
                        transport=httpx.MockTransport(lambda _request: httpx.Response(500))
                    ),
                )
                data_url = provider._image_data_url(image, descriptor=descriptor)
                decoded = base64.b64decode(data_url.partition(",")[2])
                self.assertEqual(decoded, b"original image")
                provider._client.close()
            finally:
                os.close(descriptor)

    def test_provider_rejects_file_that_grows_during_bounded_read(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            image = Path(directory) / "capture.png"
            image.write_bytes(b"small")
            provider = LocalGemmaProvider(
                FakeRuntime(),
                ProviderConfig(max_image_bytes=1024),
                client=httpx.Client(
                    transport=httpx.MockTransport(lambda _request: httpx.Response(500))
                ),
            )

            def grow_then_read(descriptor: int, limit: int):
                with image.open("ab") as stream:
                    stream.write(b"x" * 2048)
                return real_read_bounded(descriptor, limit)

            with (
                patch(
                    "mingtu_ai.providers.local_gemma.read_bounded",
                    side_effect=grow_then_read,
                ),
                self.assertRaisesRegex(ProviderInputError, "changed|exceeds"),
            ):
                provider._image_data_url(image)
            provider._client.close()

    def test_image_is_data_url_and_thinking_is_disabled(self) -> None:
        captured: dict = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured.update(json.loads(request.content))
            return httpx.Response(
                200,
                json={"choices": [{"message": {"content": json.dumps(VALID_CONTENT)}}]},
            )

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "capture.png"
            source.write_bytes(b"source-stays-the-same")
            image = root / "adapted.png"
            image.write_bytes(b"\x89PNG\r\n")
            client = httpx.Client(transport=httpx.MockTransport(handler))
            runtime = FakeRuntime()
            provider = LocalGemmaProvider(runtime, client=client)

            result = provider.analyze(
                AnalysisInput(
                    source_path=source,
                    mime_type="image/png",
                    kind="image",
                    image_paths=(image,),
                )
            )

            self.assertEqual(result.confidence, 0.92)
            self.assertEqual(runtime.jobs, 1)
            self.assertEqual(source.read_bytes(), b"source-stays-the-same")
            image_url = captured["messages"][1]["content"][1]["image_url"]["url"]
            self.assertTrue(image_url.startswith("data:image/png;base64,"))
            self.assertEqual(captured["chat_template_kwargs"], {"enable_thinking": False})
            self.assertEqual(captured["reasoning_effort"], "none")
            self.assertEqual(
                captured["response_format"],
                {
                    "type": "json_schema",
                    "json_schema": {
                        "name": "naming_suggestion",
                        "strict": True,
                        "schema": VALID_RESPONSE_SCHEMA,
                    },
                },
            )
            self.assertFalse(captured["stream"])
            client.close()

    def test_invalid_contract_raises_and_never_changes_source(self) -> None:
        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={"choices": [{"message": {"content": '{"suggested_stem":"bad"}'}}]},
            )

        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "original.txt"
            source.write_text("important", encoding="utf-8")
            client = httpx.Client(transport=httpx.MockTransport(handler))
            provider = LocalGemmaProvider(FakeRuntime(), client=client)
            with self.assertRaises(ProviderError):
                provider.analyze(
                    AnalysisInput(
                        source_path=source,
                        mime_type="text/plain",
                        kind="text",
                        text="important",
                    )
                )
            self.assertEqual(source.name, "original.txt")
            self.assertEqual(source.read_text(encoding="utf-8"), "important")
            client.close()

    def test_image_limit_is_enforced_before_runtime_start(self) -> None:
        runtime = FakeRuntime()
        provider = LocalGemmaProvider(
            runtime,
            ProviderConfig(max_images=1),
            client=httpx.Client(transport=httpx.MockTransport(lambda _request: httpx.Response(500))),
        )
        item = AnalysisInput(
            source_path=Path("capture.png"),
            mime_type="image/png",
            kind="image",
            image_paths=(Path("a.png"), Path("b.png")),
        )
        with self.assertRaises(ProviderError):
            provider.analyze(item)
        self.assertEqual(runtime.jobs, 0)
        provider._client.close()


if __name__ == "__main__":
    unittest.main()
