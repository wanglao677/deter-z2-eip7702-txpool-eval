#!/usr/bin/env bash
set -euo pipefail

if [[ $# -lt 2 || $# -gt 3 ]]; then
  echo "usage: $0 baseline|attack http://127.0.0.1:RPC_PORT [seed01]" >&2
  exit 2
fi

MODE="$1"
RPC="$2"
SEED_NAME="${3:-seed01}"

if [[ "$MODE" != "baseline" && "$MODE" != "attack" ]]; then
  echo "mode must be baseline or attack" >&2
  exit 2
fi

ENCLAVE="${ENCLAVE:-besu-lighthouse-7702-lowcap10-phased}"
PYTHON_BIN="${PYTHON_BIN:-.venv-exp5/bin/python}"
PRICE_UNIT="${PRICE_UNIT:-1000000000}"
SETUP_PRICE="${SETUP_PRICE:-1}"
NORMAL_PRICE="${NORMAL_PRICE:-3}"
ATTACKER_PRICE="${ATTACKER_PRICE:-7}"
ATTACK_FIRST_PRICE="${ATTACK_FIRST_PRICE:-}"
NORMAL_RATE_PER_BLOCK="${NORMAL_RATE_PER_BLOCK:-8}"
ATTACK_RATE_PER_BLOCK="${ATTACK_RATE_PER_BLOCK:-20}"
TRIAL_ID="lowcap10_${SEED_NAME}_${MODE}"

COMMON_ARGS=(
  devtools/mpfuzz_style_exp5_pos/mp_exp5_7702_phased_pos.py
  --mode "$MODE"
  --trial-id "$TRIAL_ID"
  --seed "lowcap10-${SEED_NAME}"
  --workload-seed "lowcap10-${SEED_NAME}"
  --rpc "$RPC"
  --enclave "$ENCLAVE"
  --client besu
  --normal-senders 5
  --normal-rate-per-block "$NORMAL_RATE_PER_BLOCK"
  --warmup-blocks 1
  --saturation-blocks 1
  --attack-blocks 2
  --recovery-blocks 2
  --drain-blocks 3
  --normal-calldata-bytes 0
  --normal-gas 21000
  --normal-fund-eth 1
  --price-unit "$PRICE_UNIT"
  --setup-price "$SETUP_PRICE"
  --normal-price "$NORMAL_PRICE"
  --batch-size 20
  --setup-wait 120
  --receipt-scope included
  --final-receipt-timeout 30
  --fresh-block-timeout 120
  --txpool-classification content
)

if [[ "$MODE" == "attack" ]]; then
  COMMON_ARGS+=(
    --attack-senders 5
    --attack-rate-per-block "$ATTACK_RATE_PER_BLOCK"
    --attack-sender-activation new-per-block
    --attack-calldata-padding-bytes 8192
    --attack-first-calldata-padding-bytes 0
    --attack-gas 500000
    --attack-balance-eth 1
    --attacker-price "$ATTACKER_PRICE"
  )
  if [[ -n "$ATTACK_FIRST_PRICE" ]]; then
    COMMON_ARGS+=(
      --attack-first-price "$ATTACK_FIRST_PRICE"
    )
  fi
fi

PYTHONUNBUFFERED=1 "$PYTHON_BIN" "${COMMON_ARGS[@]}"
