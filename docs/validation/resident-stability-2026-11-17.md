# 2026-11-17 常駐與截圖監控修正

## 現場證據

- KDE 自動登入啟動正常；原服務 `Restart=no`。
- 已儲存監控資料夾與實際 inotify inode 均對應截圖、下載目錄；新截圖存在，但歷史紀錄無處理事件。
- 以系統 Qt6 的 `QSaveFile` 在相同檔案系統建立測試檔：舊監控收到 0 個事件，加上 `IN_CREATE` 後收到最終檔名事件 `0x100`。
- Spectacle 官方 [ExportManager.cpp](https://github.com/KDE/spectacle/blob/master/src/ExportManager.cpp) 的本機儲存使用 `QSaveFile`。參考 [Qt QSaveFile 文件](https://doc.qt.io/qt-6/qsavefile.html)。

## 修正與驗證

- 接收新增檔案事件，保留穩定性與去重流程。10 張以 QSaveFile 寫入的合成圖片收到 10 張，無重複。
- 原生 UI 的 SIGTERM/SIGINT 處理只設定退出旗標，透過 Qt 事件迴圈結束；避免在 Python/Qt 回呼或鎖操作中突然丟出 KeyboardInterrupt。參考 [Python signal 文件](https://docs.python.org/3/library/signal.html)。
- 監控執行緒失敗時結束 Qt 並回傳非零退出碼，讓 systemd 能重新啟動。
- 登入安裝腳本新增受管理的 drop-in，異常退出後 5 秒重啟；正常離開不重啟。
- `tests.test_resident`、`tests.test_integration`、`tests.test_ui`、`tests.test_watcher` 共 44 項通過，包含 hardlink 發布檔案、Qt SIGTERM、監控目錄失效後退出與鎖釋放。
- 本機模型端到端測試使用 `verify_resident.py` 的隔離資料夾／歷史紀錄／Qt UI 流程，將輸入寫檔改用 QSaveFile。7.02 秒產生命名建議，原始圖片雜湊不變，模型用畢釋放。數據見 [JSON](resident-qsave-2026-11-17.json)。
- 實際桌面服務已重新啟動，啟用 `Restart=on-failure`；監控包含 `IN_CREATE`。

未重開機或重新登入實測；未自動補處理先前漏接的使用者截圖。此驗證不代表長時間運行的所有故障皆已消除。
