"""Read-mostly host diagnostics for the local KDE/llama.cpp stack."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
import errno
import os
from pathlib import Path
import shutil
import socket
import subprocess
import tempfile
from typing import Any

from mingtu_ai.notifications import has_desktop_session


class CheckStatus(StrEnum):
    PASS = "pass"
    WARN = "warn"
    FAIL = "fail"


@dataclass(frozen=True, slots=True)
class CheckResult:
    name: str
    status: CheckStatus
    summary: str
    details: Mapping[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "name": self.name,
            "status": self.status.value,
            "summary": self.summary,
        }
        if self.details:
            result["details"] = dict(self.details)
        return result


@dataclass(frozen=True, slots=True)
class DoctorReport:
    checks: tuple[CheckResult, ...]

    @property
    def healthy(self) -> bool:
        return not any(check.status is CheckStatus.FAIL for check in self.checks)

    @property
    def exit_code(self) -> int:
        return 0 if self.healthy else 1

    def to_dict(self) -> dict[str, Any]:
        return {
            "healthy": self.healthy,
            "checks": [check.to_dict() for check in self.checks],
        }

    def render_text(self) -> str:
        symbols = {
            CheckStatus.PASS: "OK",
            CheckStatus.WARN: "WARN",
            CheckStatus.FAIL: "FAIL",
        }
        lines = [
            f"[{symbols[check.status]}] {check.name}: {check.summary}"
            for check in self.checks
        ]
        lines.append("結果：" + ("可啟動" if self.healthy else "尚有必要條件未通過"))
        return "\n".join(lines)


CommandRunner = Callable[[Sequence[str], float], subprocess.CompletedProcess[str]]
Which = Callable[[str], str | None]
PortProbe = Callable[[str, int], int]


def _run_command(command: Sequence[str], timeout: float) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        list(command),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=timeout,
        check=False,
    )


def _probe_port(host: str, port: int) -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        try:
            probe.bind((host, port))
        except OSError as exc:
            return exc.errno or errno.EIO
    # Reuse ECONNREFUSED as the established "available" result understood by
    # check_port.  A successful bind is stronger evidence than a refused connect
    # because a bound-but-not-listening socket also blocks llama-server startup.
    return errno.ECONNREFUSED


def _permission_limited(message: str) -> bool:
    lowered = message.casefold()
    markers = (
        "access denied",
        "operation not permitted",
        "permission denied",
        "insufficient permissions",
        "not authorized",
    )
    return any(marker in lowered for marker in markers)


def check_gpu(*, runner: CommandRunner, which: Which) -> CheckResult:
    executable = which("nvidia-smi")
    if not executable:
        return CheckResult("GPU", CheckStatus.FAIL, "找不到 nvidia-smi")
    try:
        result = runner(
            [
                executable,
                "--query-gpu=name,driver_version,memory.total",
                "--format=csv,noheader,nounits",
            ],
            5.0,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return CheckResult("GPU", CheckStatus.WARN, f"無法完成 GPU 查詢：{exc}")
    output = (result.stdout or "").strip()
    error = (result.stderr or "").strip()
    if result.returncode == 0 and output:
        return CheckResult("GPU", CheckStatus.PASS, output, {"tool": executable})
    combined = error or output or f"nvidia-smi exited {result.returncode}"
    if _permission_limited(combined):
        return CheckResult(
            "GPU",
            CheckStatus.WARN,
            f"目前執行環境無權查詢 GPU，不判定為未安裝：{combined}",
        )
    if "couldn't communicate with the nvidia driver" in combined.casefold():
        return CheckResult(
            "GPU",
            CheckStatus.WARN,
            "nvidia-smi 無法與驅動通訊；可能是驅動狀態或目前隔離環境限制，"
            f"不誤判為無 GPU：{combined}",
        )
    return CheckResult("GPU", CheckStatus.FAIL, f"nvidia-smi 查詢失敗：{combined}")


def check_llama_server(
    *,
    runner: CommandRunner,
    which: Which,
    configured_path: Path | str | None = None,
) -> CheckResult:
    executable = (
        os.fspath(Path(configured_path).expanduser())
        if configured_path is not None
        else which("llama-server")
    )
    if not executable:
        return CheckResult("llama-server", CheckStatus.FAIL, "PATH 中找不到 llama-server")
    executable_path = Path(executable)
    if not executable_path.is_file() or not os.access(executable_path, os.X_OK):
        return CheckResult(
            "llama-server",
            CheckStatus.FAIL,
            f"可執行檔不存在或無執行權限：{executable_path}",
        )
    try:
        result = runner([executable, "--version"], 5.0)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return CheckResult(
            "llama-server",
            CheckStatus.WARN,
            f"已找到 {executable}，但版本查詢失敗：{exc}",
        )
    output = ((result.stdout or "") + "\n" + (result.stderr or "")).strip().splitlines()
    version = next(
        (line for line in output if line.casefold().startswith("version")),
        output[0] if output else f"exit {result.returncode}",
    )
    if result.returncode == 0:
        return CheckResult("llama-server", CheckStatus.PASS, version, {"path": executable})
    return CheckResult(
        "llama-server",
        CheckStatus.WARN,
        f"可執行檔存在，但 --version 回傳 {result.returncode}：{version}",
        {"path": executable},
    )


def _check_gguf_file(
    path: Path,
    *,
    check_name: str,
    subject: str,
    require_gguf: bool,
) -> CheckResult:
    """Check one local model component without changing it.

    Main models retain the existing warning for a non-``.gguf`` suffix, while
    an explicitly configured multimodal projector must have a GGUF header
    regardless of its filename.
    """

    try:
        exists = path.exists()
    except OSError as exc:
        return _local_file_error(check_name, path, f"檢查{subject}路徑", exc)
    if not exists:
        return CheckResult(check_name, CheckStatus.FAIL, f"{subject}檔不存在：{path}")
    try:
        is_file = path.is_file()
    except OSError as exc:
        return _local_file_error(check_name, path, f"判定{subject}路徑類型", exc)
    if not is_file:
        return CheckResult(
            check_name,
            CheckStatus.FAIL,
            f"{subject}路徑不是一般檔案：{path}",
        )
    try:
        readable = os.access(path, os.R_OK)
    except OSError as exc:
        return _local_file_error(check_name, path, f"檢查{subject}檔讀取權限", exc)
    if not readable:
        return CheckResult(check_name, CheckStatus.FAIL, f"{subject}檔無法讀取：{path}")
    try:
        size = path.stat().st_size
    except OSError as exc:
        return _local_file_error(check_name, path, f"讀取{subject}檔資訊", exc)

    has_gguf_suffix = path.suffix.casefold() == ".gguf"
    if require_gguf or has_gguf_suffix:
        try:
            with path.open("rb") as stream:
                header = stream.read(4)
        except OSError as exc:
            return _local_file_error(check_name, path, f"讀取{subject}檔頭", exc)
        if header != b"GGUF":
            return CheckResult(check_name, CheckStatus.FAIL, f"GGUF 檔頭無效：{path}")

    status = CheckStatus.PASS if require_gguf or has_gguf_suffix else CheckStatus.WARN
    suffix_note = "" if status is CheckStatus.PASS else "（副檔名非 .gguf，請再確認格式）"
    return CheckResult(
        check_name,
        status,
        f"{path} ({size / (1024 ** 3):.2f} GiB){suffix_note}",
        {"path": str(path), "bytes": size},
    )


def check_model(model_path: Path | str | None) -> CheckResult:
    if model_path is None:
        return CheckResult(
            "模型",
            CheckStatus.FAIL,
            "未指定模型路徑（使用 --model 或 MINGTU_AI_MODEL）",
        )
    return _check_gguf_file(
        Path(model_path).expanduser(),
        check_name="模型",
        subject="模型",
        require_gguf=False,
    )


def check_mmproj(mmproj_path: Path | str) -> CheckResult:
    """Validate an explicitly configured llama.cpp multimodal projector."""

    return _check_gguf_file(
        Path(mmproj_path).expanduser(),
        check_name="多模態 projector",
        subject="多模態投影模型",
        require_gguf=True,
    )


def _local_file_error(
    check_name: str,
    path: Path,
    action: str,
    exc: OSError,
) -> CheckResult:
    """Convert local model-component I/O failures to doctor failures.

    Permission failures must remain distinguishable from a missing path.  That
    matters for model caches owned by a service account, and also keeps the CLI
    from leaking an uncaught traceback when a parent directory is inaccessible.
    """

    denied = isinstance(exc, PermissionError) or exc.errno in {errno.EACCES, errno.EPERM}
    prefix = "無權" if denied else "無法"
    error_name = type(exc).__name__
    return CheckResult(
        check_name,
        CheckStatus.FAIL,
        f"{prefix}{action}：{path}（{error_name}: {exc}）",
        {"path": str(path), "error": error_name, "errno": exc.errno},
    )


def check_notifications(
    *,
    runner: CommandRunner,
    which: Which,
    environ: Mapping[str, str],
) -> CheckResult:
    notify_send = which("notify-send")
    if not notify_send:
        return CheckResult("KDE 通知", CheckStatus.FAIL, "找不到 notify-send")
    if not has_desktop_session(environ):
        return CheckResult(
            "KDE 通知",
            CheckStatus.WARN,
            "目前行程沒有可用的桌面 session bus，執行時會降級為文字提示",
        )

    qdbus = which("qdbus6") or which("qdbus")
    gdbus = which("gdbus")
    if qdbus:
        command = [
            qdbus,
            "org.freedesktop.Notifications",
            "/org/freedesktop/Notifications",
            "org.freedesktop.Notifications.GetCapabilities",
        ]
    elif gdbus:
        command = [
            gdbus,
            "call",
            "--session",
            "--dest",
            "org.freedesktop.Notifications",
            "--object-path",
            "/org/freedesktop/Notifications",
            "--method",
            "org.freedesktop.Notifications.GetCapabilities",
        ]
    else:
        return CheckResult(
            "KDE 通知",
            CheckStatus.WARN,
            "notify-send 存在，但無 qdbus6/gdbus 可驗證通知服務",
        )

    try:
        result = runner(command, 5.0)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return CheckResult("KDE 通知", CheckStatus.WARN, f"無法完成 D-Bus 查詢：{exc}")
    combined = ((result.stdout or "") + "\n" + (result.stderr or "")).strip()
    if result.returncode == 0:
        if "actions" not in combined.casefold():
            return CheckResult(
                "KDE 通知",
                CheckStatus.WARN,
                "通知服務可用，但未宣告 actions 能力；操作按鈕可能被忽略",
            )
        return CheckResult("KDE 通知", CheckStatus.PASS, "KDE/Freedesktop 通知與 actions 可用")
    if _permission_limited(combined):
        return CheckResult(
            "KDE 通知",
            CheckStatus.WARN,
            f"目前執行環境無權連線 session bus，不判定為 KDE 服務缺少：{combined}",
        )
    return CheckResult("KDE 通知", CheckStatus.FAIL, f"通知 D-Bus 服務不可用：{combined}")


def check_port(*, host: str, port: int, probe: PortProbe) -> CheckResult:
    if host != "127.0.0.1" or port != 3001:
        return CheckResult(
            "推論連接埠",
            CheckStatus.FAIL,
            f"安全設定錯誤：只允許 127.0.0.1:3001，收到 {host}:{port}",
        )
    try:
        result = probe(host, port)
    except OSError as exc:
        if exc.errno in {errno.EACCES, errno.EPERM}:
            return CheckResult(
                "推論連接埠",
                CheckStatus.WARN,
                f"目前執行環境不允許探測 {host}:{port}：{exc}",
            )
        return CheckResult("推論連接埠", CheckStatus.WARN, f"無法判定 {host}:{port}：{exc}")
    if result in {0, errno.EADDRINUSE}:
        return CheckResult(
            "推論連接埠",
            CheckStatus.FAIL,
            f"{host}:{port} 已有連線監聽者；命圖 AI 不會終止或釋放它",
        )
    if result in {errno.ECONNREFUSED, errno.ETIMEDOUT, errno.EHOSTUNREACH}:
        return CheckResult("推論連接埠", CheckStatus.PASS, f"{host}:{port} 目前無 TCP 監聽者")
    if result in {errno.EACCES, errno.EPERM}:
        return CheckResult(
            "推論連接埠",
            CheckStatus.WARN,
            f"連接埠探測被環境拒絕（errno {result}），不誤判為可用",
        )
    return CheckResult(
        "推論連接埠",
        CheckStatus.WARN,
        f"無法確定 {host}:{port} 是否可用（errno {result}）",
    )


def check_watch_directory(path: Path | str) -> CheckResult:
    directory = Path(path).expanduser()
    name = f"監控目錄 {directory}"
    if not directory.exists():
        return CheckResult(name, CheckStatus.FAIL, "目錄不存在")
    if not directory.is_dir():
        return CheckResult(name, CheckStatus.FAIL, "路徑不是目錄")
    if not os.access(directory, os.R_OK | os.X_OK):
        return CheckResult(name, CheckStatus.FAIL, "目錄無法讀取或進入")
    try:
        # An actual create/delete is more reliable than permission bits alone,
        # especially with ACLs.  The temporary file is empty and auto-removed.
        with tempfile.NamedTemporaryFile(prefix=".mingtu-ai-doctor-", dir=directory):
            pass
    except OSError as exc:
        return CheckResult(name, CheckStatus.FAIL, f"無法建立檔案：{exc}")
    return CheckResult(name, CheckStatus.PASS, "可讀、可進入且實際寫入測試成功")


def check_ffmpeg(*, runner: CommandRunner, which: Which) -> CheckResult:
    executable = which("ffmpeg")
    if not executable:
        return CheckResult("ffmpeg", CheckStatus.FAIL, "PATH 中找不到 ffmpeg")
    try:
        result = runner([executable, "-version"], 5.0)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return CheckResult("ffmpeg", CheckStatus.FAIL, f"無法執行 ffmpeg：{exc}")
    first_line = ((result.stdout or "") + "\n" + (result.stderr or "")).strip().splitlines()
    summary = first_line[0] if first_line else f"exit {result.returncode}"
    status = CheckStatus.PASS if result.returncode == 0 else CheckStatus.FAIL
    return CheckResult("ffmpeg", status, summary, {"path": executable})


def run_doctor(
    *,
    model_path: Path | str | None = None,
    mmproj_path: Path | str | None = None,
    llama_server_path: Path | str | None = None,
    watch_directories: Sequence[Path | str] = (),
    host: str = "127.0.0.1",
    port: int = 3001,
    runner: CommandRunner = _run_command,
    which: Which = shutil.which,
    environ: Mapping[str, str] | None = None,
    port_probe: PortProbe = _probe_port,
) -> DoctorReport:
    env = os.environ if environ is None else environ
    checks = [
        check_gpu(runner=runner, which=which),
        check_llama_server(runner=runner, which=which, configured_path=llama_server_path),
        check_model(model_path),
        check_notifications(runner=runner, which=which, environ=env),
        check_port(host=host, port=port, probe=port_probe),
    ]
    if mmproj_path is not None:
        checks.insert(3, check_mmproj(mmproj_path))
    checks.extend(check_watch_directory(path) for path in watch_directories)
    checks.append(check_ffmpeg(runner=runner, which=which))
    return DoctorReport(tuple(checks))
