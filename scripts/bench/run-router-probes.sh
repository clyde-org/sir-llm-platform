#!/bin/bash
# Phase 4 — router-specific probes.
#   4a KV/prefix: CodeFlowBench MULTI-TURN (subproblem turns grow context;
#       shared problem statement across a conversation) at 8 rps, 600 s.
#   4b len-aware fairness: 90% short (lmsys-512 @7.2 rps) + 10% long
#       (lmsys-8k @0.8 rps) in PARALLEL; check short-request p95 TTFT.
#   4c ctx-guard: existing functional test (synthetic over-context), fast 400s.
set -uo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
REPO="$(cd "$HERE/../.." && pwd)"
ICBC_SRC="${ICBC_SRC:-/mnt/data/code/llm-la-icbc/src}"
BASE_URL="${BASE_URL:-http://<NODE_IP>:30400}"
case "$BASE_URL" in *"<NODE_IP>"*) echo "set BASE_URL, e.g. http://<node-ip>:30400 (any cluster node)" >&2; exit 1;; esac
OUT="${OUT:-$REPO/bench/reports/$(date +%F)}"
DATA="$REPO/bench/data"
MODEL="${MODEL:-qwen3.8-27b}"
mkdir -p "$OUT"

echo "=== P4a codeflow multi-turn @ 8 rps (600s) ==="
cat > /tmp/bench-p4a.yaml <<EOF
backend: "litellm"
router_url: "$BASE_URL"
total_requests: 800
prompt_source: "codeflow"
hf_lmsys:
  dataset_name: "/mnt/data/datasets/codeflowbench"
  tokenizer_name: "/mnt/data/models/Qwen3-14B"
  min_input_tokens: 256
  multi_turn: true
  seed: 7
load_pattern:
  pattern: "det"
  rate_rps: 8
  duration_s: 600.0
  warmup_reqs: 5
generation:
  max_tokens: 1024
  temperature: 0.0
  length_mode: "legacy"
  ignore_eos: true
  think: false
output_log_mode: "summary"
print_trace: false
metrics:
  enabled: false
transport:
  mode: "sync"
  submit_path: "/submit"
  results_zmq: ""
  topic: "results"
  idle_timeout_s: 7200.0
  idle_zero_running_s: 30.0
litellm:
  base_url: "$BASE_URL"
  chat_path: "/v1/chat/completions"
  model: "$MODEL"
  api_key: "YOUR-API-KEY"
  timeout_s: 7200.0
  stream: true
experiments_root: "/mnt/data/experiments"
EOF
cd "$ICBC_SRC" && python3 main.py --config /tmp/bench-p4a.yaml || echo "FAILED: p4a"
cd - >/dev/null

echo "=== P4b fairness mix (7.2 rps short + 0.8 rps long, 600s, parallel) ==="
python3 "$HERE/loadgen.py" open --tag p4b-short-$MODEL --rate 7.2 --duration 600 \
  --base-url "$BASE_URL" --model "$MODEL" --output-tokens 512 \
  --prompts-jsonl "$DATA/lmsys-512.jsonl" --out-dir "$OUT" &
SHORT_PID=$!
python3 "$HERE/loadgen.py" open --tag p4b-long-$MODEL --rate 0.8 --duration 600 \
  --base-url "$BASE_URL" --model "$MODEL" --output-tokens 512 \
  --prompts-jsonl "$DATA/lmsys-8k.jsonl" --out-dir "$OUT" &
LONG_PID=$!
wait $SHORT_PID || echo "FAILED: p4b-short"
wait $LONG_PID || echo "FAILED: p4b-long"

echo "=== P4c ctx-guard functional test ==="
CTX_TEST_URL="$BASE_URL" python3 "$REPO/scripts/test-ctx-guard.py" || echo "ctx-guard test reported failures"

echo "phase 4 done"
