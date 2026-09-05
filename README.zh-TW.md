# 命圖 AI (Mingtu AI)

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python 3.13](https://img.shields.io/badge/Python-3.13-blue.svg)](https://www.python.org/)
[![Platform Support](https://img.shields.io/badge/Platform-Linux%20%7C%20macOS%20%7C%20Windows-brightgreen.svg)](#)
[![CI Status](https://github.com/Pernoven/mingtu-ai/actions/workflows/ci.yml/badge.svg)](../../actions/workflows/ci.yml)

> 在檔案誕生的那一刻，用你自己電腦裡的 AI 讀懂它，再給它一個以後找得到的名字。

[English README](README.md) | 繁體中文說明

命圖 AI 是支援 **Linux (CachyOS/Arch/KDE)**、**Windows** 與 **macOS (Apple Silicon / Intel)** 的跨平台本地檔案命名助手。它監控新檔案，在本機擷取受限內容，按需啟動 `llama-server`，產生檔名建議，再由使用者透過桌面通知／主視窗決定是否改名。

## 工作流程示意

```text
檔案誕生 (inotify / Windows DirectoryWatcher / macOS Watcher)
  │
  ├──► 穩定性偵測與單一佇列 (Single Worker Queue)
  │
  ├──► 本地預覽處理 (圖片 / PDF 頁面 / 文字 / 影格)
  │
  ├──► 按需啟動 llama-server (:3001) 本地多模態 AI 推論
  │
  ├──► 嚴格 JSON 結構化檔名建議
  │
  └──► 桌面通知 / 原生 UI 決策 (Accept / Edit / Ignore / Undo)
```

## 目前狀態

這是 V2 開發中版本，不是已驗證的一般使用者發行版。

- **跨平台適配層 (Platform Abstraction Layer)**：CLI、跨平台監控引擎（Linux `inotify` / Windows & macOS `PolledWatcher`）、單 worker 佇列、本地 adapters、llama.cpp 生命週期、系統通知降級、SQLite 歷史、安全改名／復原與 `--doctor` 已實作。程式自己的 accept / undo 移動會以監控 cookie、來源／目的路徑與檔案 identity 精確關聯，不會把改名後的檔案當新檔重複分析。
- `--resolve-ollama` 可透過 Ollama 官方 CLI 唯讀檢查模型 reference 對應的本機 GGUF blob；它不會複製模型、修改權限或下載資料。
- 沒有附帶模型權重；未指定可讀的 GGUF 模型時，推論不會成功。
- 已用真實 Gemma 4 E2B GGUF + mmproj 完成一個可重建的 public synthetic 圖片 smoke：冷啟動 2,846.233 ms、推論 1,448.970 ms、關閉 364.591 ms；GPU 記憶體由 41 MiB 基線升至 3,134 MiB，結束後回到 41 MiB，owned child 已退出且 `3001` 已釋放。這是開發機的單次管線實測，不是效能或硬體需求承諾。
- 本地 provider 的 strict output 使用 Chat Completions `response_format.json_schema` 完整 envelope；`--doctor --mmproj PATH` 也會檢查 projector 的一般檔案、可讀與 `GGUF` 檔頭條件。
- 上述 smoke 只證明單一合成圖片的 multimodal 管線、格式合約與資源釋放。廣泛真實截圖、OCR、中英混合、專有名詞保留與檔名搜尋品質仍未驗證。
- 原生 Qt 6 CPython extension、系統匣、主視窗與暫停／恢復已實作。主視窗會列出本次執行中的待決建議，可接受、修改後接受或忽略；歷史視窗列出最近 100 筆事件，符合資格的改名可選列復原。設定視窗仍是唯讀狀態摘要。
- production accept / undo 使用 ledger-first 的平台安全不覆蓋改名（Linux `renameat2(RENAME_NOREPLACE)`、Windows `os.rename` 防蓋檔、macOS `renameatx_np(RENAME_EXCL)`），不以 hard-link+unlink 實作；若 host 缺少相應 API，會 fail closed。若 atomic move 後的 identity postcheck 發現 source 在 syscall 前後被取代，會以 `RenameRecoveryRequiredError` 保留路徑並標記需人工復原。SQLite 在完全無法寫入、尚未建立新 ledger 時仍可能沒有新 event。
- 實際桌面 session 已驗證原生 UI 與通知 callbacks 的閉環操作。同一個 host 的 `--doctor` 檢查可協助釐清 GPU、工具、模型、projector、通知、`3001` 與測試目錄條件。
- 預設 daemon 在有桌面 session 時嘗試原生 UI；缺少 ABI 相符的 extension 或桌面 session 時會清楚降級為 headless。`--headless` 可強制終端模式。
- `packaging/` 內的 service / autostart 都是待審查資產，不會自動安裝或啟用。

## 15 分鐘本機開發路徑（不下載）

下列路徑只使用機器上已有的 Python 與工具，不會執行 `pacman`、`pip install`、模型下載、service enable 或任何網路請求。

### Linux / macOS
```bash
cd /path/to/命圖AI

python --version
python -c 'import httpx, pydantic; print(httpx.__version__, pydantic.__version__)'

export PYTHONPATH="$PWD/src"
python -m unittest discover -s tests -v
python scripts/build_native_ui.py
```

### Windows (PowerShell)
```powershell
cd \path\to\命圖AI

python --version
python -c "import httpx, pydantic; print(httpx.__version__, pydantic.__version__)"

$env:PYTHONPATH = "$PWD\src"
python -m unittest discover -s tests -v
python scripts\build_native_ui.py
```

要跑真實推論，必須先準備本地模型：

* **Linux (Fish Shell)**:
  ```fish
  set -x MINGTU_AI_MODEL /path/to/gemma-4-E2B-it-Q4_0.gguf
  ./scripts/dev-run.fish --watch /path/to/Screenshots
  ```
* **Windows (PowerShell)**:
  ```powershell
  $env:MINGTU_AI_MODEL = "C:\path\to\gemma-4-E2B-it-Q4_0.gguf"
  .\scripts\dev-run.ps1 --watch "C:\path\to\Screenshots"
  ```
* **Windows (CMD)**:
  ```cmd
  set MINGTU_AI_MODEL=C:\path\to\gemma-4-E2B-it-Q4_0.gguf
  .\scripts\dev-run.cmd --watch "C:\path\to\Screenshots"
  ```

若不傳 `--watch`，程式會安全讀取作業系統預設圖片目錄（Linux XDG Pictures / Windows Pictures / macOS Pictures），不會 source shell 設定檔。

### Python 版本界線

`pyproject.toml` 目前明確要求 Python `>=3.13,<3.14`。

## 需要下載時（必須由使用者本人執行）

### Python 3.13 environment

建議使用獨立 Python 3.13 environment。以下指令會從 Python package index 解析與下載依賴：

```bash
python -m pip install -e .
```

### Gemma 模型

候選模型為 Google 官方 `google/gemma-4-E2B-it-qat-q4_0-gguf`。

- [Google Gemma 官方入門](https://ai.google.dev/gemma/docs/get_started)
- [Google 官方 QAT Q4_0 GGUF repository](https://huggingface.co/google/gemma-4-E2B-it-qat-q4_0-gguf)

## CLI

```text
mingtu-ai                         # 以預設圖片目錄常駐監控
mingtu-ai --watch DIR            # 指定監控目錄，可重複
mingtu-ai --once FILE            # 單檔分析與現場 demo 備援
mingtu-ai --doctor               # 診斷 GPU、模型、桌面通知、連接埠與工具
mingtu-ai --doctor --json        # 結構化診斷
mingtu-ai --resolve-ollama MODEL # 唯讀檢查 Ollama reference 與 blob
mingtu-ai --resolve-ollama MODEL --json
mingtu-ai --headless             # 常駐監控，強制不用原生 Qt UI
```

## 不變的安全界線

- 只在 `127.0.0.1:3001` 啟動受管理的 `llama-server`；不對 LAN 開放。
- `localhost:3000` 是使用者預留服務；命圖 AI 永遠不使用、停止或清理它。
- `3001` 被占用時只報錯，不根據名稱、PID 或連接埠終止他人行程。
- 只終止命圖 AI 自己建立且持有 handle 的 model 子行程。
- AI 建議不會自動改名；接受與復原都不覆寫現有檔案。
- 預設不上傳檔案內容，不需 API key，也沒有雲端 fallback。

更完整的資料處理與攻擊面請看 [隱私與威脅模型](docs/privacy-and-threat-model.md)。

## 格式支援摘要

| 類型 | 目前處理 |
|---|---|
| PNG、JPEG、WebP 等圖片 | 直接傳給本地 multimodal provider |
| PDF | 最多 12 頁本地文字預覽 + 第一頁代表圖 |
| TXT、Markdown、程式碼、JSON、CSV | 受限大小的本地文字預覽 |
| DOCX、PPTX、XLSX、ODF | 受限的 ZIP/XML 文字擷取，不完整還原版面 |
| 影片 | ffprobe metadata + FFmpeg 代表影格 |
| 音訊 | ffprobe metadata |
| ZIP、TAR | 只列出受限數量的成員名，不解壓 |
| 其他二進位檔 | MIME、大小、原檔名 metadata fallback |

完整限制與數值在 [支援格式](docs/supported-formats.md)。

## 架構

```text
Watcher (inotify / Windows / macOS) -> stable/dedupe policy -> single worker queue
       -> bounded local adapter -> on-demand llama-server :3001
       -> strict NamingSuggestion JSON -> Desktop notification / Qt UI
       -> accept/edit/ignore -> safe rename + SQLite history -> undo
```

細節見 [架構說明](docs/architecture.md)。

## 測試

```bash
python -m unittest discover -s tests -v
```

目前自動測試共 **209+ tests 通過**。

## 文件索引

- [架構](docs/architecture.md)
- [隱私與威脅模型](docs/privacy-and-threat-model.md)
- [支援格式](docs/supported-formats.md)
- [運行與故障排除](docs/operations.md)
- [2026-09-05 開發與驗收狀態](docs/development-status-2026-09-05.md)
- [systemd / autostart 資產](docs/packaging.md)
- [貢獻指南](CONTRIBUTING.md)
- [安全政策](SECURITY.md)

## License

本專案採用 [MIT License](LICENSE) 授權發行。
