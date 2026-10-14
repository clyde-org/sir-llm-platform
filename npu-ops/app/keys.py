"""API key management on top of the existing LiteLLM proxy.

  - users *apply* for a key (owner / team / purpose / models / budget)
  - an admin *approves* (one click) -> dashboard calls LiteLLM /key/generate
    with the master key; the virtual key is bound to the user via user_id
  - per-user stats come from LiteLLM's own Postgres
    (LiteLLM_VerificationToken + LiteLLM_SpendLogs)

Apply state lives in ConfigMap `npu-ops-keys` (ns npu-ops). The plaintext
virtual key is shown exactly once at approval time (standard LiteLLM
behaviour); only the sha256 hash is kept.
"""
import json
import logging
import os
import time
import uuid

import httpx

log = logging.getLogger("npu-ops.keys")

LITELLM_API = os.environ.get("LITELLM_API",
                             "http://litellm-proxy.sir-llm-platform:4000")
LITELLM_MASTER_KEY = os.environ.get("LITELLM_MASTER_KEY", "")
LITELLM_DB_URL = os.environ.get("LITELLM_DB_URL", "")
ADMIN_TOKEN = os.environ.get("NPU_OPS_ADMIN_TOKEN", "")
KEYS_CM = ("npu-ops-keys", "npu-ops", "applies.json")


class KeysError(Exception):
    """Raised for user-facing errors (message goes to the UI)."""


# ------------------------------------------------------------- apply store (ConfigMap)

def read_applies():
    from kubernetes import client
    try:
        cm = _k8s().api.read_namespaced_config_map(KEYS_CM[0], KEYS_CM[1])
        return json.loads((cm.data or {}).get(KEYS_CM[2]) or "{}")
    except client.exceptions.ApiException as e:
        if e.status == 404:
            body = {"metadata": {"name": KEYS_CM[0], "namespace": KEYS_CM[1]},
                    "data": {KEYS_CM[2]: "{}"}}
            try:
                _k8s().api.create_namespaced_config_map(KEYS_CM[1], body=body)
            except client.exceptions.ApiException:
                pass
        return {}


def write_applies(doc):
    from kubernetes import client
    for _ in range(3):
        cm = _k8s().api.read_namespaced_config_map(KEYS_CM[0], KEYS_CM[1])
        cm.data = {KEYS_CM[2]: json.dumps(doc)}
        try:
            _k8s().api.replace_namespaced_config_map(KEYS_CM[0], KEYS_CM[1], body=cm)
            return
        except client.exceptions.ApiException as e:
            if e.status == 409:
                time.sleep(0.2)
                continue
            raise KeysError(f"failed to save applications: {e}")


def _k8s():
    # late import: main.K8s
    from main import get_k8s
    return get_k8s()


# ------------------------------------------------------------- LiteLLM proxy API

def _litellm(method, path, body=None):
    if not LITELLM_MASTER_KEY:
        raise KeysError("litellm master key not configured")
    try:
        r = httpx.request(method, LITELLM_API + path,
                          headers={"Authorization": "Bearer " + LITELLM_MASTER_KEY},
                          json=body, timeout=30)
    except httpx.HTTPError as e:
        raise KeysError(f"litellm unreachable: {e}")
    if r.status_code >= 400:
        raise KeysError(f"litellm {path} -> {r.status_code}: {r.text[:300]}")
    return r.json()


def list_models():
    d = _litellm("GET", "/v1/models")
    return [m.get("id") for m in d.get("data", []) if m.get("id")]


def generate_key(owner, full_name, team, models):
    payload = {"user_id": owner,
               "metadata": {"owner": owner}}
    if full_name:
        payload["metadata"]["full_name"] = full_name
    if team:
        payload["metadata"]["team"] = team
    if models:
        payload["models"] = models
    d = _litellm("POST", "/key/generate", payload)
    key = d.get("key")
    if not key:
        raise KeysError(f"litellm /key/generate returned no key: {str(d)[:200]}")
    return key, d.get("token")  # plaintext (once only), sha256 hash


def block_key(key_hash):
    _litellm("POST", "/key/block", {"key": key_hash})


# ------------------------------------------------------------- stats (LiteLLM Postgres)

def _db():
    if not LITELLM_DB_URL:
        raise KeysError("litellm database url not configured")
    import psycopg2
    try:
        return psycopg2.connect(LITELLM_DB_URL, connect_timeout=10)
    except Exception as e:
        raise KeysError(f"litellm db unreachable: {e}")


def _jsonify(v):
    if isinstance(v, str):
        try:
            return json.loads(v)
        except Exception:
            return v
    return v


def list_keys(include_blocked=True):
    """All virtual keys with aggregate usage (one row per key)."""
    q = """
      SELECT t."token", t.user_id, t.metadata, t.models,
             t.blocked, t."created_at", t.last_active,
             COALESCE(s.requests,0), COALESCE(s.total_tokens,0),
             COALESCE(s.prompt_tokens,0), COALESCE(s.completion_tokens,0)
      FROM "LiteLLM_VerificationToken" t
      LEFT JOIN (SELECT api_key, COUNT(*) AS requests,
                        COALESCE(SUM(total_tokens),0)    AS total_tokens,
                        COALESCE(SUM(prompt_tokens),0)   AS prompt_tokens,
                        COALESCE(SUM(completion_tokens),0) AS completion_tokens
                 FROM "LiteLLM_SpendLogs"
                 GROUP BY api_key) s ON s.api_key = t."token"
      WHERE t.user_id IS NOT NULL AND t.user_id <> 'default_user_id'
        -- 'default_user_id' rows are LiteLLM's internal master-key entries
    """
    conn = _db()
    try:
        with conn.cursor() as cur:
            cur.execute(q)
            out = []
            for (kh, user_id, metadata, models, blocked,
                 created, last_active, requests, total_tokens, prompt_tokens,
                 completion_tokens) in cur.fetchall():
                md = _jsonify(metadata) or {}
                out.append({
                    "hash": kh,
                    "owner": user_id or md.get("owner") or "unassigned",
                    "full_name": md.get("full_name", ""),
                    "team": md.get("team", ""),
                    "models": list(models or []),
                    "blocked": bool(blocked),
                    "created": created.isoformat() if created else None,
                    "last_active": last_active.timestamp() if last_active else None,
                    "requests": int(requests),
                    "total_tokens": int(total_tokens or 0),
                    "prompt_tokens": int(prompt_tokens or 0),
                    "completion_tokens": int(completion_tokens or 0),
                })
            return out
    finally:
        conn.close()


def key_detail(key_hash):
    """Per-day (30d) and per-model usage for one key."""
    conn = _db()
    try:
        with conn.cursor() as cur:
            cur.execute("""
              SELECT to_char("startTime",'YYYY-MM-DD') d, COUNT(*),
                     COALESCE(SUM(total_tokens),0), COALESCE(SUM(spend),0)
              FROM "LiteLLM_SpendLogs"
              WHERE api_key=%s AND "startTime" >= now() - interval '30 days'
              GROUP BY 1 ORDER BY 1""", (key_hash,))
            daily = [{"date": d, "requests": int(r), "tokens": int(t),
                      "spend": float(s or 0)} for d, r, t, s in cur.fetchall()]
            cur.execute("""
              SELECT model, COUNT(*), COALESCE(SUM(total_tokens),0),
                     COALESCE(SUM(spend),0)
              FROM "LiteLLM_SpendLogs" WHERE api_key=%s
              GROUP BY 1 ORDER BY 2 DESC""", (key_hash,))
            by_model = [{"model": m, "requests": int(r), "tokens": int(t),
                         "spend": float(s or 0)} for m, r, t, s in cur.fetchall()]
            cur.execute("""
              SELECT to_char(MAX("startTime"),'YYYY-MM-DD HH24:MI:SS')
              FROM "LiteLLM_SpendLogs" WHERE api_key=%s""", (key_hash,))
            last = cur.fetchone()[0]
    finally:
        conn.close()
    return {"daily": daily, "by_model": by_model, "last_request": last}


# ------------------------------------------------------------- workflow

def apply_key(body):
    owner = str(body.get("owner") or "").strip()           # Huawei ID / username
    full_name = str(body.get("full_name") or "").strip()   # real name, mandatory
    team = str(body.get("team") or "").strip()             # mandatory
    if not owner:
        raise KeysError("Huawei ID / username is required")
    if len(owner) > 64 or any(ch.isspace() for ch in owner):
        raise KeysError("Huawei ID: 1-64 chars, no spaces")
    if not full_name:
        raise KeysError("full name is required")
    if len(full_name) > 80:
        raise KeysError("full name too long (max 80 chars)")
    if not team:
        raise KeysError("team is required")
    if len(team) > 64:
        raise KeysError("team too long (max 64 chars)")
    doc = read_applies()
    rid = "ap-" + uuid.uuid4().hex[:8]
    doc[rid] = {
        "id": rid,
        "owner": owner,
        "full_name": full_name,
        "team": team,
        "models": [str(m) for m in (body.get("models") or []) if str(m).strip()],
        "status": "pending",
        "created": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    write_applies(doc)
    return doc[rid]


def list_applies():
    doc = read_applies()
    order = {"pending": 0, "approved": 1, "rejected": 2}
    out = list(doc.values())
    out.sort(key=lambda r: r.get("created", ""), reverse=True)
    out.sort(key=lambda r: order.get(r.get("status"), 9))  # stable: pending first
    return out


def approve_apply(rid):
    doc = read_applies()
    req = doc.get(rid)
    if not req:
        raise KeysError(f"unknown application {rid}")
    if req.get("status") != "pending":
        raise KeysError(f"application is {req.get('status')}, not pending")
    key, key_hash = generate_key(req["owner"], req.get("full_name", ""),
                                 req.get("team", ""), req.get("models"))
    req["status"] = "approved"
    req["key_hash"] = key_hash
    req["decided"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    write_applies(doc)
    log.info("approved %s -> key %s for %s (%s)", rid, key_hash[:12],
             req["owner"], req.get("full_name", ""))
    return {"plaintext_key": key, "key_hash": key_hash,
            "owner": req["owner"], "full_name": req.get("full_name", ""),
            "models": req.get("models", [])}


def reject_apply(rid):
    doc = read_applies()
    req = doc.get(rid)
    if not req:
        raise KeysError(f"unknown application {rid}")
    if req.get("status") != "pending":
        raise KeysError(f"application is {req.get('status')}, not pending")
    req["status"] = "rejected"
    req["decided"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    write_applies(doc)
    return req
