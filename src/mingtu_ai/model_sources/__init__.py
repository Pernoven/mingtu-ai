"""Read-only discovery of local model files managed by other applications.

Exports are loaded lazily so ``python -m mingtu_ai.model_sources.ollama`` does
not import its target module once through the package before executing it.
"""

from __future__ import annotations

import importlib
from typing import Any

__all__ = [
    "OllamaModelReport",
    "OllamaSourceError",
    "parse_modelfile_blob_path",
    "resolve_ollama_model",
    "resolve_ollama_path",
]


def __getattr__(name: str) -> Any:
    if name not in __all__:
        raise AttributeError(name)
    module = importlib.import_module(f"{__name__}.ollama")
    return getattr(module, name)
