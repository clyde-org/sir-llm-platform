# Claude Code → Qwen3.8-27B (sir-llm-platform)

Use Claude Code CLI against the in-lab Qwen3.8-27B deployment instead of a cloud provider.

## How it connects

Claude Code talks to **LiteLLM** (NodePort **30400**) over its Anthropic
`/v1/messages` pass-through route. LiteLLM forwards those requests to the
**kv-router's model-aware `/v1/messages` endpoint**, which picks the serving
pool from the request body's `model` field and raw-streams the request to that
pool's vLLM (vLLM serves the Messages API natively) — the same model-name
selection as the OpenAI `/v1/chat/completions` path (see
[DEPLOYMENT.md](../DEPLOYMENT.md)). The base URL is therefore **stable**; the
model (and its context pool) is chosen by name, exactly like Pi's config.
**No `kubectl port-forward` required** — any machine that can reach a node IP
on the lab network can use it directly.

> **Migrating from the old `/262k` URL:** settings with
> `ANTHROPIC_BASE_URL=http://<node>:30400/262k` keep working (the static
> pass-through is retained), but that bakes the pool into the URL. Drop the
> `/262k` suffix and select the pool via the model name instead
> (`qwen3.8-27b-262k`, see below).

If a machine cannot reach NodePort 30400, an alternative is to port-forward the
`-claude` Service directly (see "Direct `-claude` Service" below).

## Quick start

```bash
# Copy the template into your user-level settings (back up any existing file first)
cp settings.qwen3-8b.json ~/.claude/settings.json
```

Or apply **per-project** by placing the settings in the project's `.claude/settings.json`
(or `.claude/settings.local.json`), leaving `~/.claude/settings.json` for your normal provider.

## What it points at

| Setting | Value | Notes |
|---------|-------|-------|
| `ANTHROPIC_BASE_URL` | `http://7.242.101.107:30400` | LiteLLM NodePort, Anthropic `/v1/messages` (model-aware via kv-router) |
| `ANTHROPIC_MODEL` | `qwen3.8-27b` | Served model name (see Model selection) |
| `ANTHROPIC_DEFAULT_*` | `qwen3.8-27b` | All Claude tiers (opus/sonnet/haiku) remap here |
| `ANTHROPIC_API_KEY` | `sk-qwen38b-local` | LiteLLM master key (required on the 30400 route) |
| `CLAUDE_CODE_AUTO_COMPACT_WINDOW` | `131072` | Must match the selected pool's limit (see below) |
| `CLAUDE_CODE_MAX_OUTPUT_TOKENS` | `8192` | Matches vLLM's per-request output budget |

## Model selection (131k vs 262k pool)

The same Qwen3.8-27B weights are served by two pools under different model
names; the router resolves the name from the request body:

| Model name | Pool | Context limit |
|------------|------|---------------|
| `qwen3.8-27b` | phase-1 (8× 910B nodes) | 131072 |
| `qwen3.8-27b-262k` | phase-2 (910B3 64GB HBM nodes) | 262144 |
| `qwen3.8-27b-131k` | phase-1 (alias) — **OpenAI path only** | 131072 |

Note: on the Anthropic path (`/v1/messages`) use the canonical names above.
The router forwards the request body verbatim and vLLM validates the model
name, so the `-131k` alias 404s there — on the OpenAI path LiteLLM rewrites
the alias to `qwen3.8-27b` before it reaches the router, which is why it
works only via `/v1/chat/completions`.

Pick per tier in the settings `env` block, e.g. short-context tiers on the 131k
pool and long-context work on the 262k pool:

```json
"ANTHROPIC_MODEL": "qwen3.8-27b-262k",
"ANTHROPIC_DEFAULT_HAIKU_MODEL": "qwen3.8-27b",
"ANTHROPIC_DEFAULT_SONNET_MODEL": "qwen3.8-27b",
"ANTHROPIC_DEFAULT_OPUS_MODEL": "qwen3.8-27b-262k",
"ANTHROPIC_SMALL_FAST_MODEL": "qwen3.8-27b"
```

**`CLAUDE_CODE_AUTO_COMPACT_WINDOW` must match the pool the session's
`ANTHROPIC_MODEL` uses:** `131072` for the 131k pool, `240000` for the 262k
pool (window minus output buffer must stay under the pool's hard limit). The
template ships with the 131k values; when pointing `ANTHROPIC_MODEL` at the
262k pool, raise it accordingly and restart the session.

## Important

- **The API key is NOT an Anthropic key.** `sk-qwen38b-local` is the LiteLLM
  master key shared by all lab clients (it is documented throughout this repo).
  The direct `-claude` Service route (below) has no auth and accepts any
  non-empty key.
- The endpoint is protected only by that static lab key on the lab network —
  anyone who knows it can call the model. Add a gateway/oauth proxy in front if
  this needs to be locked down.
- The template sets `NO_PROXY=... ,7.242.101.107`, i.e. the node IP is reached
  **directly**, which is what works for lab workstations. If your machine can
  only reach the cluster *through* a local HTTP(S) proxy (`127.0.0.1:3128`),
  remove the node IP from `NO_PROXY` so requests route through the proxy
  instead; if you have direct access and no local proxy, you can drop the
  `*_PROXY` lines entirely.

## Context window & autocompact (required)

The served model name (`qwen3-8b` / `qwen3.8-27b`) is not in Claude Code's
internal model registry, so Claude Code **assumes a 200k-token context window**.
Its autocompact trigger is derived from that window (window − max output −
buffer ≈ 178.8k), which sits **above** this deployment's hard limit of 128k
(131072 tokens). Long sessions therefore die with:

```
API Error: 500 ... maximum context length is 131072
```

before autocompact ever gets a chance to fire.

**Fix:** set `CLAUDE_CODE_AUTO_COMPACT_WINDOW=131072` in the `env` block —
`settings.qwen3-8b.json` already includes it. This is the largest window that
still leaves the trigger safely below the server's hard limit:

- Autocompact trigger ≈ `131072 − maxOutput(8192) − buffer(~13k)` ≈ **109.9k**
  tokens.
- The worst request sent at the trigger ≈ 109.9k prompt + 8.2k output ≈
  **118.1k** — still ~13k under 131,072, and the compaction request itself
  fits comfortably.
- Verified 2026-09-18 (initially at `100000`): with long-context usage,
  autocompact fired automatically at 71,117 tokens and compacted cleanly down
  to ~3.5k, with no 500. Raising the window to 131072 only moves the trigger
  up to ≈110k, with the same margin — less frequent compaction, more usable
  context. The env is read at session start, so **restart the session** after
  changing the value.

Equivalent ways to set it without editing the file:

- `/autocompact 131072` in the REPL (persists to user settings)
- `--autocompact` CLI flag

Notes:

- `CLAUDE_CODE_MAX_CONTEXT_TOKENS` does **not** fix this — Claude Code only
  reads it when `DISABLE_COMPACT` is truthy, which *disables* autocompact
  entirely.
- Do not "fix" it by raising vLLM `maxModelLen`; 131072 is the model's native
  context limit.
- A session already past ~122k input tokens cannot be rescued with `/compact`
  (compaction re-sends the whole conversation and 500s the same way) — use
  `/clear` or restart the session.

## Verify

```bash
claude -p "Reply with PONG" --model opus
```

## Alternative: direct `-claude` Service (port-forward only)

If your machine cannot reach NodePort 30400 at all, you can bypass LiteLLM and
talk to vLLM's Messages API directly:

```bash
# keep this running in a shell
kubectl -n sir-llm-platform port-forward svc/vllm-qwen3-8b-claude 8200:8200
```

Then set `ANTHROPIC_BASE_URL=http://127.0.0.1:8200` in the settings. That
Service has **no auth** — any non-empty `ANTHROPIC_API_KEY` works — and no
`*_PROXY`/`NO_PROXY` entries are needed. Everything else (model name,
`CLAUDE_CODE_AUTO_COMPACT_WINDOW`, `CLAUDE_CODE_MAX_OUTPUT_TOKENS`) stays the
same.
