# Benchmark plan — sir-llm-platform

Goal: find the limits of the **router** and the rest of the stack
(LiteLLM gateway, vLLM engines, NPU, Redis/Postgres side effects) on a
running lab deployment, with all results visible in the existing Grafana
dashboard (`sir-llm-platform-vllm`, NodePort 30300).

Agreed scope (2026-10-06):
- **Both pools**: `qwen3.8-27b` (131k, 4 pods) and `qwen3.8-27b-262k`
  (262k, 4 pods — 1 pod was unhealthy at start; noted in report).
- **Rate cap**: ramp stops at 96 req/s or on abort criteria, whichever first.
- **Generator**: throwaway pod (`bench` namespace), in-cluster, targeting the
  gateway ClusterIP (`litellm-proxy.sir-llm-platform:4000`). One low-rate
  pass against the external NodePort measures the external-path delta.
- **No invasive tests** (no router kill, no 200k-prefill runs).
- **SLO used for "max rate at SLO" reporting**: p95 TTFT ≤ 2 s and
  error rate < 1 % (chat-shape ≤ 8k input).

## Datasets (real traffic, not synthetic)

Both from the llm-la-icbc benchmarking repo (`src/download_*.py`), already on
this box:

| Dataset | Path | Used for |
|---|---|---|
| **CodeFlowBench-2505** (5,258 competitive-programming problems; input p50 ~381 tok, max ~1.5k) | `/mnt/data/datasets/codeflowbench` | P2 rate ramp, P3 concurrency, P4a multi-turn KV probe, P6 gateway compare |
| **LMSYS-Chat-1M** (1M real conversations) | `/mnt/data/datasets/lmsys_chat_1m` | P1 latency grid, P4b len-aware fairness |

Prompt pools (`bench/data/*.jsonl`, built by `scripts/bench/build_pools.py`):
- `lmsys-512`: last user turn, 400–640 tok — single-message chat shape.
  (LMSYS last-user turns are hard-capped ~730 tok, so longer tiers use…)
- `lmsys-2k/8k/16k/32k`: **full conversations** as message history — the
  agent-style shape this platform actually serves (Claude Code / pi send the
  whole history). 16k/32k pools are thin (rare long conversations).
- `codeflow`: 500 problem descriptions (single-turn code tasks).

Loaders: the **llm-la-icbc harness** (`/mnt/data/code/llm-la-icbc/src/main.py`,
`backend: litellm` → our NodePort) drives P2/P4a with `prompt_source: codeflow`;
our `scripts/bench/loadgen.py` (open/closed loop, pool JSONL) drives P1/P3/P4b/P6.
Token counting with the local Qwen3-14B tokenizer (same family as served
Qwen3.8-27B). `ignore_eos: true` pins output to `max_tokens`.

## Phases

- **P0 baseline + warm-up** (15 min): idle metrics snapshot; 10-request warm-up
  burst per pool to settle graphs/caches.
- **P1 latency by shape** (~1.5–2 h): LMSYS tiers {512, 2k, 8k, 16k, 32k} ×
  output {128, 512, 1024}, sequential N=1, both pools, 3 reps (≤8k) / 1 rep
  (16k/32k). Client TTFT/TPOT vs `router_request_*` and `vllm:*_latency`
  histograms.
- **P2 open-loop rate ramp** (~40 min): Poisson arrivals, {1,2,4,8,16,32,48,64,96}
  req/s × 180 s, 1k/1k chat shape. 131k pool full ramp; 262k pool
  {1,4,16,32} (smaller healthy pool). Per step: client p50/p95/p99,
  central queue max, queue-wait p95, admission vs dispatch rate, per-pod
  running reqs, KV usage, preemptions, AICore util, router pod CPU/RSS.
  Find: max sustainable rate, queue knee, first-error rate.
- **P3 closed-loop concurrency sweep** (~20 min): N ∈ {4,8,16,32,64,128,256},
  1k/1k, 3+ requests per worker. Aggregate out-tok/s and TPOT growth;
  batching sweet spot.
- **P4 router-specific probes** (~25 min):
  a) KV/prefix: CodeFlowBench **multi-turn** (subproblem turns grow context,
     shared problem statement) at 8 rps → `router_affinity_hits`,
     `vllm:prefix_cache_hits/queries`, TTFT delta vs single-turn baseline;
  b) len-aware fairness: 90 % short (lmsys-512 @7.2 rps) + 10 % long
     (lmsys-8k @0.8 rps) in parallel → short-request p95 TTFT (does
     `short_first` protect the tail?);
  c) over-context burst: `scripts/test-ctx-guard.py` (synthetic, functional)
     → expect fast 400, zero NPU cost.
- **P5 soak** (optional, later): 1–2 h at ~75 % of P2 max rate → memory
  growth (router/Redis), KV eviction, thermals, Postgres write load.
- **P6 gateway path comparison** (~15 min): OpenAI vs Anthropic
  `/v1/messages` at 8 rps, streaming vs non-streaming;
  client E2E − router E2E = LiteLLM + network overhead.

## Tooling

- `scripts/bench/loadgen.py` — stdlib-only generator (open/closed/grid/warmup),
  per-request CSV (ttft/tpot/e2e/tokens/status), JSON summary, JSONL manifest.
- `scripts/bench/summary.py` — pulls the corresponding Prometheus windows per
  manifest entry (router/vLLM/NPU/gateway) and emits the combined markdown
  table in the report.
- `bench/reports/<date>/` — manifest.jsonl, per-phase CSVs, summaries, final
  report.

## Safety / abort

- Abort a phase on: > 5 % errors over the last 50 requests, HBM ECC
  double-bit error, NPU temp > 90 °C, or unrelated lab traffic detected.
- Generator in-flight cap 4096; per-request socket timeout 120 s.
- Read-only everywhere else; no config changes to running deployments.
- 96 rps × ~30 s E2E ≈ 3k concurrent streams at the extreme — that is
  intended (we are looking for the ceiling), but the ramp is stepwise.
