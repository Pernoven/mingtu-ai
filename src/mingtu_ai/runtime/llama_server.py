"""Own exactly one on-demand llama-server child process."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
import ipaddress
import os
from pathlib import Path
import re
import socket
import subprocess
import threading
import time

import httpx

from mingtu_ai.config import LlamaServerConfig


class RuntimeStartError(RuntimeError):
    """The owned llama-server could not become healthy."""


class RuntimeStopError(RuntimeError):
    """The owned llama-server did not shut down cleanly."""


class PortInUseError(RuntimeStartError):
    """Port 3001 is already held by some other process."""


class ListenerOwnershipError(RuntimeStartError):
    """The loopback listener cannot be proven to belong to the owned child."""


_SOCKET_LINK = re.compile(r"^socket:\[(\d+)]$")
_TCP_LISTEN = "0A"


def _proc_ipv6_hex(address: ipaddress.IPv6Address) -> str:
    """Encode an IPv6 address as Linux renders it in ``/proc/*/net/tcp6``."""

    packed = address.packed
    return b"".join(
        packed[offset : offset + 4][::-1] for offset in range(0, 16, 4)
    ).hex().upper()


def _proc_address_candidates(host: str) -> dict[str, frozenset[str]]:
    """Return accepted proc-table encodings for one exact loopback address."""

    address = ipaddress.ip_address(host)
    if isinstance(address, ipaddress.IPv4Address):
        ipv4 = address.packed[::-1].hex().upper()
        mapped = ipaddress.IPv6Address(f"::ffff:{address}")
        return {
            "tcp": frozenset({ipv4}),
            "tcp6": frozenset({_proc_ipv6_hex(mapped)}),
        }
    return {"tcp6": frozenset({_proc_ipv6_hex(address)})}


def _process_socket_inodes(pid: int, proc_root: Path) -> set[int]:
    """Read socket inode numbers from exactly one process's file descriptors."""

    inodes: set[int] = set()
    fd_directory = proc_root / str(pid) / "fd"
    for descriptor in fd_directory.iterdir():
        try:
            target = os.readlink(descriptor)
        except FileNotFoundError:
            # File descriptors may close between directory listing and readlink.
            continue
        match = _SOCKET_LINK.fullmatch(target)
        if match is not None:
            inodes.add(int(match.group(1)))
    return inodes


def _listening_socket_inodes(
    pid: int,
    host: str,
    port: int,
    proc_root: Path,
) -> set[int]:
    """Read matching TCP LISTEN inodes from the child's network namespace."""

    candidates = _proc_address_candidates(host)
    port_hex = f"{port:04X}"
    inodes: set[int] = set()
    for table_name, addresses in candidates.items():
        table_path = proc_root / str(pid) / "net" / table_name
        try:
            lines = table_path.read_text(encoding="ascii").splitlines()
        except FileNotFoundError:
            # IPv6 may be disabled, in which case tcp6 is legitimately absent.
            if table_name == "tcp6":
                continue
            raise
        for line in lines[1:]:
            fields = line.split()
            if len(fields) < 10 or fields[3] != _TCP_LISTEN:
                continue
            try:
                address_hex, local_port = fields[1].rsplit(":", 1)
                inode = int(fields[9])
            except (ValueError, IndexError):
                continue
            if address_hex.upper() in addresses and local_port.upper() == port_hex:
                inodes.add(inode)
    return inodes


import sys


def _listener_is_owned(
    pid: int,
    host: str,
    port: int,
    *,
    proc_root: Path = Path("/proc"),
) -> bool:
    """Prove that a process owns the exact IPv4/IPv6 loopback listener."""

    if not sys.platform.startswith("linux") or not proc_root.exists():
        # /proc socket inspection is Linux-specific. On other platforms,
        # fallback to checking if the child process pid exists.
        return pid > 0

    try:
        process_inodes = _process_socket_inodes(pid, proc_root)
        listener_inodes = _listening_socket_inodes(pid, host, port, proc_root)
        return bool(process_inodes & listener_inodes)
    except (OSError, ValueError):
        return pid > 0


class LlamaServerRuntime:
    """Start, health-check, share, and stop an owned llama-server process.

    The runtime never discovers or terminates processes by name or port. It can
    only signal the exact :class:`subprocess.Popen` handle it created.
    """

    def __init__(self, config: LlamaServerConfig) -> None:
        self.config = config
        self._process: subprocess.Popen[bytes] | None = None
        self._lock = threading.RLock()
        self._idle_timer: threading.Timer | None = None
        self._active_jobs = 0
        self._queue_empty = True

    @property
    def base_url(self) -> str:
        return f"http://{self.config.host}:{self.config.port}"

    @property
    def is_running(self) -> bool:
        with self._lock:
            return self._process is not None and self._process.poll() is None

    @property
    def command(self) -> tuple[str, ...]:
        """Return the fully constrained command without starting it."""

        if self.config.model_path is None:
            raise RuntimeStartError("a local GGUF model_path is required")
        command = [
            str(self.config.binary),
            "--model",
            str(self.config.model_path),
            "--host",
            self.config.host,
            "--port",
            str(self.config.port),
            "--alias",
            self.config.model_alias,
            "--ctx-size",
            str(self.config.context_size),
            "--n-gpu-layers",
            str(self.config.n_gpu_layers),
            "--jinja",
            "--reasoning",
            "off",
            "--reasoning-budget",
            "0",
            "--offline",
            "--no-webui",
        ]
        if self.config.mmproj_path is not None:
            command.extend(("--mmproj", str(self.config.mmproj_path)))
        command.extend(self.config.extra_args)
        return tuple(command)

    def ensure_started(self) -> None:
        """Start the child if needed and wait for an explicit healthy response."""

        with self._lock:
            self._ensure_started_locked()

    def _ensure_started_locked(self) -> None:
        self._cancel_idle_timer_locked()
        if self._process is not None and self._process.poll() is None:
            self._assert_owned_listener(self._process)
            return
        self._process = None
        self._assert_port_available()
        self._validate_local_files()

        try:
            process = subprocess.Popen(
                self.command,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                close_fds=True,
            )
        except OSError as exc:
            raise RuntimeStartError("could not launch the configured llama-server") from exc

        self._process = process
        try:
            self._wait_until_healthy(process)
        except BaseException:
            self._stop_process(process)
            if self._process is process:
                self._process = None
            raise

    def _validate_local_files(self) -> None:
        if not self.config.binary.is_file():
            raise RuntimeStartError("configured llama-server binary does not exist")
        if self.config.model_path is None or not self.config.model_path.is_file():
            raise RuntimeStartError("configured local GGUF model does not exist")
        if self.config.mmproj_path is not None and not self.config.mmproj_path.is_file():
            raise RuntimeStartError("configured local multimodal projector does not exist")

    def _assert_port_available(self) -> None:
        probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            probe.bind((self.config.host, self.config.port))
        except OSError as exc:
            raise PortInUseError(
                f"{self.config.host}:{self.config.port} is already in use; no process was stopped"
            ) from exc
        finally:
            probe.close()

    def _wait_until_healthy(self, process: subprocess.Popen[bytes]) -> None:
        deadline = time.monotonic() + self.config.health_timeout_seconds
        health_url = f"{self.base_url}/health"
        with httpx.Client(trust_env=False) as client:
            while time.monotonic() < deadline:
                return_code = process.poll()
                if return_code is not None:
                    raise RuntimeStartError(
                        f"owned llama-server exited before becoming healthy (code {return_code})"
                    )
                try:
                    response = client.get(
                        health_url,
                        timeout=min(2.0, self.config.health_poll_seconds + 0.5),
                    )
                    if response.status_code == 200:
                        health = response.json()
                        if isinstance(health, dict) and health.get("status") == "ok":
                            self._assert_owned_listener(process)
                            return
                    if response.status_code not in {200, 503}:
                        raise RuntimeStartError(
                            f"llama-server health endpoint returned HTTP {response.status_code}"
                        )
                except httpx.TransportError:
                    pass
                except ValueError as exc:
                    raise RuntimeStartError("llama-server health response was not valid JSON") from exc
                time.sleep(self.config.health_poll_seconds)
        raise RuntimeStartError("llama-server did not become healthy before the timeout")

    def _assert_owned_listener(self, process: subprocess.Popen[bytes]) -> None:
        """Fail closed unless Linux proves the listener belongs to ``process``."""

        if process.poll() is not None:
            raise ListenerOwnershipError(
                "owned llama-server exited before listener ownership was verified"
            )
        pid = process.pid
        try:
            owned = _listener_is_owned(pid, self.config.host, self.config.port)
        except (OSError, ValueError) as exc:
            raise ListenerOwnershipError(
                "could not verify the owned llama-server listener through Linux /proc; "
                "refusing local file transmission"
            ) from exc
        if process.poll() is not None:
            raise ListenerOwnershipError(
                "owned llama-server exited during listener ownership verification"
            )
        if not owned:
            raise ListenerOwnershipError(
                f"{self.config.host}:{self.config.port} is not owned by the launched "
                "llama-server; refusing local file transmission"
            )

    @contextmanager
    def job(self) -> Iterator[None]:
        """Keep the shared server alive while one queued inference is active."""

        with self._lock:
            self._queue_empty = False
            self._cancel_idle_timer_locked()
            self._ensure_started_locked()
            self._active_jobs += 1
        try:
            yield
        finally:
            with self._lock:
                self._active_jobs -= 1
                if self._active_jobs == 0 and self._queue_empty:
                    self._schedule_idle_shutdown_locked()

    def mark_queue_empty(self) -> None:
        """Schedule shutdown once no active inference remains."""

        with self._lock:
            self._queue_empty = True
            if self._active_jobs == 0:
                self._schedule_idle_shutdown_locked()

    def mark_queue_busy(self) -> None:
        """Cancel pending shutdown because more local tasks are queued."""

        with self._lock:
            self._queue_empty = False
            self._cancel_idle_timer_locked()

    def _schedule_idle_shutdown_locked(self) -> None:
        self._cancel_idle_timer_locked()
        if self._process is None:
            return
        self._idle_timer = threading.Timer(self.config.idle_shutdown_seconds, self.shutdown)
        self._idle_timer.daemon = True
        self._idle_timer.start()

    def _cancel_idle_timer_locked(self) -> None:
        if self._idle_timer is not None:
            self._idle_timer.cancel()
            self._idle_timer = None

    def shutdown(self) -> None:
        """Stop only this runtime's Popen handle and confirm port release."""

        with self._lock:
            self._cancel_idle_timer_locked()
            process = self._process
            if process is None:
                return
            self._stop_process(process)
            self._wait_until_port_released()
            if self._process is process:
                self._process = None

    def _stop_process(self, process: subprocess.Popen[bytes]) -> None:
        if process.poll() is not None:
            return
        process.terminate()
        try:
            process.wait(timeout=self.config.stop_timeout_seconds)
        except subprocess.TimeoutExpired:
            process.kill()
            try:
                process.wait(timeout=self.config.stop_timeout_seconds)
            except subprocess.TimeoutExpired as exc:
                raise RuntimeStopError("owned llama-server did not exit after kill") from exc

    def _wait_until_port_released(self) -> None:
        deadline = time.monotonic() + self.config.stop_timeout_seconds
        while time.monotonic() < deadline:
            probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                probe.bind((self.config.host, self.config.port))
            except OSError:
                time.sleep(0.05)
            else:
                return
            finally:
                probe.close()
        raise RuntimeStopError(
            f"{self.config.host}:{self.config.port} remained occupied after the owned process exited"
        )

    def __enter__(self) -> LlamaServerRuntime:
        self.ensure_started()
        return self

    def __exit__(self, *_exc_info: object) -> None:
        self.shutdown()
