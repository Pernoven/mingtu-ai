"""Command-line entry point for Mingtu AI."""

from __future__ import annotations

import argparse
from contextlib import ExitStack
import json
import os
from pathlib import Path
import sys
from typing import Sequence, TextIO

from mingtu_ai import AppConfig, LlamaServerConfig, __version__
from mingtu_ai.app import ApplicationCoordinator, ProcessingError
from mingtu_ai.desktop_paths import (
    home_directory,
    load_user_watch_directories,
    spectacle_screenshot_directory,
)
from mingtu_ai.diagnostics import run_doctor
from mingtu_ai.history import HistoryStore
from mingtu_ai.notifications import NotificationService
from mingtu_ai.runtime.instance import ResidentAlreadyRunning, resident_lock
from mingtu_ai.ui import NativeUiUnavailable, run_native_ui


def _default_watch_directories(environ: dict[str, str] | os._Environ[str]) -> list[Path]:
    configured = environ.get("MINGTU_AI_WATCH_DIRS", "")
    if configured:
        home = home_directory(environ)
        return [
            _expand_path(item, home)
            for item in configured.split(os.pathsep)
            if item
        ]
    saved = load_user_watch_directories(environ)
    if saved:
        return saved
    return [spectacle_screenshot_directory(environ)]


def _default_history_path(environ: dict[str, str] | os._Environ[str]) -> Path:
    state_home = environ.get("XDG_STATE_HOME")
    home = home_directory(environ)
    root = _expand_path(state_home, home) if state_home else home / ".local" / "state"
    return root / "mingtu-ai" / "history.sqlite3"


def _expand_path(value: str | os.PathLike[str], home: Path) -> Path:
    text = os.fspath(value)
    if text == "~":
        return home
    if text.startswith("~/"):
        return home / text[2:]
    return Path(text)


def build_parser(
    environ: dict[str, str] | os._Environ[str] = os.environ,
) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="mingtu-ai",
        description="KDE 本地檔案命名助手",
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--once", metavar="FILE", type=Path, help="分析一個檔案後結束")
    mode.add_argument("--batch", "--batch-dir", metavar="DIR", type=Path, help="批次重命名指定資料夾內的所有檔案")
    mode.add_argument("--doctor", action="store_true", help="檢查本地執行環境")
    mode.add_argument(
        "--resolve-ollama",
        metavar="MODEL",
        help="唯讀顯示 Ollama 模型對應的本機 GGUF blob",
    )
    parser.add_argument("--recursive", action="store_true", help="批次處理時包含子資料夾")
    parser.add_argument(
        "--preset",
        choices=["default", "photo_date", "category_ai"],
        default="default",
        help="命名策略範本 (default, photo_date, category_ai)",
    )
    parser.add_argument("--hint", type=str, default=None, help="批次命名的 AI 提示語")
    parser.add_argument("--dry-run", action="store_true", help="批次處理時僅顯示建議不實際寫入/改名")
    parser.add_argument("-y", "--yes", action="store_true", help="批次處理時一鍵自動套用所有改名建議")
    parser.add_argument(
        "--model",
        type=Path,
        default=Path(environ["MINGTU_AI_MODEL"]).expanduser()
        if environ.get("MINGTU_AI_MODEL")
        else None,
        help="本地 GGUF 模型路徑（或 MINGTU_AI_MODEL）",
    )
    parser.add_argument(
        "--mmproj",
        type=Path,
        default=Path(environ["MINGTU_AI_MMPROJ"]).expanduser()
        if environ.get("MINGTU_AI_MMPROJ")
        else None,
        help="選用的 llama.cpp multimodal projector",
    )
    parser.add_argument(
        "--llama-server",
        type=Path,
        default=Path(environ.get("MINGTU_AI_LLAMA_SERVER", "/usr/bin/llama-server")),
        help="llama-server 可執行檔",
    )
    parser.add_argument(
        "--watch",
        action="append",
        type=Path,
        default=None,
        metavar="DIR",
        help="監控目錄（可重複）",
    )
    parser.add_argument("--history-db", type=Path, default=None, help="SQLite 歷史路徑")
    parser.add_argument(
        "--json",
        action="store_true",
        help="以 JSON 輸出 --doctor/--once/--resolve-ollama 結果",
    )
    parser.add_argument(
        "--headless",
        action="store_true",
        help="不啟動選用的 Qt6 系統匣與視窗",
    )
    parser.add_argument("--start-minimized", action="store_true", help="啟動至系統匣，不開啟主視窗")
    parser.add_argument("--single-instance", action="store_true", help="常駐模式只允許一個監視程式")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    return parser


def _config_from_args(args: argparse.Namespace) -> AppConfig:
    return AppConfig(
        llama=LlamaServerConfig(
            binary=args.llama_server.expanduser().absolute(),
            model_path=args.model.expanduser().absolute() if args.model else None,
            mmproj_path=args.mmproj.expanduser().absolute() if args.mmproj else None,
        )
    )


def _watch_directories(
    args: argparse.Namespace,
    environ: dict[str, str] | os._Environ[str],
) -> list[Path]:
    selected = args.watch if args.watch is not None else _default_watch_directories(environ)
    return [path.expanduser().absolute() for path in selected]


def _history_path(args: argparse.Namespace, environ: dict[str, str] | os._Environ[str]) -> Path:
    selected = args.history_db or _default_history_path(environ)
    return selected.expanduser().absolute()


def _validate_history_location(
    history_path: Path,
    watch_directories: Sequence[Path],
) -> None:
    """Reject a database whose DB/WAL/SHM churn would re-enter the watcher."""

    canonical_history = history_path.resolve(strict=False)
    for watch_directory in watch_directories:
        canonical_watch = watch_directory.resolve(strict=False)
        if canonical_history.is_relative_to(canonical_watch):
            raise ValueError(
                "history database must be outside every watch directory; "
                "otherwise SQLite DB/WAL/SHM updates can recursively enqueue work "
                f"({history_path} is inside {watch_directory})"
            )


def create_coordinator(
    args: argparse.Namespace,
    *,
    environ: dict[str, str] | os._Environ[str] = os.environ,
) -> ApplicationCoordinator:
    watch_directories = _watch_directories(args, environ)
    history_path = _history_path(args, environ)
    _validate_history_location(history_path, watch_directories)
    return ApplicationCoordinator(
        config=_config_from_args(args),
        watch_directories=watch_directories,
        history=HistoryStore(history_path),
        notifications=NotificationService(),
    )


def main(
    argv: Sequence[str] | None = None,
    *,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
    environ: dict[str, str] | os._Environ[str] = os.environ,
) -> int:
    out = stdout or sys.stdout
    err = stderr or sys.stderr
    parser = build_parser(environ)
    args = parser.parse_args(argv)

    if args.resolve_ollama is not None:
        # Keep Ollama optional for normal startup and share one implementation
        # with the standalone ``python -m ...model_sources.ollama`` entry point.
        from mingtu_ai.model_sources.ollama import main as ollama_source_main

        resolver_arguments = [args.resolve_ollama]
        if args.json:
            resolver_arguments.append("--json")
        return ollama_source_main(
            resolver_arguments,
            stdout=out,
            stderr=err,
            environ=environ,
        )

    watches = _watch_directories(args, environ)

    if args.doctor:
        report = run_doctor(
            model_path=args.model,
            mmproj_path=args.mmproj,
            llama_server_path=args.llama_server,
            watch_directories=watches,
            host="127.0.0.1",
            port=3001,
            environ=environ,
        )
        if args.json:
            print(json.dumps(report.to_dict(), ensure_ascii=False, indent=2), file=out)
        else:
            print(report.render_text(), file=out)
        return report.exit_code

    try:
        with ExitStack() as stack:
            if args.single_instance and not (args.once or args.batch):
                stack.enter_context(resident_lock(environ))
            coordinator = stack.enter_context(create_coordinator(args, environ=environ))
            if args.once is not None:
                proposal = coordinator.analyze_once(args.once)
                payload = {
                    "source_path": str(proposal.source_path),
                    "suggested_filename": proposal.suggested_filename,
                    "suggestion": proposal.suggestion.model_dump(mode="json"),
                    "history_event_id": proposal.history_event_id,
                }
                if args.json:
                    print(json.dumps(payload, ensure_ascii=False, indent=2), file=out)
                else:
                    print(f"建議檔名：{proposal.suggested_filename}", file=out)
                return 0

            if args.batch is not None:
                batch_res = coordinator.analyze_batch(
                    args.batch,
                    recursive=args.recursive,
                    preset=args.preset,
                    hint=args.hint,
                )
                if args.json:
                    output_data = batch_res.model_dump(mode="json")
                    if args.yes and not args.dry_run:
                        renamed = coordinator.accept_batch(batch_res.batch_id)
                        output_data["renamed_count"] = len(renamed)
                    print(json.dumps(output_data, ensure_ascii=False, indent=2), file=out)
                else:
                    print(f"資料夾批次分析完成 (Batch ID: {batch_res.batch_id})", file=out)
                    print(f"目錄：{batch_res.directory}", file=out)
                    print(f"總檔案數：{batch_res.total_files} | 成功：{batch_res.processed_files} | 失敗：{batch_res.failed_files}", file=out)
                    for item in batch_res.items:
                        print(f"  [原檔] {item.source_path.name}  ==>  [建議] {item.suggested_filename}", file=out)

                    if args.dry_run:
                        print("\n[Dry Run] 試執行模式：未實施實際檔案改名。", file=out)
                    elif args.yes:
                        renamed_events = coordinator.accept_batch(batch_res.batch_id)
                        print(f"\n[一鍵改名] 已成功套用 {len(renamed_events)} 個檔案的批次改名！", file=out)
                    else:
                        print("\n提示：加上 `-y` / `--yes` 可一鍵自動套用上述改名建議，或加上 `--dry-run` 進行試執行。", file=out)
                return 0

            if not args.headless:
                try:
                    if args.start_minimized:
                        return run_native_ui(coordinator, show_windows=False)
                    return run_native_ui(coordinator)
                except NativeUiUnavailable as exc:
                    print(f"命圖 AI 原生 UI 不可用：{exc}；改以 headless 模式執行。", file=err)

            print("命圖 AI 開始監控（headless）：" + ", ".join(map(str, watches)), file=out)
            try:
                coordinator.run_forever()
            except KeyboardInterrupt:
                print("\n命圖 AI 已停止。", file=out)
            return 0
    except KeyboardInterrupt:
        print("\n命圖 AI 已停止。", file=out)
        return 0
    except ResidentAlreadyRunning as exc:
        print(str(exc), file=out)
        return 0
    except (ProcessingError, OSError, ValueError, RuntimeError) as exc:
        print(f"命圖 AI 錯誤：{exc}", file=err)
        return 1
