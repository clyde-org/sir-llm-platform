# Qwen3.8-27B LLM Deployment - Setup & Usage Documentation

## Overview

| Component | Version | Description |
|-----------|---------|-------------|
| Model | Qwen3.8-27B | Served as `qwen3.8-27b` |
| vLLM | v0.23.0rc1 | Inference engine on Ascend NPUs (TP=4) |
| LiteLLM | main-stable | API proxy with auth & pass-through |
| Router | kv-router | Request routing to vLLM pods |
| Redis | 7-alpine | Queue state |

## Architecture

```
                     /v1/chat/completions  (OpenAI)
Client ───────────────────┐
    └── LiteLLM (NodePort 30400) ──► kv-router (ClusterIP :8080) ──► vLLM pods x4
                                                                  (TP=4, two pods per 8-NPU node)
                     /v1/messages  (Anthropic)
    └── LiteLLM ──► pass-through ──► kv-router /v1/messages ──► vLLM pool per model name
                                                                  (131k / 262k pools)

kv-sidecar (per vLLM pod) ⇄ kv-router (pulls work, posts results)
                              Redis (ClusterIP :6379) — queue state
```

Key points:

- **4 vLLM replicas**, each a single-node pod running TP=4 on 4 of a node's 8
  Ascend 910B NPUs — two pods per node (`nodeSelector llm-pool: qwen-38b-phase1`,
  `huawei.com/Ascend910: 4` per pod). A `kv-sidecar` container runs alongside
  each vLLM pod.
- **LiteLLM** (`litellm-proxy`, NodePort **30400**) is the only externally exposed
  entry point. It serves **two paths**:
  - `/v1/chat/completions` (OpenAI) is routed through the **kv-router**
    (`litellm → router → vllm`), which dispatches to the vLLM pods.
    `litellm.apiBase = http://router-service:8080/v1`.
  - `/v1/messages` (Anthropic) is a **pass-through endpoint** to the kv-router's
    **model-aware** raw-forward endpoint (`litellm → router → vllm`). The
    request body's `model` field picks the serving pool (router env
    `ANTHROPIC_UPSTREAMS`, templated in `03-router.yaml`); vLLM speaks the
    Anthropic Messages API natively, so the request and (streaming) response are
    forwarded verbatim. A legacy static path `/262k/v1/messages` (→ 262k pool)
    is kept for clients that bake the pool into the URL.
- **kv-router** (`router-service`, ClusterIP :8080 + ZMQ :5559) and **Redis**
  (ClusterIP :6379) are **internal only** — no NodePorts. Reach them from inside
  the cluster or via `kubectl port-forward`.
- **llm-auth**: LiteLLM uses master key `sk-qwen38b-local` for both paths.
- Control plane (Redis, router, LiteLLM) is pinned to one node
  (`pin.nodeName: devserver-bms-2eff03de-0.novalocal`).
- Scrape targets for sir-llm-platform are declared by the release itself
  (`vllm-stack/templates/07-podmonitors.yaml`); monitoring-namespace monitors live
  in [monitoring/servicemonitors.yaml](monitoring/servicemonitors.yaml).

## Service Endpoints

| Service | Type | Port | Internal URL | Purpose |
|---------|------|------|--------------|---------|
| `litellm-proxy` | NodePort | **30400** | `http://litellm-proxy:4000` | API gateway (only external port) |
| `router-service` | ClusterIP | 8080 | `http://router-service:8080` | Request routing |
| `redis` | ClusterIP | 6379 | `redis:6379` | Queue state |
| `vllm-qwen3-8b` | ClusterIP | 8200 | `http://vllm-qwen3-8b:8200` | vLLM OpenAI API |
| `vllm-qwen3-8b-claude` | ClusterIP | 8200 | `http://vllm-qwen3-8b-claude:8200` | Claude Code /v1/messages |

Example node IP: `7.242.101.107` (`devserver-bms-2956fa59`).

---

## Accessing LiteLLM UI

LiteLLM includes a Swagger UI for API exploration and documentation.

**URL**: `http://<NODE_IP>:30400/`

This provides interactive API documentation via Swagger UI.

### Admin UI (Optional)

The LiteLLM Admin UI requires environment variables. To enable:

```bash
kubectl set env deployment/litellm-proxy -n sir-llm-platform \
  LITELLM_UI_USERNAME=admin \
  LITELLM_UI_PASSWORD=your_secure_password
```

Then access at: `http://<NODE_IP>:30400/ui`

---

## Curl Commands

The only externally reachable port is LiteLLM's NodePort 30400. To hit the router
or vLLM directly, port-forward first, e.g.:

```bash
kubectl -n sir-llm-platform port-forward svc/router-service 8080:8080 &
```

### Health Checks

```bash
# LiteLLM health (with auth)
curl -s -H "Authorization: Bearer sk-qwen38b-local" \
  http://<NODE_IP>:30400/health

# Router health (after port-forward)
kubectl -n sir-llm-platform port-forward svc/router-service 8080:8080 &
curl -s http://127.0.0.1:8080/health

# Direct vLLM health (from within cluster, or port-forwarded)
curl -s http://vllm-qwen3-8b:8200/health
```

### Chat Completions (OpenAI path — LiteLLM → kv-router → vLLM)

```bash
# Via LiteLLM (recommended)
curl -s -X POST http://<NODE_IP>:30400/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer sk-qwen38b-local" \
  -d '{
    "model": "qwen3.8-27b",
    "messages": [{"role": "user", "content": "Hello"}],
    "max_tokens": 100
  }'

# LiteLLM pass-through endpoint (same auth, routed straight to the router)
curl -s -X POST http://<NODE_IP>:30400/raw/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer sk-qwen38b-local" \
  -d '{
    "model": "qwen3.8-27b",
    "messages": [{"role": "user", "content": "Hello"}],
    "max_tokens": 100
  }'
```

### Anthropic Messages (Anthropic path — LiteLLM pass-through → kv-router → vLLM native /v1/messages)

The `model` field selects the pool: `qwen3.8-27b` / `qwen3.8-27b-131k` → 131k
pool, `qwen3.8-27b-262k` → 262k pool (phase-2 nodes).

```bash
curl -s -X POST http://<NODE_IP>:30400/v1/messages \
  -H "Content-Type: application/json" \
  -H "x-api-key: sk-qwen38b-local" \
  -H "anthropic-version: 2023-06-01" \
  -d '{
    "model": "qwen3.8-27b",
    "max_tokens": 100,
    "messages": [{"role": "user", "content": "Hello"}]
  }'
```

This is the path used by Claude Code and Anthropic-SDK clients
(`ANTHROPIC_BASE_URL=http://7.242.101.107:30400`, key `sk-qwen38b-local`). The
legacy `http://<NODE_IP>:30400/262k/v1/messages` static pass-through still
works but is deprecated in favour of model-name selection.

### Model Info

```bash
# List available models via LiteLLM
curl -s -H "Authorization: Bearer sk-qwen38b-local" \
  http://<NODE_IP>:30400/v1/models

# Router backend status (after port-forward)
curl -s http://127.0.0.1:8080/health | jq '.backends'
```

---

## Prometheus & Grafana Monitoring

A full Prometheus/Grafana stack (kube-prometheus-stack) is installed in the
`monitoring` namespace.

### Access URLs

| Service | URL | Credentials |
|---------|-----|-------------|
| **Prometheus** | http://<NODE_IP>:30900 | No auth required |
| **Grafana** | http://<NODE_IP>:30300 | admin / prometheus-admin |

### Available Metrics

The following metrics are scraped:

**vLLM Metrics** (scraped from port 8200 on each vLLM pod):
- `vllm:num_requests_running` - Requests currently running
- `vllm:num_requests_waiting` - Requests waiting in queue
- `vllm:kv_cache_usage_perc` - KV cache utilization
- `vllm:prompt_tokens_total` - Total prompt tokens processed
- `vllm:generation_tokens_total` - Total generated tokens
- `vllm:request_success_total` - Successful requests by finish reason
- `vllm:e2e_request_latency_seconds` - End-to-end request latency
- `vllm:time_to_first_token_seconds` - Time to first token
- `vllm:time_per_output_token_seconds` - Time per output token

**Router Metrics** (scraped from port 8080 on the router pod):
- `router_central_queue_length` - Global queue depth
- `router_admission_requests_total` - Requests admitted at router
- `router_dispatch_requests_total` - Requests dispatched to backends
- `router_request_ttft_seconds` - Router-measured TTFT
- `router_request_e2e_seconds` - Router-measured E2E latency
- `router_slo_predicted_miss_total` - Predicted SLO misses
- `router_affinity_hits_total` - Affinity routing hits

**NPU Metrics** (scraped from the `npu-exporter` DaemonSet, port 8082, 30s interval)

### Prometheus Queries

Example PromQL queries in Prometheus UI:

```promql
# vLLM request rate
rate(vllm:request_success_total[5m])

# KV cache usage
vllm:kv_cache_usage_perc

# Queue depth
router_central_queue_length

# Router dispatch rate
rate(router_dispatch_requests_total[5m])

# P95 E2E latency
histogram_quantile(0.95, rate(router_request_e2e_seconds_bucket[5m]))
```

### Grafana

Grafana is pre-configured with the Prometheus data source. To add dashboards:

1. Navigate to http://<NODE_IP>:30300
2. Login with `admin` / `prometheus-admin`
3. Go to Dashboards → Import to add custom dashboards

### Current Scrape Status

```bash
# Check Prometheus targets
curl -s http://<NODE_IP>:30900/api/v1/targets | jq '.data.activeTargets[] | select(.labels.namespace == "sir-llm-platform")'
```

---

## Mooncake KV-Cache Store

Mooncake runs a cluster-wide KV-cache pool (KVCache-centric disaggregation).
Enabled in this deployment via the user-supplied values
`mooncake.enabled: true` + `mooncake.attachToVllm: true` (chart defaults are
`false`).

- **mooncake-master** — 1-replica Deployment (pinned to the control-plane
  node) owning the object index and segment metadata. **Stateless, in-memory:
  restarting it discards all segment registrations and keys**; clients
  re-register on the new instance.
- **Embedded segments** — with `attachToVllm`, every vLLM TP rank registers a
  `global_segment_size` (8 GiB) slice of node DRAM with the pool via
  `AscendStoreConnector` (`kv_role: kv_both`). Pool size = 8 GiB × TP 4 ×
  4 pods = **128 GiB** across the two hosting nodes.
- **Transport: Ascend NPU protocol** (`"protocol": "ascend"` in the store
  config). This is *not* RDMA — it is the NPU-fabric transport that the
  vllm-ascend build's transfer engine is hard-initialized with. The generic
  `tcp` protocol does not work for the data path on this image (see
  Troubleshooting → "Store registered but every put fails").
- The vLLM pods receive
  `--kv-transfer-config '{"kv_connector": "AscendStoreConnector", "kv_role": "kv_both"}'`
  (connector name templated from `mooncake.kvConnector`),
  `MOONCAKE_CONFIG_PATH=/etc/mooncake/mooncake_store_config.json`, and the
  chart-generated `mooncake-store-config` ConfigMap mounted at `/etc/mooncake`.

Why: when a prefix block is evicted from a pod's local KV cache, its KV data
survives in the pool, so a later request sharing that prefix — even if routed
to a *different* pod — can load it from the store instead of re-running
prefill. Router pod affinity remains the fast path; the store is the
cross-pod / post-eviction fallback.

### Services

| Service | Port | Purpose |
|---------|------|---------|
| `mooncake-master` | 50051 (`rpc`) | gRPC control plane (mount/put/get/remove) |
| `mooncake-master` | 8080 (`metadata`) | HTTP metadata server (`/metadata?key=...`) for segment discovery |
| `mooncake-master` | 9003 (`metrics`) | Prometheus metrics (scraped by the `mooncake-metrics` ServiceMonitor; see [monitoring/README.md](monitoring/README.md)) |

### Health checks

```bash
# Via Prometheus (easiest from outside the cluster)
curl -s "http://<NODE_IP>:30900/api/v1/query?query=master_active_clients"
# expect 16 active clients once all 4 pods are up (4 pods x 4 TP ranks)

# Per-rank segments: 16 entries, one 8 GiB segment per TP rank
curl -s "http://<NODE_ID>:30900/api/v1/query?query=segment_total_capacity_bytes"

# Or directly, with a port-forward
kubectl -n sir-llm-platform port-forward svc/mooncake-master 9003:9003 &
curl -s --noproxy '*' http://127.0.0.1:9003/metrics | grep -E 'master_active_clients|master_total_capacity_bytes|segment_total_capacity_bytes'
```

Put-pipeline health (master metrics):

- `master_batch_put_start_requests_total` / `_end_` / `_revoke_` — healthy
  transfers have `end` tracking `start`; `revoke ≈ start` means transfers are
  failing.
- On the vLLM side, vLLM logs show `save_put_failed_keys` lines and an
  `External prefix cache hit rate:` gauge — 100% failed keys + 0.0% external
  hit rate = store registered but transfers failing.

### Troubleshooting

#### Crash loop at startup: `SocketHandShakePlugin: bind ... Address already in use` / `Initialize MooncakeDistributedStore failed`

**Root cause (verified 2026-09-22 from engine source + pod logs):** this is
what happens when `MC_LEGACY_RPC_PORT_BINDING=1` is combined with
auto-picked segment ports (the `MC_STORE_CLIENT_MIN_PORT`–`MAX_PORT` band).
In legacy/P2P mode the engine re-binds the registered segment port with a
*new* socket (`desc.sockfd = -1` → `SocketHandShakePlugin::startDaemon` does
a plain `bind()`), but `RealClient`'s auto-port path has already bound that
exact port with a live `AutoPortBinder` socket and holds it for the duration
of client creation — and the client's whole lifetime after. Same port, two
binders: every rank fails with EADDRINUSE, and all 20 retries fail the same
way (each retry picks a fresh port and re-creates the self-conflict):

```
transfer_engine_impl.cpp:182] Transfer Engine RPC using legacy/P2P, listening on 10.244.10.237:14015
transfer_metadata_plugin.cpp:713] SocketHandShakePlugin: bind (port 14015): Address already in use [98]
tcp_transport.cpp:541] TcpTransport: cannot start handshake daemon
client_service.cpp:527] Failed to install TCP transport
real_client.cpp:707] Failed to create client on port 13799, retry 20/20
worker.py:1011] RuntimeError: Initialize MooncakeDistributedStore failed.
→ Engine core initialization failed → pod CrashLoopBackOff
```

**Keep `mooncake.legacyRpcPortBinding: false`** (chart default; the chart
only sets the env when it is true). New-mapping mode reuses the binder's
live fd (no second bind), so it works with auto ports. In that mode the
engine's RPC listener ends up on a *different* port than the registered
segment port (published via `rpc_meta`), and the TCP data port is published
in the segment descriptor — writers dial the data port, not the registered
segment port, so nothing breaks. This is the mode the pods ran 22h+
without crash-looping in (helm revision 9) — but note those pods used the
generic `tcp` connector, whose *data path* was still failing (see next
entry); "no crash loop" ≠ "store working".

History: on 2026-09-21 we reproduced the registered-vs-listening port
mismatch in-pod (registered `10.244.10.234:13001` → engine listening on
`10.244.10.234:16875`) and concluded writers would ECONNREFUSED the
registered port. That conclusion was wrong (the data path uses
`tcp_data_port` from the segment descriptor — see
`tcp_transport.cpp`), and the "fix" it inspired (the legacy flag) is what
broke the revision-10 rollout. Do not re-enable the flag while segment
ports are auto-picked; legacy mode only works with explicitly
user-assigned ports (the path that skips the `AutoPortBinder`).

#### Store registered but every put fails (`master_batch_put_end ≈ 0`, 100% failed keys)

**Symptom:** segments register, `master_active_clients` is 16, the pod is
healthy — but the master shows
`master_batch_put_start_requests_total` growing with
`master_batch_put_end_requests_total = 0` and
`master_batch_put_revoke_requests_total ≈ start` (verified 2026-09-22:
31,995 starts / 0 ends / 31,864 revokes after ~27 h), and the vLLM logs show
`save_put_total_keys == save_put_failed_keys` with
`External prefix cache hit rate: 0.0%`.

**Root cause (verified 2026-09-22 from `vllm_ascend` source + a pilot pod):**
the vLLM pods were using the generic upstream `MooncakeStoreConnector` with
`"protocol": "tcp"`. On the vllm-ascend NPU image only the data path
(transfer of block bytes into/out of the mounted segment) works —
registration and metadata are fine, but every `BatchPut` transfer fails and
is revoked, so nothing ever lands in the pool and external lookups return
no bytes. The NPU-native path is `AscendStoreConnector`, whose
`MooncakeBackend` *requires* `"protocol": "ascend"`
(`NotImplementedError` for anything else) and reuses the build's shared
transfer engine, which is hard-initialized with the `ascend` protocol
(`mooncake_transfer_engine.py`: `initialize(hostname, "P2PHANDSHAKE",
"ascend", ...)`). The old connector is also deprecated upstream ("MoonCakeStoreConnector
will be removed in the future").

**Fix (applied 2026-09-22, helm revision 12):** `mooncake.kvConnector:
AscendStoreConnector` in `--kv-transfer-config` + `"protocol": "ascend"` in
`mooncake-store-config` (both now chart defaults — see
`vllm-stack/values.yaml`). Verification: a pilot pod on the ascend path
saved an 11.7k-token prefix (11 `BatchPutEnd` completions on the master,
zero failures), then all 4 pods were rolled onto it.

**Do not** "fix" this by switching back to `tcp` (that is the broken path)
or by enabling `MC_LEGACY_RPC_PORT_BINDING` (see above).

#### Stale segment registrations (secondary)

The master's metadata is in-memory with no TTL: when vLLM pods restart, old
registrations linger, and pod-IP reuse lets a stale entry shadow the live
process (the metadata server also rejects duplicate `rpc_meta` keys with 400).
If a master restart is ever needed, do it **first**, then the vLLM pods, so
fresh clients register into a clean master:

```bash
kubectl delete pod -l app=mooncake-master -n sir-llm-platform   # clears all registrations/keys
kubectl rollout restart deployment/vllm-qwen3-8b -n sir-llm-platform
```

After the rollout: 16 segments on the *current* pod IPs, and the segment
endpoints must actually accept connections.

### Rollback

```bash
# Detach from vLLM only (master stays up)
helm upgrade vllm ./vllm-stack -n sir-llm-platform --set mooncake.attachToVllm=false

# Remove everything
helm upgrade vllm ./vllm-stack -n sir-llm-platform --set mooncake.enabled=false
```

---

## Deployment Management

### Check Pod Status

```bash
kubectl get pods -n sir-llm-platform

# Watch pods
kubectl get pods -n sir-llm-platform -w
```

### Check Logs

```bash
# LiteLLM logs
kubectl logs -n sir-llm-platform -l app=litellm-proxy --tail=100

# Router logs
kubectl logs -n sir-llm-platform -l app=router-service --tail=100

# vLLM logs
kubectl logs -n sir-llm-platform -l app=vllm-qwen3-8b --tail=100
```

### Restart Components

```bash
# Restart LiteLLM
kubectl rollout restart deployment/litellm-proxy -n sir-llm-platform

# Restart Router
kubectl rollout restart deployment/router-service -n sir-llm-platform

# Restart vLLM
kubectl rollout restart deployment/vllm-qwen3-8b -n sir-llm-platform
```

### Scale vLLM Replicas

```bash
# Scale vLLM (two pods per 8-NPU node)
kubectl scale deployment/vllm-qwen3-8b -n sir-llm-platform --replicas=4

# Scale router
kubectl scale deployment/router-service -n sir-llm-platform --replicas=2
```

---

## Configuration Notes

### Current Settings

| Setting | Value | Notes |
|---------|-------|-------|
| Model | Qwen3.8-27B (served `qwen3.8-27b`) | TP=4 per pod, 4 pods |
| Max Model Len | 131072 | 128k context |
| Batch Size | 8 | `--max-num-seqs` per vLLM instance |
| Max batched tokens | 8192 | `--max-num-batched-tokens` |
| KV-Aware Routing | Disabled | Inline hash needs tokenizer deps |
| Database | In-memory | No persistence |

### vLLM Arguments (from the chart)

- `--enable-prefix-caching --enable-prompt-tokens-details --enable-force-include-usage`
- `--compilation-config '{"cudagraph_mode": "FULL_DECODE_ONLY"}'`
- `--enable-auto-tool-choice --tool-call-parser qwen3_coder --reasoning-parser qwen3`
- `--speculative-config '{"method": "mtp", "num_speculative_tokens": 3, "enforce_eager": true}'`
- `--kv-events-config` publishing ZMQ KV-cache events on `tcp://*:5557`
  (topic `kv@<pod>@qwen3.8-27b`) — consumed by the router/sidecars.
- A bash wrapper also serves a `vllm_threads{pod=...}` gauge on `:9101`
  (thread count of the vLLM process), scraped by the release's ServiceMonitor.

### Router Configuration

The router runs with:
- `MODEL_NAME=qwen3.8-27b`
- `KV_AWARE=false` (disabled due to missing sentencepiece/tiktoken)
- `LEN_AWARE=true`, `LEN_POLICY=short_first`
- `LABEL_SELECTOR=component=vllm`
- `VLLM_PORT=8200`
- `ROUTER_MODE=pull` (kv-sidecars pull from the router and post results back via
  `RESULT_TRANSPORT_MODE=submit_ack` / `/result_submit`)
- `CTX_GUARD=true`, `CTX_GUARD_MARGIN=4096`, `CTX_GUARD_MAX_MODEL_LEN=0`

### Context Guard (CTX_GUARD)

Rejects `/v1/chat/completions` requests that cannot fit the model's context window
with an immediate 400 using the vLLM error shape:

```
{"error":{"message":"prompt is too long: <N> tokens > <max_model_len> maximum", ...}}
```

Before this (router < d0eec27), an oversized request went through the sidecar
pipeline and came back as a **fake 200** (empty stream when `stream=true`), so
agent clients like pi never saw an overflow error and could not auto-compact
and continue.

- `<N>` = prompt tokens counted by the inline tokenizer (`KV_TOKENIZER_PATH`);
  `CTX_GUARD_MARGIN` is subtracted from the cap to cover chat-template and
  tool-schema tokens the flattened prompt undercounts.
- The cap is the `max_model_len` the model's endpoints declare in `/pull`
  (live per pool: 131072 for `qwen3.8-27b`, 262144 for `qwen3.8-27b-262k`);
  `CTX_GUARD_MAX_MODEL_LEN` pins it statically when non-zero.
- Fails open (no rejection) until the first endpoint declares a cap or if the
  tokenizer is unavailable.
- Rejections are logged as `[API] ctx_guard reject model=... max_tokens=...`.
- Pairs with the sidecar change in the same commit: vLLM errors that get past
  the guard (estimation miss) are propagated as `result["error"]` and surfaced
  as real error responses instead of error text masquerading as model output.

### Why KV-Aware is Disabled

`KV_AWARE` is off by choice for this single-model stack (`LEN_AWARE` short-first
is the active policy). Note: the Qwen tokenizer itself loads fine with the
bundled `transformers`+`tokenizers` (CTX_GUARD uses it); `sentencepiece`/
`tiktoken` would only be needed for other tokenizer families.

To enable KV-aware routing, you would need:
1. A kv-router image with tokenizer dependencies installed, OR
2. Use `KV_HASH_SOURCE=external` with a separate hash service

---

## Troubleshooting

### Router CrashLoopBackOff

Check logs: `kubectl logs -n sir-llm-platform -l app=router-service`

Common cause: Missing tokenizer for inline hash. Fix:
```bash
kubectl set env deployment/router-service -n sir-llm-platform KV_AWARE=false
```

### LiteLLM Returns 401

Ensure you're passing the auth header:
```bash
-H "Authorization: Bearer sk-qwen38b-local"
```

### vLLM Pods Not Ready

Check vLLM logs: `kubectl logs -n sir-llm-platform -l app=vllm-qwen3-8b --tail=50`

Check pod events: `kubectl describe pod <pod-name> -n sir-llm-platform`

vLLM model loading is slow; the startup probe allows ~6h
(`initialDelaySeconds: 60`, `periodSeconds: 30`, `failureThreshold: 720`).

### Claude Code: `API Error: 500 ... maximum context length is 131072`

Claude Code does not know this model name, so it assumes a 200k-token context
window and places its autocompact trigger (~178.8k) **above** the model's 128k
hard limit — long sessions 500 before compaction can fire. Fix: set
`CLAUDE_CODE_AUTO_COMPACT_WINDOW=131072` in the client settings (already in
`claude-code/settings.qwen3-8b.json`), which puts the trigger at ≈110k with
~13k headroom under the hard limit. Do **not** try to fix it by raising vLLM
`maxModelLen` (131072 is the model's native limit) or with
`CLAUDE_CODE_MAX_CONTEXT_TOKENS` (only read when `DISABLE_COMPACT` is set, which
disables autocompact). Full details:
[claude-code/README.md](claude-code/README.md).

---

## Helm Values File

The deployed `sir-llm-platform` release uses the chart defaults in
`vllm-stack/values.yaml` plus one user-supplied override (verify with
`helm get values vllm -n sir-llm-platform`):

```yaml
mooncake:
  enabled: true       # deploy mooncake-master + Service + store config
  attachToVllm: true  # attach MooncakeStoreConnector to the vLLM pods
```

Everything else is chart default. The `vllm-stack/values/` directory contains
stale files (`qwen3-8b.yaml`, `values-qwen38b.yaml`) from an older multi-model
chart schema — they are not used by this deployment.

Key configurations in the defaults:
- `model.servedName: qwen3.8-27b` - served / registered model name
- `model.replicas: 4` - two vLLM pods per 8-NPU node (2 nodes x 8 NPUs)
- `model.tensorParallelSize: 4` - TP=4
- `model.batchSize: 8` - `--max-num-seqs`; router FIXED_BATCH_ESTIMATE
- `model.maxModelLen: 131072` - 128k context
- `modelVolume.hostPath: /data/models` - pre-staged model hostPath
- `litellm.enabled: true` - LiteLLM proxy (NodePort 30400)
- `litellm.apiBase: http://router-service:8080/v1` - chat completions route
  through the kv-router
- `litellm.anthropicMessagesTarget: http://router-service:8080/v1/messages`
  - Anthropic `/v1/messages` pass-through target: the kv-router's model-aware
    raw-forward endpoint (pool picked from the body's `model` field)
- `claudeService.enabled: true` - `vllm-qwen3-8b-claude` Service
- `mooncake.enabled: false` / `mooncake.attachToVllm: false` - disabled by
  default in the chart; this deployment enables both via user-supplied values
  (see [Mooncake](#mooncake-kv-cache-store) below)
