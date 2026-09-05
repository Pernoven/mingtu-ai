# PowerShell Development Launcher for Windows

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Definition
$RepositoryDir = Resolve-Path "$ScriptDir\.."

$pythonCmd = Get-Command python -ErrorAction SilentlyContinue
if (-not $pythonCmd) {
    Write-Error "dev-run: 找不到 python；需要 Python 3.13。"
    exit 2
}

python -c "import sys; sys.exit(0 if sys.version_info[:2] == (3, 13) else 1)"
if ($LASTEXITCODE -ne 0) {
    Write-Warning "dev-run: 這個開發版建議使用 Python 3.13。"
}

python -c "import httpx, pydantic"
if ($LASTEXITCODE -ne 0) {
    Write-Error "dev-run: 當前 Python environment 缺少 httpx 或 pydantic。"
    exit 2
}

$env:PYTHONPATH = "$RepositoryDir\src;$env:PYTHONPATH"
Set-Location $RepositoryDir
python -m mingtu_ai @args
