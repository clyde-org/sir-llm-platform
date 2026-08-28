# Monitoring Setup

## Components

### Prometheus (kube-prometheus-stack v0.85.0)

Prometheus for metrics collection with ServiceMonitors for automatic endpoint discovery.

### NPU Exporter (v6.0.0)

Official Ascend MindX NPU exporter for Huawei Ascend 910B NPU metrics.

### Grafana (v12.1.1)

Visualization and dashboards pre-configured with Prometheus data source.

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
```

## Files

- `servicemonitors.yaml` - ServiceMonitor/PodMonitor configs
- `npu-exporter.yaml` - NPU exporter DaemonSet
- `prometheus/values.yaml` - Helm values for kube-prometheus-stack