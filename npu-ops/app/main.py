"""npu-ops dashboard backend.

Aggregates, per node:
  - NPU state (util, HBM, processes)      <- existing npu-exporter at node-IP:8082
  - pods + NPU device ownership + owner   <- K8s API (all namespaces)
  - dummy load state + actions            <- npu-node-agent at node-IP:8083

Derives per-node status: FREE / BUSY / DUMMY / ORPHAN / MIXED / UNKNOWN and runs
the auto-load controller (start dummy after N hours idle, stop it when a real
workload lands on the node).
"""
import asyncio
import collections
import hashlib
import hmac
import json
import logging
import os
import re
import threading
import time
from typing import Optional

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

from aggregate import (ABSENT, is_npu_pod, npu_devices_from_exporter, pod_age_hours,
                       pod_npu_count, pod_npu_devices, pod_owner)

log = logging.getLogger("npu-ops")
logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"),
                    format="%(asctime)s %(levelname)s %(message)s")

# ------------------------------------------------------------- configuration
EXPORTER_PORT = int(os.environ.get("NPU_EXPORTER_PORT", "8082"))
AGENT_PORT = int(os.environ.get("NPU_AGENT_PORT", "8083"))
SCRAPE_INTERVAL = float(os.environ.get("SCRAPE_INTERVAL", "15"))       # s
AUTOLOAD_AFTER_HOURS = float(os.environ.get("AUTOLOAD_AFTER_HOURS", "4"))
BUSY_UTIL_PCT = float(os.environ.get("BUSY_UTIL_PCT", "5"))            # util threshold
BUSY_HBM_MARGIN_MB = float(os.environ.get("BUSY_HBM_MARGIN_MB", "4096"))  # hbm above baseline
BASELINE_HBM_MB = float(os.environ.get("BASELINE_HBM_MB", "3000"))     # per-device driver baseline
AUTOLOAD_LABEL = "npu-ops.io/auto-load"
UI_VERSION = "10"  # bump with every UI/API change; stale pages detect & self-reload
# Access control: one password gates the NPU tab AND the key-admin actions.
# Clients need no password (apply + track-by-name are public).
ADMIN_PASSWORD = os.environ.get("NPU_OPS_ADMIN_PASSWORD", "")
TOKEN_TTL = int(os.environ.get("NPU_OPS_TOKEN_TTL", str(12 * 3600)))  # s
TAG_LABEL = "npu-ops.io/tags"          # mirror only (BMS plane may prune node labels)
TAGS_CM = ("npu-ops-tags", "npu-ops", "tags.json")  # source of truth
TAG_RE = re.compile(r'^[a-z0-9][a-z0-9-]{0,31}$')
_TAGS_LOCK = threading.Lock()

app = FastAPI(title="npu-ops", version="1.0")

# ------------------------------------------------------------- auth (one password)
# A login returns a short-lived signed token (HMAC of the expiry with the admin
# password as key). No session store: the server re-derives the signature, so
# there is nothing to operate or expire. Kept in the browser's localStorage.

def _token_now():
    if not ADMIN_PASSWORD:
        raise HTTPException(503, "admin password not configured")
    exp = int(time.time()) + TOKEN_TTL
    sig = hmac.new(ADMIN_PASSWORD.encode(), str(exp).encode(), hashlib.sha256).hexdigest()
    return f"{exp}.{sig}"


def _valid_token(tok):
    if not ADMIN_PASSWORD or not tok:
        return False
    parts = tok.split(".")
    if len(parts) != 2:
        return False
    exp_s, sig = parts
    try:
        exp = int(exp_s)
    except ValueError:
        return False
    if exp < time.time():
        return False
    expect = hmac.new(ADMIN_PASSWORD.encode(), exp_s.encode(), hashlib.sha256).hexdigest()
    return hmac.compare_digest(sig, expect)


def _auth_token(request):
    h = request.headers.get("authorization") or ""
    if h.startswith("Bearer "):
        return h[7:].strip()
    return request.query_params.get("token") or ""


def _require_auth(request):
    if not _valid_token(_auth_token(request)):
        raise HTTPException(401, "authentication required")

# ------------------------------------------------------------- in-memory state
nodes_state = {}          # name -> mutable per-node tracker
actions = collections.deque(maxlen=200)
scrape_seq = {"last_ok": None, "last_err": None, "count": 0}


def tracker(name):
    t = nodes_state.get(name)
    if t is None:
        # fresh nodes start "quiet": no surprise auto-loads right after (re)start
        t = {"last_activity": time.time(), "free_since": None}
        nodes_state[name] = t
    return t


def record_action(node, action, source, ok, detail=""):
    rec = {"ts": int(time.time()), "node": node, "action": action,
           "source": source, "ok": ok, "detail": detail}
    actions.appendleft(rec)
    log.info("ACTION %s %s by %s ok=%s %s", node, action, source, ok, detail)


# ------------------------------------------------------------- kubernetes
class K8s:
    def __init__(self):
        from kubernetes import client, config
        try:
            config.load_incluster_config()
            self.config_kind = "incluster"
        except config.ConfigException:
            config.load_kube_config()
            self.config_kind = "kubeconfig"
        self.api = client.CoreV1Api()

    def nodes(self):
        items = self.api.list_node().items
        out = []
        for n in items:
            addr = next((a.address for a in n.status.addresses or []
                         if a.type == "InternalIP"), "")
            caps = n.status.capacity or {}
            npu_cap = 0
            for k, v in caps.items():
                if "scend" in k:
                    try:
                        npu_cap = max(npu_cap, int(v))
                    except (TypeError, ValueError):
                        pass
            labels = n.metadata.labels or {}
            tags = [t.strip() for t in (labels.get(TAG_LABEL) or "").split(",") if t.strip()]
            out.append({
                "name": n.metadata.name,
                "ip": addr,
                "roles": [v for k, v in labels.items()
                          if k.startswith("node-role.kubernetes.io/")],
                "tags": tags,
                "labels": labels,
                "npu_capacity": npu_cap,
                "os_image": (n.status.node_info or {}).os_image,
                "k8s_version": (n.status.node_info or {}).kubelet_version,
                "unschedulable": bool(n.spec.unschedulable),
            })
        return out

    def npu_pods_by_node(self):
        """node -> [pod summary] for pods that request NPU resources (Pending/Running).

        Uses status.phase field selectors instead of a full list: a plain
        list_pod_for_all_namespaces deserializes *every* pod in the cluster
        (dead Failed pods pile up to thousands) and OOM-killed the 512Mi
        dashboard on every 15 s scrape.
        """
        items = []
        for phase in ("Running", "Pending"):
            items.extend(self.api.list_pod_for_all_namespaces(
                watch=False, field_selector=f"status.phase={phase}").items)
        out = {}
        for p in items:
            phase = p.status.phase
            if phase not in ("Pending", "Running"):
                continue
            pd = p.to_dict()
            if not is_npu_pod(pd):
                continue
            node = pd["spec"].get("node_name") or ""
            devices = pod_npu_devices(pd)  # list | None(all) | ABSENT
            out.setdefault(node, []).append({
                "ns": pd["metadata"]["namespace"],
                "name": pd["metadata"]["name"],
                "phase": phase,
                "ready": _ready(pd),
                "owner": pod_owner(pd),
                "devices": devices,
                "npu_count": pod_npu_count(pd["spec"]),
                "age_hours": pod_age_hours(pd),
            })
        return out


def _ready(pod):
    for c in pod.get("status", {}).get("conditions") or []:
        if c.get("type") == "Ready":
            return c.get("status") == "True"
    return False


k8s = None  # lazy (import time for kubernetes client is non-trivial)


def get_k8s():
    global k8s
    if k8s is None:
        k8s = K8s()
    return k8s


# ------------------------------------------------------------- tag store (ConfigMap)

def read_tags_map():
    """{node: [tags]} from the ConfigMap (source of truth)."""
    from kubernetes import client
    try:
        cm = get_k8s().api.read_namespaced_config_map(TAGS_CM[0], TAGS_CM[1])
        return json.loads((cm.data or {}).get(TAGS_CM[2]) or "{}")
    except client.exceptions.ApiException as e:
        if e.status == 404:
            body = {"metadata": {"name": TAGS_CM[0], "namespace": TAGS_CM[1]},
                    "data": {TAGS_CM[2]: "{}"}}
            try:
                get_k8s().api.create_namespaced_config_map(TAGS_CM[1], body=body)
            except client.exceptions.ApiException:
                pass
        return {}


def write_tags_map(tags_map):
    from kubernetes import client
    for _ in range(3):
        cm = get_k8s().api.read_namespaced_config_map(TAGS_CM[0], TAGS_CM[1])
        cm.data = {TAGS_CM[2]: json.dumps(tags_map)}
        try:
            get_k8s().api.replace_namespaced_config_map(TAGS_CM[0], TAGS_CM[1], body=cm)
            return True
        except client.exceptions.ApiException as e:
            if e.status == 409:  # concurrent writer - retry with fresh resourceVersion
                time.sleep(0.2)
                continue
            raise


# ------------------------------------------------------------- node state
async def fetch_node_metrics(ip):
    """Return per-device dict from exporter, or None."""
    if not ip:
        return None
    async with httpx.AsyncClient(timeout=10) as c:
        r = await c.get(f"http://{ip}:{EXPORTER_PORT}/metrics")
        r.raise_for_status()
        return npu_devices_from_exporter(r.text)


async def fetch_agent(ip):
    if not ip:
        return None
    async with httpx.AsyncClient(timeout=5) as c:
        r = await c.get(f"http://{ip}:{AGENT_PORT}/state")
        r.raise_for_status()
        return r.json()


def _owns(dev_id, p):
    dv = p.get("devices")
    if dv is None:
        return True                      # visible devices = all
    if isinstance(dv, list):
        return dev_id in dv
    return False


def device_state(dev_id, dev, pods_on_node, dummy_running, baseline_mb):
    """Classify one device: FREE / BUSY / DUMMY / ORPHAN + owner info."""
    owned_by = [p for p in pods_on_node
                if p["phase"] == "Running" and _owns(dev_id, p)]
    if owned_by:
        p = owned_by[0]
        return "BUSY", f'{p["ns"]}/{p["name"]}', p["owner"]
    has_proc = bool(dev.get("proc_pid"))
    hbm_high = dev.get("hbm_used_mb", 0) > baseline_mb + BUSY_HBM_MARGIN_MB
    util_high = dev.get("util", 0) > BUSY_UTIL_PCT
    if has_proc and hbm_high:
        if dummy_running:
            return "DUMMY", "npu_mem_load (host)", "dummy"
        return "ORPHAN", f'host proc {dev["proc_pid"]}', ""
    if has_proc or hbm_high or util_high:
        # light activity (e.g. process starting, small alloc) - not free, not full dummy
        return "BUSY", ("proc %s" % dev["proc_pid"]) if has_proc else "activity", ""
    return "FREE", "", ""


def build_card(node, pods, exporter, agent, now, tags_map=None):
    t = tracker(node["name"])
    npus = []
    if exporter:
        baseline_mb = BASELINE_HBM_MB
        for id_ in sorted(exporter.keys(), key=lambda x: int(x) if str(x).isdigit() else 999):
            dev_id = int(id_) if str(id_).isdigit() else id_
            dev = exporter[id_]
            st, owner_pod, owner = device_state(dev_id, dev, pods, _dummy_running(agent), baseline_mb)
            npus.append({
                "id": dev_id,
                "state": st,
                "util": round(dev.get("util", 0), 1),
                "hbm_used_mb": round(dev.get("hbm_used_mb", 0)),
                "hbm_total_mb": round(dev.get("hbm_total_mb", 0)),
                "proc_pid": dev.get("proc_pid", ""),
                "owner_pod": owner_pod,
                "owner": owner,
            })
    n_running_pods = [p for p in pods if p["phase"] == "Running"]
    n_pending_pods = [p for p in pods if p["phase"] == "Pending"]
    dummy = (agent or {}).get("dummy") or {"running": False}
    agent_ok = agent is not None

    # node status
    states = [n["state"] for n in npus]
    if not exporter:
        status = "BUSY" if n_running_pods else "UNKNOWN"
    elif "BUSY" in states and any(s in ("DUMMY", "ORPHAN") for s in states):
        status = "MIXED"
    elif "BUSY" in states:
        status = "BUSY"
    elif "ORPHAN" in states:
        status = "ORPHAN"
    elif "DUMMY" in states:
        status = "DUMMY"
    else:
        status = "FREE"

    owners = sorted({p["owner"] for p in n_running_pods if p["owner"]} |
                    ({"dummy"} if dummy.get("running") else set()))
    if not owners and n_running_pods:
        owners = ["unlabeled"]
    if not owners:
        owners = []

    # activity / free tracking
    active = bool(n_running_pods or dummy.get("running") or
                  any(s in ("BUSY", "DUMMY", "ORPHAN") for s in states))
    if active:
        t["last_activity"] = now
        t["free_since"] = None
    elif exporter or agent_ok:
        if t["free_since"] is None:
            t["free_since"] = now

    auto_load_enabled = node["labels"].get(AUTOLOAD_LABEL, "true") != "false"
    idle_h = (now - t["last_activity"]) / 3600.0
    auto_eta_min = None
    if (auto_load_enabled and agent_ok and not dummy.get("running")
            and not pods and exporter and status == "FREE"):
        remaining = AUTOLOAD_AFTER_HOURS - idle_h
        auto_eta_min = max(0.0, remaining * 60)

    return {
        "name": node["name"],
        "ip": node["ip"],
        "roles": node["roles"],
        "os_image": node["os_image"],
        "k8s_version": node["k8s_version"],
        "unschedulable": node["unschedulable"],
        "tags": (tags_map or {}).get(node["name"], node.get("tags", [])),
        "npu_capacity": node["npu_capacity"],
        "status": status,
        "owners": owners,
        "pods": pods,
        "npus": npus,
        "exporter_ok": exporter is not None,
        "agent_ok": agent_ok,
        "docker_containers": (agent or {}).get("docker_containers"),
        "dummy": {
            "running": bool(dummy.get("running")),
            "managed": bool(dummy.get("managed")),
            "pid": dummy.get("pid"),
            "started_at": dummy.get("started_at"),
        },
        "auto_load": {
            "enabled": auto_load_enabled,
            "idle_hours": round(idle_h, 2),
            "eta_minutes": (round(auto_eta_min, 0) if auto_eta_min is not None else None),
            "threshold_hours": AUTOLOAD_AFTER_HOURS,
        },
        "free_since": t["free_since"],
        "ts": int(now),
    }


def _dummy_running(agent):
    return bool(agent and agent.get("dummy", {}).get("running"))


# ------------------------------------------------------------- auto-load
async def autoload_tick(nodes_info, pods_by_node, agent_by_ip):
    now = time.time()
    for node in nodes_info:
        name, ip = node["name"], node["ip"]
        if node["labels"].get(AUTOLOAD_LABEL, "true") == "false":
            continue
        agent = agent_by_ip.get(ip)
        if not agent:
            continue
        pods = pods_by_node.get(name, [])
        dummy = agent.get("dummy", {})
        running_npu_pods = [p for p in pods if p["phase"] == "Running"]
        t = tracker(name)
        # safety: real workload landed on a node holding dummy -> release HBM
        if dummy.get("running") and running_npu_pods:
            log.info("autoload: stopping dummy on %s (real workload %s present)",
                     name, [f'{p["ns"]}/{p["name"]}' for p in running_npu_pods])
            rec = await do_action(name, ip, "stop", "auto")
            record_action(name, "dummy/stop (auto: workload arrived)", "auto",
                          rec.get("ok"), rec.get("detail", ""))
            continue
        # idle long enough -> start dummy
        idle_h = (now - t["last_activity"]) / 3600.0
        if (not dummy.get("running") and not pods
                and idle_h >= AUTOLOAD_AFTER_HOURS):
            log.info("autoload: starting dummy on %s (idle %.1fh)", name, idle_h)
            rec = await do_action(name, ip, "start", "auto")
            record_action(name, "dummy/start (auto: idle %.1fh)" % idle_h, "auto",
                          rec.get("ok"), rec.get("detail", ""))
            if rec.get("ok"):
                t["last_activity"] = now


async def do_action(node, ip, action, source):
    """Call the node agent; returns {ok, detail, body}."""
    if not ip:
        return {"ok": False, "detail": "node has no internal IP", "body": None}
    path = f"/dummy/{action}"
    try:
        async with httpx.AsyncClient(timeout=180) as c:
            r = await c.post(f"http://{ip}:{AGENT_PORT}{path}")
            body = {}
            try:
                body = r.json()
            except ValueError:
                pass
            ok = r.status_code == 200
            detail = body.get("error") or body.get("note") or (
                "pid=%s" % body.get("pid") if body.get("pid") else "")
            if action == "start" and ok:
                detail = ("pid=%s verified=%s" % (body.get("pid"), body.get("hbm_verified"))
                          + (("; " + body["note"]) if body.get("note") else ""))
            if action == "stop" and ok:
                detail = "pid=%s hbm_released=%s" % (body.get("pid"), body.get("hbm_released"))
            return {"ok": ok, "detail": detail, "body": body, "status": r.status_code}
    except httpx.HTTPError as e:
        return {"ok": False, "detail": f"agent unreachable: {e.__class__.__name__}", "body": None}


# ------------------------------------------------------------- scrape loop
async def scrape_loop():
    while True:
        try:
            now = time.time()
            nodes = await asyncio.to_thread(get_k8s().nodes)
            pods_by_node = await asyncio.to_thread(get_k8s().npu_pods_by_node)
            # parallel per-node fetches
            exporter_f = [fetch_node_metrics(n["ip"]) for n in nodes]
            agent_f = [fetch_agent(n["ip"]) for n in nodes]
            results = await asyncio.gather(*exporter_f, *agent_f, return_exceptions=True)
            exporters = results[:len(nodes)]
            agents = results[len(nodes):]
            agent_by_ip = {}
            for n, ex, ag in zip(nodes, exporters, agents):
                if isinstance(ex, Exception):
                    ex = None
                if isinstance(ag, Exception):
                    ag = None
                if ag:
                    agent_by_ip[n["ip"]] = ag
            tags_map = await asyncio.to_thread(read_tags_map)
            # adopt label-only tags into the CM: users may tag via
            # `kubectl label node <n> npu-ops.io/tags=...`; the BMS plane prunes
            # node labels, so persist whatever shows up there
            changed = False
            for n in nodes:
                lbl = [t.strip() for t in (n["labels"].get(TAG_LABEL) or "").split(",") if t.strip()]
                cur = tags_map.get(n["name"], [])
                add = [t for t in lbl if t not in cur]
                if add:
                    tags_map[n["name"]] = cur + add
                    changed = True
            if changed:
                try:
                    await asyncio.to_thread(write_tags_map, tags_map)
                except Exception:
                    log.exception("failed to persist adopted tags")
            cards = []
            for n, ex, ag in zip(nodes, exporters, agents):
                if isinstance(ex, Exception):
                    ex = None
                if isinstance(ag, Exception):
                    ag = None
                cards.append(build_card(n, pods_by_node.get(n["name"], []), ex, ag, now, tags_map))
            await autoload_tick(nodes, pods_by_node, agent_by_ip)
            STATE["cards"] = cards
            STATE["generated"] = int(now)
            scrape_seq["last_ok"] = now
            scrape_seq["count"] += 1
            nstat = collections.Counter(c["status"] for c in cards)
            log.debug("scrape ok: %d nodes %s", len(cards), dict(nstat))
        except Exception as e:
            scrape_seq["last_err"] = f"{e.__class__.__name__}: {e}"
            log.exception("scrape failed")
        await asyncio.sleep(SCRAPE_INTERVAL)


STATE = {"cards": [], "generated": None}


@app.on_event("startup")
async def _start():
    asyncio.create_task(scrape_loop())


# ------------------------------------------------------------- endpoints
@app.post("/api/auth/login")
def login(body: dict = {}):
    if not ADMIN_PASSWORD:
        raise HTTPException(503, "admin password not configured")
    if not hmac.compare_digest(str(body.get("password") or ""), ADMIN_PASSWORD):
        raise HTTPException(401, "wrong password")
    return {"ok": True, "token": _token_now(), "expires_in": TOKEN_TTL}


@app.get("/api/healthz")
def healthz():
    return {"ok": True, "scrapes": scrape_seq["count"],
            "last_ok": scrape_seq["last_ok"], "last_err": scrape_seq["last_err"]}


@app.get("/api/state")
def api_state(request: Request):
    _require_auth(request)  # node-level detail is admin-only
    cards = STATE["cards"]
    # live tag read: chips appear on the next UI poll, not the next scrape
    try:
        live_tags = read_tags_map()
        for c in cards:
            c["tags"] = live_tags.get(c["name"], c.get("tags", []))
    except Exception:
        pass
    return {
        "generated": STATE["generated"],
        "ui_version": UI_VERSION,
        "autoload_after_hours": AUTOLOAD_AFTER_HOURS,
        "actions": list(actions)[:50],
        "nodes": cards,
    }


def _find_node(name):
    for c in STATE["cards"]:
        if c["name"] == name:
            return c
    raise HTTPException(404, f"unknown node {name}")


@app.post("/api/nodes/{name}/dummy/start")
async def dummy_start(name, request: Request):
    _require_auth(request)
    card = _find_node(name)
    if not card["agent_ok"]:
        raise HTTPException(502, f"agent not reachable on {name}")
    if any(p["phase"] in ("Running", "Pending") for p in card["pods"]):
        raise HTTPException(409, "node has NPU pods scheduled/running; refusing to start dummy load")
    rec = await do_action(name, card["ip"], "start", "ui")
    record_action(name, "dummy/start", "ui", rec["ok"], rec["detail"])
    if not rec["ok"]:
        raise HTTPException(502, rec["detail"])
    return {"ok": True, **rec}


@app.post("/api/nodes/{name}/dummy/stop")
async def dummy_stop(name, request: Request):
    _require_auth(request)
    card = _find_node(name)
    if not card["agent_ok"]:
        raise HTTPException(502, f"agent not reachable on {name}")
    rec = await do_action(name, card["ip"], "stop", "ui")
    record_action(name, "dummy/stop", "ui", rec["ok"], rec["detail"])
    if not rec["ok"]:
        raise HTTPException(502, rec["detail"])
    return {"ok": True, **rec}


@app.post("/api/nodes/{name}/tags")
async def node_tags(name, request: Request, body: dict = {}):
    _require_auth(request)
    """Add/remove a tag on a node. Stored in ConfigMap npu-ops-tags (source of
    truth); node label npu-ops.io/tags is mirrored best-effort (the BMS
    management plane may prune node labels)."""
    tag = str(body.get("tag") or "").strip().lower().replace("_", "-").replace(" ", "-")
    if not TAG_RE.match(tag):
        raise HTTPException(400, "tag must be 1-32 chars: lowercase a-z, 0-9, '-' (no leading '-')")
    remove = bool(body.get("remove"))
    with _TAGS_LOCK:
        tags_map = read_tags_map()
        current = list(tags_map.get(name, []))
        if remove:
            if tag not in current:
                raise HTTPException(404, f"node {name} does not have tag {tag}")
            current.remove(tag)
        elif tag not in current:
            current.append(tag)
        if current:
            tags_map[name] = current
        else:
            tags_map.pop(name, None)
        try:
            write_tags_map(tags_map)
        except Exception as e:
            raise HTTPException(500, f"failed to save tags: {e}")
    # best-effort mirror to the node label (visible via kubectl when it sticks)
    try:
        get_k8s().api.patch_node(name, body={"metadata": {
            "labels": {TAG_LABEL: ",".join(current) if current else None}}})
    except Exception:
        pass
    record_action(name, f"tag/{tag}/{'remove' if remove else 'add'}", "ui", True)
    return {"ok": True, "node": name, "tags": current}


# ------------------------------------------------------------- API keys (LiteLLM)
import keys as kkeys


@app.get("/api/keys/models")
def keys_models():
    try:
        return {"models": kkeys.list_models(), "ui_version": UI_VERSION}
    except kkeys.KeysError as e:
        raise HTTPException(502, str(e))


@app.post("/api/keys/apply")
def keys_apply(body: dict = {}):
    try:
        req = kkeys.apply_key(body)
    except kkeys.KeysError as e:
        raise HTTPException(400, str(e))
    record_action("keys", f"apply/{req['owner']}", "ui", True, req["id"])
    return {"ok": True, "apply": req}


@app.get("/api/keys/applies")
def keys_applies(request: Request):
    _require_auth(request)  # pending approvals are admin-only
    try:
        return {"applies": kkeys.list_applies()}
    except Exception as e:
        raise HTTPException(500, f"failed to list applications: {e}")


@app.post("/api/keys/applies/{rid}/approve")
async def keys_approve(rid, request: Request):
    _require_auth(request)
    try:
        out = await asyncio.to_thread(kkeys.approve_apply, rid)
    except kkeys.KeysError as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        raise HTTPException(502, f"key generation failed: {e}")
    record_action("keys", f"approve/{out['owner']}", "ui", True, out["key_hash"][:10])
    return {"ok": True, **out}


@app.post("/api/keys/applies/{rid}/reject")
async def keys_reject(rid, request: Request):
    _require_auth(request)
    try:
        req = await asyncio.to_thread(kkeys.reject_apply, rid)
    except kkeys.KeysError as e:
        raise HTTPException(400, str(e))
    record_action("keys", f"reject/{req['owner']}", "ui", True, rid)
    return {"ok": True, "apply": req}


@app.get("/api/keys")
def keys_list(request: Request, owner: Optional[str] = None):
    """Public, but anonymous callers must look up by owner name (that is how
    clients track their own keys). Authenticated admins see the full list."""
    authed = _valid_token(_auth_token(request))
    try:
        keys, cache_stats = kkeys.list_keys()
    except kkeys.KeysError as e:
        raise HTTPException(502, str(e))
    if owner:
        o = owner.strip().lower()
        keys = [k for k in keys if k["owner"].strip().lower() == o]  # case-insensitive
    elif not authed:
        keys = []
    return {"keys": keys, "admin": authed, "ui_version": UI_VERSION,
            "cache_stats": cache_stats}


@app.get("/api/keys/{key_hash}/detail")
def keys_detail(key_hash):  # public: usage stats for a key the holder already has
    try:
        return kkeys.key_detail(key_hash)
    except kkeys.KeysError as e:
        raise HTTPException(502, str(e))


@app.post("/api/keys/{key_hash}/revoke")
async def keys_revoke(key_hash, request: Request):
    _require_auth(request)
    try:
        await asyncio.to_thread(kkeys.block_key, key_hash)
    except kkeys.KeysError as e:
        raise HTTPException(502, str(e))
    record_action("keys", f"revoke/{key_hash[:10]}", "ui", True)
    return {"ok": True}


@app.get("/")
@app.get("/npus")
@app.get("/keys")
def index():
    from fastapi.responses import HTMLResponse
    with open(os.path.join(os.path.dirname(__file__), "static", "index.html")) as f:
        html = f.read()
    # no-cache + SPA: same app on every path, the JS router picks the view
    return HTMLResponse(html, headers={"Cache-Control": "no-cache, max-age=0"})


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", "8080")))
