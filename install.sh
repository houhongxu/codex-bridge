#!/bin/sh
set -eu
project_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
python_bin=${CB_PYTHON:-python3}
"$python_bin" -c 'import sys; sys.exit("Codex Bridge requires macOS and Python 3.9+ (3.11+ recommended).") if sys.platform != "darwin" or sys.version_info < (3, 9) else None'
exec "$python_bin" "$project_dir/cb.py" install "$@"
