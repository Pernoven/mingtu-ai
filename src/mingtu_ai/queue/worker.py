"""A dependency-free queue that shares one runtime across each drained batch."""

from __future__ import annotations

from collections import deque
from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
import threading
import time
from typing import Generic, Protocol, TypeVar
from uuid import uuid4


RuntimeT = TypeVar("RuntimeT")
ResultT = TypeVar("ResultT")


DEFAULT_MAX_PENDING_TASKS = 256


class TaskState(StrEnum):
    """Lifecycle states for queue tasks."""

    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


_TRANSITIONS = {
    TaskState.PENDING: {TaskState.RUNNING, TaskState.FAILED, TaskState.CANCELLED},
    TaskState.RUNNING: {TaskState.SUCCEEDED, TaskState.FAILED},
    TaskState.SUCCEEDED: set(),
    TaskState.FAILED: set(),
    TaskState.CANCELLED: set(),
}


class InvalidTaskTransition(RuntimeError):
    """A queue task was asked to make an invalid state transition."""

    pass


class QueueCapacityError(RuntimeError):
    """A task was rejected because the pending-work limit was reached."""

    pass


@dataclass(slots=True)
class QueueTask(Generic[ResultT]):
    """Represent one admitted or rejected queue submission."""

    path: Path
    id: str = field(default_factory=lambda: uuid4().hex)
    state: TaskState = TaskState.PENDING
    created_at: float = field(default_factory=time.time)
    started_at: float | None = None
    finished_at: float | None = None
    result: ResultT | None = None
    error: str | None = None

    def transition(self, new_state: TaskState, *, now: float | None = None) -> None:
        """Move the task to ``new_state`` and record lifecycle timestamps."""

        if new_state not in _TRANSITIONS[self.state]:
            raise InvalidTaskTransition(f"cannot transition {self.state} to {new_state}")
        timestamp = time.time() if now is None else now
        self.state = new_state
        if new_state is TaskState.RUNNING:
            self.started_at = timestamp
        elif new_state in (TaskState.SUCCEEDED, TaskState.FAILED, TaskState.CANCELLED):
            self.finished_at = timestamp


class RuntimeFactory(Protocol[RuntimeT]):
    """Create the context shared by one drained queue batch."""

    def __call__(self) -> AbstractContextManager[RuntimeT]: ...


class TaskHandler(Protocol[RuntimeT, ResultT]):
    """Process one admitted path with the current batch runtime."""

    def __call__(self, path: Path, runtime: RuntimeT) -> ResultT: ...


TaskCallback = Callable[[QueueTask[ResultT]], None]


class SingleWorkerQueue(Generic[RuntimeT, ResultT]):
    """FIFO queue that never invokes two handlers concurrently.

    The runtime context is entered once before the first pending task and kept
    alive until the queue is empty.  Tasks arriving while a task is running join
    that same batch and therefore avoid another expensive model cold start.
    """

    def __init__(
        self,
        *,
        runtime_factory: RuntimeFactory[RuntimeT],
        handler: TaskHandler[RuntimeT, ResultT],
        on_change: TaskCallback[ResultT] | None = None,
        thread_name: str = "mingtu-ai-worker",
        terminal_task_retention: int = 1024,
        max_pending_tasks: int = DEFAULT_MAX_PENDING_TASKS,
    ) -> None:
        if isinstance(terminal_task_retention, bool) or not isinstance(
            terminal_task_retention, int
        ):
            raise TypeError("terminal_task_retention must be an integer")
        if terminal_task_retention < 0:
            raise ValueError("terminal_task_retention must not be negative")
        if isinstance(max_pending_tasks, bool) or not isinstance(max_pending_tasks, int):
            raise TypeError("max_pending_tasks must be an integer")
        if max_pending_tasks < 0:
            raise ValueError("max_pending_tasks must not be negative")
        self._runtime_factory = runtime_factory
        self._handler = handler
        self._on_change = on_change
        self._thread_name = thread_name
        self._terminal_task_retention = terminal_task_retention
        self._max_pending_tasks = max_pending_tasks
        self._pending: deque[QueueTask[ResultT]] = deque()
        self._tasks: dict[str, QueueTask[ResultT]] = {}
        self._terminal_task_ids: deque[str] = deque()
        self._condition = threading.Condition()
        self._drain_lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._stopping = False
        self._active = False

    def submit(self, path: Path | str) -> QueueTask[ResultT]:
        """Submit a path without blocking.

        When pending capacity is exhausted, the returned task is already in the
        terminal ``FAILED`` state with a :class:`QueueCapacityError` description.
        The normal ``on_change`` callback is still emitted, allowing a daemon to
        record and notify the rejection without crashing its filesystem monitor.
        """

        task = QueueTask[ResultT](path=Path(path).absolute())
        with self._condition:
            if self._stopping:
                raise RuntimeError("queue is stopping")
            self._tasks[task.id] = task
            if len(self._pending) >= self._max_pending_tasks:
                task.error = self._describe_error(
                    QueueCapacityError(
                        "pending task capacity "
                        f"{self._max_pending_tasks} reached; submission rejected"
                    )
                )
                task.transition(TaskState.FAILED)
                self._remember_terminal_locked(task)
            else:
                self._pending.append(task)
            self._condition.notify_all()
        self._emit(task)
        return task

    def start(self) -> None:
        """Start the single background worker if it is not already running."""

        with self._condition:
            if self._thread is not None and self._thread.is_alive():
                return
            if self._stopping:
                raise RuntimeError("queue is stopping")
            self._thread = threading.Thread(
                target=self._worker,
                name=self._thread_name,
                daemon=True,
            )
            self._thread.start()

    def run_until_idle(self) -> list[QueueTask[ResultT]]:
        """Synchronously process the current batch, mainly for ``--once``."""

        with self._condition:
            if self._thread is not None and self._thread.is_alive():
                raise RuntimeError("background worker is already running")
        return self._drain_one_batch()

    def cancel(self, task_id: str) -> bool:
        """Cancel a pending task and return whether cancellation succeeded."""

        with self._condition:
            task = self._tasks.get(task_id)
            if task is None or task.state is not TaskState.PENDING:
                return False
            try:
                self._pending.remove(task)
            except ValueError:
                return False
            task.transition(TaskState.CANCELLED)
            self._remember_terminal_locked(task)
            self._condition.notify_all()
        self._emit(task)
        return True

    def get(self, task_id: str) -> QueueTask[ResultT] | None:
        """Return a currently tracked task by identifier."""

        with self._condition:
            return self._tasks.get(task_id)

    @property
    def pending_count(self) -> int:
        """Return the number of admitted tasks waiting for the worker."""

        with self._condition:
            return len(self._pending)

    @property
    def max_pending_tasks(self) -> int:
        """Return the configured pending admission limit."""

        return self._max_pending_tasks

    @property
    def tracked_task_count(self) -> int:
        """Return pending, running, and retained terminal task count."""

        with self._condition:
            return len(self._tasks)

    @property
    def is_active(self) -> bool:
        """Return whether a runtime batch is currently active."""

        with self._condition:
            return self._active

    def join(self, timeout: float | None = None) -> bool:
        """Wait for pending and running work; return ``False`` on timeout."""

        deadline = None if timeout is None else time.monotonic() + timeout
        with self._condition:
            while self._pending or self._active:
                remaining = None if deadline is None else deadline - time.monotonic()
                if remaining is not None and remaining <= 0:
                    return False
                self._condition.wait(remaining)
        return True

    def stop(
        self,
        *,
        wait: bool = True,
        drain: bool = True,
        timeout: float | None = None,
    ) -> None:
        """Stop admission and optionally drain or cancel accepted pending work."""

        cancelled: list[QueueTask[ResultT]] = []
        with self._condition:
            self._stopping = True
            if not drain:
                while self._pending:
                    task = self._pending.popleft()
                    task.transition(TaskState.CANCELLED)
                    self._remember_terminal_locked(task)
                    cancelled.append(task)
            thread = self._thread
            self._condition.notify_all()
        for task in cancelled:
            self._emit(task)
        if wait and thread is not None and thread is not threading.current_thread():
            thread.join(timeout)

    def _worker(self) -> None:
        while True:
            with self._condition:
                while not self._pending and not self._stopping:
                    self._condition.wait()
                if not self._pending and self._stopping:
                    self._condition.notify_all()
                    return
            self._drain_one_batch()

    def _drain_one_batch(self) -> list[QueueTask[ResultT]]:
        # This second lock also protects callers that invoke ``run_until_idle``
        # concurrently before a background worker exists.
        with self._drain_lock:
            return self._drain_one_batch_locked()

    def _drain_one_batch_locked(self) -> list[QueueTask[ResultT]]:
        with self._condition:
            if not self._pending:
                return []
            self._active = True
            self._condition.notify_all()

        handled: list[QueueTask[ResultT]] = []
        try:
            try:
                runtime_context = self._runtime_factory()
                with runtime_context as runtime:
                    while task := self._take_next():
                        handled.append(task)
                        self._run_task(task, runtime)
            except Exception as error:
                # Runtime startup/shutdown failure is a batch-level failure.  A
                # future submission may form a new batch and try again.
                failed = self._fail_current_batch(error)
                handled.extend(failed)
        finally:
            with self._condition:
                self._active = False
                self._condition.notify_all()
        return handled

    def _take_next(self) -> QueueTask[ResultT] | None:
        with self._condition:
            if not self._pending:
                return None
            task = self._pending.popleft()
            task.transition(TaskState.RUNNING)
            self._condition.notify_all()
        self._emit(task)
        return task

    def _run_task(self, task: QueueTask[ResultT], runtime: RuntimeT) -> None:
        try:
            result = self._handler(task.path, runtime)
        except Exception as error:
            with self._condition:
                task.error = self._describe_error(error)
                task.transition(TaskState.FAILED)
                self._remember_terminal_locked(task)
                self._condition.notify_all()
        else:
            with self._condition:
                task.result = result
                task.transition(TaskState.SUCCEEDED)
                self._remember_terminal_locked(task)
                self._condition.notify_all()
        self._emit(task)

    def _fail_current_batch(self, error: BaseException) -> list[QueueTask[ResultT]]:
        failed: list[QueueTask[ResultT]] = []
        with self._condition:
            while self._pending:
                task = self._pending.popleft()
                task.error = self._describe_error(error)
                task.transition(TaskState.FAILED)
                self._remember_terminal_locked(task)
                failed.append(task)
            self._condition.notify_all()
        for task in failed:
            self._emit(task)
        return failed

    @staticmethod
    def _describe_error(error: BaseException) -> str:
        message = str(error).strip()
        return f"{type(error).__name__}: {message}" if message else type(error).__name__

    def _remember_terminal_locked(self, task: QueueTask[ResultT]) -> None:
        """Retain only the configured number of most recent terminal tasks."""

        self._terminal_task_ids.append(task.id)
        while len(self._terminal_task_ids) > self._terminal_task_retention:
            expired_id = self._terminal_task_ids.popleft()
            expired = self._tasks.get(expired_id)
            if expired is not None and expired.state in {
                TaskState.SUCCEEDED,
                TaskState.FAILED,
                TaskState.CANCELLED,
            }:
                del self._tasks[expired_id]

    def _emit(self, task: QueueTask[ResultT]) -> None:
        if self._on_change is None:
            return
        try:
            self._on_change(task)
        except Exception:
            # UI observers must never stop filesystem processing.
            return
