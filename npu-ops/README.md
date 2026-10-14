# npu-ops — NPU fleet dashboard

A card-based dashboard to see, per server, what is running on its Ascend 910B
NPUs and **who owns it** — real workloads vs dummy load vs free — with one-click
**Load NPUs / Stop** and **auto-load** of dummy memory after 4h of idleness.

UI: `http://<any-node-ip>:30410` (NodePort) or
`kubectl port-forward -n npu-ops svc/npu-ops 8080:8080`.

## Components

| Piece | What it is | Where |
|---|---|---|
| `npu-node-agent` | DaemonSet, privileged + hostPID + hostNetwork on all NPU nodes. Detects the host-level dummy process (`/root/npu_mem_load.py`) and starts/stops it via `chroot` into the host root (`/host`). Serves `:8083`. | ns `npu-ops`, `manifests/10-agent.yaml` |
| `npu-ops` (dashboard) | FastAPI deployment (1 replica). Aggregates K8s API (nodes + pods, all namespaces) + the existing `npu-exporter` metrics (`node-IP:8082`) + each agent (`:8083`). Serves the UI + REST API on `:8080` / NodePort `30410`. Runs the auto-load controller. | ns `npu-ops`, `manifests/20-dashboard.yaml` |
| `npu_mem_load.py` | The dummy workload: allocates ~90% HBM on the node's NPUs (via `libascendcl`) and holds it until SIGTERM (which frees everything). Bundled in the agent image; the agent syncs it to `/root/npu_mem_load.py` on every start. | `agent/npu_mem_load.py` |

## What the dashboard knows

Per NPU (from `npu-exporter`): AI-core utilization, HBM used/total, host process id.
Per pod (from the K8s API, **all namespaces**): NPU ownership, in two flavours:

1. **Device-plugin pods** — request `huawei.com/Ascend910`; devices come from the
   `huawei.com/AscendReal` annotation.
2. **Direct-visibility pods** — privileged pods using
   `ASCEND[_RT]_VISIBLE_DEVICES` env (no resource request, invisible to the
   device plugin — e.g. `vllm-glm45-air`, `vllm-timeshare-*`). Devices come from
   the env value (empty value = all devices).

Device state: `FREE` / `BUSY` (pod owns it) / `DUMMY` (managed dummy load) /
`ORPHAN` (host process with HBM but no owning pod — flag for manual attention).
Node status is the rollup: `FREE`, `BUSY`, `DUMMY`, `ORPHAN`, `MIXED`, `UNKNOWN`
(no exporter/agent reachable).

### Ownership
- Pod label **`platform/owner: <name>`** → owner chip on the card.
- `sir-llm-platform` pods are labelled `sheriffo` (live pods already labelled;
  the vllm-stack chart stamps it on new pods via the `owner` value).
- `vllm-ascend` and `sir-llm-lmcache` pods are labelled with placeholder owners
  (`vllm-ascend`, `lmcache`) — change when those get real owners; label the pod
  template, not just pods, so it survives restarts.
- Unlabelled NPU pods show `unlabeled`.

## Actions

- **Load NPUs** (button): refused when
  - any NPU pod is Running/Pending on the node (dashboard guard), or
  - NPU HBM is already loaded before start (agent guard, "HBM already in use
    (X GiB)" + host docker container names) — protects against racing a
    workload that lands during the start pre-flight, and against nodes whose
    NPUs are held by **host-level docker containers** (invisible to k8s).
  Otherwise the agent syncs the script, starts it with the host's python3 + a
  discovered `LD_LIBRARY_PATH`, and waits for HBM to actually rise (verified).
- **Stop** (button): SIGTERM (the script frees all HBM in its handler), waits
  for the process to exit and HBM to fall back to baseline, removes the pidfile.
  Also stops **orphan** loads (anything matching the script name).
- **Auto-load** (controller, every scrape tick):
  - node idle ≥ `AUTOLOAD_AFTER_HOURS` (default **4h**) with no NPU pods and no
    dummy → start dummy;
  - a real NPU pod lands on a node holding dummy → stop dummy immediately
    (protects real workloads from HBM contention);
  - opt-out per node: label `npu-ops.io/auto-load: "false"`.
  - On (re)start of the dashboard, `last-activity` timestamps reset to *now*,
    so nothing auto-loads in the first hours after a deploy.

## Tagging

Each card has tag chips + a `+ tag` button (click a chip to remove). Tags are
matched by the search box. **Source of truth is the ConfigMap `npu-ops-tags`
(ns `npu-ops`, key `tags.json`)** — this BMS/MindCluster environment has a
management plane that periodically prunes node labels, so node labels alone
were not reliable. The node label `npu-ops.io/tags` is a best-effort mirror
(visible via kubectl when it sticks), and tags set via
`kubectl label node <n> npu-ops.io/tags=...` are **auto-adopted into the
ConfigMap on the next scrape**, so they persist too.

```bash
# API equivalent:
curl -X POST :30410/api/nodes/<name>/tags -d '{"tag":"marco"}'
curl -X POST :30410/api/nodes/<name>/tags -d '{"tag":"marco","remove":true}'
# or the kubectl way (auto-adopted within ~15s):
kubectl label node <name> npu-ops.io/tags=marco --overwrite
```

The UI is served with `Cache-Control: no-cache`; if a page behaves like an old
version, hard-refresh (Ctrl+Shift+R).

## API key management ("API keys" tab)

Self-service virtual keys on top of the existing **LiteLLM proxy**
(`litellm-proxy.sir-llm-platform:4000`, NodePort 30400). The master key never
leaves the cluster — it lives in the `npu-ops-litellm` secret (ns `npu-ops`).

## Access control

One admin password (default `Huaawei@Mgmt!!!`, stored in secret
`npu-ops-litellm` key `NPU_OPS_ADMIN_PASSWORD`) gates **both** the NPU tab
and the key-admin actions. Clients need no password.

| Path | Access | What |
|------|--------|------|
| `/` | public | Portal: NPU Fleet (admin) + API Keys |
| `/npus` | password | Node status, Load/Stop dummy, tags, action log |
| `/keys` | public + gated admin | Public: apply + track-your-keys (by owner name). Admin: approve/reject/revoke + all keys |

Login (`POST /api/auth/login {password}`) returns a **signed 12h token**
(HMAC of the expiry with the password as key) that the browser keeps in
localStorage — no session store to operate. Gated HTTP endpoints accept
`Authorization: Bearer <token>`; a 401 re-shows the login box.

Anonymous callers of `GET /api/keys` must pass `?owner=<name>` (that is how
clients track their own keys by Huawei ID, case-insensitive); they never see
other owners' keys.

**UI**: dark theme by default with a light-mode toggle (☀️/🌙 in the header,
remembered per browser). The admin section of `/keys` is hidden while logged
out; a gear button (⚙️, bottom-right) opens the login modal for anonymous
users. On `/npus` the login box appears inline instead.

Rotate the password:

```bash
kubectl -n npu-ops patch secret npu-ops-litellm \
  -p '{"data":{"NPU_OPS_ADMIN_PASSWORD":"'$(printf '%s' 'NEW-PASSWORD' | base64)'"}}'
kubectl -n npu-ops rollout restart deploy/npu-ops   # existing tokens die on re-login
```

Note: the dashboard still has no login for *viewing* the keys tab (apply and
tracking are meant to be public); sensitive actions and all node detail are
password-gated.

Flow:
1. **Request** — user fills the form: **Huawei ID / username** (required, no spaces —
   becomes the key's `user_id` and the tracking handle), **full name** (required,
   shown in tables), **team** (required), and the models to access. There is no
   budget/duration/purpose — this is an internal, uncosted system.
2. **Approve / reject** — one click in the admin section (password required).
   Approve calls LiteLLM `/key/generate` and shows the plaintext key **once** (copy it in
   the modal). Only the sha256 hash is kept.
3. **Usage** — the user calls `http://<node>:30400/v1` (OpenAI-compatible) with
   `Authorization: Bearer <key>`.
4. **Per-user stats** — spend, request count, prompt/completion tokens, last
   active, 30-day daily chart and per-model breakdown, straight from LiteLLM's
   own Postgres (`LiteLLM_VerificationToken` + `LiteLLM_SpendLogs`).
5. **Revoke** — one click in the admin section blocks the key in LiteLLM
   immediately.

**Admin token** (gates approve/reject/revoke): see *Access control* above —
the UI derives the signed token from the password automatically.

Note: spend is not tracked or displayed (self-hosted models, no cost map); the
meaningful per-user metrics are request count and token counts.

## Host docker containers

Some workloads run as **host-level docker containers** (e.g. vLLM+lmcache
experiments on 62245bb7) — invisible to the K8s API. The agent lists them
(`docker ps` via chroot, cached 5 min) and the card shows a `⚓ docker: N`
chip (hover for names). Such nodes typically show **ORPHAN** (NPU memory held
by unmanaged host processes); Load NPUs is refused there by the HBM guard.

## CANN toolkit discovery

Toolkits live in different layouts per node (`ascend-toolkit/latest/lib64`,
`cann-8.5.2/aarch64-linux/lib64`, multi-dir `ascend-toolkit/7.0.1.5/...`,
`/mnt/...`). Agent and script therefore **discover** every dir containing
`libascendcl.so` and verify each in an isolated subprocess by calling
`aclrtGetDeviceCount` — this rejects CANN **stub** libraries (`devlib/`), which
load fine but fail at execution. The script re-execs itself once with the
correct `LD_LIBRARY_PATH` when started without it (dlopen only honours the env
at process start).

## API

```
GET  /api/state                     # full fleet state (what the UI renders)
GET  /api/healthz
POST /api/nodes/{name}/dummy/start  # start dummy load on a node
POST /api/nodes/{name}/dummy/stop   # stop it
# per-node agent (node IP, cluster-internal only):
GET  :8083/state   POST :8083/dummy/start   POST :8083/dummy/stop
```

## Build & deploy

```bash
cd npu-ops
./build.sh              # build (arm64, qemu-emulated) + push + pin tags in manifests
kubectl apply -f manifests/
```

- Images: `ghcr.io/fanbondi/npu-ops` and `ghcr.io/fanbondi/npu-ops-agent`,
  content-hash tags (`v1-<sha>`). **Don't use `latest`** — node containerd
  caches `latest` aggressively even with `imagePullPolicy: Always`.
- The build host here cannot pull fresh registry images (corporate TLS MITM
  breaks the docker daemon's trust), so both images are built on the local
  arm64 `kv-sidecar` base (python3.11 + pip, already present). If normal
  registry access is available, swap the `FROM` to `python:3.11-slim` and
  rebuild for the platform(s) you need.
- Pull secret `ghcr-fanbondi` (ns `npu-ops`) is required (repos are private).
- Nodes: any node with label `accelerator=huawei-Ascend910` gets the agent
  (currently all 16, including the 3 without an exporter — they show as
  `UNKNOWN` for NPU detail but still get dummy start/stop via the agent).

## Tuning (env on the dashboard deployment)

| Var | Default | Meaning |
|---|---|---|
| `AUTOLOAD_AFTER_HOURS` | `4` | idle time before dummy auto-starts |
| `SCRAPE_INTERVAL` | `15` | scrape tick seconds |
| `BUSY_UTIL_PCT` | `5` | AI-core util threshold for "activity" |
| `BUSY_HBM_MARGIN_MB` | `4096` | HBM above per-device baseline = "loaded" |
| `BASELINE_HBM_MB` | `3000` | per-device driver baseline HBM |
| `NPU_DUMMY_PCT` (agent) | `90` | target HBM % for dummy load |

## Operations notes

- **ConfigMaps are NOT in the manifests.** `npu-ops-tags` and `npu-ops-keys`
  hold live user data; a manifest with their initial `data: {}` would reset
  them on every `kubectl apply` (verified: it wiped user tags). The dashboard
  auto-creates both on first use instead.
- Node label/tag changes in this cluster cannot be attributed via the API
  (pruned node objects, BMS management plane prunes node labels).

- The agent is **privileged** (must run the host's python and signal host
  processes) — same trust level as the existing `npu-exporter` DS.
- Dummy load is a host process: invisible to the scheduler. The start-guard +
  auto-stop rules are the safety net. If a pod somehow gets scheduled onto a
  dummy-loaded device, it will OOM — that's exactly what the buttons/auto-load
  prevent.
- `npu-exporter` pods restart a lot (their own issue); the dashboard tolerates
  that and marks the node `UNKNOWN` instead of failing.
- Action log (last 200) is in-memory on the dashboard and rendered at the
  bottom of the UI; agent logs: `kubectl logs -n npu-ops -l app=npu-node-agent`.
