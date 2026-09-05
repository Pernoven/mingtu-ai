
# 初始想法
不是每次**截圖預設的檔名都是日期**嗎  
這樣之後要找某張照片出來都很麻煩因為不會記得截圖的日期  
每次都要自己改檔名也超麻煩  
我可以做一個截圖→主動理解圖片→主動建議檔名→一鍵修改檔名

# 核心流程：

**新檔案出現 → 判斷檔案類型 → 理解內容 → 產生建議檔名 → 通知 → 一鍵套用**

例如截到這張：

> VS Code 裡 Python 出現 CUDA out of memory

原始檔：

`Screenshot_20260827_113421.png`

模型輸出：

`PyTorch_CUDA_記憶體不足錯誤.png`

然後 KDE 通知：

> 建議檔名：`PyTorch_CUDA_記憶體不足錯誤.png`  
> **[重新命名] [編輯] [忽略]**


## *第一版甚至不用碰 Spectacle 本身。

1. 用 `inotify` 監控 `~/Pictures/Screenshots/`
2. 發現新的 PNG
3. 把圖片交給 Vision Model
4. Prompt 要求產生 10～30 字的可搜尋檔名
5. 用 KDE Notification 顯示建議
6. 點「重新命名」就 `rename()`


# 流程

              File Created
                    │
          ┌─────────▼─────────┐
          │   File Observer   │
          └─────────┬─────────┘
                    │
              Detect MIME
                    │
       ┌────────────┼────────────┐
       ▼            ▼            ▼
     Image         PDF          Other
       │            │            │
    Vision      Extract text   Metadata
       └────────────┼────────────┘
                    ▼
             AI understands
                the file
                    │
                    ▼
             Filename proposal
                    │
                    ▼
              KDE Notification

       ✨ 重新命名為：
       電磁學_畢歐沙伐定律講義.pdf

       [接受] [修改] [忽略]
