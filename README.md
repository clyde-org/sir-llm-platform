# SIR LLM Platform

Qwen3.8-27B LLM deployment on Huawei Ascend 910B NPUs with vLLM, LiteLLM, and kv-router.

## Architecture

```
Client → LiteLLM (30400) → kv-router (30080) → vLLM (8200) → 2 Pods (TP=4)
                                     ↓
                                  Redis
```

## Components

| Component | Version | Description |
|-----------|---------|-------------|
| Model | Qwen3.8-27B | Served as `qwen3-8b` |
| vLLM | v0.23.0rc1 | Inference on Ascend NPUs |
| LiteLLM | 1.98.0 | API proxy with auth |
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
# Install vLLM stack
helm upgrade --install vllm ./vllm-stack \
  -n sir-llm-platform --create-namespace \
  -f values/vllm-qwen3-8b.yaml

# Install monitoring (Prometheus + Grafana)
helm upgrade --install prometheus ./monitoring/prometheus \
  -n monitoring --create-namespace \
  -f values/prometheus.yaml

# Apply NPU exporter
kubectl apply -f monitoring/npu-exporter.yaml

# Apply ServiceMonitors
kubectl apply -f monitoring/servicemonitors.yaml
```

## Access Endpoints

| Service | URL | Credentials |
|---------|-----|-------------|
| LiteLLM API | http://NODE_IP:30400 | Bearer sk-qwen38b-local |
| Swagger UI | http://NODE_IP:30400/ | - |
| Prometheus | http://NODE_IP:30900 | - |
| Grafana | http://NODE_IP:30300 | admin / prometheus-admin |

## Documentation

- [Deployment Guide](DEPLOYMENT.md) - Complete setup and usage
- [Monitoring](monitoring/README.md) - Prometheus & Grafana setup

## Repository Structure

```
sir-llm-platform/
├── README.md
├── DEPLOYMENT.md
├── monitoring/
│   ├── README.md
│   ├── servicemonitors.yaml
│   ├── npu-exporter.yaml
│   └── prometheus/
│       └── values.yaml
└── vllm-stack/
    ├── Chart.yaml
    ├── values.yaml
    └── templates/
        ├── 01-configmap.yaml
        ├── 02-redis.yaml
        ├── 03-vllm.yaml
        ├── 04-router.yaml
        └── 05-litellm.yaml
```

## License

Internal use only - Clyde Org