# NPU Fleet Dashboard — Plan

> **STATUS: DEPLOYED (2026-10-08)** — see `npu-ops/README.md` for operations.
> All milestones done: agent DS on 16 nodes, dashboard on NodePort 30410,
> dummy start/stop verified end-to-end on `.118` (22 GiB → 233 GiB → 22 GiB),
> owners labelled (sheriffo / vllm-ascend / lmcache / presentation* / metrics-experiments).
> Known follow-ups: exporter `containerMode=docker` pod attribution still broken
> (we bypass it via K8s annotations/env); `latest` tag must never be used
> (containerd caches it) — `build.sh` pins content-hash tags.

Goal: a simple card-based dashboard to see, per server (node), what is running on its NPUs,
who owns it (real workload vs dummy load vs free), with one-click **Load NPUs / Stop**
buttons and **auto-load** of dummy load after N hours of idleness.

## 1. Findings from the current cluster

| Fact | Evidence |
|---|---|
| 16 nodes, all aarch64, each with **8x Ascend 910B** (`huawei.com/Ascend910=8` in capacity) | `kubectl get nodes` |
| 13 nodes labelled `sir.huawei.com/vllm-node=true` run the existing **npu-exporter** DaemonSet (Prometheus metrics at `node-IP:8082/metrics`, hostNetwork, privileged, hostPID) | `monitoring/npu-exporter.yaml` |
| 3 nodes have no exporter/label: `devserver-bms-6c1da567`, `b11214d6-tmp0306`, `d9a5373f-tmp0306` | node labels |
| Exporter gives per-device: `npu_chip_info_utilization`, `npu_chip_info_hbm_used_memory`, `npu_chip_info_hbm_total_memory`, `npu_chip_info_process_info{process_id=...}` (HBM used by the process) | scraped metrics |
| Exporter's `pod_name`/`container_name` attribution is **empty** (`-containerMode=docker` but cluster runs containerd) — not usable for ownership | metrics |
| Device→pod ownership IS available via pod annotation **`huawei.com/AscendReal: "Ascend910-0,Ascend910-1,..."`** set by the Ascend device plugin | vllm pods |
| Workloads live in **multiple namespaces**: `sir-llm-platform` (vllm-qwen3-8b, vllm-qwen3-8b-p2), `vllm-ascend` (vllm-glm45-air on control-plane node), `sir-llm-lmcache` (vllm-qwen3-8b on 2eff03de-2), plus infra (litellm, open-webui, postgres, redis, router) | `kubectl get pods -A -o wide` |
| Dummy load = **`/root/npu_mem_load.py`** (found on `.118`): ctypes → `libascendcl.so`, allocates ~90% HBM (26.3 GiB of 29.5 GiB) on **all 8 NPUs** and holds forever; releases on SIGTERM; writes `/root/npu_mem_load.pid`, logs to `/root/npu_mem_load.log`. **Not currently running** on .118 (stale pid file, ~2.8 GiB/NPU baseline in metrics). It is a **host-level process, invisible to the K8s device plugin** — a vLLM pod could be scheduled on the same device and OOM. | probe pod + script source |
| Live state at survey time: node3/node4 + control-plane = real vLLM (~55 GiB/NPU); 2eff03de-0/1 = vllm-qwen3-8b (~30 GiB/NPU); **2eff03de-2..5 have ~16.7 GiB/NPU processes with NO k8s pod owning them and 0% util = orphan/unmanaged dummy load**; 62245bb7, cfe7f2f1-0/1, b6af27bc = free (baseline only) | metric scan |
| No ingress in cluster; other user-facing services use **NodePort** (30400, 30401) | `kubectl get svc` |
| Exporter pods restart a lot (e.g. 950 restarts on control-plane) — treat exporter as best-effort | pod list |

## 2. Architecture

```
                browser (node-IP:30410 or kubectl port-forward)
                              │
                 ┌────────────▼────────────┐
                 │  npu-dashboard (Deploy) │  FastAPI, ns npu-ops
                 │  - serves static UI     │
                 │  - /api/state aggregate │
                 │  - /api/nodes/{n}/dummy │
                 │  - auto-load loop (60s) │
                 └──┬──────────┬───────────┘
        K8s API │    │ HTTP node-IP:8082/metrics      │ HTTP node-IP:8083 (agent)
   (SA: nodes,  │    │  npu-exporter (existing DS)    │
    pods R)     │    │  per-device util/HBM/procs     │
        │       └────┴────────────┬──────────────────┘
     nodes + pods (all ns)        │
                     ┌────────────▼────────────┐
                     │ npu-node-agent (DaemonSet)│  privileged, hostPID, hostNetwork
                     │ GET  /state  → dummy proc, script present |
                     │ POST /dummy/start | /dummy/stop (chroot host python3)
                     └─────────────────────────┘
```

Why this split:
- **Read path (NPU state)** reuses the already-running, already-battle-tested exporter — no new
  metrics plumbing needed.
- **Action path (dummy start/stop)** needs host access (the dummy load is a host process), so a
  small privileged DaemonSet does it. This also *formalizes* what is currently manual SSH + kill.
- Ownership is computed from the K8s API (pod annotations), not from the broken exporter
  `pod_name` field.

## 3. Components

### 3.1 `npu-node-agent` — DaemonSet (ns `npu-ops`)
- nodeSelector: `accelerator=huawei-Ascend910` (covers all 16 nodes incl. the 3 unlabeled ones).
- `privileged: true`, `hostPID: true`, `hostNetwork: true`, port **8083** (same pattern as exporter).
- Bundles `npu_mem_load.py` (ConfigMap). If `/root/npu_mem_load.py` is missing on a node, it
  installs it (so Load works on every node, not just the ones where it was copied by hand).
- Endpoints (JSON, no auth — same trust domain as exporter; NodePort not exposed):
  - `GET /state` → `{node, dummy: {running, pid, started_at, managed}, script_present, host_python_ok, ts}`
    (detects dummy via pidfile + `pgrep -f npu_mem_load`, distinguishes agent-managed vs orphan).
  - `POST /dummy/start` → refuse if already running; else
    `chroot /host /usr/bin/python3 /root/npu_mem_load.py --pct 90 >> /root/npu_mem_load.log 2>&1 &`
    (chroot so host libs resolve), wait ≤60s for HBM to rise (polls exporter localhost:8082),
    returns ok + allocated GiB.
  - `POST /dummy/stop` → SIGTERM pid (script frees memory on SIGTERM), wait ≤60s for HBM to
    fall to baseline, remove pidfile. Works on **orphan** processes too (same pidfile/pgrep).
- Small Python stdlib-only server (no deps) → tiny image (e.g. distroless/static or python:slim).

### 3.2 `npu-dashboard` backend — Deployment 1 replica (ns `npu-ops`)
FastAPI + `kubernetes` client, in-cluster SA with RBAC: `nodes` get/list, `pods` get/list
(all namespaces — read only).

- `GET /api/state` → one JSON doc for the UI:
  - per node: name, internal IP, role, npu count (capacity), agent reachable?, exporter reachable?
  - per NPU device: `util%`, `hbm_used/total MB`, `state` ∈ {FREE, BUSY, DUMMY, ORPHAN}, owner pod
    (`ns/pod` from `huawei.com/AscendReal` device match), owner user (pod label, see 3.4).
  - per node card summary:
    - `status` ∈ **FREE** (all free) / **DUMMY** (only dummy) / **BUSY** (only real) / **MIXED** /
      **ORPHAN** (unmanaged host process) / **UNKNOWN** (no exporter/agent)
    - `owners`: list of owner labels of NPU pods on the node (e.g. `sheriffo`, `lmcache`)
    - `free_since`, `auto_load_eta` ("loads dummy in 1h12m"), `dummy: {running, managed}`
  - busy definition per device: `util > 5%` for ≥2 consecutive samples **or**
    `hbm_used > baseline + 4 GiB` **or** an NPU pod allocated on the device.
- `POST /api/nodes/{name}/dummy/start` → guard: refuse if any real NPU pod is Running on the node
  (would OOM); else proxy to agent.
- `POST /api/nodes/{name}/dummy/stop` → proxy to agent (always allowed).
- Action log: keep last 50 actions in memory, rendered on the card ("stopped dummy 12:03 by UI").
- **Auto-load controller** (async loop, 60s tick):
  - track `last_busy_ts` per node (in-memory; on (re)start, nodes currently free get
    `last_busy_ts = now` — no surprise loads right after deploy).
  - if node free for ≥ `AUTOLOAD_AFTER_HOURS` (env, default **4h**) and dummy not running →
    `dummy/start` (respecting the same guards as manual).
  - if a real NPU pod appears on a node where dummy is running → `dummy/stop` immediately
    (protects real workloads from HBM contention).
  - per-node opt-out: node label `npu-ops.io/auto-load: "false"`.
- `GET /` serves the static UI.

### 3.3 Frontend — single static `index.html` (vanilla JS, no build)
- Header: cluster summary chips (e.g. `5 BUSY · 3 DUMMY · 4 FREE · 2 ORPHAN · 1 UNKNOWN`) +
  filters (all/free/busy/dummy/orphan) + search box + 10s auto-refresh + "auto-load: 4h" indicator.
- One card per node:
  - title: node name + IP + role; big status badge (FREE green / DUMMY amber / BUSY blue /
    ORPHAN red / UNKNOWN grey); **owner chips** (user names, or `dummy`, or `?` if unlabeled).
  - NPU grid: 8 mini-cells, each with util bar + HBM `x/y GiB`; color by state.
  - pod list: NPU pods on the node (`ns/pod`, devices, owner chip, age).
  - meta row: `free for 5h12m`, `auto-load in 38m` (or `auto-load off`), dummy age.
  - buttons: **[ Load NPUs ]** (enabled when FREE/ORPHAN… ORPHAN → "Take over" = stop orphan then
    start managed? see open Q4) and **[ Stop dummy ]** (enabled when dummy running). Confirm
    dialog on stop.
- All data from `GET /api/state`; buttons call the two POST endpoints and re-fetch.

### 3.4 Ownership convention
- Pod label **`platform/owner: <name>`** on anything that consumes NPUs (vllm deployments etc.).
- Infra pods (litellm, open-webui, postgres, redis, router, lmcache-coordinator…) get
  `platform/owner: platform` so they don't show as `?`.
- One-off labeling of existing deployments (vllm-stack chart values + external namespaces) —
  needs your call on the names (open Q5).
- Unlabeled NPU pod → `?` chip (prompts labeling), never blocks anything.
- Dummy load (host process) always shows owner `dummy`.

## 4. Delivery

New top-level dir `npu-ops/` in this repo:
```
npu-ops/
  Dockerfile              # python:3.11-slim, fastapi, kubernetes, uvicorn (backend)
  agent/
    Dockerfile            # tiny stdlib-only agent
    agent.py
    npu_mem_load.py       # the script, versioned here (source of truth)
  app/
    main.py               # FastAPI: /api/state, actions, auto-load loop
    aggregate.py          # exporter metrics parsing, ownership, status derivation
    static/index.html
  manifests/
    00-namespace.yaml
    10-agent.yaml         # SA + DaemonSet + ConfigMap(script)
    20-dashboard.yaml     # SA + RBAC(nodes,pods) + Deployment + Service NodePort 30410
  README.md               # usage, env vars (AUTOLOAD_AFTER_HOURS, PCT), operations
```
Access: `http://<any-node-ip>:30410` (NodePort, consistent with litellm/open-webui) or
`kubectl port-forward -n npu-ops svc/npu-dashboard 8080:8080`.

## 5. Build order / milestones

1. **M1 — agent**: DaemonSet + `/state`, `/dummy/start`, `/dummy/stop`. Validate on
   `devserver-bms-b6af27bc` (.118): start → exporter shows ~90% HBM → stop → back to baseline.
   Also validate takeover on an orphan node (2eff03de-2).
2. **M2 — backend aggregation**: `/api/state` with statuses incl. ORPHAN/UNKNOWN, ownership via
   annotations + labels. `curl`-driven check against the 16 nodes.
3. **M3 — UI**: cards, badges, bars, owner chips, buttons wired to actions.
4. **M4 — auto-load**: 4h timer, auto-stop on real workload, opt-out label, action log.
5. **M5 — labeling + docs**: `platform/owner` on existing deployments in all 3 workload
   namespaces, README, optional: fix exporter `-containerMode` → `containerd` (nice-to-have).

## 6. Risks / notes
- Agent is **privileged** (needs host python + process control) — same trust level as the
  existing exporter; confined to a dev cluster.
- Dummy load at host level is invisible to the scheduler: the auto-stop-on-workload rule and the
  start-guard (no real NPU pod on node) are the safety net.
- Exporter is flaky (restart loops); agent/backend tolerate missing metrics → node shows
  UNKNOWN instead of failing the page.
- Host python on .118 is py3.7 with broken default `LD_LIBRARY_PATH` from containers → agent uses
  `chroot /host` to run it, exactly like a host process.

## 7. Open questions
1. Auto-load threshold: **4h** default (env-tunable) — ok?
2. Dummy load target: keep **90%** of HBM, all 8 NPUs? (Script could gain `--devices 0,3` for
   partial loading — needed? Default plan: all 8, as today.)
3. Include the 3 nodes without exporter (`6c1da567`, `b11214d6-tmp0306`, `d9a5373f-tmp0306`)?
   Plan: yes for agent (start/stop + UNKNOWN NPU state). Optionally also extend the exporter
   DaemonSet to them by adding the label.
4. The orphan ~16.7 GiB/NPU loads on 2eff03de-2..5: leave running until you click, or should
   M1 stop them and replace with managed dummy?
5. Owner names for labeling: `sir-llm-platform` → ? ; `vllm-ascend` → ? ; `sir-llm-lmcache` → ?
6. Dashboard port: NodePort **30410** ok?
