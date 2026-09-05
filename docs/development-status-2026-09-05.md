# 2026-09-05 開發與驗收狀態

本紀錄是自動命名程式的驗收基準。命名流程與設計記錄見 `原生AI.md` 與 `開發方案V2.md`；以下內容依目前程式、測試、原生 Qt build 與指定 CachyOS / KDE host 的實測更新。

## 目前結論

這個切片已完成：Python 測試、原生 Qt build、KDE 原生 UI 操作、真實 E2B 模型到 Qt 按鈕的閉環，以及 host doctor 均已通過。專案仍是 V2 開發中版本，不是已驗證的一般使用者發行版。

## 藍圖對照

| 階段 | 目前狀態 |
|---|---|
| M0 模型可行性 | 真實 Gemma 4 E2B GGUF + mmproj 對 public synthetic 圖片的本地管線已通過，包含 owned child 結束、VRAM 回到基線與 `3001` 釋放；廣泛真實截圖、OCR、中英混合、專有名詞保留與檔名品質仍未完成驗收。 |
| M1 圖片 MVP | 監控、佇列、模型生命週期、通知、安全改名、復原與 Qt 待決建議操作已實作；指定 KDE host 已通過原生 C++ 的 accept、edit、ignore、undo、conflict、cancel 六條 callback 路徑。 |
| M2 產品外殼 | Qt 系統匣、主視窗、暫停／恢復、歷史表格與復原已實作；設定仍是唯讀摘要，歷史搜尋與篩選尚未實作。 |
| M3 格式支援 | 圖片、文字、PDF、Office、影片、archive 與 metadata adapters 已存在；不代表每種真實文件的理解與命名品質都已驗收。 |
| M4 交付 | 自動測試、原生 build、指定 host UI 與模型閉環已通過；授權決策、廣泛品質測試、長時間壓力與下列已知限制仍是發行前工作。 |

## 自動與 host 驗證

- `PYTHONPATH=src python -W error::ResourceWarning -m unittest discover -s tests -q`：**209 tests 通過**。
- `python scripts/build_native_ui.py`：原生 Qt 6 CPython extension build 通過。
- 指定 KDE host 的 `--doctor`：GPU、llama-server、模型、projector、通知 actions、`127.0.0.1:3001`、測試目錄權限與 ffmpeg 共 **8/8 通過**。
- 使用 deterministic synthetic fixture 與 SQLite 的真 C++ Qt 表格操作，accept、edit、ignore、undo、conflict、cancel 六條 callback 均通過；atomic rename 版原生 KDE 截圖位於 `/tmp/mingtu-20260905-validation/native-actions-atomic-kde.png` 與 `/tmp/mingtu-20260905-validation/native-actions-atomic-kde-history.png`。
- 使用真實 Gemma 4 E2B + mmproj 與 public synthetic image，模型 → 主視窗 → Qt buttons 的 atomic rename 閉環通過；截圖位於 `/tmp/mingtu-20260905-validation/native-actions-e2b-atomic-kde.png` 與 `/tmp/mingtu-20260905-validation/native-actions-e2b-atomic-kde-history.png`。上述截圖已檢查，沒有裁切問題。

這些 host 證據確認指定環境的功能閉環，不代表廣泛真實截圖的畫面品質、OCR、命名、搜尋性或長時間壓力已完成驗收。

## Public E2B benchmark

來源為 `/tmp/mingtu-20260905-validation/e2b-baseline.md` 與對應 JSON；測試為單一 public synthetic case，使用 Gemma 4 E2B UD Q4_K_XL、F16 mmproj 與 llama.cpp build 10509。

| 指標 | 實測值 |
|---|---:|
| Cases | 1/1 成功 |
| Adapter | 0.386 ms |
| Inference | 1,448.970 ms |
| Case total | 1,449.530 ms |
| Cold start | 2,846.233 ms |
| Shutdown | 364.591 ms |
| GPU memory | 41 MiB 基線 → 3,134 MiB 峰值 → 關閉後 41 MiB |
| GPU peak delta | 3,093 MiB |

佇列已清空、owned model child 已退出、`3001` 已釋放。這是開發 host 的單次 smoke measurement，不是效能保證或硬體需求承諾；該 case 沒有提供廣泛命名品質、OCR 或 term-retention 分數。

## 安全修補與回歸

- production `HistoryStore.rename_file()` 與 `undo_rename()` 已改為 ledger-first Linux `renameat2(RENAME_NOREPLACE)`；collision candidate 各自先寫 `PENDING`，`EEXIST` 後標記 `FAILED` 再試下一候選。缺少 symbol 或 `ENOSYS`／`EINVAL` 時 fail closed，不使用 hard-link / unlink fallback。
- `RenameRecoveryRequiredError` 用於無法唯一判斷路徑身分的情況；程式保留仍存在的 source／target，並標記 `requires_manual_recovery`，要求人工比對，不自動刪除或搬回。
- safety regression suite 已通過，涵蓋 source replacement、target replacement、ledger failure 與路徑消失／變更情況。舊 hard-link pending ledger 只作保守對帳，雙路徑歧義時標記 `requires_manual_recovery`，不自動 unlink。
- `HistoryStore.reconcile_pending_undo()` 支援重啟後的 pending undo 對帳；它只讀 filesystem 並更新 ledger，不搬移或刪除路徑：expected inode 在 original 時標記 `SUCCESS`，在 current 時標記 `FAILED`，兩邊皆非 expected 時維持 `PENDING` 並標記 `requires_manual_recovery`。
- 仍有明確邊界：pre-syscall identity check 與 atomic syscall 之間仍可能發生 source replacement；postcheck 會保留資料並要求人工復原。`renameat2` 不可用或安全失敗時不移動檔案。若 SQLite 在 initial ledger 完全無法寫入，可能沒有新的 ledger event；atomic move 尚未發生，原檔仍保留在原路徑，exception／通知回報 DB failure，不保證列出 target。
- 長 CJK basename 的 collision suffix 已修正，並加入長名稱回歸測試；預覽與實際接受結果現在使用相同的長度／碰撞規則。

## 尚未完成

- 廣泛真實截圖、OCR、中英混合、專有名詞保留、檔名搜尋性與整體畫面品質。
- 同 UID 惡意程序若能持續重寫父目錄或 source path，仍超出信任邊界；現行設計在 postcheck 證據歧義時保留檔案並要求人工復原。
- GUI preference editor、歷史搜尋／篩選與長時間壓力驗收。
- 任何雲端 provider、音訊原生 Gemma 輸入、遺留 Office 二進位格式完整解析與正式授權決策。

## 固定邊界

不下載套件或模型，不操作使用者的 `localhost:3000`，只管理命圖 AI 自己建立並持有 handle 的 `127.0.0.1:3001` model child；原始檔在使用者接受前不改名，所有安全操作都維持 no-clobber。
