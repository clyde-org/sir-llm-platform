# SIR LLM Platform

Qwen3.8-27B LLM deployment on Huawei Ascend 910B NPUs with vLLM, LiteLLM, and kv-router.

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

mooncake-master (ClusterIP :50051/:8080/:9003) — cluster-wide KV-cache store;
each vLLM TP rank registers an 8 GiB DRAM segment (pool = 128 GiB, TCP)
```

- **4 vLLM replicas** (TP=4), two pods per 8-NPU node. Each pod also runs a `kv-sidecar`.
- **Mooncake** KV-cache store: `mooncake-master` plus embedded segments in the
  vLLM pods (`MooncakeStoreConnector`) — prefix blocks survive local eviction
  and are shareable across pods (see
  [DEPLOYMENT.md](DEPLOYMENT.md#mooncake-kv-cache-store)).
- **LiteLLM** is the only external port (NodePort 30400) and serves both routes:
  - `/v1/chat/completions` (OpenAI) through the **kv-router**
  - `/v1/messages` (Anthropic) is a pass-through to the **kv-router's
    model-aware** `/v1/messages` endpoint — the request body's `model` field
    picks the serving pool, the same model namespace as the OpenAI path. This
    is the path Claude Code uses (see
    [claude-code/README.md](claude-code/README.md)).
- **kv-router** and **Redis** are ClusterIP (internal only).

## Components

| Component | Version | Description |
|-----------|---------|-------------|
| Model | Qwen3.8-27B | Served as `qwen3.8-27b` |
| vLLM | v0.23.0rc1 | Inference on Ascend NPUs |
| LiteLLM | main-stable | API proxy with auth |
| kv-router | latest | Request routing |
| Redis | 7-alpine | Queue state |
| Mooncake | (vllm-ascend image) | Cluster-wide KV-cache store (master + 128 GiB embedded segments) |
| NPU Exporter | 6.0.0 | Ascend NPU metrics |
| Prometheus | 3.5.0 | Metrics collection |
| Grafana | 12.1.1 | Visualization |

## Quick Start

### Prerequisites

- Kubernetes cluster with Ascend 910B NPUs
- kubectl configured
- Helm 3.x

### Installation

```bash
# Install vLLM stack (chart defaults + the mooncake override match the live deployment)
helm upgrade --install vllm ./vllm-stack \
  -n sir-llm-platform --create-namespace \
  --set mooncake.enabled=true --set mooncake.attachToVllm=true

# Install monitoring (Prometheus + Grafana)
helm upgrade --install prometheus prometheus-community/kube-prometheus-stack \
  -n monitoring --create-namespace \
  -f monitoring/prometheus/values.yaml

# Apply NPU exporter (in the npu-exporter namespace)
kubectl apply -f monitoring/npu-exporter.yaml

# Apply monitoring-namespace ServiceMonitors / PodMonitors
kubectl apply -f monitoring/servicemonitors.yaml
```

## Access Endpoints

| Service | URL | Credentials |
|---------|-----|-------------|
| LiteLLM API | http://NODE_IP:30400 | Bearer sk-qwen38b-local |
| Swagger UI | http://NODE_IP:30400/ | - |
| Open WebUI (chat GUI) | http://NODE_IP:30401 | first account = admin |
| Prometheus | http://NODE_IP:30900 | - |
| Grafana | http://NODE_IP:30300 | admin / prometheus-admin |

`NODE_IP` is any reachable cluster node (e.g. `7.242.101.107`). Only LiteLLM is
exposed via NodePort; the router and Redis are ClusterIP-only (use `kubectl
port-forward`).

## Documentation

**Start here → [docs/platform/README.md](docs/platform/README.md)** — the complete
documentation set for this platform (written for colleagues new to the lab):

- [Architecture](docs/platform/architecture.md) — system overview with figures: request
  paths, component responsibilities, KV-cache store, context guard, monitoring
- [Setup guide](docs/platform/setup.md) — prerequisites, Helm installation, phase-2
  rollout, verification checklist, day-2 operations, rollback
- [User guide](docs/platform/user-guide.md) — API calls, model/pool selection, coding
  agents (Claude Code, pi), chat GUIs, monitoring, troubleshooting

Deep references:

- [DEPLOYMENT.md](DEPLOYMENT.md) - Deployment deep-dive (Mooncake root-cause history,
  exact Helm values, vLLM arguments, router env)
- [Monitoring](monitoring/README.md) - Prometheus & Grafana setup, metric reference
- [Claude Code integration](claude-code/README.md) - Out-of-the-box client setup (copy `settings.qwen3-8b.json`)
- [Pi integration](pi/README.md) - Out-of-the-box client setup (copy `pi/models.json` + `pi/settings.json`)
- [deepseek-chat harness](deepseek-harness/README.md) - Minimal stdlib-only chat CLI +
  alternative GUI options

## Repository Structure

```
sir-llm-platform/
├── README.md
├── DEPLOYMENT.md                    # deployment deep-dive
├── docs/
│   ├── platform/                    # ← main documentation (start here)
│   │   ├── README.md
│   │   ├── architecture.md
│   │   ├── setup.md
│   │   └── user-guide.md
│   └── pool-router-sidecar-changes.md
├── vllm-stack/                      # Helm chart (the whole serving stack)
│   ├── Chart.yaml
│   ├── values.yaml
│   ├── values/                      # stale older-schema files (do not use)
│   │   ├── qwen3-8b.yaml
│   │   └── values-qwen38b.yaml
│   └── templates/
│       ├── 01-configmap.yaml        # model-registry + litellm-config
│       ├── 02-redis.yaml
│       ├── 03-router.yaml
│       ├── 04-vllm.yaml             # phase-1 vLLM pods (+ kv-sidecar)
│       ├── 05-litellm.yaml
│       ├── 06-claude-service.yaml
│       ├── 07-podmonitors.yaml
│       ├── 08-mooncake.yaml
│       ├── 09-vllm-phase2.yaml      # phase-2 (262k) pool, off by default
│       └── _helpers.tpl
├── monitoring/                      # Prometheus values, NPU exporter, monitors, dashboards
├── claude-code/                     # Claude Code settings template
├── pi/                              # pi models.json + settings.json templates
├── deepseek-harness/                # minimal Python chat CLI + Open WebUI manifest
└── scripts/
    └── test-ctx-guard.py            # client-side overflow contract test
```

## License

Internal use only - Clyde Org
