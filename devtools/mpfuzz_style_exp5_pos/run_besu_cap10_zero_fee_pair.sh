#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"

SEED="${SEED:-seed01}"
FIRST_GWEI="${FIRST_GWEI:-1}"
TAIL_GWEI="${TAIL_GWEI:-7}"

if [[ $# -gt 0 && "$1" != --* ]]; then
  SEED="$1"
  shift
fi

exec "$SCRIPT_DIR/run_besu_pool_pair.sh" pair \
  --profile cap10 \
  --seed "$SEED" \
  --first-gwei "$FIRST_GWEI" \
  --tail-gwei "$TAIL_GWEI" \
  "$@"
