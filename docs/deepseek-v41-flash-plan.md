# Plan: Add DeepSeek-V4.1-Flash-1017 to the cluster

**Status: IN PROGRESS (2026-10-26).** Phase 1 (262k → .107) **done & verified**;
DeepSeek weights downloading on node3/node4; dedicated A2 image being imported.
Investigated 2026-10-23. Sources: live `kubectl` state, `npu-smi` on every node
(via the `npu-ops` node agents), Hugging Face model repo, quay.io vllm-ascend
tags, vllm-ascend v0.30.0rc1 release notes, and the **official
DeepSeek-V4.1-Flash A2 tutorial** (shipped inside the v0.30.0rc2 image under
`docs/source/tutorials/models/DeepSeek-V4.1-Flash.md`).

## Target topology (current plan)

```
node3 ──┐
        ├── DeepSeek-V4.1-Flash-1017: ONE instance spanning BOTH nodes
node4 ──┘   (8 NPUs each; official A2 recipe run at DP2/TP8/EP16;
             head=node3 :8000 + :13399 DP-RPC, worker=node4 headless;
             510 GB weights staged on BOTH nodes)

node .107 (7.242.101.107, 910B3, 8 NPUs) — Qwen3.8-27B 262k-context pool
   2 pods, TP=4, nodeSelector llm-pool=qwen-38b-phase2b, maxSurge 2 / maxUnavailable 0
   (MOVED here from node3/node4 on 2026-10-26 — see §10 execution log)

2eff03de-0/1 (910B4) — Qwen3.8-27B 131k pool, 4 pods, unchanged
bench 910B4 nodes — untouched
```

**DeepSeek = two-node adaptation of the official A2 recipe.** The v0.30.0rc2
tutorial §5.3 retains a **four-server A2 colocated config: global DP4/TP8/EP32**
— one DP rank per server, TP8 inside each node, expert-parallel across all 32
ranks; node0 serves the API, the others run `--headless --data-parallel-start-rank N`
and join over `--data-parallel-address`. We have **2** free 910B3 nodes with
room for the 510 GB checkpoint (the two "tmp" 910B3 nodes have <200 GB free
disk), so we run the same recipe at **DP2/TP8/EP16**: it divides evenly (384
routed experts/16 = 24 per rank; 24 engram heads/8 = 3 per device) but is not
itself a Huawei-validated config. If DP2 refuses to load (e.g. engram/MTP
sharding), options are: (a) free disk on the tmp 910B3 nodes for the validated
4-node DP4 layout, or (b) stop and report. The cross-node traffic is DP
coordination + EP all-to-all over the 10 Gbps RoCE link — the same *kind* of
traffic as Huawei's own 4-node A2 layout, just halved. (Supersedes the earlier
"TP=16 primary / PP2×TP8 fallback" idea: the official A2 pattern keeps TP
intra-node.)

## 1. The model

`deepseek-ai/DeepSeek-V4.1-Flash-1017` (published 2026-10-17, MIT license):

| Property | Value |
|---|---|
| Architecture | `deepseek_v41` MoE, 40 layers, hidden 5120, vision-capable (image-text-to-text) |
| Experts | 384 routed (top-6) + 1 shared, `moe_intermediate_size` 2304 → ~10–30B active params/token |
| Total params | **763B** (204B FP8, 557B **FP4 experts** block-quant 32×32/ue8m0, ~2B BF16) |
| Weights on disk | **~510 GB** (475 GiB), 48 safetensors shards (~10.6 GB each) |
| Context | **1,048,576 (1M)** — YaRN 16× from 65,536; recommended `max_tokens ≥ 256K` |
| KV design | MLA + sliding-window (128) + compressed sparse attention, 2-level indexer — the paper's theme is *KV-cache compression*; vllm-ascend adds TurboQuant 4-bit latent KV for DSA models (A2/A3) |
| Extra machinery | Engram n-gram embedding layers (layers 1, 14; ~384M rows each), DSpark MTP (3 next-token-pred layers + 128-expert Markov head), Hyper-Connections |
| Prompt format | **No Jinja chat template.** Self-contained Python reference (`encoding/encoding.py`) or the maintained `deepseek-recipe` (Rust + Python) toolkit for multi-turn/tool/thinking/image encoding |
| Sampling recs | temperature 1.0, top_p 1.0 |

Compared to what we serve today: ~28× the Qwen3.8-27B in total params, but a
"Flash" MoE with small active set, and 1M context vs 262k — exactly the
"much larger but larger context window" trade the user expects.

## 2. Software support (the gating item — and it's good news)

- **Deployed today:** `quay.io/ascend/vllm-ascend:v0.23.0rc1` → vLLM 0.23.0.
  Its registry has `DeepseekV4ForCausalLM` only; **no `deepseek_v41`**
  (verified in the running pod, and on upstream vLLM `main`).
- **vllm-ascend v0.30.0rc2 (released 2026-10-13)** — one week before this
  model's 1017 update — is a model-focused release whose flagship four include
  **DeepSeek V4.1**. From the release notes:
  - Hardware scope table: *DeepSeek V4.1 — Atlas 800I A3, **Atlas 800I A2** —
    W8A8 serving; Model Runner V2 execution, torch.compile, P/D deployment.*
    **A2 = our 910B family (910B3/910B4). We are in scope.**
  - Engram fixes, DSpark fixes, W4A4C8 quant scheme documented for
    DSV4-Flash/Pro (matches our model's FP4 experts), TurboQuant 4-bit KV
    (A2/A3), P/D disaggregation + KV pool.
  - Deps: vLLM v0.30.0, **CANN 9.2.0**, torch 2.10, Python 3.12.
  - Known issue to avoid: open accuracy investigation for DSV4.1 + DSA-CP
    (#17729) → do not enable DSA-CP in the initial config.
  - Also: **Qwen3.8-27B is one of the four flagship models** in this release
    (non-contiguous KV, FLA NPU backend, 310P fixes) — so the upgrade benefits
    the Qwen pools too; the platform can converge on a single image version.
- **Image (confirmed): `quay.io/ascend/vllm-ascend:deepseek-v4.1-flash-openeuler`** —
  the dedicated A2 image for this model (tutorial: "this dedicated image
  includes the validated DeepSeek V4.1 Flash A2 image"); the `openeuler`
  variant matches node3/4 (openEuler 22.03). The plain `v0.30.0rc2` /
  `v0.30.0rc2-openeuler` tags are the general-purpose line; the model-specific
  tag is the one to run. Nodes have no registry egress → the image is pulled
  on the office machine, `docker save`d, and `crictl rimport`ed on both nodes
  (manifests use `imagePullPolicy: Never`).
- Caveat: v0.30.0rc2 predates the **1017** minor update (10-17). It is the
  same architecture/model_type, so it should load; the boot must confirm the
  config is accepted by the v0.30.0 loader.

**Conclusion: the blocker is not the model — the dedicated A2 image already
exists; the remaining work is staging (510 GB × 2 nodes), no-egress image
import, and gateway wiring.**

## 3. Cluster hardware inventory (verified via npu-smi)

All 16 nodes: 8× `huawei.com/Ascend910`, aarch64.

| Node | Chip | HBM/chip | NPU HBM | NPU free | /data (free) | Current occupants |
|---|---|---|---|---|---|---|
| 2956fa59 (control-plane) | 910B3 | 64 GB | 512 GiB | 8/8 | 21 TB (19 TB) | control plane (litellm/redis/pg/router) |
| 2eff03de-0 | **910B4** | 32 GB | 256 GiB | 4/8 | 7.1 TB (7.0 TB) | phase-1 vllm ×1, control-plane pods |
| 2eff03de-1 | **910B4** | 32 GB | 256 GiB | 4/8 | 7.1 TB (6.9 TB) | phase-1 vllm ×1 |
| 2eff03de-2 | **910B4** | 32 GB | 256 GiB | **8/8** | 7.1 TB (5.9 TB) | lmcache redis |
| 2eff03de-3 | **910B4** | 32 GB | 256 GiB | 0/8 | 1.0 TB (0.8 TB) | bench-vllm ×2 |
| 2eff03de-4 | **910B4** | 32 GB | 256 GiB | **8/8** | 7.1 TB (6.9 TB) | loki |
| 2eff03de-5 | **910B4** | 32 GB | 256 GiB | 0/8 | 7.1 TB (7.0 TB) | bench-vllm ×2 |
| 62245bb7 | **910B4** | 32 GB | 256 GiB | 0/8 | 503 GB (126 GB) | bench-vllm ×2 |
| 6c1da567 | **910B4** | 32 GB | 256 GiB | 8/8 | 503 GB (97 GB) | keda, monitoring |
| b11214d6-tmp0306 | 910B3 | 64 GB | 512 GiB | 8/8 | 503 GB (74 GB) | — |
| b6af27bc | **910B4** | 32 GB | 256 GiB | 0/8 | 503 GB (340 GB) | bench-vllm ×2 |
| cfe7f2f1-0 | **910B4** | 32 GB | 256 GiB | 0/8 | 7.1 TB (7.0 TB) | bench-vllm ×2 |
| cfe7f2f1-1 | **910B4** | 32 GB | 256 GiB | 0/8 | 7.1 TB (6.9 TB) | bench-vllm ×2 |
| d9a5373f-tmp0306 | 910B3 | 64 GB | 512 GiB | 8/8 | 503 GB (198 GB) | presentation redis |
| **node3** | **910B3** | **64 GB** | **512 GiB** | 0/8 (2× p2 pods) | → /home, 3.4 TB (**902 GB free**) | **262k pool ×2 pods** |
| **node4** | **910B3** | **64 GB** | **512 GiB** | 0/8 (2× p2 pods) | → /home, 3.4 TB (**508 GB free**) | **262k pool ×2 pods** |

Notes:
- On node3/node4 `/data/models` is a **symlink to `/home/data/models`** (the
  3.4 TB /home volume), not the 69 GB root disk.
- Live pools right now: phase-1 131k = 2 pods (spec=2; repo values say 4 —
  live state drifted, worth reconciling separately); phase-2 262k = 4 pods
  (2 on node3, 2 on node4); KEDA scaleout pool for p2 sits at 0.
- A phase-1 pod was mid-rolling-update during the investigation (one
  Terminating + one Pending with a hostPort conflict) — transient.

## 4. Does DeepSeek fit where?

Weights ≈ 475 GiB must be resident, plus CANN workspaces/activations (~50–100
GiB), plus the KV pool.

| Placement | HBM | Fits? |
|---|---|---|
| 1× 910B4 node (TP=8) | 256 GiB | **No** (2× too small) |
| 1× 910B3 node (TP=8) | 512 GiB | **No** — 475 GiB weights leave <37 GiB for KV+act |
| **2× 910B3 = node3+node4 (DP2/TP8/EP16)** | **1024 GiB** | **Yes** — per node: ~204 GiB replicated non-expert + ~140 GiB EP-sharded experts ≈ 350–400 GiB weights → ~100+ GiB KV left (a full-1M request is only ~0.9 GB with the compressed KV) |
| 4× 910B4 nodes | 1024 GiB | Technically yes, but 4 nodes, 32 GB chips, and worse cross-node traffic — wasteful |

**node3 + node4 (both 910B3, formerly occupied by the 262k Qwen pool) is the
right home** — matching the user's instinct, and it works *because* the 262k
pool moved off them. It is also the only pair of 910B3 nodes with all 8 NPUs
free-able at once.

Inter-node interconnect (measured): each node has one **active 10 Gbps RoCE**
port (`rocep189s0f0` → interface `enp189s0f0`; both nodes on 10.50.156.0/24);
a 40 Gbps port pair exists but is **Down** (cable/firmware — bringing it up
would materially help). The official A2 recipe is built for exactly this
fabric: cross-node DP/EP, intra-node TP8.
- **P/D disaggregation** (also supported) needs a *second* full replica
  (2 more 910B3 nodes) — not feasible today; keep as a future option.

## 5. Moving the 262k pool off node3/node4 — destination: **.107 (910B3)**

**Revised 2026-10-23 (user decision):** 262k pool → **single node
`devserver-bms-2956fa59.novalocal` (7.242.101.107, 910B3, 8×64 GB)**,
"one instance" instead of 4 pods on 2 nodes; DeepSeek → node3+node4; the
910B4 nodes are **left untouched** (bench workloads keep running).

### 5.1 Does 262k context survive? — Yes (measured), and how capacity compares

Per-request context is **262,144 in every option** — node count never changes
that. Node count only changes *concurrency* (how many full-262k requests can
run at once). Measured from live pods (`/metrics` cache_config_info,
block_size=1536; one 262k request = 171 blocks):

| Option | HBM total | KV blocks (measured/est.) | Concurrent full-262k requests |
|---|---|---|---|
| **Today**: 2×910B3, 4 pods TP=4 | 1024 GiB | 4 × 1555 = 6220 (9.55M tok) | **~36** |
| 2×910B4 (2eff03de-2/4), 4 pods TP=4 | 1024 GiB | 4 × 439 = 1756 (2.70M tok) | ~10 |
| 4×910B4, 8 pods TP=4 | 1024 GiB | 8 × 439 = 3512 (5.39M tok) | ~20 — but needs stopping bench pods on 2 more nodes |
| **.107, 2 pods TP=4 (recommended)** | 512 GiB | 2 × 1555 = 3110 (4.78M tok) | **~18** |
| .107, 1 pod TP=8 ("one instance") | 512 GiB | est. ~3500 (5.4M tok; weights/rank halved → more KV) | ~20 |

Key facts:
- **The .107 (910B3) plan delivers the same ~18–20 concurrency as 4×910B4**,
  with zero disruption to bench workloads, full 64 GB chips, and **no model
  download** — `Qwen3.8-27B` is already staged on .107's /data (21 TB, 19 TB
  free).
- .107 verified today: 8/8 NPUs free, 192 CPU / 1.5 TiB RAM, nothing else
  selects its `llm-pool=qwen-38b-exp-b3` label → relabel is safe.
- 2×TP=4 vs 1×TP=8: 2 pods of TP=4 reuse the **exact pod spec already running
  in production** (zero new variables) and a rolling restart keeps 50 %
  serving; 1×TP=8 gives a slightly bigger KV pool (~+15 %) but is an untested
  TP size for this model/image and is 100 % down for ~5–8 min on every
  restart. **Recommendation: 2×TP=4** ("one deployment, two replicas").
- Proof 262k works on 910B4 too (if ever needed): the live phase-1 pool on
  the 910B4 nodes has been running since this morning with
  `--max-model-len 262144` (live drift — see §8.9). A 910B4 TP=4 pod holds
  439 blocks ≈ 0.67M tokens ≥ 171 → a full 262k request fits.
- Observed 262k load: in-flight avg 1.4 / max 4 → the .107 pool keeps
  **4–5× headroom** over observed peak. `max_model_len` stays 262,144 →
  context guard, client caps and API contract unchanged. If 262k-heavy usage
  grows later: FP8 KV cache (~2×), a second .107-like node, or accept
  queueing.

### 5.2 .107 caveat — it is the Kubernetes control-plane node

.107 hosts etcd/apiserver/scheduler/controller-manager/coredns (plus clyde
and a legacy `llm-openai-gateway` pod in ns `vllm-ascend`, 122 d old —
confirm it is not serving traffic). The sir-llm gateway (litellm/router/
redis/postgres) is on 2eff03de-0, not .107. Consequences:
- .107 down → **existing pods keep serving**, but the cluster cannot
  schedule/restart/roll anything until it recovers (affects every pool's
  self-healing, not just 262k).
- A TP workload shares the node with etcd/apiserver: 192 CPU / 1.5 TiB is
  ample, but set sensible resource requests on the new pods so they can't
  starve the control plane.
- Acceptable for a lab; noted so the trade is conscious.

### 5.3 Storage for the DeepSeek weights (510 GB, needed on BOTH node3+node4)
- ✅ **Done (2026-10-23, user-directed):** removed
  `/home/models/GLM-5-w4a8-mtp-QuaRot` (382 GB) on **both** node3 and node4
  (verified no running pod references `/home/models`). Now: node3 ~1.28 TB
  free, node4 ~890 GB free → both fit the 510 GB staging with ~600 GB target.
- Staging path: nodes are on the 10.50.x subnet; if they have direct HF
  egress, `huggingface-cli download` on each node. Otherwise download once to
  a 7 TB node (e.g. 2eff03de-2) and rsync to node3/node4 (~510 GB each way;
  over 10 Gbps ≈ 70 min per copy). Run rate-limited/off-peak — the rollout
  must not dent serving latency.

## 6. Gateway & client integration work

1. **Chat template — largely moot for boot.** The dedicated image ships
   `--tokenizer-mode deepseek_v41 --reasoning-parser deepseek_v41
   --tool-call-parser deepseek_v41` — the model-specific encoding lives in the
   image. POC milestone still: spot-check multi-turn/tool/thinking outputs vs
   the `encoding/` fixtures.
2. **Model registration:** `deepseek-v4.1-flash` added to the
   **litellm-config** ConfigMap as a **direct upstream**
   (`api_base: http://dsv41:8000/v1`, the head's ClusterIP service),
   deliberately **bypassing the kv-router** — the router's backends are
   `component=vllm` pods with the kv-sidecar on :9000, and the official
   DeepSeek recipe has no sidecar/KV events. vLLM's own max-model-len
   rejection (real 400) covers context overflow. Anthropic `/v1/messages`
   path: second step — add the model to the router's `ANTHROPIC_UPSTREAMS`
   raw-forward map.
3. **Context guard:** not needed for this pool (no router pass-through);
   vLLM enforces 1,048,576 itself.
4. **Clients:** 1M context needs per-client compaction/limits updates (pi
   `contextWindow`, claude-code `CLAUDE_CODE_AUTO_COMPACT_WINDOW`); the 262k
   Qwen name stays untouched.
5. **Vision:** the model is image-text-to-text; POC scope is text, images
   later.
6. **Monitoring:** npu-exporter already runs on node3/node4; add the new
   pool to the Grafana rows. No KEDA (single 16-NPU instance).

## 7. Proposed rollout (sequencing matters)

**⚠ Coordination note (observed 2026-10-23 ~11:30–11:45):** the phase-1
deployment (`vllm-qwen3-8b`) was being actively modified during the
investigation (replicas 4→2→1, strategy 0/1, live max_model_len=262144).
Confirm with whoever is experimenting that their work is done/paused before
starting the rollout, so the two changes cannot interfere.

**⚠ Operator dependency:** the agent session driving this rollout
(defaultModel `qwen3.8-27b-262k`) runs **on the 262k pool being migrated**.
Phase 1 therefore uses the safety procedure in §7.1.

**Phase 0 — prep (no service impact)**
- ✅ Remove `/home/models/GLM-5-w4a8-mtp-QuaRot` (382 GB) on node3 **and**
  node4 (user-directed) → node3 ~1.28 TB / node4 ~890 GB free.
- ✅ 262k destination decided: .107 (910B3). Nothing to stage there —
  `Qwen3.8-27B` already on .107 /data (19 TB free).
- Confirm the active phase-1 experimentation is paused (§7 note).
- Download + stage 510 GB DeepSeek weights to node3 and node4 (rate-limited),
  checksum.
- Label .107 `llm-pool=qwen-38b-phase2b` (verified: nothing else selects its
  old `qwen-38b-exp-b3` label). Label node3/node4 `llm-pool=ds-v41-flash`
  (after p2 moves).
- Check/bring up the 40 Gbps RoCE ports on node3/node4 if possible.
- Pre-check: v0.30.0rc2 image vs node3/node4 host driver/firmware (npu-smi
  25.5.x; host CANN 8.5.0) — dry-run a probe pod with the new image on node3
  (`npu-smi` from container + `npu-exporter` health) before committing.
- Check .107's flapping npu-exporter (78 restarts in 2 d) — monitoring only,
  fix before adding a workload there.

**Phase 1 — 262k migration (Qwen keeps serving, ~30 min of double-load)**

§7.1 Safe-migration procedure (the operator is a live client of this pool):

1. **kubectl-only, no helm.** Stored helm values (release `vllm` rev 30) have
   drifted from live state (phase-1 stored replicas 4 / maxModelLen 131072 vs
   live 1 / 262144) — a `helm upgrade` would re-roll phase-1 unexpectedly.
2. Label .107 `llm-pool=qwen-38b-phase2b` (new value; verified safe — nothing
   selects the old `qwen-38b-exp-b3`). 910B4 nodes get NO labels (untouched).
3. `kubectl patch` p2 deployment strategy → `maxSurge: 2, maxUnavailable: 0`
   (live today is 25%/25% — would drop the pool). With surge-only the pool
   never drops below its current 4 pods (transiently 6).
4. `kubectl patch` p2 deployment `replicas: 4→2` + nodeSelector →
   `llm-pool=qwen-38b-phase2b` (keep TP=4) → rolling update: the 2 new pods
   can only land on .107 (8 free NPUs; model already local); the 4 old
   node3/4 pods terminate one-by-one **only after** replacements are Ready —
   262k is never 100 % down, and my own agent session (a live client of this
   pool) keeps working. Same image (v0.23.0rc1) — no image change combined
   with the move.
5. Canary after every step: PONG through the gateway with
   `model=qwen3.8-27b-262k` (the operator's own model) + p2 ready count.
   Stop-and-revert if ready < 1 of the new pair.
6. Verify the new pool on .107: ctxGuard cap 262144, long-prompt smoke, KV
   blocks (~1555/pod expected), both pods on .107.
7. **Rollback window:** old label stays on node3/4 until verification passes;
   rollback = re-point nodeSelector back to `qwen-38b-phase2` + replicas 4
   (old nodes still eligible). After verification: relabel node3/4 →
   `ds-v41-flash`, drop the old `qwen-38b-phase2` label.
8. Post-migration hygiene (separate, quiet-time step): reconcile the helm
   release's stored values with live reality (p2 → .107 label + 2 replicas +
   the phase-1 drift) after a `helm template` dry-run diff proves no other
   spec changes.

**Phase 2 — DeepSeek POC on node3+node4 (new image, isolated)** — IN PROGRESS
- Weights: 510 GB downloading on node3 **and** node4 directly from the Huawei
  internal HF mirror (`mirrors.tools.huawei.com/huggingface`, ~110 MB/s; no
  public egress on any node; `reg.local:32000` dead; 2-parallel resumable
  curl with per-file size verification + `.download-complete` marker;
  log `/tmp/dsv41-node-dl.log`).
- Image: `quay.io/ascend/vllm-ascend:deepseek-v4.1-flash-openeuler` pulled on
  the office machine (proxy), then `docker save` → HTTP serve → `crictl
  rimport` on both nodes (`imagePullPolicy: Never`).
- Manifests written: `dsv41/01-dsv41-service.yaml` (ClusterIP `dsv41` → head
  :8000), `dsv41/02-dsv41-head.yaml` (node3, hostNetwork, 8×Ascend910, 512 GiB
  tmpfs), `dsv41/03-dsv41-worker.yaml` (node4, headless) — official A2 recipe
  at DP2/TP8/EP16, 1M context, DSpark, `--quantization ascend`, `deepseek_v41`
  tokenizer/parsers, NIC `enp189s0f0` for HCCL/GLOO. Apply order: service →
  head → (worker after head's DP port 13399 listens).
- Validate: config accepted for the 1017 weights; boot + `/health`; PONG +
  long-context (up to 1M prefill) + output sanity vs the HF `inference/`
  example; tok/s over the 10 Gbps link; KV block count from `/metrics`.

**Phase 3 — gateway cutover**
- litellm-config: add `deepseek-v4.1-flash` → `http://dsv41:8000/v1` (direct
  upstream; one brief litellm restart — schedule between operator turns).
  Then router `ANTHROPIC_UPSTREAMS` for `/v1/messages` (second blip). Per-user
  key scoping + client snippets + README.
- Optional (later, after soak): converge the Qwen pools onto v0.30.0rc2 too
  (Qwen3.8-27B is a flagship of that release) so the fleet runs one image.

## 8. Risks & open questions

| # | Risk / question | Mitigation |
|---|---|---|
| 1 | v0.30.0rc2 (10-12) vs 1017 weights (10-17): same arch, unverified | POC loads + output cross-check before any gateway work |
| 2 | CANN 9.2.0 container vs node3/4 host driver/firmware (npu-smi 25.5.0/25.5.1, host CANN 8.5.0) | probe pod first; Huawei driver/CANN compat matrix |
| 3 | Only 10 Gbps RoCE between node3/node4 (40 G ports Down) | measure TP=16 decode latency; fallback PP2×TP8; bring up 40 G links |
| 4 | 262k pool concurrency drops ~36 → ~18–20 on .107 | 4–5× headroom over observed peak (max 4); FP8 KV / 2nd node later if needed |
| 5 | .107 is the **Kubernetes control-plane** node (etcd/apiserver) | .107 down = serving continues but cluster can't self-heal; set resource requests so vLLM can't starve etcd/apiserver; acceptable lab trade |
| 6 | Steady-state 262k = 2 replicas on one node: restart takes 50 % (2×TP=4) or 100 % (1×TP=8) for ~5–8 min | recommend 2×TP=4; rolling restarts; node is not under other NPU load |
| 7 | No Jinja chat template — gateway integration is real work | encoding.py fixtures as the test oracle; consider deepseek-recipe |
| 8 | MRV2 is opt-in and has known issues (DSA-CP accuracy #17729) | start on validated defaults; avoid DSA-CP |
| 9 | bench-vllm pods (12 pods across exp nodes) own most other 910B4 NPUs | **untouched by this plan** (910B4 nodes not relabeled/used); only relevant if P/D or a 2nd DeepSeek replica is wanted later |
| 10 | Repo values.yaml vs live state drift (phase-1 replicas/262k, p2 pool) — a blind helm upgrade re-rolls pools | rollout is kubectl-only; reconcile helm state in a separate quiet-time step with a dry-run diff first |
| 11 | Single-instance DeepSeek = no HA (16 NPUs gone on either node's failure) | acceptable for lab; P/D across 4 nodes is the future HA story |
| 12 | Legacy `llm-openai-gateway` pod (ns vllm-ascend, 122 d) on .107 | confirm it serves no live traffic; leave as-is |

## 9. One-line recommendation

**Move the 262k Qwen pool to the .107 node (910B3, 8×64 GB) as a single
deployment of 2×TP=4 pods (surge-only rollout → never 100 % down), then
serve DeepSeek-V4.1-Flash-1017 as a single instance spanning node3+node4
(2×910B3, 1024 GiB combined) using the dedicated
`deepseek-v4.1-flash-openeuler` A2 image at the official DP2/TP8/EP16 layout
— first proving image + 1017 weights + 1M-context serving in a POC. The
910B4 nodes stay exactly as they are (bench workloads untouched), and the
262k pool keeps ~18–20 concurrent full-length requests (4–5× the observed
peak).**

## 10. Execution log

| When | What |
|---|---|
| 2026-10-23 | Phase-0 cleanup: removed GLM-5-w4a8-mtp-QuaRot (382 GB) on node3+node4 (user-directed). |
| 2026-10-26 | Labeled .107 `llm-pool=qwen-38b-phase2b`; patched p2 deploy kubectl-only (replicas 4→2, maxSurge 2 / maxUnavailable 0, nodeSelector→phase2b). Rollback backup `/tmp/p2-deploy-backup-1828.json`. |
| | Surge-only rollout: 2 new pods on .107 (**1555 KV blocks each** — matches measured 910B3 TP=4), old node3/4 pods terminated one-by-one only after replacements Ready — pool never below 2 pods. Canary PONG green. 262k now 8/8 on .107; node3/node4 at 0/8. |
| | Relabeled node3+node4 `llm-pool=ds-v41-flash`. |
| | Egress mapped: no public internet from any node; Huawei internal HF mirror reachable (~25–110 MB/s); `reg.local:32000` dead; office machine has proxied internet (GitHub/quay) and is reachable from nodes. |
| | DeepSeek weights **COMPLETE on both nodes** (20:52 node3 / 20:56 node4, 2026-10-26): 510.31 GB, all 54 files exact-size-verified vs manifest, `.download-complete` markers. SHA256 verification running in background on both nodes (`/tmp/dsv41-sha256-{node3,node4}.txt`). |
| | Image staging: `quay.io/ascend/vllm-ascend:deepseek-v4.1-flash` (the tag the tutorial's A2 §5.3 references; 25.2 GB, built 2026-09-11, vllm_ascend dev5097 / vLLM 0.27.1, `deepseek_v41` support via vllm_ascend patches). Note: the local docker store on the office machine lost two earlier 22 GB pulls to an unknown janitor — distribution now goes through `crane pull` straight to `/mnt/data/dsv41-image.tar` → HTTP serve → `crictl rimport` on both nodes. |
| | Manifests written: `dsv41/01..03` (official A2 recipe at DP2/TP8/EP16); server-side dry-run validated. Verified: 8 NPU allocatable on node3/4, ports 8000/13399 free, `/etc/hccn.conf` on node3 (not mounted by device plugin — matches proven p2 pod config, add hostPath only if HCCL init complains). |
| | Image identity resolved: the tag the tutorial's A2 §5.3 references is `quay.io/ascend/vllm-ascend:deepseek-v4.1-flash` (base, 25.2 GB, built 2026-09-11, vllm_ascend dev5097 / vLLM 0.27.1, CANN 9.1.0 in-image; `deepseek_v41` via vllm_ascend patches). The `-openeuler` sibling is the same build, different base packaging — the tutorial never references it; base tag is production-proven on our openEuler hosts (v0.23.0rc1 base runs in prod). |
| | **Image ENTRYPOINT gotcha:** the image entrypoint sources the CANN/ATB `set_env.sh` chain then `exec "$@"`. The k8s manifests therefore keep the image entrypoint and pass `vllm serve ...` as `args` (a `command:` override would skip the CANN env). No jemalloc in image (tutorial's conditional is a no-op). |
| | **Import pipeline proven** (node containerd 2.2 rejects both crane-format tars and prefixed OCI tars — "unrecognized image format"): parallel blob fetch (6× proxy-routed curl, per-blob sha256) → OCI layout (oci-layout/index.json/blobs at tar root) → HTTP from office machine (7.191.128.118:8899) → `ctr -n k8s.io images import` on each node. Validated end-to-end with `quay.io/centos/centos:stream9` on node3. Scripts: `dsv41/build-oci-import.py` (fetcher: `/tmp/fetch-parallel.py`), `dsv41/boot.sh`, `dsv41/cutover-gateway.py`. |
| | Gateway cutover design (scripts ready, to run last): litellm-config CM entry (direct upstream `http://dsv41:8000/v1`) + manual `checksum/litellm-config` annotation bump (live annotation is static — CM edit alone would NOT roll litellm); router-service `ANTHROPIC_UPSTREAMS` (live value is empty `{  }` — drifted; add `"deepseek-v4.1-flash":"http://dsv41:8000/v1/messages"`); per-user keys: litellm virtual keys carry explicit `models` lists (10–11 keys) → `/key/update` each + `npu-ops-keys` applies.json. |
| | **Measured HBM reality (v0.27.1 image, DP2/TP8/EP16): the model does NOT fit on 2 nodes with Engram in HBM.** OOM at `NodeShardedEngram` allocation: 59.66/60.96 GiB already allocated per NPU, then a 22.89 GiB Engram table → crash. 2 nodes ≈ 81 GiB/NPU needed vs 61 available; the official 4-node DP4 layout lands at ≈60 GiB/NPU — that's exactly why Huawei's A2 recipe is 4 servers. |
| | **Decision: 2-node path = v0.30.0rc2 image + `--engram-config '{"cpu_offload":true,"dp_shared_memory":true}'`** (the A3 recipe's flag; Engram → 2.1 TiB host RAM, frees ~23 GiB/NPU HBM). The Sept-11 `deepseek-v4.1-flash` image (vLLM 0.27.1) lacks `--engram-config` (Engram is HBM-only there: `engram_hbm.py`) and rejects `--quantization ascend` (v0.30.0 W8A8 method) — it cannot run the 2-node layout at all. v0.30.0rc2-openeuler (CANN 9.2.0, built 10-13) is now being imported on both nodes; boot uses the full tutorial A2 recipe (DP4→DP2) + engram cpu_offload. |
| | **Fallback if cpu_offload misbehaves on A2:** the two "tmp" 910B3 nodes (b11214d6/d9a5373f) have 2–6.5 TB free model disks → the validated 4-node DP4/TP8/EP32 is feasible, but they sit on the routed 7.150.x TCP net (enp189 RoCE fabric DOWN there) — cross-fabric EP all-to-all = untested/slow; treat as last resort. |
| | Boot attempts 1–5 log: (1) `rtGetSocVersion failed` — image ships no host driver; fixed by mounting host driver paths (dcmi, npu-smi, driver/lib64, version.info, ascend_install.info, hisi_hdc, devmm_svm, hccn.conf) like the chart's p2 template; (2) literal `{{…}}` in JSON args (yaml bug); (3) `--quantization ascend` rejected by v0.27.1 (model config says fp8) — flag is v0.30.0-only; (4) `--engram-config` unrecognized by v0.27.1 — v0.30.0-only; (5) NPU OOM on Engram (the 2-node HBM wall, above). 16-rank HCCL group formation across node3/4 over the 10 Gbps RoCE **works** (world_size=16, ranks 8–15 on node4). |
| | NEXT: v0.30.0rc2 import on node3/4 → boot head (node3) + worker (node4) with full A2 recipe + engram cpu_offload → validate (health, PONG, long-context, KV blocks) → gateway cutover → report. |

---

## 11. Pivot: DeepSeek-V4-Flash (2026-10-27, user-directed)

After ~1 day of loader debugging on 1017 (blocked in the V4.1-specific
`ds_linear` 32×32 dense-scale expansion — `shape [5120,4,2] invalid for
46080`), the user approved switching to **DeepSeek-V4-Flash** (June release,
core `deepseek_v4` model path, CI-tested, official A2 single-node recipe).

### 11.1 FP4 path blocked (documented, weights kept)

Downloaded `deepseek-ai/DeepSeek-V4-Flash` (159.63 GB, 51 files,
SHA-verified sizes) from the Huawei HF mirror to node3
`/home/data/models/DeepSeek-V4-Flash` in ~20 min. Boot attempts on
v0.30.0rc2-openeuler and nightly-main-openeuler (CANN 9.2.0, vLLM 0.30/0.31)
both crash identically in
`w4a8_mxfp4.py:521 process_weights_after_loading`:

```
RuntimeError: aclnnNpuFormatCast not support recovery format.
```

Root-cause analysis (torch_npu 2.10.0.post7 `FormatCastKernelNpu.cpp`):
the 910B aclnn path downgrades FRACTAL_NZ(29)→ND(2) because
`allow_internal_format=False`, then for 4-bit (FP4 e2m1) inputs requires
CANN's `aclnnNpuFormatCastCalculateSizeAndFormat` to return a dstFormat in
`{ND, FRACTAL_NZ_C0_32}`; on this node's **driver 25.5.0 + CANN 9.2.0**
combination (the "32 padding size" CANN/HDK mismatch warning fires on every
allocation) it returns something else for the actually-loaded weight
tensors → hard error. Identical calls on freshly-allocated probe tensors
succeed (probe matrix: shapes, alloc paths, preallocation, 8-rank HCCL,
vllm import, exact format constant 29 — all OK), so the trigger is
subtle tensor-storage-descriptor state after the real load path. The
master-branch torch_npu source contains active 32-padding workarounds
(`ShouldFallbackNzToNd`, view/aclop fallbacks) — a **driver upgrade**
(matched to CANN 9.2.0) or a newer vllm-ascend image with the backport is
the real fix. Driver is not in the node dnf repos (manual Huawei install) →
needs Huawei channel. **160 GB FP4 weights kept on node3** (node4 gets none;
1017's 510 GB×2 kept on both) for when that lands.

Side findings: `--quantization deepseek_v4_fp8` is the correct flag for the
HF-raw FP4 checkpoint (auto-routing works via model_type too); the
sitecustomize patch from the 1017 work is NOT needed for V4-Flash;
`multistream_overlap_shared_expert`, `enable_cpu_binding`, and loader choice
(multithread vs default) are all innocent — same crash either way.

### 11.2 Chosen path: W8A8-MTP weights (official A2 recipe)

The tutorial's A2 table points single-node A2 at
**`Eco-Tech/DeepSeek-V4-Flash-w8a8-mtp`** (ModelScope, **300.10 GB**,
70×~4.3 GB shards + `quant_model_description.json`) with
`--quantization ascend` (ModelSlim W8A8: FP8 128×128 block experts — the
originally supported layout, no FP4 cast at all).

Transfer (node has NO public egress; ModelScope only reachable via office
CNTLM proxy at ~6 MB/s; office→node3 ssh ≈ 8.7 MB/s):
- office downloader `dl-loop.sh` (xargs -P 4, curl -C - resume, size verify)
  → `/mnt/data/w8a8-dl/DeepSeek-V4-Flash-w8a8-mtp/`
- office→node3 `rsync-loop.sh` (incremental, --partial) →
  `/home/data/models/DeepSeek-V4-Flash-w8a8-mtp/` (same dir as
  `/data/models/DeepSeek-V4-Flash-w8a8-mtp` — per-model dirs under
  `/data/models` are shared with `/home/data/models`, verified by inode +
  functional write-through test).
- ETA: ~14 h (download-bound; transfer pipelines in parallel).
- Started 2026-10-27 ~23:10 CST. Monitor:
  `wc -l /mnt/data/w8a8-dl/ok.log` (target 82/82).

Manifest ready: `dsv4f/03-dsv4f-w8a8-deploy.yaml` (single Deployment `dsv4f`
on node3, v0.30.0rc2-openeuler, exact tutorial A2 flags + `--quantization
ascend`, served-model-name `dsv4`, 133120 ctx, MTP spec-decode 1 token,
EP/TP8, int8 indexer KV, multistream shared expert, cpu_binding).

### 11.3 Hedges running in parallel

- **`nightly-releases-v0.30.0-openeuler`** (NEW quay line, rolling daily —
  today's build 10-27 12:29 UTC) being fetched to the office (8.6 GB).
  If it backports the master 32-padding FormatCast fixes, the FP4 path
  (V4-Flash 160 GB already on node3, and 1017) may unblock.
- Recurring quay tag check for a 1017-compatible image (latest scan: no
  v0.31.0/v0.30.0-final yet; the `nightly-releases-v0.30.0*` line is the
  most promising candidate).

### 11.4 Next steps

1. W8A8 download completes (ok.log 82/82) + rsync verify on node3 (sizes vs
   `/mnt/data/w8a8-dl/files.txt`).
2. `kubectl apply -f dsv4f/03-dsv4f-w8a8-deploy.yaml` → boot → /health 200.
3. Validate: /v1/models, canary PONG, long-context (e.g. 100k) generation,
   tool-call + reasoning parsers, KV block count.
4. Gateway cutover between operator turns (litellm `dsv4` → `http://dsv4f:8000/v1`
   + router ANTHROPIC_UPSTREAMS + per-user key model lists) — reuse
   `dsv41/cutover-gateway.py` with name/URL swapped.
5. Optional: 2nd instance on node4 (needs its own 300 GB copy); 1017 revival
   only if a compatible image lands (quay watch) or driver upgrade is obtained.

---

## 12. FP4-on-nightly attempt (2026-10-28) — abandoned, W8A8 is primary

### 12.1 `nightly-releases-v0.30.0-openeuler` (build 2026-10-26 09:37) has TWO vLLM-core regressions

Fetched (8.6 GB), imported on node3, booted. Deterministic crashes, both
reproduced with a clean manifest (no diag/sitecustomize):

1. **Circular import at vllm bootstrap**
   `vllm/__init__ → env_override:153 → utils/torch_utils:75 (PIN_MEMORY =
   is_pin_memory_available()) → utils/platform_utils:45
   (from vllm.platforms import current_platform)` →
   `ImportError: cannot import name 'current_platform' ... Did you mean
   '_current_platform'?`
   This build's `vllm/platforms/__init__.py` exposes `current_platform`
   lazily via module-level PEP 562 `__getattr__`; the bootstrap chain hits a
   partially-initialized module. Patched at container start
   (`dsv4f/fix-import.py`: make `is_pin_memory_available()` exception-safe;
   semantically fine on NPU) — boot got past it.

2. **torch/triton shim mismatch** (unpatched; blocked here)
   `torch/_dynamo/utils.py:2503  common_constant_types.add(triton.language.dtype)`
   → `AttributeError: module 'triton.language' has no attribute 'dtype'`
   The tutorial recipe needs `cudagraph_mode=FULL_DECODE_ONLY` (dynamo), so
   the stock recipe is unusable on this build without deeper surgery.

Also: this build has **no** `/usr/local/bin/vllm-ascend-entrypoint.sh`; its
image Entrypoint is the inline `bash -c "source ascend-toolkit set_env && ...
exec \"$@\""` wrapper (replicated in `dsv4f/boot.sh`).

Verdict: today's rolling build is too broken; not worth untested
compilation-mode surgery. Re-visit if a later rolling build is needed (keep
`dsv4f/boot.sh` + `fix-import.py` + manifest patch wiring as-is; FP4 manifest
is `01-dsv4f-deploy.yaml`, weights still on node3).

### 12.2 node3 disk pressure — 1017 weights removed from node3 only

node3 /home hit 86% (2.7T/3.4T) → kubelet disk-pressure taint + **image GC
wiped all vllm-ascend images**. node3 had no user pods running. To unblock:

- Deleted `/home/data/models/DeepSeek-V4.1-Flash-1017` **on node3 only**
  (510,310,560,386 B, 54 files).
- **node4 retains the verified byte-identical copy** (same byte size, same
  file-name md5 `af01315e...`, SHA256-verified in the earlier session).
  Re-download to node3 from the internal Huawei HF mirror takes ~2-3 h if a
  1017-compatible image ever lands.
- node3 /home now 73% used (~900G free); W8A8 landing (+55G) keeps it < 80%.
- NOTE: node4 /home is at 89% — a 2nd W8A8 instance on node4 will need its
  own cleanup there first.
- Office /mnt/data has the W8A8 full copy (300G) + all image tars/blobs as
  backup sources.

### 12.3 Current primary path (unchanged, ~5h to boot)

W8A8 `Eco-Tech/DeepSeek-V4-Flash-w8a8-mtp` (300.1 GB) office download 63/82
files (261G) with live rsync to node3 (`/data/models/DeepSeek-V4-Flash-w8a8-mtp`).
rc2 image (v0.30.0rc2-openeuler, GC'd) being re-transferred to node3 from
office (`/tmp/v030-oci.tar`, 9.0 GB) for `ctr -n k8s.io images import` before
applying `dsv4f/03-dsv4f-w8a8-deploy.yaml`.

## 13. W8A8 V4-Flash LIVE + gateway cutover (2026-10-29 ~19:20 CST)

### 13.1 Serving
- Pod `dsv4f-799bf68c64-nwdfm` on node3, image `v0.30.0rc2-openeuler`,
  W8A8-MTP weights, TP8/EP1/DP1, `--quantization ascend`, 133120 ctx,
  MTP spec-decode (1 token), int8 indexer KV. HBM ~62.2/65.5 GB per NPU.
- Validated: /v1/models (`dsv4`), canary PONG, 61.7k-token needle recall
  (TURQUOISE-9, finish stop), tool-calls (get_weather JSON args,
  finish_reason=tool_calls), reasoning in `reasoning` field.

### 13.2 Gateway (zero-downtime, both gateways surge-first rollouts)
- `litellm-config`: `dsv4` → direct `http://dsv4f:8000/v1` (bypasses pull
  queue; dsv4f has no kv-sidecar).
- `model-registry`: `dsv4` added (label_selector app=dsv4f) for router
  /v1/messages model resolution.
- router env `ANTHROPIC_UPSTREAMS={"dsv4": "http://dsv4f:8000"}`.
- Verified end-to-end: litellm OpenAI path PONG ✓, router /v1/messages
  Anthropic path PONG (stop_reason=end_turn) ✓.
- Model name for clients: **`dsv4`** (per-user litellm key model lists may
  need `dsv4` added on the user side).

### 13.3 Download-pipeline bugs fixed along the way (kept in dsv4f/../w8a8 scripts)
- dl-loop verify used paths without the model subdir (reset ok.log to 1/82
  every cycle); remaining_list compared $2 (size) vs path set (never
  converged); sz=0 `optional` entry created a stray file (ModelScope returns
  JSON error for directory paths) that blocked the `optional/` dir.
- boot-driver gate switched from ok.log line-count to byte-verify of all
  files (line-count inflated by duplicate OK appends).

### 13.4 Open items
- node4 /home at 89% — clean up before any 2nd W8A8 instance there.
- FP4 V4-Flash weights (160 GB) parked on node3 for a future
  driver-matched or fixed image; 1017 weights only on node4 now
  (510 GB, re-download ~2-3 h if a compatible image lands).
- Quay watch: nightly-releases-v0.30.0 line is promising but today's build
  had two vLLM-core regressions (current_platform circular import, triton
  language.dtype); re-check future builds.
