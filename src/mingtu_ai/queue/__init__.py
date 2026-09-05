"""Single-worker task queue with an explicit state machine."""

from .worker import (
    DEFAULT_MAX_PENDING_TASKS,
    InvalidTaskTransition,
    QueueCapacityError,
    QueueTask,
    SingleWorkerQueue,
    TaskState,
)

__all__ = [
    "DEFAULT_MAX_PENDING_TASKS",
    "InvalidTaskTransition",
    "QueueCapacityError",
    "QueueTask",
    "SingleWorkerQueue",
    "TaskState",
]
