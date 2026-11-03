#!/bin/bash
# Phase 6 — gateway path comparison (loadgen, CodeFlowBench prompts, 8 rps, 300 s).
#   6a OpenAI path, streaming
#   6b OpenAI path, non-streaming
#   6c Anthropic /v1/messages path, streaming
# client E2E - router E2E (from Prometheus) = LiteLLM + network overhead.
set -uo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
REPO="$(cd "$HERE/../.." && pwd)"
BASE_URL="${BASE_URL:-http://<NODE_IP>:30400}"
case "$BASE_URL" in *"<NODE_IP>"*) echo "set BASE_URL, e.g. http://<node-ip>:30400 (any cluster node)" >&2; exit 1;; esac
OUT="${OUT:-$REPO/bench/reports/$(date +%F)}"
DATA="$REPO/bench/data"
MODEL="${MODEL:-qwen3.8-27b}"
mkdir -p "$OUT"
[ -f "$DATA/codeflow.jsonl" ] || { echo "missing codeflow pool (run build_pools.py)"; exit 1; }

run() { # tag extra-args...
  local TAG="$1"; shift
  echo "=== $TAG ==="
  python3 "$HERE/loadgen.py" open --tag "$TAG" --rate 8 --duration 300 \
    --base-url "$BASE_URL" --model "$MODEL" --output-tokens 512 \
    --prompts-jsonl "$DATA/codeflow.jsonl" --out-dir "$OUT" "$@" \
    || echo "FAILED: $TAG"
}
run "p6-openai-stream-$MODEL"
run "p6-openai-nostream-$MODEL" --no-stream
run "p6-anthropic-stream-$MODEL" --anthropic
echo "phase 6 done"
