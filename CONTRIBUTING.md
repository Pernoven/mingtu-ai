# 貢獻指南

感謝你關心命圖 AI。本專案採用 [MIT License](LICENSE) 開源授權條款，歡迎社群提出 Issue 與 Pull Request。提交貢獻前請詳閱下方開發原則與提交流程。

## 開發原則

- 不上傳檔案內容，不加入默認雲端 fallback。
- 原檔在使用者接受前不改名；改名與復原永不覆寫。
- 模型只在有工作時啟動，且只能停止專案自己建立的 process handle。
- 本地 server 只能綁定 `127.0.0.1:3001`。不得使用或操作使用者的 `localhost:3000`。
- 日誌與 SQLite 不得儲存原始檔案內容。
- 新增 external tool 前，先以該工具官方文件核對介面與安全邊界。
- 不得在開發或測試腳本中自動下載套件、模型或啟用 user service。

## 快速驗證

需要 Python 3.13 以及已安裝的 `httpx>=0.28,<1`、`pydantic>=2.12,<3`。專案 script 不會幫你安裝。

### Linux / macOS
```bash
cd /path/to/命圖AI
export PYTHONPATH="$PWD/src"

python -W error::ResourceWarning -m unittest discover -s tests -v
python -m compileall -q src tests
python scripts/build_native_ui.py
```

### Windows (PowerShell)
```powershell
cd \path\to\命圖AI
$env:PYTHONPATH = "$PWD\src"

python -W error::ResourceWarning -m unittest discover -s tests -v
python -m compileall -q src tests
python scripts\build_native_ui.py
```

原生 build 使用當前 Python ABI 與已安裝的 `g++` / `pkg-config` / Qt 6 headers，不下載任何東西。如果本機未準備這些工具，仍可先跑純 Python 測試；請不要讓測試腳本自動安裝。

若你明確決定為這個 checkout 建立可重建的 Python 3.13 environment，請依 [README](README.md#python-313-environment) 由你本人執行會下載依賴的指令。

## 變更流程

1. 先以測試或最小 reproducer 說明要修的行為。
2. 保留 dirty/shared worktree 中不相關的使用者變更。
3. 對 filesystem、subprocess、D-Bus、model process 與不受信任檔案介面加上失敗測試。
4. 不用虛構的效能數字。效能變更需附硬體、模型、輸入集、測量方法與 raw result。
5. 若改變 CLI、格式支援、資料留存或 packaging，同步更新 README 與 `docs/`。

## Pull request checklist

- [ ] `unittest discover` 通過（Linux, Windows, macOS），且沒有 `ResourceWarning`。
- [ ] GitHub Actions CI 流水線測試全數通過。
- [ ] 沒有新增自動下載、自動啟用 service 或對外網路傳輸。
- [ ] 沒有 hard-coded 個人目錄、token、API key、email 或模型絕對路徑。
- [ ] 檔案改名、復原與 port 衝突的 no-clobber / no-kill 不變式仍成立。
- [ ] 新的對外介面有官方文件來源。
- [ ] 文件準確區分已實作、可選、待驗證與未支援。
- [ ] 若改原生 UI，已檢查 CPython ABI、Qt 主執行緒、headless 降級與正常離開。

## 安全問題

不要在公開 issue 貼出敏感檔案、本機路徑、通知內容或可利用細節。請依 [SECURITY.md](SECURITY.md) 使用私密管道。
