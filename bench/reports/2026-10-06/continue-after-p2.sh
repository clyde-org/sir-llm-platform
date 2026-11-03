#!/bin/bash
# Wait for the running Phase 2 (pid $1) to finish, then run P1 rerun (fixed
# thinking-token parser), P3, P4, P6, and the summary.
set -uo pipefail
cd /mnt/data/code/sir-llm-platform/scripts/bench
REPORT=/mnt/data/code/sir-llm-platform/bench/reports/2026-10-06
while kill -0 "$1" 2>/dev/null; do sleep 30; done
echo "P2 finished at $(date -u +%FT%TZ), starting P1 rerun"
bash run-grid.sh        > "$REPORT/phase1-rerun.log" 2>&1
echo "P1 rerun done, starting P3"
bash run-concurrency.sh > "$REPORT/phase3.log" 2>&1
echo "P3 done, starting P4"
bash run-router-probes.sh > "$REPORT/phase4.log" 2>&1
echo "P4 done, starting P6"
bash run-gateway-compare.sh > "$REPORT/phase6.log" 2>&1
echo "P6 done, generating summary"
python3 summary.py --report-dir "$REPORT" 2>&1 | tail -2
echo "ALL PHASES DONE $(date -u +%FT%TZ)"
