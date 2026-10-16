# opencode → Qwen3.8-27B

Use the [opencode](https://opencode.ai) CLI against the in-lab Qwen3.8-27B
instead of a cloud provider. opencode talks to LiteLLM (NodePort 30400) over
the OpenAI-compatible route — same gateway, same per-user key as every other
client.

## Quick start

```bash
npm install -g opencode-ai        # or: curl -fsSL https://opencode.ai/install | bash

mkdir -p ~/.config/opencode
cp opencode/opencode.json ~/.config/opencode/opencode.json
```

Then in `~/.config/opencode/opencode.json` replace **`<NODE_IP>`** (any
cluster node IP — lab network, ask an admin) and **`YOUR-API-KEY`** (request
one at `http://<NODE_IP>:30410/keys`, admin approves). Use your personal key
so requests are attributed to you.

Verify:

```bash
opencode run "Reply with PONG"                    # 131k pool (default)
opencode run -m sirlab/qwen3.8-27b-262k "Reply with PONG"   # 262k pool
```

Then just run `opencode` in your project for the TUI. Model picker:
`sirlab / qwen3.8-27b{,-131k,-262k}`.

## Key values (and why stock defaults break)

| File | Field | Value | Why |
|------|-------|-------|-----|
| opencode.json | `provider.sirlab.npm` | `@ai-sdk/openai-compatible` | opencode drives custom providers through AI SDK packages; LiteLLM serves the OpenAI route |
| opencode.json | `options.baseURL` | `http://<NODE_IP>:30400/v1` | any cluster node; LiteLLM, OpenAI-compatible route |
| opencode.json | `options.apiKey` | your key from `http://<NODE_IP>:30410/keys` | per-user LiteLLM virtual key (**not** an OpenAI key); the shared lab master key also works |
| opencode.json | `models.*.limit.context` | `131072` / `262144` | the real vLLM limit per pool |
| opencode.json | `models.*.limit.output` | `8192` | **do not raise** — matches the platform's per-request output budget |
| opencode.json | `model` | `sirlab/qwen3.8-27b` | new sessions start on the 131k pool |

## Notes

- The pool is picked by the **model name**, exactly like pi and Claude Code:
  `qwen3.8-27b` / `qwen3.8-27b-131k` → 131k pool, `qwen3.8-27b-262k` → 262k
  pool. One base URL for both.
- `~/.config/opencode/opencode.json` is the **global** config; an
  `opencode.json` in a project root overrides it for that project (e.g. pin a
  different default model per repo).
- opencode installs the AI SDK provider package on first run (needs egress to
  the npm registry — same as `npm install -g opencode-ai`).
- If your workstation reaches the cluster only through the local HTTP proxy
  (`127.0.0.1:3128`), make sure the node IP is in `NO_PROXY` (direct access
  works best).
- Reasoning: the model's thinking stream is served as the non-standard
  `reasoning_content` field; opencode does not render it for custom
  providers, so you only see the final answer (thinking still runs server-side).
