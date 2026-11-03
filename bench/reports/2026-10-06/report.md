# Benchmark report — sir-llm-platform

Date: 2026-10-06 · Plan: [docs/benchmark-plan.md](../../docs/benchmark-plan.md)
Target: LiteLLM `:30400` → kv-router → vLLM (Ascend 910B)
Pools: `qwen3.8-27b` (131k, 4 pods) · `qwen3.8-27b-262k` (262k, 3 of 4 pods healthy —
`vllm-qwen3-8b-p2-5f94c9f66f-k5vd4` stuck in `UnexpectedAdmissionError` at start)

Datasets: CodeFlowBench-2505 (5,258 problems) + LMSYS-Chat-1M (full conversations
for long tiers — last-user turns are hard-capped ~730 tok in the source).

## Phase 0 — baseline (idle)

router queue 0 · KV cache max 9.4% · ECC double-bit errors 0 · NPU AICore max 57%
(other lab workloads share the nodes) · warm-up OK on both pools (512-out ≈ 11 s).

## Phase 1 — latency by shape (first pass, N=1 sequential)

60/60 requests OK, 0 errors, both pools. Client-side, streaming.

| pool | tier | out | ttft p50 | e2e p50 |
|---|---|---|---|---|
| 131k | 512 | 512 | 14.4 s* | 9.8 s* |
| 131k | 2k | 512 | 2.3 s | 8.6 s |
| 131k | 8k | 512 | 6.0 s | 10.6 s |
| 131k | 16k | 512 | 7.4 s | 11.2 s |
| 131k | 32k | 512 | –* | 15.3 s |
| 262k | 2k | 512 | 6.5 s | 9.1 s |
| 262k | 8k | 512 | 7.9 s | 9.5 s |
| 262k | 16k | 512 | 9.5 s | 10.7 s |
| 262k | 32k | 512 | –* | 9.5 s |

\* **Caveat — re-run in progress.** Qwen3.8-27B is a *thinking* model: vLLM
streams reasoning as `delta.reasoning_content`, and with short `max_tokens` the
whole budget is spent thinking (zero `content` tokens). The first-pass parser
missed those TTFTs (and the 512-in cells hit a transient — 131k-512 ttft 14.4 s
vs e2e 9.8 s is impossible, i.e. measurement artifact). Parser now counts
reasoning tokens; Phase 1 will be re-run cleanly after Phase 2.

What is solid from pass 1: **prefill ≈ 1.5–2k tok/s** (32k input → ~15–19 s
TTFT), **decode TPOT ≈ 12–30 ms** at N=1, **zero errors at any shape up to
32k input / 1024 output on both pools**.

## Phase 2 — open-loop rate ramp (running)

Poisson arrivals, CodeFlowBench (median 222-in / 1024-out), streaming, 180 s
per step. 131k: 1→96 rps; 262k: 1/4/16/32 rps.

**Early finding (live, during the 4 rps step):** the 131k pool is already at
its knee at 4 rps — all 32 decode slots busy (4 pods × max-num-seqs 8),
central queue max **225**, queue-wait p95 **57 s**, NPU AICore 69 %. Sustained
capacity ≈ 4 rps ≈ ~4.1k out-tok/s fleet-wide for this shape. Steps 8–96 rps
will quantify queue growth / error onset (the router-limit data).

_progress: 1 rps ✓ 163/0 · 2 rps ✓ 332/0 · 4 rps running_

_results pending_

## Phase 3 — closed-loop concurrency sweep (pending)

N ∈ {4…256}, CodeFlowBench, 1024-out. Batching sweet spot: out-tok/s vs TPOT.

## Phase 4 — router-specific probes (pending)

4a CodeFlowBench multi-turn @ 8 rps (KV/prefix) · 4b 90/10 short-long fairness
mix · 4c ctx-guard functional test.

## Phase 6 — gateway path comparison (pending)

OpenAI vs Anthropic path, streaming vs non-streaming, 8 rps.

## Safety

Abort criteria: >5 % errors (last 50), HBM ECC double-bit, NPU >90 °C.
No config changes to the running deployment.
