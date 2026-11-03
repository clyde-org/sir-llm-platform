# Benchmark tooling

Benchmarks the sir-llm-platform gateway stack
(LiteLLM :30400 → kv-router → vLLM on Ascend 910B). Full plan:
[docs/benchmark-plan.md](../../docs/benchmark-plan.md).

## Files

| File | Purpose |
|---|---|
| `build_pools.py` | Build prompt pools (`bench/data/*.jsonl`) from CodeFlowBench + LMSYS-Chat-1M |
| `loadgen.py` | Open/closed-loop load generator (stdlib only), pool JSONL or synthetic prompts, per-request CSV + JSON summary + manifest |
| `make-ramp-configs.sh` | Render llm-la-icbc harness configs (one per rate) from the template |
| `sir-llm-platform-bench-template.yaml` | Harness config template (CodeFlowBench, Poisson, streaming) |
| `run-phase0.sh` | Idle baseline snapshot + warm-up |
| `run-grid.sh` | Phase 1 — latency by shape (N=1, LMSYS tiers × outputs × both pools) |
| `run-ramp.sh` | Phase 2 — open-loop rate ramp (harness, CodeFlowBench) |
| `run-concurrency.sh` | Phase 3 — closed-loop concurrency sweep (loadgen, CodeFlowBench) |
| `run-router-probes.sh` | Phase 4 — KV multi-turn, len-aware fairness, ctx-guard |
| `run-gateway-compare.sh` | Phase 6 — OpenAI vs Anthropic path, streaming vs not |
| `run-all.sh [phases]` | Orchestrator (default: 0 1 2 3 4 6) |
| `summary.py` | Per-phase Prometheus pull (router/vLLM/NPU/gateway) → `summary.md` + `summary-prom.json` |

## Usage

```bash
# 1. one-time: build prompt pools (~5 min)
python3 scripts/bench/build_pools.py --out-dir bench/data

# 2. run everything (or: bash scripts/bench/run-all.sh 0 1 2)
bash scripts/bench/run-all.sh

# 3. results
python3 scripts/bench/summary.py --report-dir bench/reports/<date>
cat bench/reports/<date>/summary.md
```

- Harness runs (P2/P4a) write to `/mnt/data/experiments/<N>/`
  (`logs.json`, `run_summary.json`, `endpoint_tokens.json`).
- Loadgen runs write `<tag>.csv` / `<tag>.summary.json` + `manifest.jsonl`
  under the report dir.
- All traffic is tagged `User-Agent: bench/<phase-tag>` — find the windows
  in Grafana `sir-llm-platform-vllm` → "Client requests (LiteLLM gateway)",
  row by user-agent.
- Env overrides: `BASE_URL` (default `http://<NODE_IP>:30400`),
  `OUT` (report dir), `ICBC_SRC` (harness src, default
  `/mnt/data/code/llm-la-icbc/src`).

## Datasets

- CodeFlowBench-2505 → `/mnt/data/datasets/codeflowbench`
  (5,258 competitive-programming problems)
- LMSYS-Chat-1M → `/mnt/data/datasets/lmsys_chat_1m` (1M real conversations)
- Tokenizer for pool tiering: `/mnt/data/models/Qwen3-14B`
