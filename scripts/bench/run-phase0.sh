#!/bin/bash
# Phase 0 — baseline + warm-up.
#   1. snapshot idle metrics (Prometheus instant values) -> baseline.json
#   2. warm-up burst per pool (loadgen, CodeFlowBench prompts, 512-out)
set -uo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
REPO="$(cd "$HERE/../.." && pwd)"
BASE_URL="${BASE_URL:-http://<NODE_IP>:30400}"
case "$BASE_URL" in *"<NODE_IP>"*) echo "set BASE_URL, e.g. http://<node-ip>:30400 (any cluster node)" >&2; exit 1;; esac
OUT="${OUT:-$REPO/bench/reports/$(date +%F)}"
DATA="$REPO/bench/data"
mkdir -p "$OUT"

echo "=== P0a idle baseline ==="
python3 - "$OUT" <<'EOF'
import json, subprocess, sys, urllib.parse
out = sys.argv[1]
QS = {
  "router_queue": 'max(router_central_queue_length{namespace="sir-llm-platform"})',
  "router_admit_rps_5m": 'sum(rate(router_admission_requests_total{namespace="sir-llm-platform"}[5m]))',
  "vllm_running": 'sum(vllm:num_requests_running{namespace="sir-llm-platform"})',
  "vllm_kv_pct_max": 'max(max by(pod)(vllm:kv_cache_usage_perc{namespace="sir-llm-platform"}))*100',
  "npu_aicore_pct_max": 'max(npu_chip_info_utilization)',
  "npu_temp_max": 'max(npu_chip_info_temperature)',
  "hbm_ecc_dbe_max": 'max(npu_chip_info_hbm_ecc_double_bit_error_cnt)',
  "gw_rps_5m": 'sum(rate(litellm_proxy_total_requests_metric_total{namespace="sir-llm-platform"}[5m]))',
}
res = {}
for k, q in QS.items():
    enc = urllib.parse.quote(q)
    cmd = ["kubectl","-n","monitoring","exec",
           "prometheus-prometheus-kube-prometheus-prometheus-0","-c","prometheus","--",
           "wget","-qO-", f"http://localhost:9090/api/v1/query?query={enc}"]
    try:
        d = json.loads(subprocess.run(cmd, capture_output=True, text=True, timeout=60).stdout)
        r = d["data"]["result"]
        res[k] = float(r[0]["value"][1]) if r else None
    except Exception as e:
        res[k] = f"ERR:{e}"
    print(f"  {k} = {res[k]}")
json.dump(res, open(f"{out}/baseline.json","w"), indent=2)
EOF

echo "=== P0b warm-up (512-out, codeflow prompts, both pools) ==="
[ -f "$DATA/codeflow.jsonl" ] || { echo "missing codeflow pool"; exit 1; }
python3 "$HERE/loadgen.py" warmup --tag p0-warmup \
  --base-url "$BASE_URL" --models qwen3.8-27b qwen3.8-27b-262k \
  --output-tokens 512 --prompts-jsonl "$DATA/codeflow.jsonl" --out-dir "$OUT" \
  || echo "FAILED: warmup"
echo "phase 0 done"
