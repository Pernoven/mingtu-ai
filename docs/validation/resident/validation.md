# 自動命名常駐監視器驗證（2026-10-16）

在既有 CachyOS / KDE、Python 3.13.12、llama.cpp build 10819 上完成。沿用現有套件與模型，沒有下載依賴、修改其他模型服務或操作預留的 3000 連接埠。

## 改善與量測

- 32 個合成檔案、每 150 ms 檢查且連續兩次穩定：原本逐檔穩定等待的參考迴圈耗時 **9.617 秒**；新 inotify 批次檢查耗時 **0.302 秒**。這是送入 AI 前的檔案穩定檢查時間，不是 32 檔 AI 推論時間。
- 真實 Qt / inotify / 既有 Gemma E2B 合成圖片流程通過，產生 `geometric_shapes_blue_orange_green.png` 建議，原檔 SHA-256 不變，未自動改名；忽略建議及正常退出通過，owned 模型已停止。
- 當時其他本機模型已使用大部分 VRAM，固定 offload 99 層會讓 projector 載入因 CUDA OOM 中止。新設定使用自動 GPU 層數、單一 slot，空閒 VRAM 不足時使用 CPU projector；同一環境的圖片命名連同冷啟動／關閉約 **27.16 秒**。
- 測試程式待命 6 秒使用約 0.015 CPU 秒、只查一次歷史資料庫，沒有建立模型 runtime。桌面實際常駐另外量測 6 秒約 **0.5% 單核心 CPU、103 MiB RSS**。這些是短時間本機快照，不代表長時間耗用保證。

原始結果：[smoke-2026-10-16.json](smoke-2026-10-16.json)、[desktop-2026-10-16.json](desktop-2026-10-16.json)。

## KDE 登入啟動

已安裝此使用者的 `org.permind.MingtuAI.desktop`，固定目前 Python 3.13 與既有模型的絕對路徑。KDE / systemd XDG 產生器成功產生 `app-org.permind.MingtuAI@autostart.service`；由該 unit 啟動後為 `active/running`，系統匣 D-Bus Title 為「命圖 AI」、Status 為 `Active`。

實際程序的 inotify inode 已比對「圖片／螢幕截圖」與「下載」兩個設定資料夾。再次執行 `./run --start-minimized` 回覆「監視程式已在執行」，原程序 PID 不變。待命時沒有子程序。驗證沒有重新開機或登出，因此下次登入的最終時序仍需在下次登入時觀察；目前已直接執行同一個自動啟動 unit 通過。

## 回歸與邊界

完整回歸 **424 項通過**，另新增的登出 SIGTERM 清理測試單獨通過；[regression-2026-10-16.log](regression-2026-10-16.log) 保留完整回歸結果。測試涵蓋大量事件、慢速寫入不阻塞其他檔案、短 poll 保留候選、緩衝上限、重複啟動、鎖釋放、待命歷史查詢與操作後刷新。fish 語法、Python 編譯、desktop-file-validate、文件連結及 diff whitespace 檢查通過。

GPU projector 判斷採保守檔案大小估計，仍可能遇到其他程序在檢查後配置 VRAM 的競爭。沒有增加無上限重試、啟動時掃描舊檔或遞迴監控；既有檔案仍需使用者明確分析。登入啟動依賴 checkout、Python environment、模型與監控資料夾的路徑仍存在。
