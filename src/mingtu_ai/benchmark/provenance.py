"""Bounded-memory provenance collection for reproducible benchmark reports."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
import selectors
import signal
import stat
import subprocess
import time
from typing import Any


HASH_CHUNK_BYTES = 1024 * 1024
VERSION_OUTPUT_BYTES = 16 * 1024
VERSION_TIMEOUT_SECONDS = 5.0


def sha256_file(path: Path | str, *, chunk_bytes: int = HASH_CHUNK_BYTES) -> str:
    """Hash one regular file incrementally without loading it into memory."""

    if chunk_bytes <= 0:
        raise ValueError("chunk_bytes must be greater than zero")
    source = Path(path).expanduser().absolute()
    info = source.stat()
    if not stat.S_ISREG(info.st_mode):
        raise ValueError(f"fingerprint source is not a regular file: {source}")
    digest = hashlib.sha256()
    with source.open("rb", buffering=0) as stream:
        while chunk := stream.read(chunk_bytes):
            digest.update(chunk)
    return digest.hexdigest()


def fingerprint_file(path: Path | str) -> dict[str, Any]:
    """Return a stable path and SHA-256 record for one required artifact."""

    source = Path(path).expanduser().absolute()
    return {
        "status": "hashed",
        "path": str(source),
        "sha256": sha256_file(source),
    }


def skipped_fingerprint(reason: str) -> dict[str, Any]:
    """Return an explicit record for an optional artifact that was not supplied."""

    return {
        "status": "skipped",
        "path": None,
        "sha256": None,
        "reason": reason,
    }


def _stop_owned_process(process: subprocess.Popen[bytes]) -> None:
    """Stop only the process group created for a provenance command."""

    if process.poll() is not None:
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
        process.wait(timeout=0.5)
    except (OSError, subprocess.TimeoutExpired):
        if process.poll() is None:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except OSError:
                pass
            try:
                process.wait(timeout=0.5)
            except subprocess.TimeoutExpired:
                pass


def bounded_version_output(
    executable: Path | str,
    *,
    max_bytes: int = VERSION_OUTPUT_BYTES,
    timeout_seconds: float = VERSION_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    """Capture ``--version`` with strict output and time bounds.

    The child runs in its own process group. If it exceeds either bound, only
    that owned group is terminated. Standard error is merged into standard
    output because llama.cpp builds may use either stream for version details.
    """

    if max_bytes <= 0:
        raise ValueError("max_bytes must be greater than zero")
    if timeout_seconds <= 0:
        raise ValueError("timeout_seconds must be greater than zero")
    binary = Path(executable).expanduser().absolute()
    try:
        process = subprocess.Popen(
            [str(binary), "--version"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    except OSError as error:
        return {
            "status": "error",
            "exit_code": None,
            "output": None,
            "truncated": False,
            "detail": f"{type(error).__name__}: could not execute --version",
        }

    assert process.stdout is not None
    descriptor = process.stdout.fileno()
    os.set_blocking(descriptor, False)
    selector = selectors.DefaultSelector()
    selector.register(descriptor, selectors.EVENT_READ)
    captured = bytearray()
    truncated = False
    timed_out = False
    deadline = time.monotonic() + timeout_seconds
    try:
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                timed_out = True
                break
            events = selector.select(min(remaining, 0.1))
            if not events:
                if process.poll() is not None:
                    try:
                        chunk = os.read(descriptor, max_bytes + 1 - len(captured))
                    except BlockingIOError:
                        chunk = b""
                    if chunk:
                        available = max_bytes - len(captured)
                        captured.extend(chunk[:available])
                        truncated = len(chunk) > available
                    break
                continue
            try:
                chunk = os.read(descriptor, min(4096, max_bytes + 1 - len(captured)))
            except BlockingIOError:
                continue
            if not chunk:
                break
            available = max_bytes - len(captured)
            captured.extend(chunk[:available])
            if len(chunk) > available:
                truncated = True
                break
    finally:
        selector.close()
        if timed_out or truncated:
            _stop_owned_process(process)
        else:
            try:
                process.wait(timeout=max(0.1, deadline - time.monotonic()))
            except subprocess.TimeoutExpired:
                timed_out = True
                _stop_owned_process(process)
        process.stdout.close()

    exit_code = process.poll()
    output = bytes(captured).decode("utf-8", errors="replace").strip() or None
    if timed_out:
        status = "timeout"
    elif truncated:
        status = "truncated"
    elif exit_code == 0:
        status = "ok"
    else:
        status = "nonzero_exit"
    return {
        "status": status,
        "exit_code": exit_code,
        "output": output,
        "truncated": truncated,
        "max_output_bytes": max_bytes,
        "timeout_seconds": timeout_seconds,
    }


def collect_runtime_provenance(
    *,
    model_path: Path | str,
    mmproj_path: Path | str | None,
    llama_server_path: Path | str,
) -> dict[str, Any]:
    """Fingerprint runtime artifacts and collect bounded llama.cpp build text."""

    server = fingerprint_file(llama_server_path)
    server["version"] = bounded_version_output(llama_server_path)
    return {
        "hash_algorithm": "sha256",
        "model": fingerprint_file(model_path),
        "mmproj": (
            fingerprint_file(mmproj_path)
            if mmproj_path is not None
            else skipped_fingerprint("no multimodal projector was provided")
        ),
        "llama_server": server,
    }
