# Qwen3-8B LLM Deployment - Setup & Usage Documentation

## Overview

| Component | Version | Description |
|-----------|---------|-------------|
| Model | Qwen3.8-27B | Served as `qwen3-8b` |
| vLLM | v0.23.0rc1 | Inference engine on Ascend NPUs (TP=4) |
| LiteLLM | main-stable | API proxy with auth & pass-through |
| Router | kv-router | Request routing to vLLM pods |
| Redis | 7-alpine | Queue state |

## Architecture

```
Client → LiteLLM (NodePort 30400) → vLLM (8200) → 4 pods (TP=4, one per node)
              (bypasses the router; see "Routing" below)
kv-router (ClusterIP :8080) ⇄ kv-sidecar (per vLLM pod) ⇄ vLLM (8200, ZMQ KV-events :5557)
                              Redis (ClusterIP :6379) — queue state
```

Key points:

- **4 vLLM replicas**, each a single-node pod running TP=4 on 4 Ascend 910B NPUs
  (`nodeSelector llm-pool: qwen-38b-phase1`, `huawei.com/Ascend910: 4` per pod).
  A `kv-sidecar` container runs alongside each vLLM pod.
- **LiteLLM** (`litellm-proxy`, NodePort **30400**) points *directly* at the
  `vllm-qwen3-8b` Service and **bypasses the kv-router**. It is the only externally
  exposed port of the stack.
- **kv-router** (`router-service`, ClusterIP :8080 + ZMQ :5559) and **Redis**
  (ClusterIP :6379) are **internal only** — there are no NodePorts for them. Reach
  them from inside the cluster or via `kubectl port-forward`.
- The `vllm-qwen3-8b` Service uses **ClientIP session affinity** (timeout 10800s)
  so a given client is pinned to one vLLM pod, keeping prefix/KV-cache locality.
- A second Service, **`vllm-qwen3-8b-claude`** (ClusterIP), fronts the same pods
  for the Claude Code client (Anthropic `/v1/messages`). Unauthenticated — see
  [claude-code/README.md](claude-code/README.md).
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
curl -s -H "Authorization: Bearer sk-sirlab" \
  http://<NODE_IP>:30400/health

# Router health (after port-forward)
curl -s http://127.0.0.1:8080/health

# Direct vLLM health (from within cluster, or port-forwarded)
curl -s http://vllm-qwen3-8b:8200/health
```

### Chat Completions

```bash
# Via LiteLLM (recommended)
curl -s -X POST http://<NODE_IP>:30400/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer sk-sirlab" \
  -d '{
    "model": "qwen3-8b",
    "messages": [{"role": "user", "content": "Hello"}],
    "max_tokens": 100
  }'

# LiteLLM pass-through endpoint (same auth, routed straight to vLLM)
curl -s -X POST http://<NODE_IP>:30400/raw/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer sk-sirlab" \
  -d '{
    "model": "qwen3-8b",
    "messages": [{"role": "user", "content": "Hello"}],
    "max_tokens": 100
  }'
```

### Model Info

```bash
# List available models via LiteLLM
curl -s -H "Authorization: Bearer sk-sirlab" \
  http://<NODE_IP>:30400/v1/models

# Router backend status (after port-forward)
curl -s http://127.0.0.1:8080/health | jq '.backends'
```

### Embeddings

```bash
curl -s -X POST http://<NODE_IP>:30400/v1/embeddings \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer sk-sirlab" \
  -d '{
    "model": "qwen3-8b",
    "input": "Hello world"
  }'
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
# Scale vLLM (one pod per 4-NPU node)
kubectl scale deployment/vllm-qwen3-8b -n sir-llm-platform --replicas=4

# Scale router
kubectl scale deployment/router-service -n sir-llm-platform --replicas=2
```

---

## Configuration Notes

### Current Settings

| Setting | Value | Notes |
|---------|-------|-------|
| Model | Qwen3.8-27B | TP=4 per pod, 4 pods |
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
  (topic `kv@<pod>@qwen3-8b`) — consumed by the router/sidecars.
- A bash wrapper also serves a `vllm_threads{pod=...}` gauge on `:9101`
  (thread count of the vLLM process), scraped by the release's ServiceMonitor.

### Router Configuration

The router runs with:
- `MODEL_NAME=qwen3-8b`
- `KV_AWARE=false` (disabled due to missing sentencepiece/tiktoken)
- `LEN_AWARE=true`, `LEN_POLICY=short_first`
- `LABEL_SELECTOR=component=vllm`
- `VLLM_PORT=8200`
- `ROUTER_MODE=pull` (kv-sidecars pull from the router and post results back via
  `RESULT_TRANSPORT_MODE=submit_ack` / `/result_submit`)

### Why KV-Aware is Disabled

The kv-router image lacks `sentencepiece` or `tiktoken` Python packages needed for
tokenizer initialization. With `KV_AWARE=true`, the router attempts to load the
tokenizer and crashes.

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
-H "Authorization: Bearer sk-sirlab"
```

### vLLM Pods Not Ready

Check vLLM logs: `kubectl logs -n sir-llm-platform -l app=vllm-qwen3-8b --tail=50`

Check pod events: `kubectl describe pod <pod-name> -n sir-llm-platform`

vLLM model loading is slow; the startup probe allows ~6h
(`initialDelaySeconds: 60`, `periodSeconds: 30`, `failureThreshold: 720`).

---

## Helm Values File

Current deployment uses the chart defaults in `vllm-stack/values.yaml`.
Environment-specific overrides live in `vllm-stack/values/`:

- `qwen3-8b.yaml`
- `values-qwen38b.yaml`
- `values-presentation-dual-model-device-plugin.yaml` (presentation dual-model timeshare)

Key configurations in the defaults:
- `model.replicas: 4` - one vLLM pod per 4-NPU node
- `model.tensorParallelSize: 4` - TP=4
- `modelVolume.hostPath: /data/models`
- `model.modelSubPath: Qwen3.8-27B`
- `litellm.enabled: true` - LiteLLM proxy (NodePort 30400)
- `vllm.sessionAffinity: ClientIP` - per-client pod pinning
- `claudeService.enabled: true` - `vllm-qwen3-8b-claude` Service
