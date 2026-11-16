# Mingtu AI (命圖 AI)

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python 3.13](https://img.shields.io/badge/Python-3.13-blue.svg)](https://www.python.org/)
[![Platform Support](https://img.shields.io/badge/Platform-Linux%20%7C%20macOS%20%7C%20Windows-brightgreen.svg)](#)
[![CI Status](https://github.com/Pernoven/mingtu-ai/actions/workflows/ci.yml/badge.svg)](../../actions/workflows/ci.yml)

> Instant, local-first AI file auto-naming assistant powered by Gemma 4 & llama.cpp.

English | [繁體中文說明](README.zh-TW.md)

**Mingtu AI** is a cross-platform (Linux, Windows, macOS) local-first AI file naming daemon. It watches your designated directories (e.g., `Downloads` or `Screenshots`), extracts local content (images, PDFs, text, video frames), dynamically spawns a local `llama-server` on demand, generates structured AI filename suggestions, and presents desktop notifications or native GUI cards for single-click rename and undo.

---

## 💡 How It Works

```text
File Created (inotify / Windows DirectoryWatcher / macOS Watcher)
  │
  ├──► Stability Check & Single-Worker Queue (Deduplication)
  │
  ├──► Local Content Preview (Images / PDF Pages / Code / Video Frames)
  │
  ├──► On-demand local llama-server (:3001) Multimodal AI Inference
  │
  ├──► Strict JSON Naming Schema Validation
  │
  └──► Desktop Notification / Qt 6 UI (Accept / Edit / Ignore / Atomic Undo)
```

---

## ✨ Key Features

- 🔒 **100% Local & Private**: No cloud API keys, no network upload. All inference runs locally via GGUF models.
- ⚡ **On-Demand Lifecycle**: `llama-server` is only spawned when files arrive and automatically shuts down after processing to free VRAM.
- 💻 **Cross-Platform**: Full support for **Linux** (KDE Plasma 6 / Desktop Notifications / inotify), **Windows** (PowerShell / CMD / WinNotifications / DirectoryWatcher), and **macOS** (Apple Silicon / Intel / FSEvents Polling).
- 🛡️ **Atomic & Non-Destructive**: Safe ledger-first renaming (`renameat2` RENAME_NOREPLACE on Linux, `os.rename` no-clobber check on Windows, `renameatx_np` RENAME_EXCL on macOS) with instant Undo history.
- 📊 **Multi-Format Support**: Images (PNG/JPEG/WebP), PDF text & thumbnails, Code/TXT/Markdown, Video keyframes via FFmpeg, and ZIP metadata.

---

## 🚀 Quick Start (Local Development)

### 1. Prerequisites

- **Python**: `>=3.13,<3.14`
- **Dependencies**: `httpx`, `pydantic`
- **Local Model**: GGUF Vision-Language Model (e.g., `google/gemma-4-E2B-it-qat-q4_0-gguf`)

### 2. Setup

#### Linux / macOS
```bash
git clone https://github.com/Pernoven/mingtu-ai.git
cd mingtu-ai

export PYTHONPATH="$PWD/src"
python -m unittest discover -s tests -v

# (Optional) Build native Qt 6 desktop UI extension
python scripts/build_native_ui.py
```

#### Windows (PowerShell)
```powershell
git clone https://github.com/Pernoven/mingtu-ai.git
cd mingtu-ai

$env:PYTHONPATH = "$PWD\src"
python -m unittest discover -s tests -v

# (Optional) Build native Qt 6 desktop UI extension
python scripts\build_native_ui.py
```

### 3. Run AI File Renaming Daemon

Set your GGUF model path and run the watcher daemon:

**Linux / macOS (Bash / Fish)**:
```bash
export MINGTU_AI_MODEL="/path/to/gemma-4-E2B-it-Q4_0.gguf"
python -m mingtu_ai.cli --watch ~/Pictures/Screenshots
```

**Windows (PowerShell)**:
```powershell
$env:MINGTU_AI_MODEL = "C:\path\to\gemma-4-E2B-it-Q4_0.gguf"
python -m mingtu_ai.cli --watch "C:\Users\YourName\Pictures\Screenshots"
```

**Single-File Demo Mode**:
```bash
python -m mingtu_ai.cli --once /path/to/sample_screenshot.png
```

---

## 🛠️ Diagnostics & CLI Commands

```text
mingtu-ai                         # Watch default system Pictures/Screenshots directory
mingtu-ai --watch DIR            # Watch custom directory (can be passed multiple times)
mingtu-ai --once FILE            # Analyze a single file instantly
mingtu-ai --batch DIR --dry-run   # Preview batch folder renaming without modifying files
mingtu-ai --batch DIR -y         # One-click batch rename folder (--preset photo_date)
mingtu-ai --doctor               # Run 8-point system diagnostic (GPU, Ports, Models, UI)
mingtu-ai --doctor --json        # Output diagnostic results as JSON
mingtu-ai --resolve-ollama MODEL # Resolve GGUF blob path from local Ollama model reference
mingtu-ai --headless             # Run daemon in headless terminal mode
mingtu-ai --single-instance --start-minimized # Start one resident in the system tray
```

For KDE login startup, run `python scripts/install-autostart.py --model /path/to/model.gguf --mmproj /path/to/mmproj.gguf` with the working Python 3.13 environment. This pins the interpreter path and uses saved watch folders. Use `--dry-run` to preview or `--remove` to disable future login startup. See [startup setup](docs/packaging.md#開發-checkout-的-kde-登入啟動).

---

## 🛡️ Security & Privacy Guarantees

- **No LAN Exposure**: `llama-server` is explicitly bound to `127.0.0.1:3001` only.
- **Port Safety**: If port `3001` is occupied by another process, Mingtu AI logs an error without killing external processes.
- **Explicit Renaming**: AI suggestions will **never** rename files automatically without user consent.
- **No Overwriting**: Rename operations will fail closed if target path already exists.

For threat model details, see [Privacy & Threat Model](docs/privacy-and-threat-model.md).

---

## 📁 Supported Formats Overview

| Category | Processing Pipeline |
|---|---|
| **Images** (PNG, JPEG, WebP) | Passed directly to local multimodal vision LLM |
| **PDF** | Extracts up to 12 pages of text preview + 1st page visual thumbnail |
| **Text / Code** (TXT, MD, PY, JSON, CSV) | Local bounded text preview |
| **Documents** (DOCX, PPTX, XLSX) | Bounded XML text extraction |
| **Video** | `ffprobe` metadata + FFmpeg representative keyframe |
| **Audio** | `ffprobe` metadata preview |
| **Archives** (ZIP, TAR) | Member listing without unpacking |

See [Supported Formats Document](docs/supported-formats.md) for complete bounds.

---

## 📖 Documentation Index

- [Architecture Overview](docs/architecture.md)
- [Privacy & Threat Model](docs/privacy-and-threat-model.md)
- [Supported Formats & Limits](docs/supported-formats.md)
- [Operations & Troubleshooting](docs/operations.md)
- [Development & Verification Status](docs/development-status-2026-09-05.md)
- [Contributing Guidelines](CONTRIBUTING.md)
- [Security Policy](SECURITY.md)

---

## 📄 License

Distributed under the [MIT License](LICENSE).
