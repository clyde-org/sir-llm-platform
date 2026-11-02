# SIR LLM Platform

Self-hosted LLM serving: **Qwen3.8-27B** and **DeepSeek-V4-Flash** on
**Huawei Ascend 910B NPUs** — **vLLM** + KV-aware router + **LiteLLM
gateway**, Prometheus/Grafana monitoring. Client how-tos:
[pi](pi/README.md) · [opencode](opencode/README.md) ·
[claude-code](claude-code/README.md) ·
[deepseek-harness](deepseek-harness/README.md) ·
[monitoring](monitoring/README.md).

## Models & pools

Same weights, two hardware pools. The `model` field in the request body picks
the pool — one base URL on both API paths.

| Model name | Pool | Context |
|------------|------|---------|
| `qwen3.8-27b` | phase-1 (4 pods × TP=4, 910B) | 131,072 — default |
| `qwen3.8-27b-262k` | phase-2 (910B3 64 GB HBM) | 262,144 — long context, off by default |
| `qwen3.8-27b-131k` | phase-1 alias | 131,072 — OpenAI path only |
| `dsv4` | DeepSeek-V4-Flash W8A8 (node3, 8×910B3 TP=8) | 133,120 — text only, MoE, MTP spec-decode |

`dsv4` is a single push-mode instance: it sits **outside the KV router**
(LiteLLM routes to it directly; the router's `/v1/messages` pass-through
reaches it via `ANTHROPIC_UPSTREAMS`). Deployment: [`dsv4f/`](dsv4f/) —
context is capped at 133,120 by the measured KV budget (195,036 tokens
total across the 8 NPUs; 1M needs ~630 GB of KV, which this hardware
doesn't have).

## Architecture

[**🖼 Architecture diagram — two deployments, one gateway**](docs/architecture.png)
(PNG; editable source: [docs/architecture.svg](docs/architecture.svg))

```mermaid
flowchart TD
    subgraph CLIENTS["Clients (lab network)"]
        OA["OpenAI SDK / curl<br/>POST /v1/chat/completions"]
        AN["Claude Code / Anthropic SDK<br/>POST /v1/messages"]
        GUI["Open WebUI (30401), Swagger UI (30400)"]
    end

    LT["LiteLLM proxy<br/>NodePort 30400 (only external port)"]

    RT["kv-router (ClusterIP)<br/>:8080 + results ZMQ :5559<br/>model-aware routing, context guard"]

    R[("Redis :6379<br/>queue state")]

    DSV4["DeepSeek-V4-Flash (node3)<br/>dsv4f pod, TP=8 :8000"]

    subgraph P1["Phase-1 pool · 131k<br/>qwen3.8-27b"]
        P1S["kv-sidecar (pull) :9000"]
        P1V["vLLM x4 pods, TP=4 :8200"]
        P1S --> P1V
    end

    subgraph P2["Phase-2 pool · 262k (when enabled)<br/>qwen3.8-27b-262k"]
        P2S["kv-sidecar (pull) :9000"]
        P2V["vLLM pods, TP=4 :8200"]
        P2S --> P2V
    end

    OA -->|"chat completions"| LT
    AN -->|"/v1/messages"| LT
    GUI --> LT
    LT -->|"via router"| RT
    LT -->|"Anthropic pass-through"| RT
    LT -->|"direct (model dsv4)"| DSV4
    AN -.->|"dsv4 via router pass-through"| DSV4
    RT <--> R
    RT <-->|"/pull, /result_submit"| P1S
    RT <-->|"/pull, /result_submit"| P2S
    P1V -.->|"KV events ZMQ :5557"| RT
    P2V -.->|"KV events ZMQ :5557"| RT
```

## Quick start

- **Base URL** — `http://<NODE_IP>:30400` (any cluster node IP works;
  replace `<NODE_IP>` throughout this guide and the config templates —
  lab network only, ask an admin for a node IP)
  - OpenAI (`/v1/chat/completions`): `http://<NODE_IP>:30400/v1`
  - Anthropic (`/v1/messages`): `http://<NODE_IP>:30400`
- **API key** — apply at `http://<NODE_IP>:30410/keys`, then contact
  **Sheriffo (s00628324)** to approve your key
- **Pool** — picked by the `model` field: `qwen3.8-27b` (131k, default) · `qwen3.8-27b-262k` (262k) · `dsv4` (DeepSeek-V4-Flash, 133k, text only)
- **Verify**:

```bash
curl -s http://<NODE_IP>:30400/v1/chat/completions \
  -H "Authorization: Bearer YOUR-API-KEY" -H "Content-Type: application/json" \
  -d '{"model":"qwen3.8-27b","messages":[{"role":"user","content":"Reply with PONG"}],"max_tokens":20}'
```

### pi

- **Setup** (merge the `sirlab` entry if `models.json` already has providers):

```bash
mkdir -p ~/.pi/agent
cp pi/models.json    ~/.pi/agent/models.json
cp pi/settings.json  ~/.pi/agent/settings.json
pi --list-models qwen      # should list sirlab / qwen3.8-27b
pi -p "Reply with PONG"    # verify
```

- **Full `~/.pi/agent/models.json`** (replace `<NODE_IP>` and `YOUR-API-KEY`):

```json
{
  "providers": {
    "sirlab": {
      "baseUrl": "http://<NODE_IP>:30400/v1",
      "api": "openai-completions",
      "apiKey": "YOUR-API-KEY",
      "compat": {
        "supportsReasoningEffort": false,
        "thinkingFormat": "qwen-chat-template"
      },
      "models": [
        {
          "id": "qwen3.8-27b",
          "name": "Qwen3.8 27B",
          "reasoning": true,
          "input": ["text", "image"],
          "contextWindow": 131072,
          "maxTokens": 8192
        },
        {
          "id": "qwen3.8-27b-131k",
          "name": "Qwen3.8 27B (131k context, 32G pool)",
          "reasoning": true,
          "input": ["text", "image"],
          "contextWindow": 131072,
          "maxTokens": 8192
        },
        {
          "id": "qwen3.8-27b-262k",
          "name": "Qwen3.8 27B (262k context, 64G pool)",
          "reasoning": true,
          "input": ["text", "image"],
          "contextWindow": 262144,
          "maxTokens": 8192
        },
        {
          "id": "dsv4",
          "name": "DeepSeek V4 Flash (W8A8, 133k context)",
          "reasoning": true,
          "input": ["text"],
          "contextWindow": 133120,
          "maxTokens": 16384,
          "compat": {
            "supportsReasoningEffort": false,
            "thinkingFormat": "chat-template",
            "chatTemplateKwargs": {
              "thinking": { "$var": "thinking.enabled" }
            }
          }
        }
      ]
    }
  }
}
```

- **Full `~/.pi/agent/settings.json`**:

```json
{
  "defaultProvider": "sirlab",
  "defaultModel": "qwen3.8-27b-131k",
  "enableInstallTelemetry": false,
  "theme": "dark",
  "quietStartup": true,
  "compaction": {
    "enabled": true,
    "reserveTokens": 32768,
    "keepRecentTokens": 20000
  },
  "defaultThinkingLevel": "high"
}
```

- don't raise `maxTokens: 8192` on the qwen models — pi's input wall is
  `contextWindow − maxTokens` (`dsv4` ships at 16384: its 133k window
  leaves an ~117k input wall)
- `dsv4` uses **per-model** `compat` (thinking via
  `chat_template_kwargs.thinking`): vLLM's DeepSeek template ignores the
  top-level `thinking` param and LiteLLM rejects it, so the provider-level
  `qwen-chat-template` format must stay untouched
- compaction triggers at ~98k → sessions run to ~122k input (262k pool:
  ~254k) and compact cleanly instead of failing with `prompt is too long`
- `models.json` is reloaded when you open `/model` — live-edit, no restart

### opencode

- **Setup** (replace `<NODE_IP>` and `YOUR-API-KEY`):

```bash
npm install -g opencode-ai      # or: curl -fsSL https://opencode.ai/install | bash
mkdir -p ~/.config/opencode
cp opencode/opencode.json ~/.config/opencode/opencode.json
opencode run "Reply with PONG"  # verify
```

- **Full `~/.config/opencode/opencode.json`** (replace `<NODE_IP>` and `YOUR-API-KEY`):

```json
{
  "$schema": "https://opencode.ai/config.json",
  "provider": {
    "sirlab": {
      "npm": "@ai-sdk/openai-compatible",
      "name": "SIR Lab",
      "options": {
        "baseURL": "http://<NODE_IP>:30400/v1",
        "apiKey": "YOUR-API-KEY"
      },
      "models": {
        "qwen3.8-27b": {
          "name": "Qwen3.8 27B",
          "limit": { "context": 131072, "output": 8192 }
        },
        "qwen3.8-27b-131k": {
          "name": "Qwen3.8 27B (131k context, 32G pool)",
          "limit": { "context": 131072, "output": 8192 }
        },
        "qwen3.8-27b-262k": {
          "name": "Qwen3.8 27B (262k context, 64G pool)",
          "limit": { "context": 262144, "output": 8192 }
        }
      }
    }
  },
  "model": "sirlab/qwen3.8-27b"
}
```

- don't raise `limit.output: 8192` — it's the platform's per-request
  output budget
- the pool is picked by the model name, same as pi: `qwen3.8-27b` /
  `qwen3.8-27b-131k` → 131k pool, `qwen3.8-27b-262k` → 262k pool
- `~/.config/opencode/opencode.json` is **global**; an `opencode.json` in a
  project root overrides it (e.g. pin the 262k pool per repo)
- opencode doesn't render Qwen's thinking stream for custom providers
  (final answer only — thinking still runs server-side)

### Claude Code

- **Setup** (back up any existing settings first; replace `<NODE_IP>` and `YOUR-API-KEY`):

```bash
cp claude-code/settings.qwen3-8b.json ~/.claude/settings.json
claude -p "Reply with PONG"                                     # verify
```

- **Full `~/.claude/settings.json`**:

```json
{
  "env": {
    "DISABLE_TELEMETRY": "1",
    "DISABLE_ERROR_REPORTING": "1",
    "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
    "MCP_TIMEOUT": "60000",
    "ANTHROPIC_API_KEY": "YOUR-API-KEY",
    "ANTHROPIC_BASE_URL": "http://<NODE_IP>:30400",
    "ANTHROPIC_MODEL": "qwen3.8-27b",
    "ANTHROPIC_DEFAULT_HAIKU_MODEL": "qwen3.8-27b",
    "ANTHROPIC_DEFAULT_SONNET_MODEL": "qwen3.8-27b-262k",
    "ANTHROPIC_DEFAULT_OPUS_MODEL": "qwen3.8-27b",
    "ANTHROPIC_SMALL_FAST_MODEL": "qwen3.8-27b",
    "CLAUDE_CODE_DISABLE_1M_CONTEXT": "1",
    "CLAUDE_CODE_AUTO_COMPACT_WINDOW": "131072",
    "CLAUDE_CODE_MAX_OUTPUT_TOKENS": "8192",
    "HTTP_PROXY": "http://127.0.0.1:3128",
    "HTTPS_PROXY": "http://127.0.0.1:3128",
    "http_proxy": "http://127.0.0.1:3128",
    "https_proxy": "http://127.0.0.1:3128",
    "NO_PROXY": "127.0.0.1,127.0.0.*,localhost,*.huawei.com,<NODE_IP>"
  },
  "model": "sonnet",
  "enabledPlugins": {
    "cc-demo-plugin@rtos-cc-marketplace": true
  },
  "outputStyle": "engineer-professional",
  "skipWebFetchPreflight": true,
  "theme": "dark"
}
```

- `CLAUDE_CODE_AUTO_COMPACT_WINDOW: 131072` is **required** — without it,
  long sessions die with `500 … maximum context length is 131072`; raise to
  `240000` to use the full 262k window (env read at session start)
- `"model": "sonnet"` pins the session to the 262k pool; everything else
  stays 131k (pin `"qwen3.8-27b"` to force 131k)
- `*_PROXY` lines + `enabledPlugins` are workstation-specific — drop if
  your machine reaches the cluster directly

## API keys (per-user)

Everyone gets their own LiteLLM **virtual key** — no more one shared lab key.
Managed in the **npu-ops** dashboard, **API keys** tab:
**`http://<NODE_IP>:30410/keys`**.

1. **Request** — fill in your Huawei ID / username (no spaces — it becomes the
   key's `user_id`), full name, team, and optionally restrict the key to
   specific models (`qwen3.8-27b`, `-131k`, `-262k`). No login needed.
2. **Approve** — an admin approves pending requests (⚙️ admin login, bottom
   right). On approval the key is generated in LiteLLM and shown **once** in
   a modal — copy it then; afterwards only its hash is kept.
3. **Use it** — wherever the shared key used to go: `Authorization: Bearer
   <key>` against `http://<NODE_IP>:30400/v1` (OpenAI) or `:30400`
   (Anthropic `/v1/messages`) — i.e. replace the key in
   `models.json` / `settings.json` / SDK clients.
4. **Track / revoke** — "track your keys" on the same page (by Huawei ID, no
   login) shows request counts, token counts and last active per key; an
   admin blocks a key with one click. Request submitted by mistake? Delete
   your own pending request from the same page (no login needed, pending
   only). Per-key usage is read straight from LiteLLM's Postgres. Full spec
   (auth model, password rotation, REST API):
   [`npu-ops/README.md`](npu-ops/README.md).

The master key is **admin/infra only** (LiteLLM admin UI, npu-ops key
management, monitoring scrapes) and must never be used in a client — it was
retired for client use on 2026-10-19; use your personal key from
`:30410/keys` above. It lives in the `litellm-config` ConfigMap plus the
`npu-ops-litellm` and `litellm-bearer-token` secrets — never in this repo.

## Who is sending requests (LiteLLM observability)

With per-user virtual keys (above), the LiteLLM admin UI attributes every
request to its owner. For shared/legacy clients on the master key, attribution
comes from source IP + user-agent, captured per request in two places:

- **Grafana** — `http://<NODE_IP>:30300/d/sir-llm-platform-vllm`, row
  **Client requests (LiteLLM gateway)**: rate by IP × agent, per-agent rate,
  requests by IP × status code, failures by client, 24h totals per client,
  token throughput per model. `client_ip` is the pod IP for in-cluster
  clients — map it back with `kubectl get pods -A -o wide | grep <ip>`.
- **CLI (no browser)** — `python3 scripts/track-requests.py [--since 2h | --tail]`
  summarizes the gateway's access log by client IP, with a live-tail mode.
- **LiteLLM admin UI** — `http://<NODE_IP>:30400/ui`, login `admin` /
  the shared master key. The **Requests** page shows every request with
  requester IP, user-agent, model, tokens, cost and status; **Spend** tracks
  totals. Backed by the Postgres deployed with the stack
  (`vllm-stack/templates/10-postgres.yaml`, static local PV on the
  control-plane node). One-time step before the first deploy: create
  `/data/litellm-db` on the control-plane node (local PVs don't create the
  path). Note: `allow_requests_on_db_unavailable: false` — while Postgres is
  down the gateway refuses LLM requests rather than serving untracked.

For services that must share a key, give each client/service its own
User-Agent; individual users should just use their own virtual key (above),
which is the cleanest attribution.

---

Internal use only — Clyde Org
