#!/usr/bin/env python3
"""Gateway cutover for deepseek-v4.1-flash (kubectl-only; no helm).

Steps (run in order):
  1. litellm   — add model entry to litellm-config CM + force rollout
                 (the live pod template has a static checksum annotation,
                 so a CM edit alone would NOT restart litellm)
  2. router    — patch ANTHROPIC_UPSTREAMS on router-service (adds
                 /v1/messages raw-forward for the new model)
  3. keys      — add the model to every approved user's litellm key
                 restriction + npu-ops-keys applies.json
  4. canary    — PONG through the gateway with the new model

Usage: cutover-gateway.py [litellm|router|keys|canary|all]
"""
import json, os, subprocess, sys, urllib.request, time

NS = "sir-llm-platform"
MODEL = "deepseek-v4.1-flash"
GATEWAY = "http://7.242.101.107:30400"
# LiteLLM master key — from the live cluster, never committed (see
# scripts/rotate-litellm-master-key.sh):
#   export LITELLM_MASTER=$(kubectl get cm litellm-config -n $NS \
#     -o jsonpath='{.data.litellm_config\.yaml}' | awk '/master_key:/{gsub(/"/,"",$2); print $2}')
MASTER = os.environ.get("LITELLM_MASTER", "")
if not MASTER:
    sys.exit("LITELLM_MASTER not set (master key from litellm-config CM)")

def k(*args):
    r = subprocess.run(["kubectl", *args], check=True, capture_output=True, text=True)
    return r.stdout

def litellm_patch():
    cur = k("get", "cm", "litellm-config", "-n", NS, "-o", "jsonpath={.data.litellm_config\\.yaml}")
    if MODEL in cur:
        print("litellm config already has the model entry")
    else:
        entry = (
            "  # DeepSeek-V4.1-Flash-1017 on node3+node4 (DP2/TP8/EP16, 1M context).\n"
            "  # Direct upstream (no kv-router): this pool has no kv-sidecar/KV\n"
            "  # events; vLLM's own max-model-len rejection covers overflow.\n"
            f'  - model_name: "{MODEL}"\n'
            "    litellm_params:\n"
            f'      model: "openai/{MODEL}"\n'
            '      api_base: "http://dsv41:8000/v1"\n'
            '      api_key: "dummy"\n'
        )
        cur2 = cur.replace("router_settings:", entry + "router_settings:")
        assert cur2 != cur, "insertion anchor not found"
        data = json.dumps({"data": {"litellm_config.yaml": cur2}})
        k("patch", "cm", "litellm-config", "-n", NS, "--type", "merge", "-p", data)
        print("litellm-config patched")
    # force the pod template to change (static checksum annotation)
    k("patch", "deploy", "litellm-proxy", "-n", NS, "-p",
      json.dumps({"spec": {"template": {"metadata": {"annotations": {
          "checksum/litellm-config": f"manual-{int(time.time())}"}}}}})))
    print("litellm rollout triggered")

def router_patch():
    cur = json.loads(k("get", "deploy", "router-service", "-n", NS,
                       "-o", "jsonpath={.spec.template.spec.containers[0].env}"))
    for e in cur:
        if e["name"] == "ANTHROPIC_UPSTREAMS":
            m = json.loads(e["value"].replace("'", '"')) if e["value"].strip() not in ("", "{  }") else {}
            if not m:  # tolerate the live '{  }'
                m = {}
            m[MODEL] = f"http://dsv41:8000/v1/messages"
            newval = json.dumps(m, separators=(",", ":"))
            e["value"] = newval
            break
    k("patch", "deploy", "router-service", "-n", NS,
      "-p", json.dumps({"spec": {"template": {"spec": {"containers": [
          {"name": "router", "env": [{"name": "ANTHROPIC_UPSTREAMS", "value": newval}]}]}}}}))
    print("router ANTHROPIC_UPSTREAMS patched ->", newval)

def keys_patch():
    # 1) every litellm virtual key
    hashes, page = [], 1
    while True:
        with urllib.request.urlopen(urllib.request.Request(
                f"{GATEWAY}/key/list",
                headers={"Authorization": f"Bearer {MASTER}"})) as r:
            d = json.load(r)
        hashes += d.get("keys", [])
        if page >= d.get("total_pages", 1):
            break
        page += 1
        time.sleep(0.2)
    for h in hashes:
        with urllib.request.urlopen(urllib.request.Request(
                f"{GATEWAY}/key/info?key={h}",
                headers={"Authorization": f"Bearer {MASTER}"})) as r:
            info = json.load(r)["info"]
        models = info.get("models") or []
        if MODEL in models:
            continue
        body = json.dumps({"models": models + [MODEL]}).encode()
        req = urllib.request.Request(
            f"{GATEWAY}/key/update?key={h}", data=body, method="POST",
            headers={"Authorization": f"Bearer {MASTER}",
                     "Content-Type": "application/json"})
        with urllib.request.urlopen(req) as r:
            json.load(r)
        print(f"key {h[:12]}...: {models} -> +{MODEL}")
    # 2) npu-ops applies.json (dashboard source of truth)
    cur = k("get", "cm", "npu-ops-keys", "-n", "npu-ops", "-o", "jsonpath={.data.applies\\.json}")
    applies = json.loads(cur)
    for _id, rec in applies.items():
        models = rec.get("models") or []
        if MODEL not in models and rec.get("status") == "approved":
            rec["models"] = models + [MODEL]
    data = json.dumps({"data": {"applies.json": json.dumps(applies)}})
    k("patch", "cm", "npu-ops-keys", "-n", "npu-ops", "--type", "merge", "-p", data)
    print("applies.json updated")

def canary():
    body = json.dumps({
        "model": MODEL,
        "messages": [{"role": "user", "content": "Reply with exactly: PONG"}],
        "max_tokens": 32, "temperature": 0,
    }).encode()
    req = urllib.request.Request(
        f"{GATEWAY}/v1/chat/completions", data=body, method="POST",
        headers={"Authorization": f"Bearer {MASTER}",
                 "Content-Type": "application/json"})
    t0 = time.time()
    with urllib.request.urlopen(req, timeout=300) as r:
        d = json.load(r)
    msg = d["choices"][0]["message"]
    print(f"canary in {time.time()-t0:.1f}s -> content={msg.get('content')!r} "
          f"reasoning={str(msg.get('reasoning_content'))[:60]!r} "
          f"usage={d.get('usage')}")

if __name__ == "__main__":
    step = sys.argv[1] if len(sys.argv) > 1 else "all"
    if step in ("litellm", "all"): litellm_patch()
    if step in ("router", "all"): router_patch()
    if step in ("keys", "all"): keys_patch()
    if step in ("canary", "all"): canary()
