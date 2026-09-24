# Pi (pi-coding-agent) → Qwen3.8-27B (sir-llm-platform)

Use the [pi](https://pi.dev) CLI (`@earendil-works/pi-coding-agent`) against the
in-lab Qwen3.8-27B deployment instead of a cloud provider.

## How it connects

Pi talks to **LiteLLM** (NodePort 30400) over the OpenAI-compatible `/v1` route
(`api: openai-completions`). Unlike Claude Code — which needs the Anthropic
`/v1/messages` route served by the `-claude` Service — pi works fine through the
LiteLLM proxy, so **no `kubectl port-forward` is required**: any machine that can
reach a node IP can use it directly.

## Quick start

```bash
# 1. Install pi
npm install -g @earendil-works/pi-coding-agent

# 2. Copy the config templates (back up / merge any existing files first)
mkdir -p ~/.pi/agent
cp pi/models.json    ~/.pi/agent/models.json
cp pi/settings.json  ~/.pi/agent/settings.json
```

If `~/.pi/agent/models.json` already exists with other providers, do **not**
overwrite it — merge the `sirlab` entry from `pi/models.json` into the existing
`providers` object instead.

## Configuration files

Pi has no single config file. Endpoint definitions can only live in
`models.json` (pi does **not** honor `ANTHROPIC_BASE_URL` or similar env vars);
`settings.json` only selects defaults and UI/behavior options.

| File | Purpose |
|------|---------|
| `~/.pi/agent/models.json` | Provider + model definitions (required for this endpoint) |
| `~/.pi/agent/settings.json` | Default provider/model, theme, telemetry opt-out, compaction tuning |

### Key fields (`models.json`)

| Field | Value | Notes |
|-------|-------|-------|
| `baseUrl` | `http://NODE_IP:30400/v1` | LiteLLM NodePort, OpenAI-compatible route |
| `api` | `openai-completions` | Chat Completions streaming |
| `apiKey` | `sk-qwen38b-local` | LiteLLM master key, same as all other lab clients |
| `compat.supportsReasoningEffort` | `false` | Server does not accept a `reasoning_effort` parameter |
| `compat.thinkingFormat` | `qwen-chat-template` | Lets pi format thinking via the Qwen chat template |
| `contextWindow` | `131072` | Must equal the vLLM `--max-model-len` (128k native) |
| `maxTokens` | `8192` | Max output per request — **do not raise** (see Compaction below) |

## Compaction (required — do not use stock defaults)

vLLM rejects any request where `prompt + max_tokens > 131072`. Pi's *input wall*
is therefore `contextWindow − maxTokens`, and its compaction trigger is
`contextWindow − reserveTokens` (settings.json; default reserve = 16384). The
stock template values (`maxTokens: 98304`, default reserve) put the input wall at
**32,768 tokens — far below the compaction trigger (~114.7k)** — so long sessions
die at ~33k input tokens with:

```
[prompt too long] prompt is ~32769 tokens but the model supports only 131072
```

before compaction ever fires. This is the same class of bug as Claude Code's
autocompact misfire (see [claude-code/README.md](../claude-code/README.md)).

**Fix (in the templates in this directory):**

```jsonc
// models.json
"contextWindow": 131072,   // the real vLLM limit, not 128000
"maxTokens": 8192          // pushes the input wall up to 122,880

// settings.json
"compaction": {
  "enabled": true,
  "reserveTokens": 32768,  // trigger at 131072 - 32768 = 98,304 tokens
  "keepRecentTokens": 20000 // recent context kept verbatim around the cut point
}
```

Why these values:

- **Trigger 98,304** ≪ **input wall 122,880** — compaction fires long before any
  request can 500, even if a single turn adds ~24k tokens between checks.
- The compaction summary request itself (≈100k prompt + ≤8k output) fits under
  131,072.
- **`maxTokens: 8192`** caps the summary budget (`min(0.8 × reserveTokens,
  maxTokens) = 8192`) and keeps every normal request comfortably under the limit.
  Raising it (e.g. to 50k or the old 98,304) shrinks the input wall back down and
  reintroduces the wall — it does **not** give pi more output headroom in practice.

Verified 2026-09-18: under the old values, sessions repeatedly hit the
`~32,769 tokens` wall; under the fixed values, sessions run to ~122,881 input
tokens, pi compacts cleanly (structured "## Goal…" summaries in the session
logs), and no 500s occur.

## Verify

```bash
pi --list-models qwen        # should list sirlab / qwen3.8-27b
pi -p "Reply with PONG"      # non-interactive round-trip
```

Then just run `pi` — it starts on `sirlab/qwen3.8-27b` by default.

## Notes

- The endpoint is on the lab network with only the static LiteLLM key — treat
  `sk-qwen38b-local` like the other lab credentials in this repo.
- If your workstation reaches the cluster only through a local HTTP proxy
  (`127.0.0.1:3128`), make sure the node IP is routable for pi (adjust
  `NO_PROXY` as described in [claude-code/README.md](../claude-code/README.md)).
- `models.json` is reloaded when you open `/model` in a session — you can edit it
  live without restarting pi.
