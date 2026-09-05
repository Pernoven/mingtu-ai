"""Host health checks exposed by ``mingtu-ai --doctor``."""

from .doctor import (
    CheckResult,
    CheckStatus,
    DoctorReport,
    check_ffmpeg,
    check_gpu,
    check_llama_server,
    check_mmproj,
    check_model,
    check_notifications,
    check_port,
    check_watch_directory,
    run_doctor,
)

__all__ = [
    "CheckResult",
    "CheckStatus",
    "DoctorReport",
    "check_ffmpeg",
    "check_gpu",
    "check_llama_server",
    "check_mmproj",
    "check_model",
    "check_notifications",
    "check_port",
    "check_watch_directory",
    "run_doctor",
]
