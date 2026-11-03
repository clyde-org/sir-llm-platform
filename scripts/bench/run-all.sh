#!/bin/bash
# Top-level bench orchestrator. Runs phases in order; each is resumable
# (just re-run the individual script). ~4.5 h total.
#
#   bash scripts/bench/run-all.sh            # everything
#   bash scripts/bench/run-all.sh 2 3 6      # selected phases
set -uo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
SELECTION="${*:-0 1 2 3 4 6}"
has() { for s in $SELECTION; do [ "$s" = "$1" ] && return 0; done; return 1; }

cd "$HERE"
echo "selected: $SELECTION"
has 0 && bash run-phase0.sh
has 1 && bash run-grid.sh
has 2 && bash run-ramp.sh
has 3 && bash run-concurrency.sh
has 4 && bash run-router-probes.sh
has 6 && bash run-gateway-compare.sh
echo "all selected phases done"
