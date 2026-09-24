# Monitoring

Prometheus + Grafana (kube-prometheus-stack) in namespace `monitoring`, an
NPU-exporter DaemonSet, and the pre-provisioned platform dashboard.

## Installation

```bash
# Prometheus + Grafana
helm upgrade --install prometheus prometheus-community/kube-prometheus-stack \
  -n monitoring --create-namespace \
  -f prometheus/values.yaml

# NPU exporter DaemonSet (namespace npu-exporter)
kubectl apply -f npu-exporter.yaml

# ServiceMonitors/PodMonitors + litellm bearer secret (namespace monitoring)
kubectl apply -f servicemonitors.yaml

# Platform dashboard (Grafana persistence is disabled — the ConfigMap is the source of truth)
kubectl apply -f dashboards/sir-llm-platform-vllm-configmap.yaml
```

## Access

| Service | URL | Credentials |
|---------|-----|-------------|
| Prometheus | `http://<NODE_IP>:30900` | — |
| Grafana | `http://<NODE_IP>:30300` | `admin` / `prometheus-admin` |
| Platform dashboard | `http://<NODE_IP>:30300/d/sir-llm-platform-vllm` | — |

Dashboard rows: **Gateway/HTTP** · **Router/SLO** (queue, TTFT/E2E, SLO
met/missed) · **Load balancing across vLLM replicas** · **Cache & speculative
decoding** (local/external prefix hits, MTP acceptance) · **Reliability &
preemption** · **NPU hardware** (AICore, HBM, temperature, power, ECC —
filtered to the phase-1 nodes). To redeploy the dashboard after editing the
JSON: `kubectl apply -f dashboards/sir-llm-platform-vllm-configmap.yaml`.

Scrape sources: vLLM pods (`:8200/metrics`), kv-router (`:8080/metrics`),
LiteLLM (`/metrics/`, authenticated via the `litellm-bearer-token` secret),
npu-exporter DaemonSet (`:8082`), mooncake-master (`:9003`).

## Key metrics

| Metric | Description |
|--------|-------------|
| `vllm:num_requests_running` / `vllm:num_requests_waiting` | running / queued requests per pod |
| `vllm:kv_cache_usage_perc` | KV cache utilization |
| `vllm:e2e_request_latency_seconds` / `vllm:time_to_first_token_seconds` | latency histograms |
| `router_central_queue_length` | router queue depth |
| `router_admission_requests_total` / `router_dispatch_requests_total` | admission vs dispatch |
| `router_request_ttft_seconds` / `router_request_e2e_seconds` | router-measured latency histograms |
| `master_active_clients` | mooncake clients (expect 16 = 4 pods × 4 ranks) |
| `master_total_capacity_bytes` / `master_allocated_bytes` | mooncake pool capacity / fill |
| `master_batch_put_start/end/revoke_requests_total` | store put health — `end` must track `start` (`revoke ≈ start` = failing transfers) |
| `npu_chip_info_hbm_used_memory` / `npu_chip_info_hbm_total_memory` | HBM usage |

Useful PromQL:

```promql
rate(vllm:request_success_total[5m])                                # request rate
vllm:kv_cache_usage_perc                                           # KV cache pressure
router_central_queue_length                                        # queue depth
histogram_quantile(0.95, rate(router_request_e2e_seconds_bucket[5m]))  # P95 E2E
master_allocated_bytes / master_total_capacity_bytes               # mooncake pool fill
rate(master_batch_put_end_requests_total[5m]) / rate(master_batch_put_start_requests_total[5m])  # put health (1.0 = good)
npu_chip_info_hbm_used_memory / npu_chip_info_hbm_total_memory     # HBM usage
```

Check scrape status:

```bash
curl -s http://<NODE_IP>:30900/api/v1/targets \
  | jq '.data.activeTargets[] | select(.labels.namespace == "sir-llm-platform") | {job: .labels.job, health}'
```

## Known data gaps

- **Redis** — no redis-exporter deployed; the router queue state isn't directly observable.
- **`vllm_threads` (:9101)** — the per-pod thread-count gauge is served but the vLLM PodMonitor only scrapes port `8200`; add a second endpoint to capture it.
- **LiteLLM** — gateway metrics are scraped but have no dashboard row yet (the Gateway row shows vLLM's own HTTP metrics).

## Files

- `servicemonitors.yaml` — ServiceMonitor/PodMonitor configs
- `npu-exporter.yaml` — NPU exporter DaemonSet
- `prometheus/values.yaml` — Helm values for kube-prometheus-stack
- `dashboards/sir-llm-platform-vllm.json` — dashboard JSON (source of truth)
- `dashboards/sir-llm-platform-vllm-configmap.yaml` — ConfigMap that provisions it
