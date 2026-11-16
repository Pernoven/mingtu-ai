# 運行與故障排除

## 執行前檢查

```fish
cd /path/to/命圖AI
./scripts/doctor.fish --watch /path/to/Screenshots
```

`--doctor` 會實際檢查：

- `nvidia-smi` 可執行與 GPU query。
- 指定的 `llama-server --version`。
- 模型路徑、一般檔案、讀取權限與 `GGUF` file header；若指定 `--mmproj`，projector 也必須是可讀的一般檔案且具有 `GGUF` 檔頭。
- KDE / Freedesktop notification service 與 `actions` capability。
- 是否可綁定 `127.0.0.1:3001`。
- 每個監控目錄的實際空檔建立 / 刪除權限。
- `ffmpeg -version`。

`WARN` 表示無法確定或可降級，例如 sandbox 拒絕 GPU、socket 或 D-Bus probe。`FAIL` 表示必要條件未通過。Doctor 不會終止 port 占用者、安裝套件或下載模型。

Model 和 projector 是兩個獨立 probe：只要明確傳入 `--mmproj PATH`，projector 的權限、一般檔案類型或 `GGUF` 檔頭失敗都會讓 doctor 以非零狀態結束，不會因主模型通過而忽略 projector。

指定的 CachyOS / KDE host 實測為 8/8 通過，涵蓋 GPU、llama-server、模型、projector、通知 actions、`127.0.0.1:3001`、測試目錄權限與 ffmpeg。這是該 host 當下的診斷結果，不代表其他機器具備相同條件。

## KDE Plasma 6 / Wayland

必須從有效的 graphical user session 執行，並且環境中有 `DBUS_SESSION_BUS_ADDRESS` 以及 `WAYLAND_DISPLAY`、`DISPLAY` 或 `KDE_FULL_SESSION`。

```fish
string length -- "$DBUS_SESSION_BUS_ADDRESS"
string length -- "$WAYLAND_DISPLAY"
qdbus6 org.freedesktop.Notifications /org/freedesktop/Notifications org.freedesktop.Notifications.GetCapabilities
```

Freedesktop 規格將 notification actions 定義為可選能力。如果 Plasma 沒有宣告 `actions`，建議文字仍可顯示，但按鈕可能被忽略。命圖 AI 在無 GUI session 或 D-Bus 失敗時會將通知降級到 stderr，不把降級當作已顯示通知。

## 常駐與結束

```fish
set -x MINGTU_AI_MODEL /path/to/model.gguf
./scripts/dev-run.fish --watch /path/to/Screenshots
```

在有 KDE / Wayland session 時，預設會開啟原生 Qt 主視窗與系統匣。它是與 Python coordinator 同一個 process 的 CPython extension，不需要 PySide / PyQt。開發 checkout 可用已安裝的 compiler / Qt headers 編譯，過程不下載：

```fish
python scripts/build_native_ui.py
./scripts/dev-run.fish --watch /path/to/Screenshots
```

Extension 與執行 build 的 Python ABI 綁定。若它不存在、無法 load 或沒有 graphical session，CLI 會在 stderr 說明原因後繼續 headless；如果你本來就不要 UI：

```fish
./scripts/dev-run.fish --headless --watch /path/to/Screenshots
```

以 `Ctrl+C` 正常結束。佇列會嘗試完成已接受工作，然後關閉自己的 model child 與 notification helper。不要用 `pkill llama-server`、`killall` 或根據 port 清理；這些作法可能傷害他人的工作。

登入 KDE 後自動常駐可使用 [開發 checkout 的登入啟動腳本](packaging.md#開發-checkout-的-kde-登入啟動)。它會使用目前 Python 3.13 的絕對路徑、啟動至系統匣並取得常駐鎖。監控沿用已儲存的資料夾，並在新增檔案時才按需載入模型。

Linux 監視器會同時檢查最多 2,048 個候選檔案的大小、mtime 與 inode，預設每 150 ms 檢查一次、連續兩輪不變才送入分析；單檔最多等待 5 秒。慢速寫入不會阻擋已穩定的檔案，跨 poll 保留未完成候選，並持續讀取 inotify。超出候選上限會明確停止並回報 `WatchOverflowError`，避免無聲遺失工作。它只監控選定資料夾的直接子檔案，不遞迴掃描。

若檔案在分析途中變更，常駐程式會捨棄過時結果，等檔案穩定後最多重試兩次。重試失敗且檔案仍可處理時，會顯示分析錯誤。同一路徑、同一檔案版本的重複事件只會送出一則建議，通知會列出原檔名。

Office 文件的 ZIP 容器尚未可讀時，常駐程式會等下一次檔案完成事件，再檢查容器並決定是否分析。需要分析損毀文件的檔案資訊時，可使用 `--once`。

系統匣介面待命時每秒檢查狀態，處理中每 250 ms 檢查；只在狀態變更時傳送 Qt 更新。歷史紀錄在操作／佇列狀態變更時刷新，待命時最多每 5 秒查詢一次。使用者操作執行緒待命時阻塞等待，不再每秒喚醒 10 次。可用下列腳本驗證 32 檔穩定等待、待命耗用與真實本機圖片分析；只建立暫存合成檔案：

```fish
python scripts/verify_resident.py --model /path/to/model.gguf --mmproj /path/to/mmproj.gguf --output /tmp/mingtu-resident-result.json
```

命名模型預設使用 llama.cpp 的 `--n-gpu-layers auto --fit on --parallel 1`，由引擎依可用記憶體配置單一工作槽。啟動模型時才查詢 NVIDIA 的空閒 VRAM；若不足以容納模型、projector 檔案大小加 1 GiB 餘裕，或無法可靠查詢，projector 會使用 CPU，避免在 GPU 很忙時因載入圖片編碼器而中止。這個檔案大小比較只是保守的啟發式，不能保證面對其他程式同時配置 VRAM 時永不 OOM；CPU 圖片處理也會較慢。Python 設定仍可明確指定 GPU 層數及 `mmproj_offload`。

參數依據：[llama.cpp server](https://github.com/ggml-org/llama.cpp/blob/master/tools/server/README.md)、[NVIDIA nvidia-smi](https://docs.nvidia.com/deploy/nvidia-smi/index.html)。

檔案短時間大量進入時，daemon 會保留最多 256 個 pending tasks，不會無限擴張佇列。超過上限的每個檔案會成為明確的 `FAILED` task，history 的 `analysis` failure 與 stderr/desktop error 可看到 `QueueCapacityError`；monitor 會繼續輪詢後續事件。這是 overload admission policy，不是 inotify overflow；只有 kernel 已報告 overflow 時才需停止 monitor 並重新驗證。

Plasma action 通知預設最多同時保留 32 組 `notify-send` child/collector thread。達限的通知不會再啟動程序，會直接顯示 stderr fallback；這可能表示大量未操作的 persistent notifications。Backend close 預設共用 5 秒 deadline，會終止、必要時 kill，並 reap 只由它啟動的 children。

## 常見問題

### `--doctor` 說未指定模型

```fish
set -x MINGTU_AI_MODEL /absolute/path/to/model.gguf
./scripts/doctor.fish --watch /path/to/Screenshots
```

路徑必須是可讀一般檔案，且 `.gguf` 檔頭必須是 `GGUF`。專案不會自動找尋或下載權重。

### Ollama cache 路徑無法讀取

Ollama 官方 [Modelfile 文件](https://docs.ollama.com/modelfile) 說明，`ollama show --modelfile MODEL` 可顯示該模型的 `FROM` 路徑；這個路徑可能是 Ollama blob cache，不是命圖 AI 的交付介面。當 Ollama 由 system service 管理時，cache 可能屬於另一個 service account，目前使用者不一定能穿越父目錄或讀取 blob。

命圖 AI 提供相同官方 CLI 契約的唯讀檢查入口：

```fish
mingtu-ai --resolve-ollama MODEL
mingtu-ai --resolve-ollama MODEL --json
```

resolver 只會以固定參數執行 `ollama show --modelfile MODEL`，把輸出當一般文字解析，並讀取候選檔案的大小與前四個 bytes。它不會執行 Modelfile、不會 source shell、不會複製模型、不會變更 owner / mode，也不會下載缺少的模型。成功且檔頭為 `GGUF` 時 exit code 為 `0`；找不到 CLI、service 無法連線、模型不存在／未完成、路徑不可讀或檔頭錯誤時為 `1`。文字錯誤寫入 stderr；`--json` 錯誤則輸出可解析的 JSON。

若模型本來就是直接下載且目前使用者可讀的 `.gguf`，不需要 resolver：直接使用 `--model "/absolute/path/model.gguf"` 或設定 `MINGTU_AI_MODEL` 即可。

Doctor 會將這種 `PermissionError` / `OSError` 明確回報為 **FAIL**，不會誤報「檔案不存在」，也不應顯示 Python traceback。正式 `llama-server --model` 應指向由啟動命圖 AI 的使用者可讀、不會被 cache lifecycle 取代的穩定 GGUF 副本。不要為了繞過診斷而對 Ollama cache 執行 `chmod` / `chown`；這可破壞 service 的所有權與更新邊界。本文件不提供固定 `sudo cp` 指令，複製流程需先依實際安裝、群組、模型大小與權重授權另行審查。

### `127.0.0.1:3001` 已被占用

命圖 AI 會停止本次 model task，保留原檔，並明確報錯。請由使用者自行判斷占用者是否屬於他目前的工作；專案不會提供或執行 kill 指令。`localhost:3000` 不在命圖 AI 範圍內，永遠不應被清理。

### `nvidia-smi` 在 sandbox 失敗

若 error 是 permission / isolation 限制，doctor 會報 `WARN` 而不誤判為沒有 GPU。請在實際 KDE user session 的終端再執行 doctor。若仍失敗，再依 NVIDIA / CachyOS 的官方驅動診斷流程處理。

### 有通知但沒有 actions

1. 用上方 `qdbus6 ... GetCapabilities` 檢查 `actions`。
2. 確認命圖 AI 在登入中的 KDE session 執行，不是遠端、cron 或缺 session environment 的 shell。
3. 檢查 Plasma notification 設定與免打擾模式。

### 原生 UI 降級為 headless

1. 確認 `python --version` 與當初 build extension 的 Python 都是相同 3.13 ABI。
2. 在同一 environment 執行 `python scripts/build_native_ui.py`。
3. 確認 `pkg-config --modversion Qt6Widgets Qt6DBus` 可成功。
4. 確認 `DBUS_SESSION_BUS_ADDRESS` 與 `WAYLAND_DISPLAY` / `DISPLAY` 存在。

想在 CI 只測試 Qt lifecycle，可使用 `QT_QPA_PLATFORM=offscreen`；這只能驗證 lifecycle，不能代替 KDE host 的視覺互動驗收。指定 KDE host 已另外通過真 C++ Qt 的 accept、edit、ignore、undo、conflict、cancel 六條 callback。

### 主視窗待決建議與歷史復原

分析成功後，主視窗會顯示原檔名、建議檔名與所在位置。選取一列後可執行「接受」、「修改並接受…」或「忽略」；修改欄位只輸入檔名主體，副檔名由程式保留。通知與主視窗使用同一筆 event ID，所以先在其中一處完成後，另一處的舊按鈕不會再次改動檔案。

檔案與 SQLite 操作由單一背景 action worker 執行。動作執行中按鈕會停用；完成訊息顯示在視窗，失敗詳情也會寫入 history 或 stderr。操作結果為未完成時，先重新整理列表並查看歷史，不要反覆點擊。

歷史視窗顯示最近 100 筆。選取仍可復原的成功改名後，「復原選取的改名」才會啟用；真正復原時仍會重新檢查檔案身分與目的衝突。此版本沒有歷史搜尋或篩選，也不能編輯設定。

重啟後若 ledger 留有 pending undo，`HistoryStore.reconcile_pending_undo()` 只讀取 current／original 兩條路徑並更新 ledger，不搬移或刪除檔案：expected inode 在 original 時標記 `SUCCESS`，在 current 時標記 `FAILED`，兩邊皆非 expected 時維持 `PENDING` 並標記 `requires_manual_recovery`。

上述六條 callback 的 deterministic synthetic fixture 驗證截圖在 `/tmp/mingtu-20260905-validation/native-actions-atomic-kde.png` 與 `/tmp/mingtu-20260905-validation/native-actions-atomic-kde-history.png`。以真實 Gemma 4 E2B + public synthetic image 重跑模型到 Qt buttons 的 atomic rename 閉環時，截圖在 `/tmp/mingtu-20260905-validation/native-actions-e2b-atomic-kde.png` 與 `/tmp/mingtu-20260905-validation/native-actions-e2b-atomic-kde-history.png`；這些截圖已檢查沒有裁切問題。廣泛真實截圖的畫面品質與命名品質仍未驗收。

待決建議只屬於目前 process。重新啟動後，舊 suggestion 仍留在 SQLite 歷史中供查看，但不會回到主視窗成為可接受項目；若原檔仍存在，請重新分析。待決建議最多 256 筆，額滿時不淘汰舊項目，新分析會明確失敗並要求先處理待決項目。

### PDF / video 只有 metadata 或 warning

檢查：

```fish
command -v pdftotext pdftoppm ffmpeg ffprobe
ffmpeg -version
```

缺少工具時 adapter 會降級並留下 warning，不會在背景安裝。套件指令見 [README](../README.md#cachyos--arch-桌面與檔案工具)。

`pdftotext` 使用官方的 `-` stdout 介面，不先產生無上限的文字中間檔。`pdftotext` 與 `ffprobe` 的 stdout / stderr 會同步排空，並在輸出生成過程強制 byte limit 和 timeout；超限只會終止本次 adapter 自己建立的 child。

### 監控因 overflow 或 watch removed 停止

inotify queue overflow 已經代表事件遺失，`IN_IGNORED` 則代表監控目錄被移除或檔案系統狀態改變。命圖 AI 會顯示錯誤並停止當前 monitor，不會假裝仍在完整監控。請先確認目錄仍存在且可讀，再重新啟動；因事件完整性已無法回溯證明，本版不會自動猜測或無上限掃描遺漏檔案。

### 復原失敗

若原檔名已被新檔案占用，復原必須失敗而不覆寫。SQLite 會記錄 failed undo，現有兩個檔案都保留，由使用者手動處理衝突。

如果錯誤含 `manual inspection`、`requires_manual_recovery` 或列出 source / target，表示 atomic move 之後至少一個路徑消失、被取代或無法再次證明身分。程式會保留仍存在的兩個路徑，不自動刪除或搬回。請先比較檔案內容與路徑，再由使用者人工決定；不要反覆接受、復原，也不要為了清理紀錄而覆寫其中一個檔案。若 initial ledger 寫入失敗，atomic move 尚未發生，原檔仍在原路徑，SQLite 可能沒有對應 rename event；exception／通知回報 DB failure，不保證列出 target。

目前 safety regression suite 已覆蓋 source／target replacement、ledger failure 與路徑消失／變更；production accept / undo 使用 ledger-first `renameat2(RENAME_NOREPLACE)`，缺少 symbol 或 `ENOSYS`／`EINVAL` 時 fail closed，不使用 hard-link / unlink fallback。atomic syscall 前後若 identity postcheck 發現 source 被取代，會保留資料並要求 manual recovery；舊 hard-link pending ledger 也只保守標記 `requires_manual_recovery`，不自動 unlink。長 CJK basename 的 collision suffix 已修正並有回歸測試，預覽與實際接受共用相同規則。

### 檢查 model child 是否釋放

發行前整合測試應同時觀察：任務前、推論中、佇列清空後的 child process 與 VRAM。只能證明命圖 AI 自己建立的 handle 已退出，不要將其他用戶或程式的 model process 當成殘留。

開發機已用真實 Gemma 4 E2B + mmproj 和公開的單一 synthetic 圖片完成上述檢查：cold start 2,846.233 ms、inference 1,448.970 ms、shutdown 364.591 ms，VRAM 41 → 3,134 → 41 MiB；owned child 退出且 `3001` 釋放。完整 benchmark 數字與限制見 [2026-09-05 開發與驗收狀態](development-status-2026-09-05.md)。此 smoke 不代表廣泛真實截圖、OCR、中英混合或檔名品質已通過驗收。

## 日誌與歷史

目前 CLI 輸出狀態到 stdout/stderr。systemd user service 的輸出會進 user journal；請在分享 journal 前移除檔案路徑與建議名。SQLite 含檔案路徑與建議 metadata，應視為敏感本地資料。
