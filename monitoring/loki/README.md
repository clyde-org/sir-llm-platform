# Cluster Log Collection — Loki + Promtail

Grafana **Loki** (single-binary) + **Promtail** DaemonSet collect container
logs from every Ready node and make them queryable via LogQL in the existing
Grafana. 7-day retention, ~1–3 GiB/day of logs, ~5 GiB RAM total.

```
promtail (DaemonSet, 1 pod/node)            loki (StatefulSet, 1 pod)
  tails /var/log/pods/*/*/*.log      -->     /var/loki  ->  PV loki-data
  CRI parse, labels: ns/pod/ct/...           (100Gi local, 2eff03de-0, /data/loki)
  http://loki.logging.svc:3100/loki/api/v1/push
                                                |
Grafana (monitoring ns) -- Loki datasource --> NodePort 32100 (API, lab only)
```

## Files

| File | Purpose |
|---|---|
| `loki-values.yaml` | helm values for release `loki` (SingleBinary mode, fs storage, 7d retention) |
| `promtail-values.yaml` | helm values for release `promtail` (DaemonSet, client URL override) |
| `static-pv.yaml` | 100Gi local PV `loki-data` on `devserver-bms-2eff03de-0` (`/data/loki`) |
| `loki-nodeport.yaml` | NodePort service (3100 → **32100**) for direct API access |
| `grafana-datasource.yaml` | ConfigMap (label `grafana_datasource=1`) provisioned by the existing Grafana sidecar — no helm upgrade needed |
| `charts/` | vendored chart tarballs (loki-7.3.0, promtail-6.17.1) — install offline, versions pinned |

## Install (already performed 2026-09-30)

Prerequisite (one-time, local PVs don't create paths): the directory
`/data/loki` on `devserver-bms-2eff03de-0.novalocal`, owned by uid 10001
(Loki's fsGroup). Done with a one-shot hostPath pod (`postgres:16-alpine`
image already on the node):

```sh
kubectl run loki-init -n logging --restart=Never \
  --overrides='{"spec":{"nodeSelector":{"kubernetes.io/hostname":"devserver-bms-2eff03de-0.novalocal"},
   "containers":[{"name":"init","image":"docker.io/library/postgres:16-alpine",
     "command":["/bin/sh","-c","mkdir -p /host/data/loki && chown -R 10001:10001 /host/data/loki"],
     "volumeMounts":[{"name":"d","mountPath":"/host/data"}]}],
   "volumes":[{"name":"d","hostPath":{"path":"/data"}}]}}'
kubectl wait --for=delete pod/loki-init --timeout=120s
```

```sh
kubectl create namespace logging
kubectl apply -f static-pv.yaml
kubectl apply -f loki-nodeport.yaml
helm install loki      charts/loki-7.3.0.tgz     -n logging -f loki-values.yaml
helm install promtail  charts/promtail-6.17.1.tgz -n logging -f promtail-values.yaml
kubectl apply -f grafana-datasource.yaml
```

Notes:
- `helm repo add grafana https://grafana.github.io/helm-charts` fails behind
  the lab's MITM proxy (curl --insecure, no CA) — hence the vendored tarballs.
- The promtail chart is officially **deprecated** in favor of Grafana Alloy,
  but 3.5.1 is multi-arch (arm64 ✓), stable, and a perfect fit here. Revisit
  only if we migrate the metrics stack to Alloy.
- The Loki chart validation requires explicitly zeroing `backend/read/write/…`
  replicas in SingleBinary mode (done in `loki-values.yaml`), and
  `compactor.delete_request_store: filesystem` is mandatory when retention is on.

## Access

- **Grafana**: `http://<NODE_IP>:30300` → Explore → datasource **Loki**.
- **Direct API** (NodePort, any node; lab network only — logs contain secrets):
  ```sh
  curl http://<NODE_IP>:32100/ready
  curl http://<NODE_IP>:32100/loki/api/v1/labels
  curl -G http://<NODE_IP>:32100/loki/api/v1/query_range \
    --data-urlencode 'query={namespace="sir-llm-platform",container="litellm-proxy"}' \
    --data-urlencode "from=$(date -d '1 hour ago' +%s)000000000" \
    --data-urlencode "to=$(date +%s)000000000"
  ```
- **Useful LogQL**:
  - errors across the whole cluster: `{namespace=~"sir-llm-platform|vllm-ascend"} |= "ERROR" |~ "(?i)error|fail|panic"`
  - one vLLM pod: `{pod=~"vllm-glm45-air-.*", container="vllm"}`
  - rate: `sum(count_over_time({job="vllm-ascend/vllm-glm45-air"}[1m]))`
  - NOTE: in stream selectors label values are **regex**, not glob
    (`{filename="/var/log/pods/*"}` does NOT match — use `{container=~".+"}` style).

## Operations

- **Retention**: 7 days (`limits_config.retention_period: 168h`, compactor
  deletes in the background). Bump in `loki-values.yaml` + `helm upgrade`.
- **Disk**: PV is 100Gi on 2eff03de-0 (`/data`, 7.5 TiB free). At ~2 GiB/day
  with 7d retention steady-state usage is ~15 GiB — headroom for longer
  retention without resizing.
- **Scaling up** (if the lab grows): raise `singleBinary.resources`, or move
  to the chart's SimpleScalable/Distributed modes + object storage (then
  delete this PV setup). At 16 nodes / ~130 pods single-binary is fine.
- **Nodes**: node3/node4 (10.50.x) powered off — their Promtail pods simply
  don't schedule; they come back when the nodes do. NotReady nodes are
  unaffected.
- **Known issue**: `devserver-bms-2eff03de-2` has DiskPressure; its Promtail
  pod gets evicted/recreated until that node's disk frees up (pre-existing,
  unrelated to Loki).
- **First-run behaviour**: when Promtail starts with no positions file it
  scans from the top of each log file; entries older than 7d are rejected by
  Loki (`timestamp too old` 400s in Promtail logs for a few minutes). Expected.
  Positions live in `/run/promtail` (hostPath, tmpfs) — a node reboot causes
  one more catch-up scan, also benign.
- **Uninstall**: `helm uninstall promtail loki -n logging`; delete the
  datasource CM; the PV keeps data (Retain) — `rm -rf /data/loki` on the node
  to free it.
