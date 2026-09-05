# 命圖 AI

> 在檔案誕生的那一刻，用你自己電腦裡的 AI 讀懂它，再給它一個以後找得到的名字。

命圖 AI 是為 CachyOS / Arch Linux、KDE Plasma 6 與 Wayland 開發的本地檔案命名助手。它監控新檔案，在本機擷取受限內容，按需啟動 `llama-server`，產生檔名建議，再由使用者透過 KDE 通知決定是否改名。

## 目前狀態

這是 V2 開發中版本，不是已驗證的一般使用者發行版。

- CLI、inotify 監控、單 worker 佇列、本地 adapters、llama.cpp 生命週期、KDE 通知、SQLite 歷史、安全改名／復原與 `--doctor` 已實作。程式自己的 accept / undo 移動會以 inotify cookie、來源／目的路徑與檔案 identity 精確關聯，不會把改名後的檔案當新檔重複分析。
- `--resolve-ollama` 可透過 Ollama 官方 CLI 唯讀檢查模型 reference 對應的本機 GGUF blob；它不會複製模型、修改權限或下載資料。
- 沒有附帶模型權重；未指定可讀的 GGUF 模型時，推論不會成功。
- 已用真實 Gemma 4 E2B GGUF + mmproj 完成一個可重建的 public synthetic 圖片 smoke：冷啟動 2,846.233 ms、推論 1,448.970 ms、關閉 364.591 ms；GPU 記憶體由 41 MiB 基線升至 3,134 MiB，結束後回到 41 MiB，owned child 已退出且 `3001` 已釋放。這是開發機的單次管線實測，不是效能或硬體需求承諾。
- 本地 provider 的 strict output 使用 Chat Completions `response_format.json_schema` 完整 envelope；`--doctor --mmproj PATH` 也會檢查 projector 的一般檔案、可讀與 `GGUF` 檔頭條件。
- 上述 smoke 只證明單一合成圖片的 multimodal 管線、格式合約與資源釋放。廣泛真實截圖、OCR、中英混合、專有名詞保留與檔名搜尋品質仍未驗證。
- 原生 Qt 6 CPython extension、系統匣、主視窗與暫停／恢復已實作。主視窗會列出本次執行中的待決建議，可接受、修改後接受或忽略；歷史視窗列出最近 100 筆事件，符合資格的改名可選列復原。設定視窗仍是唯讀狀態摘要。
- production accept / undo 使用 ledger-first 的 Linux `renameat2(RENAME_NOREPLACE)`，不以 hard-link+unlink 實作；若 host 缺少 syscall、symbol 或回報 `ENOSYS`／`EINVAL`，會 fail closed。若 atomic move 後的 identity postcheck 發現 source 在 syscall 前後被取代，會以 `RenameRecoveryRequiredError` 保留路徑並標記需人工復原。SQLite 在完全無法寫入、尚未建立新 ledger 時仍可能沒有新 event。舊 hard-link ledger 只由 pending reconciliation 保守標記人工復原，不自動 unlink；重啟時 pending undo 只做 filesystem 對帳與 ledger 更新，不搬移或刪除路徑。
- 實際 KDE session 已驗證原生 C++ Qt 的 accept、edit、ignore、undo、conflict、cancel 六條 callback 路徑；使用真實 E2B 與 public synthetic 圖片時，模型到 Qt 按鈕的 atomic rename 閉環也已通過。這些是指定 host 的操作證據，廣泛真實截圖與畫面品質仍未完成驗收。
- 同一個 KDE host 的 `--doctor` 8 項檢查全數通過；這只代表該 host 當下的 GPU、工具、模型、projector、通知、`3001` 與測試目錄條件成立。
- 預設 daemon 在有桌面 session 時嘗試原生 UI；缺少 ABI 相符的 extension 或桌面 session 時會清楚降級為 headless。`--headless` 可強制終端模式。
- `packaging/` 內的 service / autostart 都是待審查資產，不會自動安裝或啟用。

> **Release blocker：**專案尚未選定或附上程式碼授權。在對外發行或宣稱為開源專案前，維護者必須完成授權決策。Gemma 權重與其他相依也有各自的授權條款，不會因本專案的未來授權而改變。

## 15 分鐘本機開發路徑（不下載）

下列路徑只使用機器上已有的 Python 與工具，不會執行 `pacman`、`pip install`、模型下載、service enable 或任何網路請求。

```fish
cd /path/to/命圖AI

python --version
python -c 'import httpx, pydantic; print(httpx.__version__, pydantic.__version__)'

set -lx PYTHONPATH "$PWD/src"
python -m unittest discover -s tests -v
./scripts/doctor.fish --watch "$PWD"

# 建立與當前 Python ABI 相符的 Qt 6 extension（不下載）
python scripts/build_native_ui.py
```

要跑真實推論，必須先準備本地模型：

```fish
set -x MINGTU_AI_MODEL /path/to/gemma-4-E2B-it-Q4_0.gguf

# 手動分析一檔；原檔在使用者接受建議前不會改名
./scripts/dev-run.fish --once /path/to/example.png

# 常駐監控；可重複 --watch
./scripts/dev-run.fish --watch /path/to/Screenshots

# 強制不開啟系統匣與視窗
./scripts/dev-run.fish --headless --watch /path/to/Screenshots
```

若不傳 `--watch`，程式會安全讀取 XDG Pictures 與 Spectacle 設定來選擇預設目錄，不會 source shell 設定檔。

### Python 版本界線

`pyproject.toml` 目前明確要求 Python `>=3.13,<3.14`。開發快照使用的 Miniconda Python 3.13 已有 `httpx` 與 `pydantic`；CachyOS 目前的 system Python 3.14 不符合這個上限，不可只因為 pacman 套件存在就混用兩個 Python environment。

如果你的 Python 3.13 environment 缺少依賴，請先閱讀下方「需要下載時」，再由你本人執行安裝。兩個 fish script 只會檢查後終止，不會自動修改 environment。

## 需要下載時（必須由使用者本人執行）

下列指令都可能下載或安裝資料，本專案不會自動執行它們。執行前請檢查套件差異、磁碟空間、網路與授權。

### CachyOS / Arch 桌面與檔案工具

以 2026-08-28 的開發機狀態核對：`ffmpeg` 9.0.1、`poppler` 26.08.0、`libnotify` 0.8.8、`kdialog` 26.08.0、`qt6-tools` 6.11.2 與 `xdg-utils` 1.2.1 均已安裝；`pacman -Qo` 也已確認它們分別提供 `ffmpeg` / `ffprobe`、Poppler CLI、`notify-send`、`kdialog`、`qdbus6` 與 `xdg-open`。這些套件名同時可由當時的 pacman sync metadata 查得；下列是給其他缺少工具的系統參考，不是本專案會執行的 installer：

```fish
# 會下載／安裝：只能由你本人執行
sudo pacman -S --needed ffmpeg poppler libnotify kdialog qt6-tools xdg-utils
```

本機的 `/usr/bin/llama-server` 由已安裝的 `llama.cpp-cuda` build 10509 提供，但同名套件目前無法從 pacman sync repository 重新查得，因此這裡不提供未核對的 pacman 安裝指令。請依 [llama.cpp 官方 server 文件](https://github.com/ggml-org/llama.cpp/blob/master/tools/server/README.md) 選擇與你的 CUDA / 硬體相符的安裝方式。

### 原生 Qt 6 build

開發快照上的 `gcc`、`pkgconf` 與 `qt6-base` 均已安裝，並已用 `pkg-config` 確認 `Qt6Widgets` / `Qt6DBus` 6.11.2。若你的 CachyOS / Arch 系統缺少這些 build tools，下列套件名已以本機 pacman metadata 核對：

```fish
# 會下載／安裝：只能由你本人執行
sudo pacman -S --needed gcc pkgconf qt6-base
```

Build 本身不下載；它用當前 Python 3.13 的 extension suffix 產生 `src/mingtu_ai/ui/_native_ui<EXT_SUFFIX>.so`：

```fish
python scripts/build_native_ui.py
```

產物與 CPython minor version / ABI 綁定，不應將某一台機器 build 的 `.so` 當成通用發行檔。這個 extension 直接連結 Qt 6 Widgets / D-Bus，不需要 PySide 或 PyQt。

### Python 3.13 environment

建議使用獨立 Python 3.13 environment。以下指令會從 Python package index 解析與下載依賴：

```fish
# 會下載／安裝：只能由你本人在 Python 3.13 environment 執行
python -m pip install -e .
```

若 `python --version` 不是 3.13，請先停下，不要對 CachyOS system Python 3.14 強制安裝這個開發版。Python 官方建議用 [`venv`](https://docs.python.org/3.13/library/venv.html) 隔離專案依賴。

### Gemma 模型

候選模型為 Google 官方 `google/gemma-4-E2B-it-qat-q4_0-gguf`。Google 文件將 Gemma 4 E2B 列為可接受文字、圖片與音訊的多模態模型，但命圖 AI 目前對音訊只傳 metadata，不宣稱已接上原生音訊推論。

- [Google Gemma 官方入門](https://ai.google.dev/gemma/docs/get_started)
- [Google 官方 QAT Q4_0 GGUF repository](https://huggingface.co/google/gemma-4-E2B-it-qat-q4_0-gguf)

模型下載需由使用者本人在官方 repository 完成；下載前請核對檔案大小、磁碟空間、權重授權與 llama.cpp 相容性。本專案不會自動登入 Hugging Face、同意條款或下載權重。

若用 `ollama show --modelfile MODEL` 取得 Ollama blob 路徑，請注意 system service 的 cache 可能不允許目前使用者讀取，也可能被 cache lifecycle 取代。Doctor 會將權限 / I/O 錯誤報為 FAIL，而不是誤報不存在或顯示 traceback。請將正式 model path 設為使用者可讀的穩定 GGUF 副本，不要 `chmod` / `chown` Ollama cache；目前不提供未核對的 `sudo cp` 程序。詳見 [Ollama cache 故障排除](docs/operations.md#ollama-cache-路徑無法讀取) 與 [Ollama 官方 Modelfile 文件](https://docs.ollama.com/modelfile)。

若手上已經是可讀的 `.gguf` 檔案，直接把它傳給 `--model` 或設為 `MINGTU_AI_MODEL`，不需要經過 Ollama resolver。路徑含空白時，fish 中請將整段路徑加上引號：

```fish
set -x MINGTU_AI_MODEL "/path/to/large models/model.gguf"
```

## CLI

```text
mingtu-ai                         # 以預設 Spectacle/XDG 目錄常駐監控
mingtu-ai --watch DIR            # 指定監控目錄，可重複
mingtu-ai --once FILE            # 單檔分析與現場 demo 備援
mingtu-ai --doctor               # 診斷 GPU、模型、KDE、連接埠與工具
mingtu-ai --doctor --json        # 結構化診斷
mingtu-ai --resolve-ollama MODEL # 唯讀檢查 Ollama reference 與 blob
mingtu-ai --resolve-ollama MODEL --json
mingtu-ai --headless             # 常駐監控，強制不用原生 Qt UI
```

`--mmproj` 是可選設定；未指定時 doctor 不會把它視為缺少。只要明確提供 `--mmproj PATH`，`--doctor` 就會要求該路徑是目前使用者可讀的一般檔案，且前四個 bytes 為 `GGUF`，否則回傳非零 exit code。

預設歷史在 `$XDG_STATE_HOME/mingtu-ai/history.sqlite3`，未設 `XDG_STATE_HOME` 時使用 `~/.local/state/mingtu-ai/history.sqlite3`。可用 `--history-db` 改寫。

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
| 音訊 | ffprobe metadata；未啟用 Gemma 原生音訊理解 |
| ZIP、TAR | 只列出受限數量的成員名，不解壓 |
| 其他二進位檔 | MIME、大小、原檔名 metadata fallback |

完整限制與數值在 [支援格式](docs/supported-formats.md)。

## 架構

```text
inotify -> stable/dedupe policy -> single worker queue
       -> bounded local adapter -> on-demand llama-server :3001
       -> strict NamingSuggestion JSON -> KDE notification
       -> accept/edit/ignore -> safe rename + SQLite history -> undo
```

- 長駐：Python coordinator、watcher、queue、notification/history shell。
- 桌面模式：同一 CPython process 的主執行緒執行 `QApplication`；monitor / status 是受控 Python threads，不另啟 UI daemon。
- 按需：`llama-server`，只在佇列有工作時存在。
- 多檔同批處理共用同一 runtime，當批結束後關閉。
- 佇列預設最多接受 256 個 pending tasks（`AppConfig.max_pending_tasks`）；超過時該任務會立即以 `FAILED` 結束、記入 history 並發出降級通知，monitor 不會因檔案風暴崩潰。
- 分析完成後另有最多 256 筆的本次執行待決建議表；不淘汰尚未決定的建議。通知與主視窗共用同一決策入口，一筆建議只能成功接受或忽略一次。重新啟動後，舊建議仍可在歷史看到，但不會重建為可操作的待決項目。
- Qt thread 只提交穩定 event ID；接受、修改、忽略與復原由單一背景 action worker 依序執行。操作期間會停用其他動作，實際檔案操作仍會重新檢查來源身分與 no-clobber 條件。
- `notify-send` action helper 預設同時上限為 32 組 child/thread；達限時不再 spawn，改走 stderr fallback。關閉 backend 會在 5 秒內嘗試終止並 reap 自己的 children。
- 暫存 PDF 頁與影格位於 system temporary directory，adapter context 結束後清除。

細節見 [架構說明](docs/architecture.md)。

## 測試

```fish
set -lx PYTHONPATH "$PWD/src"
python -W error::ResourceWarning -m unittest discover -s tests -v
python -m compileall -q src tests
```

目前自動測試共 **209 tests 通過**，不需 GPU 或真實 model；真實 Gemma 4 E2B + mmproj 的單一 public synthetic smoke 與 VRAM / child / `3001` 釋放也已在開發機完成。benchmark 數字與驗證範圍見 [2026-09-05 開發與驗收狀態](docs/development-status-2026-09-05.md) 及 [benchmark 說明](benchmark/README.md#public-e2b-synthetic-smoke)。廣泛真實檔案命名品質、OCR、中英混合、專有名詞保留、檔名搜尋與長時間壓力仍是發行前門檻。

原生 UI 已有 Python bridge 單元測試，Qt extension build 通過；指定 KDE host 的六條原生 C++ callback 與 E2B 模型閉環已通過，證據截圖位於 `/tmp/mingtu-20260905-validation/native-actions-atomic-kde*.png` 與 `/tmp/mingtu-20260905-validation/native-actions-e2b-atomic-kde*.png`。這些 smoke 不代表廣泛畫面品質或長時間壓力已驗收。

## 文件索引

- [架構](docs/architecture.md)
- [隱私與威脅模型](docs/privacy-and-threat-model.md)
- [支援格式](docs/supported-formats.md)
- [運行、KDE Wayland 與故障排除](docs/operations.md)
- [2026-09-05 開發與驗收狀態](docs/development-status-2026-09-05.md)
- [systemd / autostart 資產](docs/packaging.md)
- [貢獻指南](CONTRIBUTING.md)
- [安全政策](SECURITY.md)
