"""Bounded capture for local parser subprocesses.

``subprocess.run(..., stdout=PIPE)`` buffers until a child exits, so checking the
result length afterwards is too late for an untrusted input file.  This module
drains both pipes incrementally and terminates only the exact ``Popen`` handle it
created when a byte or time limit is reached.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import os
import selectors
import subprocess
import time
from typing import Literal, Sequence


StreamName = Literal["stdout", "stderr"]


@dataclass(frozen=True, slots=True)
class BoundedProcessResult:
    """Completed child result whose captured streams respected their limits."""

    returncode: int
    stdout: bytes
    stderr: bytes


class BoundedProcessError(RuntimeError):
    """A child was stopped because a configured resource boundary was reached."""


class ProcessCaptureTimeout(BoundedProcessError):
    """The owned child did not finish before its wall-clock deadline."""


class ProcessOutputLimitError(BoundedProcessError):
    """One captured stream produced more than its allowed number of bytes."""

    def __init__(self, stream: StreamName, limit: int) -> None:
        self.stream = stream
        self.limit = limit
        super().__init__(f"{stream} exceeded the {limit}-byte limit")


@dataclass(slots=True)
class _CaptureState:
    name: StreamName
    limit: int
    data: bytearray = field(default_factory=bytearray)


def run_bounded_process(
    command: Sequence[str | os.PathLike[str]],
    *,
    timeout: float,
    stdout_limit: int | None,
    stderr_limit: int | None,
    pass_fds: Sequence[int] = (),
) -> BoundedProcessResult:
    """Run one child while enforcing capture limits during output generation.

    A ``None`` stream limit redirects that stream to ``DEVNULL``.  Captured
    streams are read concurrently so a noisy stderr cannot deadlock a child
    whose stdout is also being consumed.  On timeout or overflow, cleanup sends
    signals only through the ``Popen`` object created in this function.
    ``pass_fds`` is restricted to explicitly owned input descriptors that a
    child parser accesses through ``/proc/self/fd``.
    """

    if timeout <= 0:
        raise ValueError("timeout must be positive")
    for name, limit in (("stdout", stdout_limit), ("stderr", stderr_limit)):
        if limit is not None and limit < 0:
            raise ValueError(f"{name}_limit must be non-negative or None")

    process = subprocess.Popen(
        list(command),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE if stdout_limit is not None else subprocess.DEVNULL,
        stderr=subprocess.PIPE if stderr_limit is not None else subprocess.DEVNULL,
        close_fds=True,
        pass_fds=tuple(pass_fds),
    )
    selector = selectors.DefaultSelector()
    states: dict[StreamName, _CaptureState] = {}
    deadline = time.monotonic() + timeout

    try:
        for name, pipe, limit in (
            ("stdout", process.stdout, stdout_limit),
            ("stderr", process.stderr, stderr_limit),
        ):
            if pipe is None or limit is None:
                continue
            state = _CaptureState(name=name, limit=limit)
            states[name] = state
            selector.register(pipe, selectors.EVENT_READ, state)

        while selector.get_map():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ProcessCaptureTimeout("subprocess exceeded its time limit")
            events = selector.select(remaining)
            if not events:
                raise ProcessCaptureTimeout("subprocess exceeded its time limit")
            for key, _mask in events:
                state: _CaptureState = key.data
                # Read at most one byte beyond the limit.  That byte proves an
                # overflow without allowing a large pipe read into memory.
                read_size = min(64 * 1024, state.limit + 1 - len(state.data))
                try:
                    chunk = os.read(key.fd, max(1, read_size))
                except BlockingIOError:
                    continue
                if not chunk:
                    selector.unregister(key.fileobj)
                    key.fileobj.close()
                    continue
                state.data.extend(chunk)
                if len(state.data) > state.limit:
                    del state.data[state.limit :]
                    raise ProcessOutputLimitError(state.name, state.limit)

        returncode = process.poll()
        if returncode is None:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ProcessCaptureTimeout("subprocess exceeded its time limit")
            try:
                returncode = process.wait(timeout=remaining)
            except subprocess.TimeoutExpired as exc:
                raise ProcessCaptureTimeout("subprocess exceeded its time limit") from exc
        return BoundedProcessResult(
            returncode=returncode,
            stdout=bytes(states["stdout"].data) if "stdout" in states else b"",
            stderr=bytes(states["stderr"].data) if "stderr" in states else b"",
        )
    except BaseException:
        _stop_owned_process(process)
        raise
    finally:
        selector.close()
        for pipe in (process.stdout, process.stderr):
            if pipe is not None and not pipe.closed:
                pipe.close()


def _stop_owned_process(process: subprocess.Popen[bytes]) -> None:
    """Reap only *process*, escalating from terminate to kill if necessary."""

    if process.poll() is not None:
        process.wait()
        return
    try:
        process.terminate()
    except ProcessLookupError:
        process.wait()
        return
    try:
        process.wait(timeout=1.0)
        return
    except subprocess.TimeoutExpired:
        process.kill()
    try:
        process.wait(timeout=1.0)
    except subprocess.TimeoutExpired as exc:
        raise BoundedProcessError("owned subprocess did not exit after kill") from exc
