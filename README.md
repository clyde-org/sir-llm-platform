# SIR LLM Platform

Qwen3.8-27B LLM deployment on Huawei Ascend 910B NPUs with vLLM, LiteLLM, and kv-router.

## Architecture

```
Client → LiteLLM (NodePort 30400) → vLLM (8200) → 4 pods (TP=4, one per node)
              (bypasses the router)
kv-router (ClusterIP :8080) ⇄ kv-sidecar (per vLLM pod) ⇄ vLLM (ZMQ KV-events :5557)
                              Redis (ClusterIP :6379) — queue state
```

- **4 vLLM replicas**, one pod per 4-NPU node (TP=4). Each pod also runs a `kv-sidecar`.
- **LiteLLM** is the only external port (NodePort 30400) and points directly at the
  vLLM Service (bypassing the router).
- **kv-router** and **Redis** are ClusterIP (internal only).
- A second Service `vllm-qwen3-8b-claude` (ClusterIP) fronts the same pods for the
  Claude Code client (Anthropic `/v1/messages`) — see [claude-code/README.md](claude-code/README.md).

## Components

| Component | Version | Description |
|-----------|---------|-------------|
| Model | Qwen3.8-27B | Served as `qwen3-8b` |
| vLLM | v0.23.0rc1 | Inference on Ascend NPUs |
| LiteLLM | main-stable | API proxy with auth |
| kv-router | latest | Request routing |
| Redis | 7-alpine | Queue state |
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
# Install vLLM stack (chart defaults match the live deployment)
helm upgrade --install vllm ./vllm-stack \
  -n sir-llm-platform --create-namespace

# Or apply an environment-specific override:
helm upgrade --install vllm ./vllm-stack \
  -n sir-llm-platform --create-namespace \
  -f vllm-stack/values/qwen3-8b.yaml

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
| LiteLLM API | http://NODE_IP:30400 | Bearer sk-sirlab |
| Swagger UI | http://NODE_IP:30400/ | - |
| Prometheus | http://NODE_IP:30900 | - |
| Grafana | http://NODE_IP:30300 | admin / prometheus-admin |

`NODE_IP` is any reachable cluster node (e.g. `7.242.101.107`). Only LiteLLM is
exposed via NodePort; the router and Redis are ClusterIP-only (use `kubectl
port-forward`).

## Documentation

- [Deployment Guide](DEPLOYMENT.md) - Complete setup and usage
- [Monitoring](monitoring/README.md) - Prometheus & Grafana setup
- [Claude Code integration](claude-code/README.md) - Point Claude Code at this deployment

## Repository Structure

```
sir-llm-platform/
├── README.md
├── DEPLOYMENT.md
├── claude-code/
│   ├── README.md
│   └── settings.qwen3-8b.json
├── monitoring/
│   ├── README.md
│   ├── servicemonitors.yaml
│   ├── npu-exporter.yaml
│   └── prometheus/
│       └── values.yaml
└── vllm-stack/
    ├── Chart.yaml
    ├── values.yaml
    ├── values/
    │   ├── qwen3-8b.yaml
    │   ├── values-qwen38b.yaml
    │   └── values-presentation-dual-model-device-plugin.yaml
    └── templates/
        ├── 01-configmap.yaml
        ├── 02-redis.yaml
        ├── 03-router.yaml
        ├── 04-vllm.yaml
        ├── 05-litellm.yaml
        ├── 06-claude-service.yaml
        ├── 07-podmonitors.yaml
        └── _helpers.tpl
```

## License

Internal use only - Clyde Org
