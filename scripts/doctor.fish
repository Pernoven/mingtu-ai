#!/usr/bin/env fish

# This wrapper only invokes the project's read-only diagnostics.
set -l script_dir (path resolve (dirname (status filename)))
exec "$script_dir/dev-run.fish" --doctor $argv
