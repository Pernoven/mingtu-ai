# 支援格式與限制

「接住所有新一般檔案」不等於「讀懂所有二進位格式」。無專用 adapter 的檔案只會提供 MIME、大小與原檔名 metadata；資訊不足時，應保留原名或報告低信心，而不假裝理解。

## 格式矩陣

| 類型 | 辨識 | 處理 | 限制 |
|---|---|---|---|
| 圖片 | AVIF、BMP、GIF、HEIC/HEIF、JPEG、PNG、TIFF、WebP 副檔名，部分 magic bytes | 原圖傳給本地 provider | 是否可解碼取決於 llama.cpp / model build；provider 單圖預設上限 20 MiB |
| PDF | `.pdf` 或 `%PDF-` | `pdftotext` 最多前 12 頁；`pdftoppm` 渲染第一頁 PNG | 掃描檔的文字取出可能為空；尚無獨立 OCR，但代表頁可供 vision model |
| 文字 / 程式碼 | TXT、MD、JSON、CSV、Python、C/C++、JS/TS、Rust、Go、HTML/CSS、YAML 等 | UTF-8-sig / UTF-16 BOM 受限預覽；JSON root type、CSV 前 100 rows | 最多讀 256 KiB，最多 16,000 字元；其他 encoding 以 replacement character 處理 |
| OOXML | DOCX、PPTX、XLSX | 只讀 ZIP 中特定 XML，擷取 paragraph / slide / cell 文字 | 不還原排版、圖片、formula 語意或 macro |
| ODF | ODT、ODS、ODP | 只讀 `content.xml` / `meta.xml` 中文字 | 不還原排版或嵌入物件 |
| 影片 | AVI、M4V、MKV、MOV、MP4、MPEG、WebM | ffprobe metadata；FFmpeg `thumbnail=60` 選一幀並縮至 1600x1600 以內 | 不分析完整 timeline、字幕或音軌；代表幀可能不具代表性 |
| 音訊 | AAC、FLAC、M4A、MP3、OGG、Opus、WAV、WMA | ffprobe codec / duration / tags metadata | 目前不將音訊 waveform 傳給 Gemma，不做 ASR |
| ZIP / TAR | `.zip`、`.tar`、`.tar.gz`、`.tgz`、`.tar.xz` 等 | 列出成員名、size、directory/link 標記 | 不解壓；最多 200 個成員，名稱最多 300 字元 |
| 未知二進位 | 其他 | metadata fallback | 不讀檔案語意；模型應保守回答 |

## 共通上限

`AdapterLimits` 預設值：

- 原始文字、pdftotext stdout、ffprobe stdout: 256 KiB，擷取過程即強制上限
- external parser stderr: 4 KiB，擷取過程即強制上限
- model 文字預覽: 16,000 字元
- PDF 文字頁數: 12
- archive 成員: 200；成員名: 300 字元
- Office XML: 最多 128 個 member，單個 2 MiB，總共 8 MiB
- ZIP / TAR parser 單次 index read: 2 MiB（進入配置前即拒絕更大要求）
- Office ZIP compression ratio: 200
- external command wall-clock timeout: 30 秒；超限只終止本程式建立的 child
- provider 圖片: 最多 4 張，單張 20 MiB
- provider request timeout: 120 秒

上限是資源與攻擊面緩解，不是品質承諾。

所有 adapter 直接讀取都由 `O_NOFOLLOW` 開啟的 regular-file descriptor 進行。文字與 provider 圖片讀取會只配置上限外加一個 overflow sentinel byte，不用 `read_bytes()` 無界配置；若讀取期間 size / mtime 改變則拒絕繼續。

## 事件過濾

預設不處理：

- 目錄與 symlink
- 點開頭隱藏檔
- editor lock / swap / backup
- `.part`、`.partial`、`.crdownload`、`.download`、`.tmp` 等未完成檔
- 已消失、無法 `lstat` 或非一般檔案

監控是每個指定目錄的一層，不自動遞迴子目錄。
