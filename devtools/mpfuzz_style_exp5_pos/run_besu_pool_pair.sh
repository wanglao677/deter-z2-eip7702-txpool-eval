#!/usr/bin/env bash
set -euo pipefail
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd -- "$SCRIPT_DIR/../.." && pwd)"
PYTHON_BIN="${PYTHON_BIN:-$REPO_ROOT/.venv-exp5/bin/python}"
export PYTHONUNBUFFERED=1
exec "$PYTHON_BIN" "$SCRIPT_DIR/besu_pool_pair.py" "$@"
