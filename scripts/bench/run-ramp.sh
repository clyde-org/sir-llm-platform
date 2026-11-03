#!/bin/bash
# Phase 2 — open-loop rate ramp (llm-la-icbc harness, CodeFlowBench prompts).
# Poisson arrivals at each rate for 180 s, streaming, both pools.
#   131k pool: 1 2 4 8 16 32 48 64 96 rps
#   262k pool: 1 4 16 32 rps  (smaller healthy pool)
set -uo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
ICBC_SRC="${ICBC_SRC:-/mnt/data/code/llm-la-icbc/src}"
CFGDIR="${CFGDIR:-/tmp/bench-cfg-$(date +%s)}"
DURATION="${DURATION:-180}"

RATES_131K=(1 2 4 8 16 32 48 64 96)
RATES_262K=(1 4 16 32)

run_rate() {
  local MODEL="$1" RATE="$2"
  local CFG
  CFG="$(bash "$HERE/make-ramp-configs.sh" "$CFGDIR" "$MODEL" "$RATE" | tail -1)"
  # patch duration if overridden
  [ "$DURATION" != "180" ] && sed -i "s/duration_s: 180.0/duration_s: ${DURATION}.0/" "$CFG"
  echo "=== P2 ramp $MODEL @ ${RATE} rps (${DURATION}s) ==="
  cd "$ICBC_SRC" && python3 main.py --config "$CFG" || echo "FAILED: $MODEL @ $RATE"
  cd - >/dev/null
}

for R in "${RATES_131K[@]}"; do run_rate qwen3.8-27b "$R"; done
for R in "${RATES_262K[@]}"; do run_rate qwen3.8-27b-262k "$R"; done
echo "phase 2 done"
