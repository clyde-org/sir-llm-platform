# DeepSeek-V4-Flash on node3 (single A2 node, 8× 910B3)

Serving recipe: **W8A8-MTP** weights (`Eco-Tech/DeepSeek-V4-Flash-w8a8-mtp`,
300.1 GB, ModelSlim) with `--quantization ascend`, the validated A2 recipe
from the vllm-ascend v0.30.0rc2 tutorial (TP8, block-size 128, int8 DSA
indexer KV, MTP spec-decode 1 token, 133,120 ctx). Served as model name
**`dsv4`**.

Full deployment history and root-cause notes:
[`../docs/deepseek-v41-flash-plan.md`](../docs/deepseek-v41-flash-plan.md).

## Files

| File | What |
|------|------|
| `03-dsv4f-w8a8-deploy.yaml` | **The deployment** (apply this): Deployment `dsv4f` on node3, image `quay.io/ascend/vllm-ascend:v0.30.0rc2-openeuler` (hand-imported via `ctr -n k8s.io images import`), model path `/data/models/DeepSeek-V4-Flash-w8a8-mtp`, served name `dsv4` |
| `02-dsv4f-service.yaml` | ClusterIP `dsv4f:8000` (hostNetwork pod also reachable at `node3:8000`) |
| `01-dsv4f-deploy.yaml` | **FP4 raw-weights variant — blocked** (CANN/driver `aclnnNpuFormatCast` on rc2/nightly-main; core import regressions in the 2026-10-26 `nightly-releases-v0.30.0` build). Kept for re-test when a good image lands; includes the boot-wrapper wiring (`boot.sh` + `fix-import.py` via ConfigMap `dsv4f-patch`) |
| `boot.sh` | Container-start wrapper for the nightly-releases build (no `vllm-ascend-entrypoint.sh` there; sources the toolkit env, applies `fix-import.py`, execs vllm) |
| `fix-import.py` | Idempotent patch for the nightly build's `current_platform` circular-import regression (vLLM core); never blocks boot |
| `diag.py` | Diagnostic `npu_format_cast` tracer used while root-causing the FP4 FormatCast failure (kept for future re-tests) |
| `dsv4f-files.txt` | Weight file list + expected byte sizes (W8A8: 82 entries; office download dir `/mnt/data/w8a8-dl/`) |

## Boot (weights already on node3)

```bash
kubectl apply -f 02-dsv4f-service.yaml
kubectl apply -f 03-dsv4f-w8a8-deploy.yaml
kubectl wait --for=condition=Ready pod -l app=dsv4f -n sir-llm-platform --timeout=3600s
```

Sanity: `curl -s http://<node3-ip>:8000/health`, `/v1/models`, canary PONG.

## Gateway

`dsv4` is registered in `litellm-config` (direct `http://dsv4f:8000/v1`,
bypasses the KV router — single push-mode instance, no kv-sidecar), in
`model-registry` (router `/v1/messages` model resolution), and in the
router's `ANTHROPIC_UPSTREAMS` env (`{"dsv4": "http://dsv4f:8000"}`).

## Context window

133,120 is the validated A2 number and fits the measured KV budget
(195,036 tokens across 8 NPUs ≈ 1.47× concurrency at 133k). The model's
1M claim needs ~630 GB of KV — not possible on 8× 64 GB; see plan doc §12–13.
