# pi (pi-coding-agent) → Qwen3.8-27B

Use the [pi](https://pi.dev) CLI (`@earendil-works/pi-coding-agent`) against
the in-lab Qwen3.8-27B instead of a cloud provider. pi talks to LiteLLM
(NodePort 30400) over the OpenAI-compatible route — no `kubectl port-forward`
needed; any machine that can reach a node IP works directly.

## Quick start

```bash
npm install -g @earendil-works/pi-coding-agent

mkdir -p ~/.pi/agent
cp pi/models.json    ~/.pi/agent/models.json
cp pi/settings.json  ~/.pi/agent/settings.json
```

If `~/.pi/agent/models.json` already has other providers, **merge** the
`sirlab` entry instead of overwriting.

Verify:

```bash
pi --list-models qwen        # should list sirlab / qwen3.8-27b{,-131k,-262k}
pi -p "Reply with PONG"
```

Then just run `pi`. `models.json` is reloaded when you open `/model` in a
session (live-editable, no restart).

## Key values (and why stock defaults break)

| File | Field | Value | Why |
|------|-------|-------|-----|
| models.json | `baseUrl` | `http://<NODE_IP>:30400/v1` | any cluster node; LiteLLM, OpenAI-compatible route |
| models.json | `apiKey` | `sk-qwen38b-local` | shared lab master key |
| models.json | model ids | `qwen3.8-27b`, `…-131k`, `…-262k` | same weights, two pools (32G / 64G HBM); the pool is picked by the model name |
| models.json | `contextWindow` | `131072` / `262144` | the real vLLM limit per pool |
| models.json | `maxTokens` | `8192` | **do not raise** — pi's input wall is `contextWindow − maxTokens`; stock values put it at ~33k and long sessions die there |
| settings.json | `defaultModel` | `qwen3.8-27b-131k` | new sessions start on the 131k pool |
| settings.json | `defaultThinkingLevel` | `high` | Qwen3 thinking budget |
| settings.json | `compaction.reserveTokens` | `32768` | compaction triggers at 98,304 tokens — well before the 122,880 input wall |
| settings.json | `compaction.keepRecentTokens` | `20000` | recent context kept verbatim around the cut point |

With these values, sessions run to ~122k input tokens on the 131k pool (the
262k pool's wall is ~254k) and pi compacts cleanly instead of failing with
`prompt is too long`.

## Notes

- The endpoint is on the lab network with only the static LiteLLM key.
- If your workstation reaches the cluster only through a local HTTP proxy
  (`127.0.0.1:3128`), make sure the node IP is in `NO_PROXY` (direct access
  works best).
