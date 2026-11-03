# DeepSeek-V4.1-Flash-1017 on node3 + node4

Single instance of `deepseek-ai/DeepSeek-V4.1-Flash-1017` (763B MoE, 1M
context) spanning **node3 (head, DP rank 0)** + **node4 (worker, DP rank 1)**:
official vllm-ascend A2 recipe (v0.30.0rc2 tutorial §5.3) at **DP2/TP8/EP16**
instead of the tutorial's 4-server DP4/TP8/EP32 (we have 2 free 910B3 nodes
with room for the 510 GB checkpoint).

Plan & rationale: [`../docs/deepseek-v41-flash-plan.md`](../docs/deepseek-v41-flash-plan.md)

## Layout

| File | What |
|---|---|
| `01-dsv41-service.yaml` | ClusterIP `dsv41` → head `:8000` |
| `02-dsv41-head.yaml` | Deployment, node3, 8×910B3, hostNetwork, `vllm serve` (image ENTRYPOINT kept — it sources the CANN env; args are the serve command) |
| `03-dsv41-worker.yaml` | Deployment, node4, headless `--data-parallel-start-rank 1` |
| `boot.sh` | `apply-head / wait-dp / apply-worker / status / health / logs-head / logs-worker / down` |
| `build-oci-import.py` | Builds the OCI-layout tar from the fetched registry blobs and imports it on the nodes (`ctr -n k8s.io images import`) — nodes have no registry egress |
| `cutover-gateway.py` | Gateway cutover: `litellm` (model entry + forced rollout), `router` (ANTHROPIC_UPSTREAMS), `keys` (per-user litellm key restrictions + npu-ops applies.json), `canary` |

## Facts that matter

- **Image**: `quay.io/ascend/vllm-ascend:deepseek-v4.1-flash` (25.2 GB,
  2026-09-11, vLLM 0.27.1, CANN 9.1.0, `deepseek_v41` via vllm_ascend patches).
  `imagePullPolicy: Never` — it is imported by hand.
- **Weights**: `/data/models/DeepSeek-V4.1-Flash-1017` on **both** nodes
  (510.31 GB, downloaded 2026-10-26 from the Huawei internal HF mirror,
  per-file size-verified + sha256-verified).
- **Network**: cross-node DP/EP over the single active 10 Gbps RoCE
  (`enp189s0f0`, both nodes on 10.50.156.0/24); HCCL env pins
  `HCCL_IF_IP`/`HCCL_SOCKET_IFNAME`/`GLOO_SOCKET_IFNAME`/`TP_SOCKET_IFNAME`.
  40 Gbps ports exist but are Down.
- **Serve params** (official A2 recipe, DP4→DP2 only change): 1M context,
  `--quantization ascend` (W8A8/FP4 path), DSpark speculation
  (`num_speculative_tokens 5`), `block-size 128`, `deepseek_v41`
  tokenizer/reasoning/tool parsers, 512 GiB tmpfs,
  `VLLM_ENGINE_READY_TIMEOUT_S=3600`.
- **Gateway**: registered in litellm as a **direct upstream** (no kv-router —
  this pool has no kv-sidecar/KV events); vLLM's own max-model-len 400 covers
  overflow. `/v1/messages` via the router's `ANTHROPIC_UPSTREAMS` map.
- **No HA**: a node3/node4 NPU failure takes the whole engine down
  (recreate ≈ weight reload, ~10–20 min). Accepted for the POC phase.

## Operate

```bash
# first boot (image + weights already in place)
./boot.sh apply-head      # node3 starts, opens :8000 + :13399
./boot.sh wait-dp         # blocks until :13399 LISTEN on node3
./boot.sh apply-worker    # node4 joins the DP group
./boot.sh health          # /health on both IPs (200 = ready)
# restart
./boot.sh down && ./boot.sh apply-head && ./boot.sh wait-dp && ./boot.sh apply-worker
```

Log watching: `./boot.sh logs-head 200` / `./boot.sh logs-worker 200`.
First boot loads 475 GiB of weights over 16 ranks — expect ~10–20 min to
`/health` 200.

## Verify (post-boot)

```bash
# basic
curl -s http://10.50.156.65:8000/health && curl -s http://10.50.156.65:8000/v1/models
# PONG
curl -s http://10.50.156.65:8000/v1/chat/completions -H 'Content-Type: application/json' \
  -d '{"model":"deepseek-v4.1-flash","messages":[{"role":"user","content":"Reply with exactly: PONG"}],"max_tokens":32,"temperature":0}'
# KV capacity
curl -s http://10.50.156.65:8000/metrics | grep -E "num_gpu_blocks|kv_cache" | head
```

Then gateway: `python3 cutover-gateway.py all` and re-run the PONG through
`http://<node>:30400/v1/chat/completions` with the model name.
