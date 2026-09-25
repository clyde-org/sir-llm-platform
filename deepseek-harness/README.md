# deepseek-chat — CLI chat harness

Minimal, stdlib-only (no pip installs) streaming chat harness for any
OpenAI-compatible endpoint. Preconfigured for this platform's LiteLLM proxy
(NodePort 30400, model `qwen3.8-27b`).

> Note: the model served here is **Qwen3.8-27B** on Ascend NPUs, not DeepSeek —
> but the harness is endpoint-agnostic and also works against the real
> DeepSeek API or any other OpenAI-compatible server.

## Quick start

```bash
cd sir-llm-platform/deepseek-harness

./deepseek-chat                          # interactive REPL
./deepseek-chat -p "hello"               # one-shot
./deepseek-chat --system "You are terse." -p "hi"
```

Works from anywhere that can reach a cluster node (NodePort 30400). Flags:
`--url`, `--key`, `--model`, `-p/--prompt`, `-s/--system`, `--no-stream`,
`--no-think`, `--max-tokens`, `--temperature`, `--extra-json '{...}'`.

REPL commands: `/help`, `/models`, `/model <name>`, `/think on|off`, `/clear`,
`/quit`. `/think off` also sends `enable_thinking: false` so Qwen answers
without the reasoning pass (faster).

Point it elsewhere:

```bash
# real DeepSeek API
export DEEPSEEK_BASE_URL=https://api.deepseek.com DEEPSEEK_API_KEY=sk-... DEEPSEEK_MODEL=deepseek-chat
./deepseek-chat

# any other OpenAI-compatible server
./deepseek-chat --url http://other-host:8000 --key xxx -m my-model
```

## Open WebUI (already deployed — NodePort 30401)

Chat GUI at `http://<any-node-ip>:30401`; first account created becomes admin.

```bash
kubectl apply -f open-webui.yaml                        # re-apply / restore
kubectl rollout status deploy/open-webui -n sir-llm-platform
```

Notes: `emptyDir` storage (pod recreation resets the admin account and
history); RAG embeddings disabled (no egress) — chat is unaffected. If a new
node can't pull from ghcr.io, add the egress-gateway CA:
`cp /etc/pki/ca-trust/extracted/pem/tls-ca-bundle.pem /etc/containerd/certs.d/ghcr.io/ca.crt`.

## Other GUI options

Any OpenAI-compatible client works: base URL `http://<NODE_IP>:30400/v1`, key
`sk-qwen38b-local`.

| GUI | Type | Notes |
|-----|------|-------|
| LiteLLM Swagger UI | browser, zero install | `http://<NODE_IP>:30400/` — API playground, not a chat UI |
| Open WebUI | web (Docker) | most popular self-hosted chat GUI; model picker, files, RAG |
| LibreChat | web (Docker compose) | multi-provider, shares, agents |
| NextChat | web (single container) | lightweight; paste base URL + key in settings |
| AnythingLLM / LM Studio / GPT4All | desktop / server | point "custom OpenAI-compatible" setting at the proxy |

Coding-agent GUIs in this repo: **Claude Code**
([claude-code/](../claude-code/README.md)) and **pi**
([pi/](../pi/README.md)).
