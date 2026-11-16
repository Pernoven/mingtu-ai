"""Validated configuration for local inference.

The network boundary is deliberately not configurable in the hackathon build:
the managed server always listens on ``127.0.0.1:3001``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from mingtu_ai.queue import DEFAULT_MAX_PENDING_TASKS


_FORBIDDEN_EXTRA_ARGUMENTS = frozenset(
    {
        "-m",
        "--model",
        "--model-url",
        "--docker-repo",
        "--host",
        "--port",
        "--reuse-port",
        "-hf",
        "-hfr",
        "--hf-repo",
        "--hf-repo-draft",
        "--tools",
        "--agent",
        "-ag",
        "--ui-mcp-proxy",
        "--webui-mcp-proxy",
        "--mcp-servers-config",
        "--mcp-servers-json",
        "--reasoning",
        "-rea",
        "--reasoning-effort",
        "--reasoning-budget",
        "--chat-template-kwargs",
        "--api-prefix",
    }
)

_DOWNLOADING_PRESETS = (
    "--embd-gemma-default",
    "--fim-qwen-",
    "--gpt-oss-",
    "--vision-gemma-",
    "--spec-default",
)


class LlamaServerConfig(BaseModel):
    """Settings for the one llama-server process owned by this application."""

    model_config = ConfigDict(extra="forbid", validate_assignment=True)

    binary: Path = Path("/usr/bin/llama-server")
    model_path: Path | None = None
    mmproj_path: Path | None = None
    host: Literal["127.0.0.1"] = "127.0.0.1"
    port: Literal[3001] = 3001
    model_alias: str = Field(default="local-gemma", min_length=1, max_length=80)
    context_size: int = Field(default=8192, ge=512, le=262_144)
    n_gpu_layers: Annotated[int, Field(ge=0, le=999)] | Literal["auto", "all"] = "auto"
    mmproj_offload: bool | Literal["auto"] = "auto"
    health_timeout_seconds: float = Field(default=180.0, gt=0.0, le=900.0)
    health_poll_seconds: float = Field(default=0.25, gt=0.0, le=5.0)
    stop_timeout_seconds: float = Field(default=10.0, gt=0.0, le=60.0)
    idle_shutdown_seconds: float = Field(default=1.0, ge=0.0, le=60.0)
    extra_args: tuple[str, ...] = ()

    @field_validator("extra_args")
    @classmethod
    def prevent_boundary_overrides(cls, arguments: tuple[str, ...]) -> tuple[str, ...]:
        for argument in arguments:
            option = argument.split("=", 1)[0]
            if option in _FORBIDDEN_EXTRA_ARGUMENTS:
                raise ValueError(f"extra_args may not override {option}")
            if option.startswith(_DOWNLOADING_PRESETS):
                raise ValueError(f"extra_args may not enable downloading preset {option}")
        return arguments


class ProviderConfig(BaseModel):
    """Generation limits for the local OpenAI-compatible endpoint."""

    model_config = ConfigDict(extra="forbid", validate_assignment=True)

    request_timeout_seconds: float = Field(default=120.0, gt=0.0, le=900.0)
    max_tokens: int = Field(default=512, ge=64, le=4096)
    temperature: float = Field(default=0.1, ge=0.0, le=1.0)
    max_text_chars: int = Field(default=16_000, ge=256, le=200_000)
    max_images: int = Field(default=4, ge=1, le=16)
    max_image_bytes: int = Field(default=20 * 1024 * 1024, ge=1024)


class AppConfig(BaseModel):
    """Top-level core configuration used by the app and CLI."""

    model_config = ConfigDict(extra="forbid", validate_assignment=True)

    llama: LlamaServerConfig = Field(default_factory=LlamaServerConfig)
    provider: ProviderConfig = Field(default_factory=ProviderConfig)
    max_pending_tasks: int = Field(
        default=DEFAULT_MAX_PENDING_TASKS,
        ge=0,
        le=100_000,
    )
