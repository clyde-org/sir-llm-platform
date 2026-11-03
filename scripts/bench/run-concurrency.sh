#!/bin/bash
# Phase 3 — closed-loop concurrency sweep (loadgen, CodeFlowBench prompts).
# Fixed concurrency N, each worker fires next request after prev completes.
# Measures batching sweet spot: out-tok/s and TPOT growth.
set -uo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
REPO="$(cd "$HERE/../.." && pwd)"
BASE_URL="${BASE_URL:-http://<NODE_IP>:30400}"
case "$BASE_URL" in *"<NODE_IP>"*) echo "set BASE_URL, e.g. http://<node-ip>:30400 (any cluster node)" >&2; exit 1;; esac
OUT="${OUT:-$REPO/bench/reports/$(date +%F)}"
DATA="$REPO/bench/data"
MODEL="${MODEL:-qwen3.8-27b}"
POOL="$DATA/codeflow.jsonl"
OUTTOK="${OUTTOK:-1024}"
mkdir -p "$OUT"
[ -f "$POOL" ] || { echo "missing $POOL (run build_pools.py)"; exit 1; }

for N in 4 8 16 32 64 128 256; do
  COUNT=$(( N * 3 )); [ "$COUNT" -lt 60 ] && COUNT=60
  TAG="p3-${MODEL}-c${N}-out${OUTTOK}"
  echo "=== $TAG ==="
  python3 "$HERE/loadgen.py" closed --tag "$TAG" --conc "$N" --count "$COUNT" \
    --base-url "$BASE_URL" --model "$MODEL" --output-tokens "$OUTTOK" \
    --prompts-jsonl "$POOL" --out-dir "$OUT" || echo "FAILED: $TAG"
done
echo "phase 3 done"
