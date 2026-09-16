# Claude Code → Qwen3-8B (sir-llm-platform)

Use Claude Code CLI against the in-lab Qwen3.8-27B deployment instead of a cloud provider.

## How it connects

Claude Code talks to the **`vllm-qwen3-8b-claude`** Service in the
`sir-llm-platform` namespace, which fronts the vLLM pods and exposes the Anthropic
`/v1/messages` endpoint. The Service is **ClusterIP** (port 8200) — it has **no
NodePort**, so a workstation must reach it via `kubectl port-forward` (or by
running Claude Code on a cluster node and using the ClusterIP directly).

## Quick start

```bash
# 1. Forward the -claude service to a local port (keep this running in a shell)
kubectl -n sir-llm-platform port-forward svc/vllm-qwen3-8b-claude 8200:8200

# 2. Copy the template into your user-level settings (back up any existing file first)
cp settings.qwen3-8b.json ~/.claude/settings.json
```

Or apply **per-project** by placing the settings in the project's `.claude/settings.json`
(or `.claude/settings.local.json`), leaving `~/.claude/settings.json` for your normal provider.

## What it points at

| Setting | Value | Notes |
|---------|-------|-------|
| `ANTHROPIC_BASE_URL` | `http://127.0.0.1:8200` | `vllm-qwen3-8b-claude` (ClusterIP) via port-forward, Anthropic `/v1/messages` endpoint |
| `ANTHROPIC_MODEL` | `qwen3-8b` | Served model name |
| `ANTHROPIC_DEFAULT_*` | `qwen3-8b` | All Claude tiers (opus/sonnet/haiku) remap here |
| `ANTHROPIC_API_KEY` | any non-empty string | Endpoint has **no auth** — placeholder only |

If you run Claude Code on a cluster node, point `ANTHROPIC_BASE_URL` at the
ClusterIP instead, e.g. `http://10.107.184.214:8200` (verify with
`kubectl -n sir-llm-platform get svc vllm-qwen3-8b-claude`).

## Important

- **The API key is NOT an Anthropic key.** The `-claude` endpoint requires no
  authentication; any non-empty value works. Claude Code only needs the env var set.
- The endpoint is **open/unauthenticated** on the lab network. Anyone who can reach
  the ClusterIP / forwarded port can call the model. Add a gateway/oauth proxy in
  front if this needs to be locked down.
- Keep the cluster node IPs (`7.242.101.107`, ...) **out** of `NO_PROXY`. On machines
  that can only reach the cluster through a local HTTP(S) proxy (`127.0.0.1:3128`),
  requests to those IPs must route *through* the proxy; if `NO_PROXY` lists them the
  connection is made directly and times out. `NO_PROXY` should keep only the usual
  bypasses: `127.0.0.1,127.0.0.*,localhost,*.huawei.com`.

## Verify

```bash
claude -p "Reply with PONG" --model opus
```

## Known limitation

LiteLLM (`:30400`) does **not** serve a working Anthropic `/v1/messages` route for
this model, so Claude Code must use the direct `-claude` Service (port 8200, via
port-forward), not the LiteLLM proxy.
