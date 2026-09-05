@echo off
setlocal

set "SCRIPT_DIR=%~dp0"
set "REPO_DIR=%SCRIPT_DIR%.."

where python >nul 2>nul
if %ERRORLEVEL% neq 0 (
    echo dev-run: 找不到 python；需要 Python 3.13。
    exit /b 2
)

python -c "import httpx, pydantic" >nul 2>nul
if %ERRORLEVEL% neq 0 (
    echo dev-run: 當前 Python environment 缺少 httpx 或 pydantic。
    exit /b 2
)

set "PYTHONPATH=%REPO_DIR%\src;%PYTHONPATH%"
cd /d "%REPO_DIR%"
python -m mingtu_ai %*
