from __future__ import annotations

import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch

import httpx
from pydantic import ValidationError

from mingtu_ai.config import LlamaServerConfig
from mingtu_ai.runtime import (
    ListenerOwnershipError,
    LlamaServerRuntime,
    PortInUseError,
    RuntimeStartError,
)
from mingtu_ai.runtime.llama_server import _listener_is_owned


class FakeProcess:
    def __init__(self, *, running: bool = True, pid: int = 4321) -> None:
        self.return_code: int | None = None if running else 0
        self.pid = pid
        self.terminated = False
        self.killed = False

    def poll(self) -> int | None:
        return self.return_code

    def terminate(self) -> None:
        self.terminated = True

    def kill(self) -> None:
        self.killed = True
        self.return_code = -9

    def wait(self, timeout: float | None = None) -> int:
        if self.terminated:
            self.return_code = 0
        return self.return_code or 0


class RuntimeTests(unittest.TestCase):
    def _config(self, directory: Path, **overrides: object) -> LlamaServerConfig:
        binary = directory / "llama-server"
        model = directory / "model.gguf"
        binary.write_bytes(b"binary")
        model.write_bytes(b"model")
        return LlamaServerConfig(binary=binary, model_path=model, **overrides)

    def test_command_fixes_loopback_port_and_disables_reasoning(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            runtime = LlamaServerRuntime(self._config(Path(directory)))
            command = runtime.command
        self.assertIn("127.0.0.1", command)
        self.assertIn("3001", command)
        self.assertEqual(command[command.index("--reasoning-budget") + 1], "0")
        self.assertEqual(command[command.index("--reasoning") + 1], "off")
        self.assertIn("--jinja", command)
        self.assertIn("--offline", command)
        self.assertIn("--no-webui", command)

    def test_config_rejects_network_or_model_overrides(self) -> None:
        with self.assertRaises(ValidationError):
            LlamaServerConfig(host="0.0.0.0")
        with self.assertRaises(ValidationError):
            LlamaServerConfig(port=3000)
        with self.assertRaises(ValidationError):
            LlamaServerConfig(extra_args=("--port=9999",))
        with self.assertRaises(ValidationError):
            LlamaServerConfig(extra_args=("--hf-repo", "remote/model"))
        with self.assertRaises(ValidationError):
            LlamaServerConfig(extra_args=("--tools=all",))
        with self.assertRaises(ValidationError):
            LlamaServerConfig(extra_args=("--reasoning-budget=100",))
        with self.assertRaises(ValidationError):
            LlamaServerConfig(extra_args=("--vision-gemma-4b-default",))

    def test_single_worker_uses_one_slot_and_auto_fit_by_default(self) -> None:
        command = LlamaServerRuntime(LlamaServerConfig(model_path=Path("model.gguf"))).command
        self.assertEqual(command[command.index("--n-gpu-layers") + 1], "auto")
        self.assertEqual(command[command.index("--parallel") + 1], "1")
        self.assertEqual(command[command.index("--fit") + 1], "on")
        with self.assertRaises(ValidationError):
            LlamaServerConfig(n_gpu_layers=-1)

    def test_busy_gpu_keeps_projector_on_cpu_without_forcing_gpu_layers(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            projector = root / "mmproj.gguf"
            projector.write_bytes(b"GGUF")
            config = self._config(root, mmproj_path=projector)
            runtime = LlamaServerRuntime(config)
            with patch("mingtu_ai.runtime.llama_server.shutil.which", return_value="nvidia-smi"), patch("mingtu_ai.runtime.llama_server.subprocess.run", return_value=Mock(stdout="512\n")):
                self.assertIn("--no-mmproj-offload", runtime.command)
            with patch("mingtu_ai.runtime.llama_server.shutil.which", return_value="nvidia-smi"), patch("mingtu_ai.runtime.llama_server.subprocess.run", return_value=Mock(stdout="8192\n")):
                self.assertNotIn("--no-mmproj-offload", runtime.command)
            config.mmproj_offload = False
            with patch("mingtu_ai.runtime.llama_server.subprocess.run") as probe:
                self.assertIn("--no-mmproj-offload", runtime.command)
                probe.assert_not_called()

    def test_occupied_port_only_reports_and_never_spawns(self) -> None:
        runtime = LlamaServerRuntime(LlamaServerConfig(model_path=Path("model.gguf")))
        fake_socket = Mock()
        fake_socket.bind.side_effect = OSError("occupied")
        with patch("mingtu_ai.runtime.llama_server.socket.socket", return_value=fake_socket):
            with patch("mingtu_ai.runtime.llama_server.subprocess.Popen") as popen:
                with self.assertRaises(PortInUseError):
                    runtime.ensure_started()
        popen.assert_not_called()
        fake_socket.close.assert_called_once()

    def test_start_waits_for_health_and_shutdown_signals_owned_handle(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            runtime = LlamaServerRuntime(self._config(Path(directory)))
            process = FakeProcess()
            with patch.object(runtime, "_assert_port_available"):
                with patch.object(runtime, "_wait_until_healthy") as health:
                    with patch("mingtu_ai.runtime.llama_server.subprocess.Popen", return_value=process):
                        runtime.ensure_started()
            health.assert_called_once_with(process)
            with patch.object(runtime, "_wait_until_port_released") as port_released:
                runtime.shutdown()
            port_released.assert_called_once_with()
            self.assertTrue(process.terminated)
            self.assertFalse(process.killed)
            self.assertFalse(runtime.is_running)

    def test_failed_health_check_stops_only_new_handle(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            runtime = LlamaServerRuntime(self._config(Path(directory)))
            process = FakeProcess()
            with patch.object(runtime, "_assert_port_available"):
                with patch.object(
                    runtime,
                    "_wait_until_healthy",
                    side_effect=RuntimeStartError("unhealthy"),
                ):
                    with patch("mingtu_ai.runtime.llama_server.subprocess.Popen", return_value=process):
                        with self.assertRaises(RuntimeStartError):
                            runtime.ensure_started()
            self.assertTrue(process.terminated)
            self.assertIsNone(runtime._process)

    def test_health_wait_accepts_loading_then_explicit_ok(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            runtime = LlamaServerRuntime(
                self._config(Path(directory), health_poll_seconds=0.001)
            )
            process = FakeProcess()
            client = Mock()
            client.__enter__ = Mock(return_value=client)
            client.__exit__ = Mock(return_value=False)
            client.get.side_effect = [
                httpx.Response(503, json={"error": {"message": "Loading model"}}),
                httpx.Response(200, json={"status": "ok", "detail": "ready"}),
            ]
            with (
                patch("mingtu_ai.runtime.llama_server.httpx.Client", return_value=client),
                patch(
                    "mingtu_ai.runtime.llama_server._listener_is_owned",
                    return_value=True,
                ) as ownership,
            ):
                runtime._wait_until_healthy(process)
            self.assertEqual(client.get.call_count, 2)
            ownership.assert_called_once_with(4321, "127.0.0.1", 3001)

    def test_health_rejects_ok_from_listener_not_owned_by_child(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            runtime = LlamaServerRuntime(self._config(Path(directory)))
            process = FakeProcess()
            client = Mock()
            client.__enter__ = Mock(return_value=client)
            client.__exit__ = Mock(return_value=False)
            client.get.return_value = httpx.Response(200, json={"status": "ok"})

            with (
                patch("mingtu_ai.runtime.llama_server.httpx.Client", return_value=client),
                patch(
                    "mingtu_ai.runtime.llama_server._listener_is_owned",
                    return_value=False,
                ),
            ):
                with self.assertRaises(ListenerOwnershipError) as caught:
                    runtime._wait_until_healthy(process)

            self.assertIn("not owned", str(caught.exception))

    def test_listener_verification_failure_is_explicit_and_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            runtime = LlamaServerRuntime(self._config(Path(directory)))
            process = FakeProcess()
            with patch(
                "mingtu_ai.runtime.llama_server._listener_is_owned",
                side_effect=PermissionError("proc access denied"),
            ):
                with self.assertRaises(ListenerOwnershipError) as caught:
                    runtime._assert_owned_listener(process)

            self.assertIn("Linux /proc", str(caught.exception))
            self.assertIn("refusing local file transmission", str(caught.exception))

    def test_reused_runtime_revalidates_listener_before_job_yields(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            runtime = LlamaServerRuntime(self._config(Path(directory)))
            runtime._process = FakeProcess()
            with patch(
                "mingtu_ai.runtime.llama_server._listener_is_owned",
                return_value=False,
            ):
                with self.assertRaises(ListenerOwnershipError):
                    with runtime.job():
                        self.fail("an unowned listener must never reach the provider")

    def test_timeout_escalates_from_terminate_to_same_handle_kill(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            runtime = LlamaServerRuntime(self._config(Path(directory)))
            process = FakeProcess()
            process.wait = Mock(side_effect=[subprocess.TimeoutExpired("llama-server", 1), 0])
            runtime._process = process
            with patch.object(runtime, "_wait_until_port_released"):
                runtime.shutdown()
            self.assertTrue(process.terminated)
            self.assertTrue(process.killed)

    def test_queue_empty_waits_for_active_job_then_schedules_shutdown(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            runtime = LlamaServerRuntime(self._config(Path(directory), idle_shutdown_seconds=0.5))
            runtime._process = FakeProcess()
            timer = Mock()
            with (
                patch(
                    "mingtu_ai.runtime.llama_server.threading.Timer",
                    return_value=timer,
                ) as factory,
                patch.object(runtime, "_assert_owned_listener"),
            ):
                with runtime.job():
                    runtime.mark_queue_empty()
                    factory.assert_not_called()
                factory.assert_called_once_with(0.5, runtime.shutdown)
                timer.start.assert_called_once()
                self.assertTrue(timer.daemon)


class ProcSocketOwnershipTests(unittest.TestCase):
    @staticmethod
    def _proc_tree(
        root: Path,
        *,
        pid: int,
        table: str,
        address: str,
        inode: int,
    ) -> None:
        fd_directory = root / str(pid) / "fd"
        net_directory = root / str(pid) / "net"
        fd_directory.mkdir(parents=True)
        net_directory.mkdir(parents=True)
        os.symlink(f"socket:[{inode}]", fd_directory / "7")
        header = "sl local_address rem_address st tx_queue rx_queue tr tm uid timeout inode\n"
        (net_directory / "tcp").write_text(header, encoding="ascii")
        (net_directory / "tcp6").write_text(header, encoding="ascii")
        row = (
            f"0: {address}:0BB9 00000000:0000 0A "
            f"00000000:00000000 00:00000000 00000000 1000 0 {inode}\n"
        )
        (net_directory / table).write_text(header + row, encoding="ascii")

    def test_matches_owned_ipv4_listener(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._proc_tree(
                root,
                pid=123,
                table="tcp",
                address="0100007F",
                inode=4242,
            )
            self.assertTrue(
                _listener_is_owned(123, "127.0.0.1", 3001, proc_root=root)
            )

    def test_matches_ipv4_mapped_ipv6_listener(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._proc_tree(
                root,
                pid=456,
                table="tcp6",
                address="0000000000000000FFFF00000100007F",
                inode=5252,
            )
            self.assertTrue(
                _listener_is_owned(456, "127.0.0.1", 3001, proc_root=root)
            )

    def test_rejects_listener_inode_not_held_by_child(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._proc_tree(
                root,
                pid=789,
                table="tcp",
                address="0100007F",
                inode=6262,
            )
            descriptor = root / "789/fd/7"
            descriptor.unlink()
            os.symlink("socket:[9999]", descriptor)
            self.assertFalse(
                _listener_is_owned(789, "127.0.0.1", 3001, proc_root=root)
            )


if __name__ == "__main__":
    unittest.main()
