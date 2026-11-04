#!/usr/bin/env bash
# Boot DeepSeek-V4.1-Flash across node3 (head) + node4 (worker).
# Precondition: image imported on both nodes (build-oci-import.py),
# weights on /data/models/DeepSeek-V4.1-Flash-1017 (verified).
#
# Usage: boot.sh [apply-head | wait-dp | apply-worker | status | health | logs-head | logs-worker | down]
set -uo pipefail
NS=sir-llm-platform
D=$(dirname "$0")
A3=npu-node-agent-b4l82   # node3
A4=npu-node-agent-hx8w8   # node4

apply-head() {
  kubectl apply -f "$D/01-dsv41-service.yaml"
  kubectl apply -f "$D/02-dsv41-head.yaml"
  echo "head deployment applied (node3)"
}

wait-dp() {
  # block until node3 has :13399 LISTEN (DP-RPC), max ~30 min
  for i in $(seq 1 90); do
    out=$(kubectl exec -n npu-ops $A3 -- chroot /host ss -ltn 2>/dev/null | grep -c ':13399 ' || true)
    if [ "${out:-0}" -ge 1 ]; then echo "DP port 13399 LISTENING on node3"; return 0; fi
    sleep 20
  done
  echo "TIMEOUT waiting for DP port"; return 1
}

apply-worker() {
  kubectl apply -f "$D/03-dsv41-worker.yaml"
  echo "worker deployment applied (node4)"
}

status() {
  kubectl get pods -n $NS -l app=dsv41 -o wide
  echo
  for pair in "node3:$A3" "node4:$A4"; do
    node=${pair%%:*}; agent=${pair##*:}
    echo "== $node NPU HBM usage (MiB) =="
    kubectl exec -n npu-ops $agent -- chroot /host sh -c 'npu-smi info 2>/dev/null | grep -A9 "HCCS" | grep -E "^\s*\|?\s*[0-7]\s" | head -8 || npu-smi info 2>/dev/null | grep -E "HBM|Memory" | head -10' 2>/dev/null | head -10
  done
}

health() {
  for ip in 10.50.156.65 10.50.156.106; do
    code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 10 "http://$ip:8000/health" || echo 000)
    echo "$ip:8000/health -> $code"
  done
  curl -s --max-time 10 http://10.50.156.65:8000/v1/models | head -c 400; echo
}

logs-head()    { kubectl logs -n $NS -l app=dsv41,role=head --tail=${1:-100} 2>&1 | tail -${1:-100}; }
logs-worker()  { kubectl logs -n $NS -l app=dsv41,role=worker --tail=${1:-100} 2>&1 | tail -${1:-100}; }

down() {
  kubectl delete -f "$D/03-dsv41-worker.yaml" --ignore-not-found
  kubectl delete -f "$D/02-dsv41-head.yaml" --ignore-not-found
  kubectl delete -f "$D/01-dsv41-service.yaml" --ignore-not-found
}

case "${1:-status}" in
  apply-head) apply-head ;;
  wait-dp) wait-dp ;;
  apply-worker) apply-worker ;;
  status) status ;;
  health) health ;;
  logs-head) logs-head "${2:-100}" ;;
  logs-worker) logs-worker "${2:-100}" ;;
  down) down ;;
  *) echo "usage: $0 [apply-head|wait-dp|apply-worker|status|health|logs-head|logs-worker|down]"; exit 1 ;;
esac
