# Claude Code → Qwen3.8-27B

Use the Claude Code CLI against the in-lab Qwen3.8-27B instead of a cloud
provider. Claude Code talks to LiteLLM (NodePort **30400**) over the Anthropic
`/v1/messages` pass-through; the pool is picked by the `model` name, so the
base URL is stable. No `kubectl port-forward` needed — any machine that can
reach a node IP on the lab network works directly.

## Quick start

```bash
# back up any existing file first
cp settings.qwen3-8b.json ~/.claude/settings.json
```

Or apply **per-project**: put the settings in the project's
`.claude/settings.json` (or `.claude/settings.local.json`), keeping your normal
provider at user level.

Verify: `claude -p "Reply with PONG"`

## What the template sets

| Setting | Value | Notes |
|---------|-------|-------|
| `ANTHROPIC_BASE_URL` | `http://<NODE_IP>:30400` | any cluster node, LiteLLM NodePort, Anthropic `/v1/messages` |
| `ANTHROPIC_MODEL` | `qwen3.8-27b` | default — 131k pool |
| `ANTHROPIC_DEFAULT_OPUS_MODEL` | `qwen3.8-27b` | "Default (recommended)" resolves to this var, so the default stays on 131k |
| `ANTHROPIC_DEFAULT_SONNET_MODEL` | `qwen3.8-27b-262k` | Sonnet tier → 262k pool (long-context work) |
| `ANTHROPIC_DEFAULT_HAIKU_MODEL` / `ANTHROPIC_SMALL_FAST_MODEL` | `qwen3.8-27b` | → 131k pool |
| `"model"` | `sonnet` | resolves to `ANTHROPIC_DEFAULT_SONNET_MODEL` → 262k pool (long-context default); pin `"qwen3.8-27b"` instead to stay on 131k |
| `ANTHROPIC_API_KEY` | `sk-qwen38b-local` | LiteLLM master key (**not** an Anthropic key) |
| `CLAUDE_CODE_DISABLE_1M_CONTEXT` | `1` | suppresses the spurious `[1m]` badge on custom endpoints |
| `CLAUDE_CODE_AUTO_COMPACT_WINDOW` | `131072` | **required** — see below |
| `CLAUDE_CODE_MAX_OUTPUT_TOKENS` | `8192` | matches the platform's per-request output budget |

The `/model` picker shows the real served names (pick by name, not tier):

```
Default (recommended)  qwen3.8-27b            (131k pool)
qwen3.8-27b           Custom Opus model      (131k pool)
qwen3.8-27b-262k      Custom Sonnet model    (262k pool)
qwen3.8-27b           Custom Haiku model     (131k pool)
```

## Why `CLAUDE_CODE_AUTO_COMPACT_WINDOW` is required

Claude Code doesn't know this model name, so it assumes a **200k** context
window and sets its autocompact trigger (~178.8k) *above* the model's 131072
hard limit — long sessions die with `API Error: 500 … maximum context length
is 131072` before compaction can fire. The template's `131072` puts the
trigger at ≈110k, safely under the limit on either pool.

- The default session (`"model": "sonnet"`) runs on the **262k** pool, where
  the 110k trigger is conservative but safe; raise the window to `240000` and
  restart the session (env is read at session start) to use the full 262k
  window before compaction.
- `CLAUDE_CODE_MAX_CONTEXT_TOKENS` does **not** fix this (only read when
  `DISABLE_COMPACT` is set, which disables compaction entirely).
- Don't "fix" it by raising vLLM `maxModelLen` — 131072 is the model's native
  limit.
- A session already past the limit can't be rescued by `/compact` — use
  `/clear` or restart.

## Notes

- The key is the shared lab master key, documented throughout this repo; the
  endpoint is on the lab network only.
- The template sets `NO_PROXY` to include the node IP (direct access). If your
  machine reaches the cluster only through a local proxy
  (`127.0.0.1:3128`), remove the node IP from `NO_PROXY`; with no proxy at
  all, drop the `*_PROXY` lines.
- **Fallback** if you can't reach NodePort 30400 at all:
  `kubectl -n sir-llm-platform port-forward svc/vllm-qwen3-8b-claude 8200:8200`
  and set `ANTHROPIC_BASE_URL=http://127.0.0.1:8200` (that Service has no
  auth — any non-empty key works; drop the proxy entries).
