#!/usr/bin/env python3
"""Install/remove a KDE login entry without installing any dependencies.

Reference: https://specifications.freedesktop.org/autostart/latest/
Exec quoting: https://specifications.freedesktop.org/desktop-entry/latest/exec-variables.html
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile


ENTRY_NAME = "org.permind.MingtuAI.desktop"
MARKER = "X-Mingtu-Managed=true"
ROOT = Path(__file__).resolve().parents[1]
RESTART_POLICY = """# X-Mingtu-Managed=true
[Unit]
StartLimitIntervalSec=0

[Service]
Restart=on-failure
RestartSec=5s
TimeoutStopSec=135s
Environment=PYTHONUNBUFFERED=1
"""


def configure_restart_policy(config: Path, *, remove: bool, dry_run: bool) -> None:
    # systemd-xdg-autostart-generator creates this unit from ENTRY_NAME.
    # https://www.freedesktop.org/software/systemd/man/latest/systemd.service.html
    destination = config / "systemd/user/app-org.permind.MingtuAI@autostart.service.d/50-mingtu-resident.conf"
    if destination.is_symlink():
        raise ValueError(f"保留符號連結：{destination}")
    if destination.exists() and MARKER not in destination.read_text(encoding="utf-8"):
        raise ValueError(f"保留其他來源的服務設定：{destination}")
    if dry_run:
        print(f"{'將移除' if remove else '將設定'}異常重啟保護：{destination}")
        return
    if remove:
        destination.unlink(missing_ok=True)
        return
    destination.parent.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(prefix=".mingtu-", dir=destination.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(RESTART_POLICY)
        os.replace(name, destination)
    finally:
        Path(name).unlink(missing_ok=True)
    print(f"已設定異常重啟保護：{destination}")
    print("服務設定會在下次登入生效；立即套用可執行 systemctl --user daemon-reload")


def desktop_string(value: str) -> str:
    return value.replace("\\", "\\\\").replace("\n", "\\n").replace("\r", "\\r").replace("\t", "\\t")


def exec_argument(value: str) -> str:
    # Desktop entries have two escaping layers; these are not shell commands.
    quoted = value.replace("\\", "\\\\").replace('"', '\\"').replace("$", "\\$").replace("`", "\\`")
    return desktop_string('"' + quoted.replace("%", "%%") + '"')


def make_entry(python: Path, launcher: Path, model: Path, mmproj: Path | None) -> str:
    arguments = [str(python), str(launcher), "--model", str(model)]
    if mmproj is not None:
        arguments.extend(["--mmproj", str(mmproj)])
    return "\n".join([
        "[Desktop Entry]", "Type=Application", "Version=1.0",
        "Name=Mingtu AI", "Name[zh_TW]=命圖 AI 自動命名監視器",
        "Comment=Start the local file naming assistant in the system tray",
        "Exec=" + " ".join(exec_argument(argument) for argument in arguments),
        "TryExec=" + desktop_string(str(python)),
        "Icon=folder-documents-symbolic", "Terminal=false", "OnlyShowIn=KDE;",
        "Hidden=false", MARKER, "",
    ])


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="設定命圖 AI 在 KDE 登入後自動常駐；不下載套件")
    parser.add_argument("--model", type=Path, help="本地 GGUF 模型")
    parser.add_argument("--mmproj", type=Path, help="圖片模型 projector")
    parser.add_argument("--dry-run", action="store_true", help="檢查並顯示設定，不寫入")
    parser.add_argument("--remove", action="store_true", help="移除本工具建立的登入啟動設定")
    args = parser.parse_args(argv)
    config = Path(os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config")))
    destination = config / "autostart" / ENTRY_NAME
    try:
        if args.remove:
            if not destination.exists():
                configure_restart_policy(config, remove=True, dry_run=args.dry_run)
                print("自動啟動設定不存在。")
                return 0
            if destination.is_symlink() or MARKER not in destination.read_text(encoding="utf-8"):
                raise ValueError("這個啟動設定不是由本工具建立，保留原檔。")
            if not args.dry_run:
                destination.unlink()
            configure_restart_policy(config, remove=True, dry_run=args.dry_run)
            print(f"{'將移除' if args.dry_run else '已移除'}：{destination}")
            return 0

        if sys.version_info[:2] != (3, 13):
            raise ValueError("請以已有依賴的 Python 3.13 執行本腳本。")
        if args.model is None:
            parser.error("安裝時需要 --model；圖片命名請同時提供 --mmproj")
        model = args.model.expanduser().resolve(strict=True)
        mmproj = args.mmproj.expanduser().resolve(strict=True) if args.mmproj else None
        for path in (model, mmproj):
            if path is not None and (not path.is_file() or not os.access(path, os.R_OK)):
                raise ValueError(f"模型無法讀取：{path}")
        python = Path(sys.executable).absolute()
        launcher = ROOT / "scripts/start-resident.py"
        # Check imports without initializing Qt, loading a model or opening ports.
        subprocess.run([
            str(python), "-c",
            "import sys; sys.path.insert(0, sys.argv[1]); import httpx, pydantic; "
            "import mingtu_ai.ui._native_ui",
            str(ROOT / "src"),
        ], check=True, timeout=15)
        sys.path.insert(0, str(ROOT / "src"))
        from mingtu_ai.cli import _default_watch_directories
        watches = _default_watch_directories(os.environ)
        for directory in watches:
            if directory.is_symlink() or not directory.is_dir() or not os.access(directory, os.R_OK | os.X_OK):
                raise ValueError(f"監控資料夾不可用：{directory}")
        entry = make_entry(python, launcher, model, mmproj)
        with tempfile.TemporaryDirectory(prefix="mingtu-autostart-") as temporary:
            preview = Path(temporary) / ENTRY_NAME
            preview.write_text(entry, encoding="utf-8")
            validator = shutil.which("desktop-file-validate")
            if validator:
                subprocess.run([validator, str(preview)], check=True, timeout=10)
        if args.dry_run:
            print(entry, end="")
            print("監控資料夾：" + ", ".join(map(str, watches)))
            print(f"目標：{destination}")
            configure_restart_policy(config, remove=False, dry_run=True)
            return 0
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.is_symlink():
            raise ValueError("啟動設定是符號連結，保留原檔。")
        if destination.exists():
            old_entry = destination.read_text(encoding="utf-8")
            if old_entry == entry:
                configure_restart_policy(config, remove=False, dry_run=False)
                print(f"設定已相同：{destination}")
                return 0
            if MARKER not in old_entry:
                raise ValueError("已有其他來源的命圖啟動設定，保留原檔。")
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
            backup = destination.with_suffix(f".desktop.bak.{stamp}")
            shutil.copy2(destination, backup)
            print(f"備份：{backup}")
        # Atomic replacement prevents the desktop session from reading a partial entry.
        descriptor, name = tempfile.mkstemp(prefix=".mingtu-", dir=destination.parent)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                stream.write(entry)
            os.replace(name, destination)
        finally:
            Path(name).unlink(missing_ok=True)
        print(f"已啟用 KDE 登入後自動啟動：{destination}")
        configure_restart_policy(config, remove=False, dry_run=False)
        print("監控資料夾：" + ", ".join(map(str, watches)))
        return 0
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        print(f"自動啟動設定失敗：{exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
