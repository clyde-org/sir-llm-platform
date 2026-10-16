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

# Loki + Promtail log collection (namespace logging) — see loki/README.md
kubectl apply -f loki/static-pv.yaml loki/loki-nodeport.yaml loki/grafana-datasource.yaml
helm install loki      loki/charts/loki-7.3.0.tgz      -n logging -f loki/loki-values.yaml
helm install promtail  loki/charts/promtail-6.17.1.tgz -n logging -f loki/promtail-values.yaml
```

## Access

| Service | URL | Credentials |
|---------|-----|-------------|
| Prometheus | `http://<NODE_IP>:30900` | — |
| Grafana | `http://<NODE_IP>:30300` | `admin` / `prometheus-admin` |
| Platform dashboard | `http://<NODE_IP>:30300/d/sir-llm-platform-vllm` | — |
| Loki log API (direct, lab only) | `http://<NODE_IP>:32100` | none — logs contain secrets |

Dashboard rows: **Gateway/HTTP** · **Router/SLO** (queue, TTFT/E2E, SLO
met/missed) · **Load balancing across vLLM replicas** · **Cache & speculative
decoding** (local/external prefix hits, MTP acceptance) · **Reliability &
preemption** · **NPU hardware** (AICore, HBM, temperature, power, ECC —
filtered to the phase-1 nodes) · **Client requests (LiteLLM gateway)** (per-client
rate by IP × user-agent, per-agent rate, requests by IP × status code,
failures by client, 24h totals per client, token in/out per model). To
redeploy the dashboard after editing the JSON: `kubectl apply -f
dashboards/sir-llm-platform-vllm-configmap.yaml` (regenerate the ConfigMap's
embedded JSON from the standalone `.json` after edits — the sidecar in the
Grafana pod syncs the ConfigMap to `/tmp/dashboards` automatically).

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
| `litellm_requests_metric_total` | gateway requests — labels `client_ip`, `user_agent`, `requested_model` (no status code) |
| `litellm_proxy_total_requests_metric_total` | proxy-level requests — adds `status_code`, `route` |
| `litellm_proxy_failed_requests_metric_total` | proxy-level failures — `client_ip`, `user_agent` (created on first failure) |
| `litellm_input_tokens_metric_total` / `litellm_output_tokens_metric_total` | token counters — `requested_model` only, **no client labels** |
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
# LiteLLM gateway — per-client attribution (all clients share one API key):
sum by (client_ip, user_agent) (rate(litellm_requests_metric_total{namespace="sir-llm-platform"}[5m]))          # who is sending, right now
sum by (client_ip) (increase(litellm_requests_metric_total{namespace="sir-llm-platform"}[24h]))                # 24h volume per client IP
sum by (client_ip, status_code) (rate(litellm_proxy_total_requests_metric_total{namespace="sir-llm-platform"}[5m]))  # per-client errors
# in-cluster client IPs are pod IPs: kubectl get pods -A -o wide | grep <ip>
```

Check scrape status:

```bash
curl -s http://<NODE_IP>:30900/api/v1/targets \
  | jq '.data.activeTargets[] | select(.labels.namespace == "sir-llm-platform") | {job: .labels.job, health}'
```

## Log collection (Loki)

All cluster container logs are shipped by Promtail (DaemonSet) to Loki
(namespace `logging`, 7-day retention, ~2 GiB/day). In Grafana: **Explore →
datasource Loki**. Stream labels: `namespace`, `pod`, `container`, `node_name`,
`job`, `app`, `filename`, `stream`.

```logql
# errors from any vLLM pod, last 30m
{namespace="vllm-ascend", container="vllm"} |= "ERROR" | line_format "{{.message}}"

# litellm proxy access log
{container="litellm-proxy"} |= "POST"

# line rate per namespace
sum by (namespace) (count_over_time({container=~".+"}[5m]))
```

Note: in stream selectors label values are **regex**, not glob
(`{filename="/var/log/pods/*"}` does not match). Full details, operations and
uninstall: [`loki/README.md`](loki/README.md).

## Known data gaps

- **Redis** — no redis-exporter deployed; the router queue state isn't directly observable.
- **`vllm_threads` (:9101)** — the per-pod thread-count gauge is served but the vLLM PodMonitor only scrapes port `8200`; add a second endpoint to capture it.
- **LiteLLM per-client tokens** — the token counters carry no client labels (upstream LiteLLM metric design), so per-client token usage must be read from the LiteLLM admin UI (`http://<NODE_IP>:30400/ui`, Requests page) instead of Grafana.

## Files

- `loki/` — Loki + Promtail (values, static PV, NodePort service, Grafana datasource, vendored charts)
- `servicemonitors.yaml` — ServiceMonitor/PodMonitor configs
- `npu-exporter.yaml` — NPU exporter DaemonSet
- `prometheus/values.yaml` — Helm values for kube-prometheus-stack
- `dashboards/sir-llm-platform-vllm.json` — dashboard JSON (source of truth)
- `dashboards/sir-llm-platform-vllm-configmap.yaml` — ConfigMap that provisions it
