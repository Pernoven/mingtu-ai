"""Strict local Gemma client for llama.cpp's OpenAI-compatible endpoint."""

from __future__ import annotations

import base64
import mimetypes
import os
from pathlib import Path
import stat
from typing import Any

import httpx
from pydantic import ValidationError

from mingtu_ai.config import ProviderConfig
from mingtu_ai.adapters.models import AdapterError
from mingtu_ai.adapters.secure_input import open_regular_file, read_bounded
from mingtu_ai.models import AnalysisInput, NamingSuggestion
from mingtu_ai.naming import build_naming_prompt
from mingtu_ai.runtime import LlamaServerRuntime


class ProviderError(RuntimeError):
    """Local inference failed without changing the source file."""


class ProviderInputError(ProviderError):
    """The adapted local input is missing or unsafe to send."""


class LocalGemmaProvider:
    """Analyze adapted content through one managed local llama-server."""

    def __init__(
        self,
        runtime: LlamaServerRuntime,
        config: ProviderConfig | None = None,
        *,
        client: httpx.Client | None = None,
    ) -> None:
        self.runtime = runtime
        self.config = config or ProviderConfig()
        self._client = client or httpx.Client(trust_env=False)
        self._owns_client = client is None

    def analyze(self, item: AnalysisInput) -> NamingSuggestion:
        """Return a validated suggestion; never rename or write ``item``."""

        payload = self._build_payload(item)
        try:
            with self.runtime.job():
                response = self._client.post(
                    f"{self.runtime.base_url}/v1/chat/completions",
                    json=payload,
                    timeout=self.config.request_timeout_seconds,
                )
                response.raise_for_status()
        except ProviderError:
            raise
        except Exception as exc:
            raise ProviderError("local Gemma request failed; the source file was not changed") from exc

        try:
            body = response.json()
            content = body["choices"][0]["message"]["content"]
            if not isinstance(content, str):
                raise TypeError("assistant content must be a JSON string")
            return NamingSuggestion.model_validate_json(content)
        except (KeyError, IndexError, TypeError, ValueError, ValidationError) as exc:
            raise ProviderError(
                "local Gemma returned an invalid naming contract; the source file was not changed"
            ) from exc

    def _build_payload(self, item: AnalysisInput) -> dict[str, Any]:
        if len(item.image_paths) > self.config.max_images:
            raise ProviderInputError("adapted input contains too many images")
        if item.image_file_descriptors and (
            len(item.image_file_descriptors) != len(item.image_paths)
        ):
            raise ProviderInputError("adapted image descriptor mapping is invalid")

        content: list[dict[str, Any]] = [
            {
                "type": "text",
                "text": build_naming_prompt(item, max_text_chars=self.config.max_text_chars),
            }
        ]
        descriptors = item.image_file_descriptors or (None,) * len(item.image_paths)
        for image_path, descriptor in zip(item.image_paths, descriptors, strict=True):
            content.append(
                {
                    "type": "image_url",
                    "image_url": {
                        "url": self._image_data_url(image_path, descriptor=descriptor)
                    },
                }
            )

        return {
            "model": self.runtime.config.model_alias,
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "You are a local file-naming engine. Understand the true semantic context "
                        "and purpose of the file. Identify specific entities, exact models, error "
                        "causes, software titles, or document topics. Never return generic scene "
                        "captions like 'white_car_on_road' or generic names like '螢幕截圖' or "
                        "'Screenshot'. Return exactly one JSON object matching the schema and no prose."
                    ),
                },
                {"role": "user", "content": content},
            ],
            "response_format": {
                "type": "json_schema",
                "json_schema": {
                    "name": "naming_suggestion",
                    "strict": True,
                    "schema": NamingSuggestion.model_json_schema(),
                },
            },
            "chat_template_kwargs": {"enable_thinking": False},
            "reasoning_effort": "none",
            "temperature": self.config.temperature,
            "max_tokens": self.config.max_tokens,
            "stream": False,
        }

    def _image_data_url(
        self,
        image_path: Path,
        *,
        descriptor: int | None = None,
    ) -> str:
        """Encode one regular image through a bounded descriptor-based read."""

        path = Path(image_path)
        mime_type, _encoding = mimetypes.guess_type(path.name)
        if mime_type is None or not mime_type.startswith("image/"):
            raise ProviderInputError("adapted image has an unsupported filename extension")

        try:
            if descriptor is None:
                with open_regular_file(path) as opened:
                    image_bytes = self._read_descriptor_image(opened.fd)
            else:
                duplicate = os.dup(descriptor)
                try:
                    image_bytes = self._read_descriptor_image(duplicate)
                finally:
                    os.close(duplicate)
        except (OSError, AdapterError) as exc:
            raise ProviderInputError("adapted image could not be read") from exc
        encoded = base64.b64encode(image_bytes).decode("ascii")
        return f"data:{mime_type};base64,{encoded}"

    def _read_descriptor_image(self, descriptor: int) -> bytes:
        """Read one stable regular-file descriptor without unbounded allocation."""

        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            raise ProviderInputError("adapted image must be a regular local file")
        if before.st_size > self.config.max_image_bytes:
            raise ProviderInputError("adapted image exceeds the configured size limit")
        payload, truncated = read_bounded(descriptor, self.config.max_image_bytes)
        after = os.fstat(descriptor)
        if (
            before.st_dev,
            before.st_ino,
            before.st_size,
            before.st_mtime_ns,
        ) != (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
        ):
            raise ProviderInputError("adapted image changed while it was being read")
        if truncated:
            raise ProviderInputError("adapted image exceeds the configured size limit")
        return payload

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def __enter__(self) -> LocalGemmaProvider:
        return self

    def __exit__(self, *_exc_info: object) -> None:
        self.close()
