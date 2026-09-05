# 隱私與威脅模型

## 保護對象

- 被監控檔案的原始內容與檔名。
- 使用者的目錄結構、通知歷史與學習到的關鍵字。
- 現有檔案不被 AI 建議、改名衝突或復原操作覆寫。
- 本機上其他行程，特別是使用 `localhost:3000` 或 `127.0.0.1:3001` 的行程。
- GPU / RAM 不在待機狀態被模型長期佔用。

## 信任與不信任邊界

### 信任

- 使用者選擇的本地 GGUF 與 `llama-server` binary。
- 操作系統提供的 Python、SQLite、inotify、KDE D-Bus、FFmpeg 與 Poppler。
- 在同一 user session 中執行命圖 AI 的使用者。

### 不信任

- 所有新檔案內容，包括惡意 XML、archive、PDF、影片與誤導模型的文字。
- 模型輸出。即使 confidence 高，仍需 schema 驗證、sanitize 與使用者接受。
- 檔案事件與實際開檔之間的狀態；檔案可能被移動、取代或繼續寫入。
- loopback 上的其他本機行程。
- Desktop notification server 對 action、markup、persistence 與關閉事件的實作細節。

## 資料流

| 資料 | 去向 | 留存 |
|---|---|---|
| 原圖片 / 受限文字 / 代表頁與影格 | 只傳給 `127.0.0.1:3001` 的受管本地 model child | 原檔不由專案複製；衍生檔正常 / exception 結束後清除 |
| 檔案路徑、建議名、category、keywords、confidence、操作結果 | SQLite history | 持久，直到使用者手動管理 database |
| 建議檔名與錯誤 | KDE notification server | 是否保留取決於 Plasma 通知設定 |
| model request / response | 受管的 `llama-server` | 應只在 child memory / process logs；本專案將 child stdout/stderr 導向 null |

命圖 AI 默認沒有雲端 request、telemetry、analytics、API key 或雲端重試。`httpx.Client(trust_env=False)` 避免 provider 請求誤走 proxy environment。

## 已有緩解

- adapter 以 `O_NOFOLLOW` 開啟 pathname，再用 `fstat` 要求 regular file；MIME、文字、Office、ZIP/TAR 與 metadata 都由同一個已綁定 descriptor 讀取，不在 `lstat` 後重新 follow pathname。
- PDF / 影音 external parser 透過只在 child 繼承的 descriptor 與 `/proc/self/fd/N` 讀取同一 inode；provider 也從 adapter 繼承的 image descriptor 進行有上限的讀取。
- 穩定檢查需要連續 filesystem snapshot 不變才交給 adapter。
- 文字、XML、archive 成員數、member size、壓縮比與 model request 都有上限；external command 有 wall-clock timeout，擷取的 stdout / stderr 在子行程產生期間同步排空並強制 byte limit。超限或逾時時只終止與回收本次建立的 `Popen` child。
- Office XML 拒絕 DTD / entity declaration。
- ZIP / TAR 只列成員，不解壓。
- 模型輸出使用 strict schema，檔名有 path / control / byte-length sanitize。
- production accept / undo 先寫入 `PENDING` ledger，再以 Linux `renameat2(RENAME_NOREPLACE)` 原子取得未被占用的目的名稱；已存在的目的名稱只讓該 candidate 失敗，不會被覆寫。缺少 symbol 或 `ENOSYS`／`EINVAL` 時 fail closed，沒有 hard-link / unlink fallback。
- safety regression suite 已驗證 source replacement、target replacement、ledger failure 與路徑消失／變更。atomic move 後的 identity postcheck 若證據歧義，丟出 `RenameRecoveryRequiredError`，保留仍存在的路徑並標記 `requires_manual_recovery`，交由人工檢查；舊 hard-link pending ledger 也不自動 unlink。
- `3001` 啟動前以 bind 檢查；衝突時不找 PID、不 kill。
- 只對持有的 child `Popen` 送 terminate / kill。

## 殘餘風險

- FFmpeg、Poppler、Python archive/XML parser 仍會處理不受信任輸入；必須及時套用上游安全更新。
- `127.0.0.1:3001` 沒有應用層認證。同 user / host 的惡意行程在 model 存活時可能發請求。
- 由命圖 AI 新建的 SQLite history 目錄會設為 `0700`，database / WAL / SHM 會收緊為 `0600`。若使用者指定既有目錄，程式不會擅自修改該目錄權限。這可防止其他 UID 直接讀取 history 檔，但無法隔離同 UID 的惡意行程。
- 權重本身可能不受信任；應從官方來源取得並核對檔案、授權與來源。
- 建議檔名會出現在 Plasma 通知及 SQLite。這些文字可能間接暴露原內容。
- `TemporaryDirectory` 可處理正常與 Python exception cleanup，但 `SIGKILL`、kernel crash 或斷電可留下 OS temp 檔。
- 監控只處理指定目錄的一層，不遞迴子目錄。
- `O_NOFOLLOW` 只阻擋最後一層 symlink；若同 UID 惡意行程可重寫父目錄、修改已開啟 inode 並回復 timestamp，或干預 private temp 目錄，不能僅靠 descriptor identity 完全隔離。目前會在 adapter/provider 讀取前後核對 inode、size 與 mtime，並在 coordinator 再檢查 pathname identity；強化的同 UID 隔離需另加 sandbox / namespace。
- `renameat2(RENAME_NOREPLACE)` 已消除 production 一般 producer 的 final unlink race；pre-syscall identity check 與 atomic syscall 之間仍可能發生 source replacement，postcheck 會保留資料並標記 `requires_manual_recovery`。這仍不能保證隔離能持續改寫路徑的同 UID 惡意行程。
- 若 SQLite 在 initial ledger 完全無法寫入，可能沒有新的 ledger event；atomic move 尚未發生，原檔仍保留在原路徑，exception／通知回報 DB failure，不保證列出 target。這是檔案系統與 SQLite 無法形成單一 ACID transaction 的已知邊界。
- 長 CJK basename 的 collision suffix 已修正並有回歸測試，預覽與實際接受共用相同規則；既有 no-clobber 行為仍保留。
- AI 可能產生不準確、含偏見或不適合公開顯示的建議；使用者確認仍是必要門檻。
- 尚未完成獨立 sandbox / seccomp / container 隔離 external parsers 與 model server。

## 不在威脅模型內

- 已被入侵的 kernel，或可完全控制本行程 filesystem / `/proc` 視圖的同 UID 惡意行程。
- 使用者自行替換為惡意 `llama-server`、模型或 system tool。
- 使用者手動加入的雲端 provider 或自行修改的網路邏輯。
