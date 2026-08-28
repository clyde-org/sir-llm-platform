# Qwen3-8B LLM Deployment - Setup & Usage Documentation

## Overview

| Component | Version | Description |
|-----------|---------|-------------|
| Model | Qwen3.8-27B | Served as `qwen3-8b` |
| vLLM | v0.23.0rc1 | Inference engine on Ascend NPUs |
| LiteLLM | 1.98.0 | API proxy with auth & routing |
| Router | kv-router | Request routing to vLLM pods |
| Redis | 7-alpine | Queue/backend discovery |

## Architecture

```
Client → LiteLLM (30400) → Router (30080) → vLLM (8200) → 2 Pods
                                     ↓
                                  Redis
```

## Service Endpoints

| Service | NodePort | Internal URL | Purpose |
|---------|----------|--------------|---------|
| LiteLLM | 30400 | `http://litellm-proxy:4000` | API Gateway |
| Router | 30080 | `http://router-service:8080` | Request routing |
| Redis | 30079 | `redis:6379` | Queue state |

**Node IP**: `7.242.101.107`

---

## Accessing LiteLLM UI

LiteLLM includes a Swagger UI for API exploration and documentation.

**URL**: `http://7.242.101.107:30400/`

This provides interactive API documentation via Swagger UI.

### Admin UI (Optional)

The LiteLLM Admin UI requires environment variables. To enable:

```bash
kubectl set env deployment/litellm-proxy -n sir-llm-platform \
  LITELLM_UI_USERNAME=admin \
  LITELLM_UI_PASSWORD=your_secure_password
```

Then access at: `http://7.242.101.107:30400/ui`

---

## Curl Commands

### Health Checks

```bash
# LiteLLM health (with auth)
curl -s -H "Authorization: Bearer sk-qwen38b-local" \
  http://7.242.101.107:30400/health

# Router health
curl -s http://7.242.101.107:30080/health

# Direct vLLM health (from within cluster)
curl -s http://vllm-qwen3-8b:8200/health
```

### Chat Completions

```bash
# Via LiteLLM (recommended)
curl -s -X POST http://7.242.101.107:30400/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer sk-qwen38b-local" \
  -d '{
    "model": "qwen3-8b",
    "messages": [{"role": "user", "content": "Hello"}],
    "max_tokens": 100
  }'

# Via Router directly (bypasses LiteLLM)
curl -s -X POST http://7.242.101.107:30080/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "model": "qwen3-8b",
    "messages": [{"role": "user", "content": "Hello"}],
    "max_tokens": 100
  }'
```

### Model Info

```bash
# List available models via LiteLLM
curl -s -H "Authorization: Bearer sk-qwen38b-local" \
  http://7.242.101.107:30400/v1/models

# Router backend status
curl -s http://7.242.101.107:30080/health | jq '.backends'
```

### Embeddings

```bash
curl -s -X POST http://7.242.101.107:30400/v1/embeddings \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer sk-qwen38b-local" \
  -d '{
    "model": "qwen3-8b",
    "input": "Hello world"
  }'
```

---

## Prometheus & Grafana Monitoring

A full Prometheus/Grafana monitoring stack has been installed in the `monitoring` namespace.

### Access URLs

| Service | URL | Credentials |
|---------|-----|-------------|
| **Prometheus** | http://7.242.101.107:30900 | No auth required |
| **Grafana** | http://7.242.101.107:30300 | admin / prometheus-admin |

### Available Metrics

The following metrics are now being scraped:

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

1. Navigate to http://7.242.101.107:30300
2. Login with `admin` / `prometheus-admin`
3. Go to Dashboards → Import to add custom dashboards

### Current Scrape Status

```bash
# Check Prometheus targets
curl -s http://7.242.101.107:30900/api/v1/targets | jq '.data.activeTargets[] | select(.labels.namespace == "sir-llm-platform")'
```

---

## Basic Health Checks (Pre-Prometheus)

Router health endpoint is still available without Prometheus:

```bash
# Get backend metrics from router
curl -s http://7.242.101.107:30080/health | jq '.backends'
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
kubectl logs -n sir-llm-platform -l app=litellm --tail=100

# Router logs
kubectl logs -n sir-llm-platform -l app=router-service --tail=100

# vLLM logs
kubectl logs -n sir-llm-platform -l app=vllm --tail=100
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
# Scale up vLLM to 4 replicas
kubectl scale deployment/vllm-qwen3-8b -n sir-llm-platform --replicas=4

# Scale router
kubectl scale deployment/router-service -n sir-llm-platform --replicas=2
```

---

## Configuration Notes

### Current Settings

| Setting | Value | Notes |
|---------|-------|-------|
| Model | Qwen3.8-27B | TP=4 per replica |
| Max Model Len | 131072 | 128k context |
| Batch Size | 8 | Per vLLM instance |
| KV-Aware Routing | Disabled | Missing tokenizer deps |
| Database | In-memory | No persistence |

### Router Configuration

The router runs with:
- `MODEL_NAME=qwen3-8b`
- `KV_AWARE=false` (disabled due to missing sentencepiece/tiktoken)
- `LABEL_SELECTOR=component=vllm`
- `VLLM_PORT=8200`

### Why KV-Aware is Disabled

The kv-router image lacks `sentencepiece` or `tiktoken` Python packages needed for tokenizer initialization. With `KV_AWARE=true`, the router attempts to load the tokenizer and crashes.

To enable KV-aware routing, you would need:
1. A kv-router image with tokenizer dependencies installed, OR
2. Use `KV_HASH_SOURCE=service` with a separate hash service

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

Check vLLM logs: `kubectl logs -n sir-llm-platform -l app=vllm --tail=50`

Check pod events: `kubectl describe pod <pod-name> -n sir-llm-platform`

---

## Helm Values File

Current deployment uses: `values-qwen4B-256k.yaml`

Key configurations in that file:
- `router.enabled: false` - vllm.router disabled
- `router.service.enabled: true` - kv-router enabled
- `litellm.enabled: true` - LiteLLM proxy enabled
- `modelVolume.hostPath: /data/models`
- `modelVolume.modelSubPath: Qwen3.8-27B`