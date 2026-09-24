# SIR LLM Platform — Documentation

A self-hosted LLM serving platform: **Qwen3.8-27B** running on **Huawei Ascend 910B
NPUs** with **vLLM** (v0.23.0rc1), fronted by a **kv-aware router** and a **LiteLLM**
gateway, with a cluster-wide **Mooncake** KV-cache store, **Prometheus/Grafana**
monitoring, and out-of-the-box client configs for **Claude Code** and **pi**.

This folder is the entry point for colleagues. Start with the pages below; deeper
implementation notes live in the files they link to.

## Documentation map

| Page | What it covers |
|------|----------------|
| [architecture.md](architecture.md) | Full system architecture with figures: request paths, component responsibilities, KV-cache store, context guard, monitoring |
| [setup.md](setup.md) | Prerequisites, installation (Helm), phase-2 rollout, verification checklist, day-2 operations, rollback |
| [user-guide.md](user-guide.md) | Daily usage: API calls (OpenAI + Anthropic), model/pool selection, coding agents (Claude Code, pi), chat GUIs, monitoring, troubleshooting |

Supporting references (also in this repo):

| File | Content |
|------|---------|
| [../../DEPLOYMENT.md](../../DEPLOYMENT.md) | Deployment deep-dive: Mooncake troubleshooting history, exact Helm values, vLLM arguments, router env |
| [../pool-router-sidecar-changes.md](../pool-router-sidecar-changes.md) | Design notes for the multi-pool (131k/262k) router & sidecar changes |
| [../../monitoring/README.md](../../monitoring/README.md) | Prometheus/Grafana/NPU-exporter details, metric reference, dashboard rows |
| [../../claude-code/README.md](../../claude-code/README.md) | Claude Code client setup + context-window/autocompact rationale |
| [../../pi/README.md](../../pi/README.md) | pi (pi-coding-agent) client setup + compaction tuning rationale |
| [../../deepseek-harness/README.md](../../deepseek-harness/README.md) | Minimal Python chat CLI + alternative GUI options (Open WebUI, LibreChat, ...) |
| [../../scripts/test-ctx-guard.py](../../scripts/test-ctx-guard.py) | Client-side contract test for overflow handling (stdlib-only) |

## 30-second overview

```
Client ──► LiteLLM (NodePort 30400, the only external port)
             ├── /v1/chat/completions (OpenAI)  ──► kv-router ──► vLLM pools
             └── /v1/messages        (Anthropic) ──► kv-router (model-aware pass-through) ──► vLLM pools

kv-router ⇄ Redis (queue state)          kv-sidecar (in every vLLM pod) pulls work from the router
mooncake-master ── cluster-wide 128 GiB KV-cache pool shared by all vLLM pods (phase-1)
```

- **Two pools, one model.** The same Qwen3.8-27B weights are served under two names
  from two hardware pools: `qwen3.8-27b` (131k context, phase-1 nodes) and
  `qwen3.8-27b-262k` (262k context, phase-2 nodes). The `model` field in the request
  body picks the pool on **both** API paths.
- **One external port.** LiteLLM on NodePort **30400** is the only exposed gateway.
  Router, Redis, and vLLM are ClusterIP-only (use `kubectl port-forward` for
  debugging — recipes in [user-guide.md](user-guide.md)).
- **Auth is a single static key** (`sk-qwen38b-local`) — it is the LiteLLM master
  key, shared by all lab clients.

## Quick reference

### Endpoints

| Service | URL | Credentials |
|---------|-----|-------------|
| LiteLLM API (the gateway) | `http://<NODE_IP>:30400` | `Bearer sk-qwen38b-local` |
| LiteLLM Swagger UI | `http://<NODE_IP>:30400/` | — |
| Open WebUI (chat GUI) | `http://<NODE_IP>:30401` | first account = admin |
| Prometheus | `http://<NODE_IP>:30900` | — |
| Grafana | `http://<NODE_IP>:30300` | `admin` / `prometheus-admin` |

`<NODE_IP>` is any reachable cluster node (e.g. `7.242.101.107`).

### Models / pools

| Model name | Pool | Hardware | Context limit |
|------------|------|----------|---------------|
| `qwen3.8-27b` | phase-1 (default) | Ascend 910B, 4 pods × TP=4 | 131,072 |
| `qwen3.8-27b-262k` | phase-2 | 910B3 64 GB HBM | 262,144 |
| `qwen3.8-27b-131k` | phase-1 (alias) | — | 131,072 (OpenAI path **only**) |

### Smallest possible smoke test

```bash
curl -s -X POST http://<NODE_IP>:30400/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer sk-qwen38b-local" \
  -d '{"model": "qwen3.8-27b", "messages": [{"role": "user", "content": "Reply with PONG"}], "max_tokens": 32}'
```

## Component inventory

| Component | Version / image | Role |
|-----------|-----------------|------|
| Model | Qwen3.8-27B (hostPath `/data/models/Qwen3.8-27B`) | Served as `qwen3.8-27b` (+ `qwen3.8-27b-262k`) |
| vLLM | `quay.io/ascend/vllm-ascend:v0.23.0rc1` | Inference engine on Ascend NPUs (TP=4) |
| LiteLLM | `ghcr.io/berriai/litellm:main-stable` | API gateway: auth, model list, pass-throughs, metrics |
| kv-router | `ghcr.io/clyde-org/llm-la-icbc/kv-router:d0eec27a` (pinned) | KV/len-aware routing, pull-mode dispatch, context guard |
| kv-sidecar | `ghcr.io/clyde-org/llm-la-icbc/kv-sidecar:cc0b976d` (pinned) | Per-pod worker: pulls from router, feeds local vLLM |
| Redis | `redis:7-alpine` | Router queue state + backend discovery |
| Mooncake | ships in the vllm-ascend image | Cluster-wide KV-cache store (master + 128 GiB DRAM segments) |
| NPU Exporter | `npu-exporter:6.0.0` (DaemonSet) | Ascend hardware metrics |
| Prometheus | kube-prometheus-stack (retention 15 d) | Metrics collection |
| Grafana | 12.1.1 | Dashboards (pre-provisioned `sir-llm-platform-vllm`) |

> Router/sidecar images are **pinned by tag** (never `:latest`) — see
> [setup.md → Day-2 operations](setup.md#day-2-operations).

## Repository layout

```
sir-llm-platform/
├── README.md                  # short overview + quick start
├── DEPLOYMENT.md              # deployment deep-dive (Mooncake, values, troubleshooting)
├── docs/
│   ├── platform/              # ← this documentation
│   │   ├── README.md
│   │   ├── architecture.md
│   │   ├── setup.md
│   │   └── user-guide.md
│   └── pool-router-sidecar-changes.md
├── vllm-stack/                # Helm chart (the whole serving stack)
│   ├── values.yaml
│   └── templates/
│       ├── 01-configmap.yaml     # model-registry + litellm-config
│       ├── 02-redis.yaml
│       ├── 03-router.yaml
│       ├── 04-vllm.yaml          # phase-1 vLLM pods (+ kv-sidecar)
│       ├── 05-litellm.yaml
│       ├── 06-claude-service.yaml
│       ├── 07-podmonitors.yaml
│       ├── 08-mooncake.yaml
│       └── 09-vllm-phase2.yaml
├── monitoring/                # Prometheus values, NPU exporter, ServiceMonitors, dashboards
├── claude-code/               # Claude Code settings template
├── pi/                        # pi models.json + settings.json templates
├── deepseek-harness/          # minimal Python chat CLI + Open WebUI manifest
└── scripts/
    └── test-ctx-guard.py      # client-side overflow contract test
```
