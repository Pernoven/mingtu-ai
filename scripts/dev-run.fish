#!/usr/bin/env fish

# Development launcher only: it never installs dependencies, downloads a model,
# enables a service, or mutates the user's shell configuration.
set -l script_dir (path resolve (dirname (status filename)))
set -l repository_dir (path resolve "$script_dir/..")

if not command -q python
    echo "dev-run: 找不到 python；需要 Python 3.13。" >&2
    exit 2
end

python -c 'import sys; raise SystemExit(0 if sys.version_info[:2] == (3, 13) else 1)'
if test $status -ne 0
    echo "dev-run: 這個開發版需要 Python 3.13（不接受 system Python 3.14）。" >&2
    exit 2
end

python -c 'import httpx, pydantic'
if test $status -ne 0
    echo "dev-run: 當前 Python 3.13 environment 缺少 httpx 或 pydantic；本 script 不會下載。" >&2
    exit 2
end

set -l project_python_path "$repository_dir/src"
if set -q PYTHONPATH
    set project_python_path (string join : "$repository_dir/src" $PYTHONPATH)
end

builtin cd "$repository_dir"; or exit 2
exec env "PYTHONPATH=$project_python_path" python -m mingtu_ai $argv
