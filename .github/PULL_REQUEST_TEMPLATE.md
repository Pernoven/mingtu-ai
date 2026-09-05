## 變更概述 (Summary)
簡要說明此 PR 解決的問題或新增的功能。

## 變更類型 (Type of Change)
- [ ] Bug 修復 (Non-breaking change which fixes an issue)
- [ ] 新功能 (Non-breaking change which adds functionality)
- [ ] 重大變更 (Fix or feature that would cause existing functionality to not work as expected)
- [ ] 文件更新 (Documentation update)

## 驗證步驟與測試結果 (Verification & Test Results)
說明如何驗證此變更：
- [ ] 執行 `PYTHONPATH=src python3 -W error::ResourceWarning -m unittest discover -s tests -v` 全數通過。
- [ ] 執行 `python3 -m compileall -q src tests` 無編譯錯誤。
- [ ] 無新增自動下載、雲端 fallback 或網路對外傳輸。
- [ ] 無硬編碼個人目錄、API key、token 或私人檔案路徑。
