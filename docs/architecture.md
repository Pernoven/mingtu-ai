# 架構說明

## 設計目標

命圖 AI 將「常駐的輕量外殼」與「按需的本地模型行程」分開。一般待機時不應存在由命圖 AI 啟動的 `llama-server`，也不應佔用其模型 VRAM。

```text
KDE / filesystem
       |
       v
InotifyWatcher -> FileEventMonitor
       |          stable + dedupe + ignore policy
       v
SingleWorkerQueue  (FIFO, one runtime context per drained batch)
       |
       v
adapt_file(path)   (bounded, local, temporary derivatives)
       |
       v
LocalGemmaProvider -> LlamaServerRuntime -> 127.0.0.1:3001
       |              owned Popen only
       v
strict NamingSuggestion JSON
       |
       +-> in-process pending proposals -> notification / Qt actions
       |
       +-> HistoryStore -> SQLite WAL
                         suggestion / ignored / failure / rename / undo
```

## 目錄結構

```text
src/mingtu_ai/
|-- app/             一次性與常駐協調、使用者 actions
|-- watcher/         inotify、穩定檢查、去重、ignore policy
|-- queue/           單 worker FIFO 與任務狀態機
|-- adapters/        圖片、PDF、文字、Office、媒體、archive、metadata
|-- runtime/         llama-server owned-child 生命週期
|-- providers/       OpenAI-compatible local Gemma request
|-- naming/          prompt、strict schema、sanitize、no-clobber rename
|-- notifications/   notify-send backend 與 callback abstraction
|-- history/         SQLite ledger 與 no-clobber undo
|-- diagnostics/     --doctor 檢查
|-- ui/              原生 Qt extension bridge 與安全降級
|-- desktop_paths.py XDG / Spectacle 路徑解析
|-- cli.py           mingtu-ai entry point
`-- __main__.py      python -m mingtu_ai

native/
`-- mingtu_native.cpp  Qt 6 Widgets / D-Bus CPython extension

scripts/
`-- build_native_ui.py  用當前 CPython ABI 本機編譯 extension
```

## 桌面 UI 執行模型

常駐 CLI 在有 graphical session 且找得 ABI 相符的 `_native_ui` extension 時，由 Python 主執行緒進入 Qt `QApplication` event loop。同一 CPython process 中：

- `NativeUiController` 用受控 monitor thread 執行 coordinator，status thread 定期將佇列、模型、待決建議、歷史與暫停狀態送回 Qt。
- C++ bridge 使用 queued invocation 將 UI 變更丟回 Qt thread。
- 主視窗以穩定的 suggestion history ID 顯示本次執行的待決建議，可接受、修改後接受或忽略。最近 100 筆歷史以表格顯示，只有通過 ledger 與目前檔案狀態檢查的改名列能提交復原。設定仍是唯讀狀態摘要，不是 preference editor。
- Qt thread 不直接執行檔案或 SQLite 操作。它只把一個動作交給容量 1 的 action worker；動作進行時其他按鈕停用，完成後再由 queued invocation 更新 UI。
- UI 離開時 controller 會只停止它持有的 monitor、queue、helpers 與 model child。

沒有 desktop session、extension 缺失或 load 失敗時，CLI 顯示原因後改用 headless；`--headless` 可明確跳過 UI。Extension 由 `python scripts/build_native_ui.py` 連結系統 Qt 6，不引入 PySide / PyQt，也不產生另一 sidecar process。

## 任務與模型生命週期

1. inotify 只將 `IN_CLOSE_WRITE` 或 `IN_MOVED_TO` 的一般檔案送入分析；`IN_MOVED_FROM` 僅用來關聯移動事件。
2. `FileEventMonitor` 確認大小 / mtime 穩定，並依 filesystem snapshot 去重。程式自己的 accept / undo 改名會在共用 lock 內完成並註冊，只有同時符合 inotify cookie、來源／目的路徑與檔案 identity 的 destination 事件會被抑制。關聯有 TTL 和 capacity 上限，不會因同名或重用 inode 而廣泛忽略外部新檔。
3. `SingleWorkerQueue` 以 FIFO 依序處理。一個檔案失敗不會丟掉後續任務。Pending admission 預設上限為 256，可由 `AppConfig.max_pending_tasks` 調整（`0` 表示拒絕所有新任務）。達限的 submission 不會丟失狀態：它會收到帶 `QueueCapacityError` 的 terminal `FAILED` task，觸發同一 `on_change` 管線記入 history 與通知；`_pending` 和 task retention 因此都有明確上限。
4. adapter 先用 `O_NOFOLLOW` + `fstat` 將輸入綁定為 regular-file descriptor，並在模型啟動前準備受限輸入。內建 parser 讀同一 descriptor；Poppler / FFmpeg 則由 `pass_fds` 繼承後開 `/proc/self/fd/N`，避免 pathname swap 導向另一個檔案。adapter 失敗時不必載入模型。
5. provider 進入 `runtime.job()` 時才檢查 `3001`、啟動 child 並等待 `/health` 明確回傳 `{"status":"ok"}`。
6. 同批次任務共用 runtime；當批 context 結束時關閉自己的 child，等待退出並確認 port 釋放。
7. 無論 adapter/provider 成功或失敗，temporary context 都會嘗試刪除衍生頁面與影格。

成功分析另進入最多 256 筆的 in-process 待決建議 registry。達限時不淘汰舊建議；新的分析明確失敗、寫入 history 並通知使用者稍後手動重試。通知 callbacks 與 Qt UI 共用同一個 decision lock 與 event ID，因此接受或忽略其中一邊成功後，另一邊的舊動作會成為無效果的 stale action。可重試的 history 寫入失敗會保留待決項目；來源已消失、被取代，或 rename 已進入 post-commit recovery 時則移除它，避免再次處理不再可信的來源。

待決 registry 不持久化，也不從 SQLite 重建。重新啟動前的 suggestion 仍是歷史紀錄，但不能由新 process 直接接受或忽略；使用者可重新分析仍存在的原檔。

Runtime 不會掃描 process name、不會從 port 找 PID，也不會對外部 `llama-server` 重用或送 signal。

Action notification 每則會使用一個受管的 `notify-send` child 與一個 collector thread。Backend 同時對兩者執行 32 組的預設上限，並在同一 lock 下完成 capacity check、child/thread 登記與啟動，避免 `send` / `close` 競爭造成未追蹤 helper。達限時不 spawn；`close` 只對自己登記的 child 送 terminate/kill，並使用共用 deadline 進行 bounded wait/reap。

inotify queue overflow 表示已無法證明事件完整；`IN_IGNORED` 表示至少一個監控已被 kernel 移除。兩者都會清除 owned-move 關聯、回報明確錯誤並停止當前 monitor，不在可能漏檔的狀態下繼續。已進入 queue 的任務仍按正常 drain 流程結束。

## 模型邊界

`LlamaServerConfig` 固定：

- host: `127.0.0.1`
- port: `3001`
- `--offline`
- `--no-webui`
- thinking / reasoning budget 關閉
- OpenAI-compatible `/v1/chat/completions`

Provider 以 `trust_env=False` 建立 HTTP client，避免無意間繼承 proxy environment。這是「不離開本機」的一部分，但不是一個本機存取控制邊界：當 server 存活時，同一使用者下的其他行程可能連線 loopback port。

Chat Completions request 使用完整的 `response_format` envelope：`type` 為 `json_schema`，`json_schema` 內含穩定名稱、`strict: true` 與 Pydantic 產生的 schema。Provider 不會在 strict schema 失敗後靜默降級為自由 JSON。
格式層級依 [OpenAI Chat Completions 官方 API reference](https://platform.openai.com/docs/api-reference/chat/create) 建立，並以本機 llama.cpp build 實測相容性。

## 輸出與檔名

Provider 必須回傳符合 strict Pydantic schema 的 JSON：

```json
{
  "suggested_stem": "PyTorch_CUDA_記憶體不足錯誤",
  "detected_language": "zh-Hant",
  "category": "software_error",
  "keywords": ["PyTorch", "CUDA", "out of memory"],
  "confidence": 0.92
}
```

自由文字、多餘欄位、型別強制或不安全 path 會被拒絕。程式保留原副檔名、清理 stem，並以 ledger-first 的 Linux `renameat2(RENAME_NOREPLACE)` 執行 no-clobber accept。每個 collision candidate 先寫入 `PENDING`，`EEXIST` 會標記該 candidate `FAILED` 後再試 `_2`、`_3` 等候選，不覆寫占用者。缺少 `renameat2` symbol、`ENOSYS` 或 `EINVAL` 時 fail closed；production accept 不再使用 hard-link / unlink。

## 歷史一致性

SQLite 以每次操作的短連線與明確 close 寫入，並使用 WAL、foreign keys 與 busy timeout。檔案系統和 SQLite 無法形成單一 ACID transaction；production accept 因此使用 ledger-first 的 crash-aware 路徑：先持久化 `PENDING` rename ledger，再以 `renameat2(RENAME_NOREPLACE)` 原子宣告來源到目的名稱，最後核對目的 identity 並標記 `SUCCESS`。不支援該 syscall 的 host 在移動前 fail closed。

- production atomic move 不建立需要 rollback 的 target link；`EEXIST` 只代表該 candidate 已被占用，該筆 `PENDING` 會標記 `FAILED` 後重新產生下一個 candidate。
- 如果 source 在 atomic syscall 前後被取代、消失或 postcheck 無法證明目的 identity，程式不猜測哪個路徑可刪。`RenameRecoveryRequiredError` 會保留仍存在的路徑並標記 `requires_manual_recovery`，要求人工檢查；不支援 syscall 時則在移動前直接失敗。initial ledger 本身完全失敗時可能沒有可確認的 history event，atomic move 尚未發生，原檔仍在原路徑，exception／通知回報 DB failure，不保證列出 target。
- `HistoryStore.reconcile_pending_rename` 依 `move_strategy` 對帳：新的 `renameat2_noreplace` ledger 只在 expected target 唯一存在時完成 commit，source 仍是原 identity 時標記未提交；舊 hard-link ledger 只使用保守的雙路徑 identity 檢查，若兩個 hard link 同時存在或證據歧義便標記 `requires_manual_recovery`，不自動 unlink。`undo_rename` 會先對 `PENDING` rename 執行這項對帳。
- `HistoryStore.reconcile_pending_undo` 是重啟後的 pending undo 對帳入口。它只讀 filesystem 並更新 ledger，不搬移或刪除路徑：expected inode 在 original 時標記 `SUCCESS`，在 current 時標記 `FAILED`，兩邊皆非 expected 時維持 `PENDING` 並標記 `requires_manual_recovery`。

Undo 也先寫 `PENDING` 再執行 `renameat2(RENAME_NOREPLACE)` no-clobber move。所有路徑都不會為了「修復紀錄」而覆寫任何檔案。

`renameat2(RENAME_NOREPLACE)` 已消除 production 一般 producer 的 final check-to-unlink race，但 pre-syscall identity check 與 atomic syscall 之間仍可能有 source replacement。若 postcheck 發現 atomic syscall 移動了非預期 identity，程式以 `RenameRecoveryRequiredError` 保留資料並要求人工復原；不支援或安全 syscall 失敗則不移動檔案。這仍不是對同 UID 惡意行程完全控制父目錄或持續重寫 source path 的隔離邊界。

`can_undo_rename()` 是 UI 用的唯讀資格檢查。它查詢完整 ledger 中是否已有成功 undo，並檢查目前 target identity 與原名衝突；判斷不受 UI 只載入最近 100 筆的限制。實際 `undo_rename()` 仍會重新執行全部安全檢查，UI 的資格結果不是檔案操作授權。

本次 safety regression suite 已驗證 source／target replacement、ledger failure 與路徑消失／變更：production accept / undo 使用 atomic ledger-first move，不會再依猜測刪除或覆寫檔案。`RenameRecoveryRequiredError` 會保留仍存在的路徑並標記 `requires_manual_recovery`，讓使用者人工比對。若 SQLite 在 initial ledger 尚未建立前完全失效，可能沒有新的 ledger event；atomic move 尚未發生，原檔仍在原路徑，exception／通知回報 DB failure，不保證列出 target。

`renameat2(RENAME_NOREPLACE)` 已消除 production 一般 producer 的 final check-to-unlink race；pre-syscall identity check 與 atomic syscall 之間若發生 source replacement，postcheck 會保留資料並標記 manual recovery。缺少 symbol、`ENOSYS`／`EINVAL` 或其他安全 syscall failure 時不移動檔案。長 CJK basename 的 collision suffix 已修正並有回歸測試，預覽與接受共用相同規則。設定編輯器、歷史搜尋／篩選，以及廣泛真實截圖的畫面與命名品質仍未完成。

## 目前未整合

- 任何雲端 provider、帳號、API key 或雲端重試。
- 音訊波形的原生 Gemma 輸入。
- 遺留 Office 二進位格式的完整解析。
- 可編輯的 GUI preference editor，以及歷史搜尋／過濾。

## 原生介面依據

- [Qt QSystemTrayIcon](https://doc.qt.io/qt-6/qsystemtrayicon.html)：系統匣可用性、context menu、activation 與 message capability。
- [Qt QTableWidget](https://doc.qt.io/qt-6/qtablewidget.html) 與 [QInputDialog](https://doc.qt.io/qt-6/qinputdialog.html)：待決／歷史表格與修改檔名輸入。
- [Qt Threads and QObjects](https://doc.qt.io/qt-6/threads-qobject.html) 與 [Synchronizing Threads](https://doc.qt.io/qt-6/threads-synchronizing.html)：QObject thread affinity 與 queued invocation 邊界。
