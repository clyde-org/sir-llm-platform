# Monitoring Setup

## Components

### Prometheus (kube-prometheus-stack v0.85.0)

Prometheus for metrics collection with ServiceMonitors for automatic endpoint discovery.

### NPU Exporter (v6.0.0)

Official Ascend MindX NPU exporter for Huawei Ascend 910B NPU metrics.

### Grafana (v12.1.1)

Visualization and dashboards pre-configured with Prometheus data source.

### Mooncake master (vLLM image)

Metrics from the `mooncake-master` Deployment (KV-cache store control plane)
are scraped from port 9003 by the `mooncake-metrics` ServiceMonitor.

## Installation

```bash
# Install kube-prometheus-stack
helm upgrade --install prometheus prometheus-community/kube-prometheus-stack \
  --namespace monitoring --create-namespace \
  --values prometheus/values.yaml

# Apply NPU exporter
kubectl apply -f npu-exporter.yaml

# Apply ServiceMonitors
kubectl apply -f servicemonitors.yaml
```

## Access

| Service | URL | Credentials |
|---------|-----|-------------|
| Prometheus | http://NODE_IP:30900 | - |
| Grafana | http://NODE_IP:30300 | admin / prometheus-admin |

## Dashboards

### sir-llm-platform / vLLM + Router + NPU

Dedicated dashboard for the `sir-llm-platform` deployment (scoped by
`namespace="sir-llm-platform"`, NPU hardware limited to the two hosting nodes).

- **URL:** `http://NODE_IP:30300/d/sir-llm-platform-vllm`
- **Source of truth:** `dashboards/sir-llm-platform-vllm.json`
- **Provisioned from:** ConfigMap `sir-llm-platform-vllm-dashboard` (label
  `grafana_dashboard: "1"`), synced by the Grafana pod's `grafana-sc-dashboard`
  sidecar into `/tmp/dashboards`. Because Grafana persistence is disabled, this
  ConfigMap is what keeps the dashboard across restarts.

Rows:

1. **Gateway / HTTP** — request rate by status, HTTP p50/p95/p99 latency, error
   rate by endpoint, requests by endpoint (the vLLM OpenAI server's own
   `http_requests_total` / `http_request_duration_seconds`).
2. **Router / SLO** — admission vs dispatch, central queue, router TTFT/E2E
   percentiles, SLO met/missed, timeshare awake/total instances, HA failovers,
   KV-aware routing affinity.
3. **Load balancing across vLLM replicas** — per-pod generation throughput,
   running requests, KV cache usage, request success rate (reveals imbalance
   across the 4 replicas).
4. **Cache & Speculative decoding** — local prefix-cache hit rate, external
   KV-sidecar hit rate, MTP acceptance rate, draft vs accepted tokens, cached
   vs recomputed prompt tokens.
5. **Reliability & preemption** — preemption rate, waiting-by-reason, engine
   sleep state.
6. **NPU hardware** — AICore utilization, HBM used/total, temperature, power,
   unhealthy-chip and HBM ECC double-bit error stats, filtered to the two
   deployment nodes via the `$npu_node` variable (default
   `7.242.96.11:8082` and `7.242.99.216:8082`).

To redeploy after editing the JSON, update both the repo file and the ConfigMap:

```bash
kubectl apply -f dashboards/sir-llm-platform-vllm-configmap.yaml
```

### Known data gaps (need scrape changes, not just panels)

- **LiteLLM** — gateway metrics are now collected: the prometheus callbacks are enabled
  in `litellm_config.yaml` (`success_callback`/`failure_callback: ["prometheus"]`, see
  `vllm-stack/templates/01-configmap.yaml`) and the `litellm-metrics` PodMonitor scrapes
  `/metrics/` with the master key via the `litellm-bearer-token` secret
  (`litellm_proxy_total_requests_metric_total`, `litellm_request_total_latency_metric`,
  `litellm_total_tokens_metric_total`, `litellm_spend_metric_total`, ...). There is no
  dashboard row for them yet — the Gateway row currently shows vLLM's own HTTP metrics.
- **Redis** — no redis-exporter is deployed, so the router's Redis queue/cache
  is not observable.
- **`vllm_threads` (:9101)** — the per-pod thread-count endpoint served by the
  vLLM wrapper is defined but the vllm PodMonitor only scrapes port `http`; add a
  second endpoint for `9101` to capture it.

## Available Metrics

### vLLM Metrics

| Metric | Type | Description |
|--------|------|-------------|
| `vllm:num_requests_running` | Gauge | Running batch size |
| `vllm:num_requests_waiting` | Gauge | Queue depth |
| `vllm:kv_cache_usage_perc` | Gauge | KV cache utilization |
| `vllm:prompt_tokens_total` | Counter | Prompt tokens processed |
| `vllm:generation_tokens_total` | Counter | Generated tokens |
| `vllm:request_success_total` | Counter | Successful requests |
| `vllm:e2e_request_latency_seconds` | Histogram | End-to-end latency |
| `vllm:time_to_first_token_seconds` | Histogram | TTFT |

### Router Metrics

| Metric | Type | Description |
|--------|------|-------------|
| `router_central_queue_length` | Gauge | Global queue depth |
| `router_admission_requests_total` | Counter | Requests admitted |
| `router_dispatch_requests_total` | Counter | Requests dispatched |
| `router_request_ttft_seconds` | Histogram | Router TTFT |
| `router_request_e2e_seconds` | Histogram | Router E2E |

### Mooncake Metrics

| Metric | Type | Description |
|--------|------|-------------|
| `master_active_clients` | Gauge | Connected store clients (expect 16 = 4 pods × 4 TP ranks) |
| `master_total_capacity_bytes` | Gauge | Pool capacity (expect 128 GiB) |
| `master_allocated_bytes` | Gauge | Bytes currently allocated in the pool |
| `segment_total_capacity_bytes{segment="ip:port"}` | Gauge | Per-rank segment (one per TP rank) |
| `master_key_count` | Gauge | Cached KV objects in the pool |
| `master_batch_put_start_requests_total` | Counter | Put starts (per-rank KV saves) |
| `master_batch_put_end_requests_total` | Counter | Put completions — should track `put_start` |
| `master_batch_put_revoke_requests_total` | Counter | Put revocations — ≈ `put_start` means transfers are failing |
| `master_put_start_alloc_failures_total` | Counter | Put starts failed on replica allocation |
| `master_evicted_key_count` | Counter | Keys evicted at the high watermark (0.9) |
| `master_evicted_size_bytes` | Counter | Bytes evicted |
| `master_mount_segment_requests_total` / `master_remount_segment_requests_total` | Counter | Segment (re)registrations from clients |

### NPU Metrics

| Metric | Type | Description |
|--------|------|-------------|
| `machine_npu_nums` | Gauge | NPUs per node |
| `npu_chip_info_aicore_current_freq` | Gauge | AICore frequency (MHz) |
| `npu_chip_info_hbm_total_memory` | Gauge | Total HBM memory |
| `npu_chip_info_hbm_used_memory` | Gauge | Used HBM memory |
| `npu_chip_info_bandwidth_rx/tx` | Gauge | Interface bandwidth |

## Prometheus Queries

```promql
# Request rate
rate(vllm:request_success_total[5m])

# KV cache usage
vllm:kv_cache_usage_perc

# Queue depth
router_central_queue_length

# P95 E2E latency
histogram_quantile(0.95, rate(router_request_e2e_seconds_bucket[5m]))

# HBM memory usage
npu_chip_info_hbm_used_memory / npu_chip_info_hbm_total_memory

# Mooncake pool utilization
master_allocated_bytes / master_total_capacity_bytes

# Mooncake put success ratio (1.0 = healthy)
rate(master_batch_put_end_requests_total[5m]) / rate(master_batch_put_start_requests_total[5m])
```

## Files

- `servicemonitors.yaml` - ServiceMonitor/PodMonitor configs
- `npu-exporter.yaml` - NPU exporter DaemonSet
- `prometheus/values.yaml` - Helm values for kube-prometheus-stack
- `dashboards/sir-llm-platform-vllm.json` - the sir-llm-platform dashboard JSON
- `dashboards/sir-llm-platform-vllm-configmap.yaml` - the ConfigMap that provisions it