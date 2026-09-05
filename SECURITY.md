# Security Policy

## 支援範圍

目前只有未發行的 V2 開發分支，尚無作為安全更新對象的穩定版本。這不表示已知問題可以被忽略；維護者會以對原檔、本機行程與隱私的影響排序。

## 回報方式

在專案尚未公開到含私密安全回報功能的 hosting platform 前，請使用你與維護者已經建立的私密溝通管道。專案不提供或猜測個人 email。

公開後，優先使用 repository 的 private security advisory / private vulnerability reporting，而不是公開 issue。回報請包含：

- 受影響的模組與開發版本。
- 可復現的最小步驟，但不附真實敏感檔案。
- 對原檔覆寫、symlink / path traversal、archive bomb、本機網路、D-Bus、模型行程、暫存檔或 SQLite 的影響。
- 已知的緩解方式。

請不要提交 API key、token、個人路徑、截圖內容、完整模型檔或能直接傷害其他使用者的 exploit。

## 安全不變式

- 管理的 server 僅綁定 `127.0.0.1:3001`。
- 不終止不屬於命圖 AI 的行程，不碰 `localhost:3000`。
- 未明確接受前不改名；改名與復原不覆寫。
- 不跟隨 symlink，默認忽略隱藏、鎖定與未完成下載檔。
- 歷史儲存路徑、建議、關鍵字與操作結果，不儲存原始內容。
- 預設沒有雲端傳輸或 API key。

完整邊界請見 [docs/privacy-and-threat-model.md](docs/privacy-and-threat-model.md)。

