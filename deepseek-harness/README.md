# deepseek-chat — CLI chat harness

Minimal, stdlib-only (no pip installs) streaming chat harness for any
OpenAI-compatible endpoint. Preconfigured for this cluster's LiteLLM proxy
(namespace `sir-llm-platform`, NodePort 30400, model `qwen3.8-27b`).

> Note: the model actually served here is **Qwen3.8-27B** on Ascend NPUs, not a
> DeepSeek model — but the harness is endpoint-agnostic and also works against
> the real DeepSeek API (see below).

## Quick start

```bash
cd sir-llm-platform/deepseek-harness

./deepseek-chat                          # interactive REPL
./deepseek-chat -p "hello"               # one-shot
./deepseek-chat --system "You are terse." -p "hi"
```

Works from anywhere that can reach a cluster node (NodePort 30400).
From inside the cluster network instead:

```bash
kubectl port-forward -n sir-llm-platform svc/litellm-proxy 4000:4000 &
./deepseek-chat --url http://localhost:4000
```

## Pointing it at other backends

```bash
# real DeepSeek API
export DEEPSEEK_BASE_URL=https://api.deepseek.com
export DEEPSEEK_API_KEY=sk-...
export DEEPSEEK_MODEL=deepseek-chat
./deepseek-chat

# any other OpenAI-compatible server
./deepseek-chat --url http://other-host:8000 --key xxx -m my-model
```

## Flags & REPL commands

| Flag | Purpose |
|------|---------|
| `--url`, `--key`, `--model` | override endpoint / credentials / model |
| `-p, --prompt` | one-shot, no REPL |
| `-s, --system` | system prompt |
| `--no-stream` | non-streaming request |
| `--no-think` | start with reasoning display off |
| `--max-tokens`, `--temperature` | sampling params |
| `--extra-json '{...}'` | merge extra JSON into the request body |

REPL commands: `/help`, `/models`, `/model <name>`, `/think on|off`,
`/clear`, `/quit`.

`/think on` (the default for this model) shows the model's reasoning in dim
text; `off` also sends `enable_thinking: false` so Qwen answers without the
reasoning pass (faster, cheaper).

## Other GUI options

All of these work with the same OpenAI-compatible endpoint
(`http://<node-ip>:30400/v1`, key `sk-qwen38b-local`):

| GUI | Type | Notes |
|-----|------|-------|
| **LiteLLM Swagger UI** | browser, zero install | Already live at `http://7.242.101.107:30400/` — API playground, not a chat UI |
| **Open WebUI** | web (Docker) | Most popular self-hosted chat GUI; model picker, files, RAG |
| **LibreChat** | web (Docker compose) | Multi-provider, shares, agents |
| **NextChat** | web (single container) | Lightweight, also runs from a hosted page by pasting the base URL |
| **AnythingLLM** | desktop or server | RAG + workspaces, supports custom OpenAI endpoints |
| **LM Studio / GPT4All** | desktop | Point "custom OpenAI-compatible" setting at the proxy |

### Open WebUI (deployed in-cluster — NodePort 30401)

**Already running** — chat GUI at `http://<any-node-ip>:30401`
(e.g. `http://7.242.101.107:30401`). First account created becomes admin.

```bash
kubectl apply -f deepseek-harness/open-webui.yaml   # re-apply / restore
kubectl rollout status deploy/open-webui -n sir-llm-platform
```

Notes:
- Pinned to the `sir-llm-platform/gui-host=true` nodes (the `2eff03de-*` pool) via
  `nodeSelector` — the `7.150.x.x` tmp0306 nodes can't pull from ghcr.io, and this
  cluster's API server rejects `nodeAffinity` in pod specs (custom build).
- Uses `emptyDir` (no StorageClass on this cluster): pod recreation resets the
  admin account and chat history.
- `HF_HUB_OFFLINE=1` — pods have no egress; RAG embeddings disabled, chat is fine.
- **Registry CA fix** (why pulls were broken): cluster egress goes through Huawei's
  MITM gateway (`netentsec` via cntlm → `proxyhk.huawei.com`), whose signing CA
  ("Huawei Web Secure Internet Gateway CA V2") was missing from containerd's trust
  store. Fixed on all 6 GUI-pool nodes with:
  ```bash
  mkdir -p /etc/containerd/certs.d/ghcr.io
  cp /etc/pki/ca-trust/extracted/pem/tls-ca-bundle.pem /etc/containerd/certs.d/ghcr.io/ca.crt
  ```
  Run the same on any new node before expecting image pulls to work.

### Open WebUI (local docker, alternative)

```bash
docker run -d -p 3000:8080 \
  -e OPENAI_API_BASE_URL=http://7.242.101.107:30400/v1 \
  -e OPENAI_API_KEY=sk-qwen38b-local \
  -v open-webui:/app/backend/data \
  --name open-webui --restart unless-stopped \
  ghcr.io/open-webui/open-webui:main
# → http://localhost:3000  (pick qwen3.8-27b in the model dropdown)
```

Run this on a machine that can reach the node IP; if the Docker host can't
reach 7.242.101.107 directly, run a port-forward on that host first
(`kubectl port-forward -n sir-llm-platform svc/litellm-proxy 30400:4000`) and
use `http://host.docker.internal:30400/v1` (or `http://172.17.0.1:30400/v1`).

> Note for local docker pulls: this workstation's egress also goes through the
> same MITM gateway, and docker correctly rejects its self-signed CA. Either add
> the CA to `/etc/docker/certs.d/ghcr.io/ca.crt` (same bundle path as above) or
> pull on a cluster node (`ctr -n k8s.io images pull ...`) and transfer.

### LibreChat

```bash
git clone https://github.com/danny-avila/LibreChat && cd LibreChat
cp .env.example .env
docker compose up -d
# → http://localhost:3080 → Settings → Models → Add Endpoint
#   (Type: OpenAI, Base URL: http://7.242.101.107:30400/v1, Key: sk-qwen38b-local)
```

### NextChat

```bash
docker run -d -p 3005:3000 --name nextchat \
  ghcr.io/chat-next-web/chat-next-web:latest
# → http://localhost:3005 → Settings → API → paste base URL + key
```

### Already in this repo (coding-agent GUIs)

- **Claude Code**: copy `claude-code/settings.qwen3-8b.json` → uses `/v1/messages`
- **pi**: copy `pi/models.json` + `pi/settings.json` → see [pi/README.md](../pi/README.md)
