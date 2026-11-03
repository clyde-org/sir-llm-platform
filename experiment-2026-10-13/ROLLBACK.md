# qwen3.8-27b experiment rollout — 2026-10-13

## What was added (temporary)

| Resource | Namespace | Notes |
|---|---|---|
| deployment `vllm-qwen3-8b-exp` (16 replicas) | sir-llm-platform | 2 pods/node × 8 nodes, TP4, `.216`/phase1 flavor: serves `qwen3.8-27b` @ 131k |
| service `vllm-qwen3-8b-exp` (ClusterIP 8200) | sir-llm-platform | selector app=vllm-qwen3-8b-exp |
| deployment `vllm-qwen3-8b-exp-b3` (2 replicas) | sir-llm-platform | node .107 (910B3, control-plane), B3 flavor: serves `qwen3.8-27b-262k`/`qwen3.8-27b-131k` @ 262k |
| service `vllm-qwen3-8b-exp-b3` (ClusterIP 8200) | sir-llm-platform | selector app=vllm-qwen3-8b-exp-b3 |
| node label `llm-pool=qwen-38b-exp` | 2eff03de-2..5, 62245bb7, b6af27bc, cfe7f2f1-0/1 | |
| node label `llm-pool=qwen-38b-exp-b3` | devserver-bms-2956fa59 (.107) | |

Pods auto-join the existing router (same ns redis; model names `qwen3.8-27b` / `qwen3.8-27b-262k`).

## What was removed/changed pre-existing

- `sir-llm-lmcache/vllm-qwen3-8b` deployment **scaled to 0** (was running 4 pods on 2eff03de-2..5, 8/8 NPU each).
  Restore with: `kubectl scale deploy vllm-qwen3-8b -n sir-llm-lmcache --replicas=4`
  (its redis/router/lmcache-coordinator pods were left running, unused)
- 2eff03de-2..5 label changed from `llm-pool=qwen-38b-lmcache` → `qwen-38b-exp` (use `--overwrite` to restore).
- **62245bb7 (7.242.97.161)**: killed host-level dummy workloads that occupied all 8 NPUs (not k8s):
  `vllm serve /workspace/qr/Qwen3.5-4B` (:8100), `vllm serve /mnt/gmem/models/Qwen3-8B` (:8000),
  2× `lmcache server`, hung `docker exec` sessions, `tee vllm.log`.
- **.107 (7.242.101.107)**: user deleted `vllm-ascend/vllm-glm45-air` (kubectl-applied) before rollout; gateway pod left running.
- **62245bb7 disk cleanup** (was 82% full, DiskPressure evicting pods): removed `/var/lib/docker` (120.8G, docker not installed/running) and `/path/to/your` (30.5G, stray Qwen3-8B copy incl. 15.3G .git). Disk now ~66% used.
- **62245bb7**: `/usr/local/Ascend/driver/version.info` was an empty *directory*; replaced with the 25.2.0.b099 file (copied content from b6af27bc) so hostPath File-type mounts work.
- Deleted finished `eg2-*` registry-test pods (default ns).

## Data added to nodes (copied from 2eff03de-0)

- 62245bb7, b6af27bc, .107: `/data/models/Qwen3.8-27B` (51.8G) — optional to remove after experiment
- 62245bb7, b6af27bc: images `quay.io/ascend/vllm-ascend:v0.23.0rc1` + `ghcr.io/clyde-org/llm-la-icbc/kv-sidecar:7f52ac0a`
- .107, cfe7f2f1-0/1: `kv-sidecar:7f52ac0a`; .107 also got `vllm-ascend:v0.23.0rc1`
- (cfe7f2f1-0/1 already had model + vllm image)

## Rollback

```bash
kubectl delete deploy,svc vllm-qwen3-8b-exp vllm-qwen3-8b-exp-b3 -n sir-llm-platform
kubectl label node devserver-bms-2eff03de-{2,3,4,5}.novalocal devserver-bms-62245bb7 devserver-bms-b6af27bc devserver-bms-cfe7f2f1-0.novalocal devserver-bms-cfe7f2f1-1.novalocal llm-pool-
kubectl label node devserver-bms-2eff03de-{2,3,4,5}.novalocal llm-pool=qwen-38b-lmcache --overwrite   # if restoring lmcache pool
kubectl label node devserver-bms-2956fa59.novalocal llm-pool-
kubectl scale deploy vllm-qwen3-8b -n sir-llm-lmcache --replicas=4    # if restoring lmcache pool
# optional: rm -rf /data/models/Qwen3.8-27B on 62245bb7, b6af27bc, .107 (via ssh or a hostPath probe pod)
```

Untouched: phase1 `vllm-qwen3-8b` (4 pods, 2eff03de-0/1), phase2 `vllm-qwen3-8b-p2` (4 pods, node3/4),
excluded nodes .243/.100/.87, and all user data under /root and /home.
