#!/bin/bash
# Generate llm-la-icbc harness configs for the sir-llm-platform benchmark.
# Usage:
#   make-ramp-configs.sh <outdir> <model> <rate1> [rate2 ...]
#   make-ramp-configs.sh /tmp/bench-cfg qwen3.8-27b 1 2 4 8 16 32 48 64 96
#
# Emits one config per rate: <outdir>/<model>-r<rate>.yaml, based on
# sir-llm-platform-bench-template.yaml in the same directory.
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
OUTDIR="${1:?outdir}"; MODEL="${2:?model}"; shift 2
RATES=("$@")
TPL="$HERE/sir-llm-platform-bench-template.yaml"
NODE_IP="${NODE_IP:?NODE_IP=<cluster node IP>, e.g. NODE_IP=10.0.0.7}"
mkdir -p "$OUTDIR"

for R in "${RATES[@]}"; do
  OUT="$OUTDIR/${MODEL}-r${R}.yaml"
  sed -e "s|__MODEL__|${MODEL}|g" \
      -e "s|__RATE__|${R}|g" \
      -e "s|__TAG__|ramp-${MODEL}-r${R}|g" \
      -e "s|__NODE_IP__|${NODE_IP}|g" \
      "$TPL" > "$OUT"
  echo "$OUT"
done
