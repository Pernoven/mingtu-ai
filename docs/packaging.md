# Python 發行、systemd user service 與 KDE autostart 資產

`packaging/` 是審查用資產，不是 installer。Repository 內沒有任何 script 會自動 copy、enable、start 或修改 KDE 設定。

## Python 發行模型

發行物採用 `setuptools.build_meta` 的正式 PEP 517 native build：

- sdist 一定包含 `native/mingtu_native.cpp`、`_mingtu_build.py`、`setup.py`、測試與公開 packaging 資產，不包含開發機已有的 `.so`、cache 或 `benchmark/results/`。
- wheel 一定從 source 重新編譯 `mingtu_ai.ui._native_ui`；它是 CPython 3.13 與 Linux platform-specific wheel，不是 `py3-none-any` wheel。
- build 只透過系統 `pkg-config` 尋找 Qt >= 6.5 Widgets / D-Bus，不會下載、安裝或改寫系統套件。缺少 `pkg-config`、Python 3.13 headers、Qt development metadata 或 C++20 compiler 時，wheel build 會失敗並說明原因。
- `--headless` 是執行期不啟動 Qt 的選項，不是另一種純 Python 發行物。這避免同一 version 交付內容不同、難以重現的 wheel。

若環境已有 PyPA `build`，標準 release gate 是：

```fish
python -m build --no-isolation
```

`--no-isolation` 會明確禁用 frontend 建立並補齊雔立 build environment；當前 Python 必須已滿足 `pyproject.toml` 的 build requirements，否則直接失敗。

若沒有 `build` package，不需要為此自動下載。在已有 `setuptools`、`wheel` 與 `pip` 的 Python 3.13 environment 可用：

```fish
mkdir -p dist
python -c 'from setuptools.build_meta import build_sdist; build_sdist("dist")'
python -m pip wheel --no-index --no-deps --no-build-isolation --wheel-dir dist dist/mingtu_ai-0.1.0.tar.gz
```

Release 時應從新產生的 sdist build wheel，再在不含 source checkout 的暫存目錄安裝 wheel，執行 `import mingtu_ai.ui._native_ui`。Linux wheel 還需要使用發行目標的 ABI 審計流程；目前未將本機 wheel 宣稱為 manylinux 或通用 CachyOS wheel。

## 現有資產

- `packaging/systemd/mingtu-ai.service`：預期 `mingtu-ai` 已安裝到 service `PATH` 的 user unit。
- `packaging/systemd/environment.example`：不含真實路徑或 secret 的 environment 範例。
- `packaging/autostart/org.permind.MingtuAI.desktop`：依 XDG Autostart 規格建立，但預設 `Hidden=true`，因此複製後仍不會自動執行。

## 使用前決策

1. 先完成專案授權決策與發行 packaging。
2. 確認可用的 Python 3.13 runtime 與已安裝 console entry point。
3. 確認模型路徑、授權、磁碟空間與真實 llama.cpp 相容性。
4. 在 KDE Wayland session 中實測 D-Bus notification actions。
5. systemd user service 與 XDG autostart **擇一**，不要同時啟用造成兩個 watcher。
6. 用目標 Python 3.13 從 clean sdist 建立 platform wheel，不要從開發 checkout 複製 `.so`。`python scripts/build_native_ui.py` 只是原地開發與 smoke 用 helper。
7. 用真實 KDE session 驗證系統匣、主視窗、離開及 headless 降級。目前已有開發機 smoke，但尚不是通用發行驗收。

## systemd user service（人工流程）

下列指令會寫入使用者設定並啟用 service，只能在使用者審查 unit 並明確決定後手動執行。本專案不會代為執行。

```fish
# 會修改 user configuration：先審查，再由你本人執行
mkdir -p ~/.config/systemd/user ~/.config/mingtu-ai
cp packaging/systemd/mingtu-ai.service ~/.config/systemd/user/
cp packaging/systemd/environment.example ~/.config/mingtu-ai/environment

# 先編輯 environment，改成真實 GGUF 路徑
systemctl --user daemon-reload
systemctl --user enable --now mingtu-ai.service
```

檢查：

```fish
systemctl --user status mingtu-ai.service
journalctl --user -u mingtu-ai.service --since today
```

停用：

```fish
systemctl --user disable --now mingtu-ai.service
```

Unit 連結 `graphical-session.target`，並以 `EnvironmentFile=-%h/.config/mingtu-ai/environment` 讀取模型設定。它不含模型、不建立 Python environment，也不會對 port 占用者做清理。有桌面 session 及可用 extension 時預設啟動原生 UI；想固定為無視窗 service，可在自己的 unit copy 審查後將 `ExecStart` 加上 `--headless`。

## XDG / KDE autostart（人工流程）

Freedesktop [Autostart Specification](https://specifications.freedesktop.org/autostart/latest/) 規定 user entry 位於 `$XDG_CONFIG_HOME/autostart`，並規定 `Hidden=true` 必須被忽略。專案附的 entry 依此預設停用。

若使用者不使用 systemd unit，且明確選擇 autostart：

```fish
# 會修改 KDE user configuration：先審查，再由你本人執行
mkdir -p ~/.config/autostart
cp packaging/autostart/org.permind.MingtuAI.desktop ~/.config/autostart/

# 手動編輯複製檔：確認 Exec/TryExec 與 environment 後，才將 Hidden 改為 false
```

Autostart entry 本身不適合存放模型路徑或 secret。如果 executable 是開發 checkout 內的 script，應由未來 installer 正確處理 quoting 與絕對路徑；不要把某一台機器的路徑 commit 進資產。Entry 啟動標準 `mingtu-ai`，因此預設會嘗試原生系統匣，並在 extension / session 不可用時由 CLI 降級。

## 驗證資產

```fish
desktop-file-validate packaging/autostart/org.permind.MingtuAI.desktop
systemd-analyze --user verify packaging/systemd/mingtu-ai.service
fish -n scripts/dev-run.fish scripts/doctor.fish
```

在沒有安裝 `mingtu-ai` console entry point 的開發機上，`systemd-analyze verify` 可能對未找到 executable 提示 warning；在 sandbox 中也可能因 user-manager socket 不可用而無法完成 verify。語法、sandbox 能力與真實安裝後的 executable 檢查是不同門檻。

## 官方依據

- [PyPA Packaging Flow](https://packaging.python.org/en/latest/flow/)
- [PyPA Packaging binary extensions](https://packaging.python.org/en/latest/guides/packaging-binary-extensions/)
- [setuptools Building Extension Modules](https://setuptools.pypa.io/en/latest/userguide/ext_modules.html)
- [pip wheel](https://pip.pypa.io/en/stable/cli/pip_wheel/)
- [systemd Desktop Environment Integration](https://systemd.io/DESKTOP_ENVIRONMENTS/)
- [Desktop Application Autostart Specification](https://specifications.freedesktop.org/autostart/latest/)
- [Desktop Entry Specification](https://specifications.freedesktop.org/desktop-entry/latest-single/)
- [fish 官方文件](https://fishshell.com/docs/current/index.html)
- [Qt QSystemTrayIcon](https://doc.qt.io/qt-6/qsystemtrayicon.html)
