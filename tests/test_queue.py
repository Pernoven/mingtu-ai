from __future__ import annotations

from contextlib import AbstractContextManager
from pathlib import Path
import threading
import unittest

from mingtu_ai.queue import (
    DEFAULT_MAX_PENDING_TASKS,
    QueueCapacityError,
    SingleWorkerQueue,
    TaskState,
)


class CountingRuntime(AbstractContextManager[dict[str, int]]):
    def __init__(self, counts: dict[str, int]) -> None:
        self.counts = counts

    def __enter__(self) -> dict[str, int]:
        self.counts["entered"] += 1
        return self.counts

    def __exit__(self, *args: object) -> None:
        self.counts["exited"] += 1


class FailingRuntime(AbstractContextManager[object]):
    def __enter__(self) -> object:
        raise RuntimeError("runtime unavailable")

    def __exit__(self, *args: object) -> None:
        return None


class QueueTests(unittest.TestCase):
    def test_batch_shares_one_runtime_and_preserves_fifo(self) -> None:
        counts = {"entered": 0, "exited": 0}
        order: list[str] = []
        queue = SingleWorkerQueue(
            runtime_factory=lambda: CountingRuntime(counts),
            handler=lambda path, _runtime: order.append(path.name) or path.stem,
        )
        tasks = [queue.submit(Path(name)) for name in ("one.txt", "two.txt", "three.txt")]

        handled = queue.run_until_idle()

        self.assertEqual(counts, {"entered": 1, "exited": 1})
        self.assertEqual(order, ["one.txt", "two.txt", "three.txt"])
        self.assertEqual(handled, tasks)
        self.assertTrue(all(task.state is TaskState.SUCCEEDED for task in tasks))
        self.assertEqual([task.result for task in tasks], ["one", "two", "three"])

    def test_handler_failure_does_not_drop_later_tasks(self) -> None:
        counts = {"entered": 0, "exited": 0}

        def handler(path: Path, _runtime: object) -> str:
            if path.name == "bad":
                raise ValueError("analysis failed")
            return path.name

        queue = SingleWorkerQueue(
            runtime_factory=lambda: CountingRuntime(counts),
            handler=handler,
        )
        first = queue.submit("bad")
        second = queue.submit("good")
        queue.run_until_idle()

        self.assertEqual(first.state, TaskState.FAILED)
        self.assertIn("ValueError", first.error or "")
        self.assertEqual(second.state, TaskState.SUCCEEDED)
        self.assertEqual(counts["entered"], 1)

    def test_task_arriving_while_running_joins_runtime_batch(self) -> None:
        counts = {"entered": 0, "exited": 0}
        first_started = threading.Event()
        release_first = threading.Event()

        def handler(path: Path, _runtime: object) -> str:
            if path.name == "first":
                first_started.set()
                self.assertTrue(release_first.wait(1))
            return path.name

        queue = SingleWorkerQueue(
            runtime_factory=lambda: CountingRuntime(counts),
            handler=handler,
        )
        queue.start()
        first = queue.submit("first")
        self.assertTrue(first_started.wait(1))
        second = queue.submit("second")
        release_first.set()
        self.assertTrue(queue.join(2))
        queue.stop()

        self.assertEqual(first.state, TaskState.SUCCEEDED)
        self.assertEqual(second.state, TaskState.SUCCEEDED)
        self.assertEqual(counts, {"entered": 1, "exited": 1})

    def test_cancel_pending(self) -> None:
        queue = SingleWorkerQueue(
            runtime_factory=lambda: CountingRuntime({"entered": 0, "exited": 0}),
            handler=lambda path, runtime: path.name,
        )
        task = queue.submit("later")
        self.assertTrue(queue.cancel(task.id))
        self.assertEqual(task.state, TaskState.CANCELLED)
        self.assertEqual(queue.pending_count, 0)

    def test_runtime_start_failure_marks_whole_batch_failed(self) -> None:
        queue = SingleWorkerQueue(
            runtime_factory=FailingRuntime,
            handler=lambda path, runtime: path.name,
        )
        tasks = [queue.submit("one"), queue.submit("two")]
        queue.run_until_idle()

        self.assertTrue(all(task.state is TaskState.FAILED for task in tasks))
        self.assertTrue(all("runtime unavailable" in (task.error or "") for task in tasks))

    def test_terminal_retention_is_bounded_under_long_running_load(self) -> None:
        counts = {"entered": 0, "exited": 0}
        observed_terminal_ids: list[str] = []

        def observe(task) -> None:
            if task.state in {
                TaskState.SUCCEEDED,
                TaskState.FAILED,
                TaskState.CANCELLED,
            }:
                observed_terminal_ids.append(task.id)

        queue = SingleWorkerQueue(
            runtime_factory=lambda: CountingRuntime(counts),
            handler=lambda path, _runtime: path.name,
            on_change=observe,
            terminal_task_retention=32,
            max_pending_tasks=5000,
        )
        tasks = [queue.submit(f"task-{index}") for index in range(5000)]

        handled = queue.run_until_idle()

        self.assertEqual(len(handled), 5000)
        self.assertEqual(len(observed_terminal_ids), 5000)
        self.assertEqual(queue.pending_count, 0)
        self.assertEqual(queue.tracked_task_count, 32)
        self.assertTrue(queue.join(0))
        self.assertTrue(all(queue.get(task.id) is None for task in tasks[:-32]))
        self.assertTrue(all(queue.get(task.id) is task for task in tasks[-32:]))
        self.assertEqual(counts, {"entered": 1, "exited": 1})

    def test_retention_zero_keeps_pending_but_discards_terminal_lookup(self) -> None:
        queue = SingleWorkerQueue(
            runtime_factory=lambda: CountingRuntime({"entered": 0, "exited": 0}),
            handler=lambda path, _runtime: path.name,
            terminal_task_retention=0,
        )
        task = queue.submit("one")
        self.assertIs(queue.get(task.id), task)

        queue.run_until_idle()

        self.assertEqual(task.state, TaskState.SUCCEEDED)
        self.assertIsNone(queue.get(task.id))
        self.assertEqual(queue.tracked_task_count, 0)

    def test_cancelled_and_batch_failed_tasks_obey_same_retention(self) -> None:
        cancelled_queue = SingleWorkerQueue(
            runtime_factory=lambda: CountingRuntime({"entered": 0, "exited": 0}),
            handler=lambda path, _runtime: path.name,
            terminal_task_retention=1,
        )
        cancelled = [cancelled_queue.submit(str(index)) for index in range(2)]
        self.assertTrue(cancelled_queue.cancel(cancelled[0].id))
        self.assertTrue(cancelled_queue.cancel(cancelled[1].id))
        self.assertIsNone(cancelled_queue.get(cancelled[0].id))
        self.assertIs(cancelled_queue.get(cancelled[1].id), cancelled[1])

        failed_queue = SingleWorkerQueue(
            runtime_factory=FailingRuntime,
            handler=lambda path, runtime: path.name,
            terminal_task_retention=1,
        )
        failed = [failed_queue.submit(str(index)) for index in range(2)]
        failed_queue.run_until_idle()
        self.assertIsNone(failed_queue.get(failed[0].id))
        self.assertIs(failed_queue.get(failed[1].id), failed[1])

    def test_terminal_retention_rejects_invalid_limits(self) -> None:
        arguments = {
            "runtime_factory": lambda: CountingRuntime({"entered": 0, "exited": 0}),
            "handler": lambda path, runtime: path.name,
        }
        with self.assertRaises(TypeError):
            SingleWorkerQueue(**arguments, terminal_task_retention=True)
        with self.assertRaises(TypeError):
            SingleWorkerQueue(**arguments, terminal_task_retention=1.5)
        with self.assertRaises(ValueError):
            SingleWorkerQueue(**arguments, terminal_task_retention=-1)

    def test_pending_admission_is_bounded_and_rejection_is_terminal(self) -> None:
        observed = []
        queue = SingleWorkerQueue(
            runtime_factory=lambda: CountingRuntime({"entered": 0, "exited": 0}),
            handler=lambda path, runtime: path.name,
            on_change=observed.append,
            max_pending_tasks=2,
            terminal_task_retention=2,
        )

        accepted = [queue.submit("one"), queue.submit("two")]
        rejected = queue.submit("three")

        self.assertEqual(queue.pending_count, 2)
        self.assertTrue(all(task.state is TaskState.PENDING for task in accepted))
        self.assertIs(rejected.state, TaskState.FAILED)
        self.assertIsNone(rejected.result)
        self.assertIsNotNone(rejected.finished_at)
        self.assertIn(QueueCapacityError.__name__, rejected.error or "")
        self.assertIs(observed[-1], rejected)

    def test_default_pending_capacity_is_finite(self) -> None:
        queue = SingleWorkerQueue(
            runtime_factory=lambda: CountingRuntime({"entered": 0, "exited": 0}),
            handler=lambda path, runtime: path.name,
        )

        tasks = [queue.submit(str(index)) for index in range(DEFAULT_MAX_PENDING_TASKS + 1)]

        self.assertEqual(queue.max_pending_tasks, DEFAULT_MAX_PENDING_TASKS)
        self.assertEqual(queue.pending_count, DEFAULT_MAX_PENDING_TASKS)
        self.assertIs(tasks[-1].state, TaskState.FAILED)

    def test_capacity_zero_rejects_without_starting_runtime(self) -> None:
        counts = {"entered": 0, "exited": 0}
        queue = SingleWorkerQueue(
            runtime_factory=lambda: CountingRuntime(counts),
            handler=lambda path, runtime: path.name,
            max_pending_tasks=0,
        )

        task = queue.submit("never-admitted")

        self.assertIs(task.state, TaskState.FAILED)
        self.assertEqual(queue.run_until_idle(), [])
        self.assertEqual(counts, {"entered": 0, "exited": 0})
        self.assertTrue(queue.join(0))

    def test_pending_and_tracking_stay_bounded_during_submission_storm(self) -> None:
        queue = SingleWorkerQueue(
            runtime_factory=lambda: CountingRuntime({"entered": 0, "exited": 0}),
            handler=lambda path, runtime: path.name,
            max_pending_tasks=32,
            terminal_task_retention=16,
        )

        tasks = [queue.submit(f"storm-{index}") for index in range(10_000)]

        self.assertEqual(queue.pending_count, 32)
        self.assertLessEqual(queue.tracked_task_count, 48)
        self.assertEqual(
            sum(task.state is TaskState.FAILED for task in tasks),
            10_000 - 32,
        )

    def test_pending_capacity_rejects_invalid_limits(self) -> None:
        arguments = {
            "runtime_factory": lambda: CountingRuntime({"entered": 0, "exited": 0}),
            "handler": lambda path, runtime: path.name,
        }
        with self.assertRaises(TypeError):
            SingleWorkerQueue(**arguments, max_pending_tasks=True)
        with self.assertRaises(TypeError):
            SingleWorkerQueue(**arguments, max_pending_tasks=1.5)
        with self.assertRaises(ValueError):
            SingleWorkerQueue(**arguments, max_pending_tasks=-1)


if __name__ == "__main__":
    unittest.main()
