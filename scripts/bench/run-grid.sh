#!/bin/bash
# Phase 1 — latency by shape.
# N=1 sequential (closed loop, conc=1), streaming, real dataset prompts.
# Grid: LMSYS input tiers x output lengths x both pools.
#   3 reps for in<=8k tiers, 1 rep for 16k/32k tiers.
set -uo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
REPO="$(cd "$HERE/../.." && pwd)"
BASE_URL="${BASE_URL:-http://<NODE_IP>:30400}"
case "$BASE_URL" in *"<NODE_IP>"*) echo "set BASE_URL, e.g. http://<node-ip>:30400 (any cluster node)" >&2; exit 1;; esac
OUT="${OUT:-$REPO/bench/reports/$(date +%F)}"
DATA="$REPO/bench/data"
mkdir -p "$OUT"

POOLS=(lmsys-512 lmsys-2k lmsys-8k lmsys-16k lmsys-32k)
for MODEL in qwen3.8-27b qwen3.8-27b-262k; do
  for POOL in "${POOLS[@]}"; do
    P="$DATA/$POOL.jsonl"
    [ -f "$P" ] || { echo "skip: missing $P"; continue; }
    case "$POOL" in *16k|*32k) REPS=1 ;; *) REPS=3 ;; esac
    for OUTTOK in 128 512 1024; do
      TAG="p1-${MODEL}-${POOL}-out${OUTTOK}"
      echo "=== $TAG ==="
      python3 "$HERE/loadgen.py" closed --tag "$TAG" --conc 1 --count "$REPS" \
        --base-url "$BASE_URL" --model "$MODEL" --output-tokens "$OUTTOK" \
        --prompts-jsonl "$P" --out-dir "$OUT" || echo "FAILED: $TAG"
    done
  done
done
echo "phase 1 done"
