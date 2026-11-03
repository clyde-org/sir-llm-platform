#!/usr/bin/env bash
# Rotate the LiteLLM master key (sir-llm-platform).
#
# The old key is read from the live ConfigMap (never passed as an argument,
# never logged in full). The new key is generated at runtime and printed ONCE
# at the end — store it in your password manager. No rollback is automatic;
# a ready-to-run rollback script (with the old key) is written to /tmp.
#
# Design: zero hard downtime for virtual-key clients. Patching the ConfigMap
# triggers the deployment's checksum-annotated rollout (1 replica, default
# RollingUpdate => maxUnavailable=0, maxSurge=1): the NEW pod must pass
# /health/readiness (~60-80s, initialDelay 60s) before the OLD pod is
# terminated. Both pods share the same Postgres, so virtual keys work on
# either pod throughout. Only the old MASTER key is affected (intermittent
# 401s while the new pod is up but the old one not yet drained, then hard 401).
#
# Run from a machine with kubectl access to the cluster. Total: ~4-6 min.
set -euo pipefail

NS=sir-llm-platform
MONNS=monitoring
OPSNS=npu-ops
GATEWAY="http://7.242.101.107:30400"      # any node's LiteLLM NodePort
PROM="http://7.242.101.107:30900"
OPS="http://7.242.101.107:30410"
STAMP=$(date +%Y%m%d-%H%M%S)

step() { echo; echo "=== $*"; }
fail() { echo "GATE FAILED: $*" >&2; echo "ABORTED before further mutations — see log above." >&2; exit 1; }
code() { curl -s -o /dev/null -w "%{http_code}" --max-time 20 "$@"; }

step "0. preflight"
kubectl -n "$NS" get deploy litellm-proxy >/dev/null || fail "kubectl/cluster unreachable"
CM=$(kubectl -n "$NS" get cm litellm-config -o yaml)
echo "$CM" > "/tmp/litellm-config-backup-$STAMP.yaml"
OLD_KEY=$(kubectl -n "$NS" get cm litellm-config -o jsonpath='{.data.litellm_config\.yaml}' \
  | awk '/master_key:/{gsub(/"/,"",$2); print $2}')
[ ${#OLD_KEY} -gt 8 ] || fail "could not read current master key from ConfigMap"
echo "old key: ${OLD_KEY:0:6}...  configmap backup: /tmp/litellm-config-backup-$STAMP.yaml"

NEW_KEY="sk-$(openssl rand -hex 24)"

step "1. patch litellm-config ConfigMap + explicit rollout restart"
NEW_YAML=$(kubectl -n "$NS" get cm litellm-config -o jsonpath='{.data.litellm_config\.yaml}' \
  | python3 -c '
import sys
old, new = sys.argv[1], sys.argv[2]
y = sys.stdin.read()
assert f"master_key: \"{old}\"" in y, "old master key not found in ConfigMap data"
print(y.replace(f"master_key: \"{old}\"", f"master_key: \"{new}\""), end="")' "$OLD_KEY" "$NEW_KEY")
kubectl -n "$NS" patch cm litellm-config --type merge \
  -p "$(python3 -c 'import json,sys; print(json.dumps({"data":{"litellm_config.yaml": sys.stdin.read()}}))' <<<"$NEW_YAML")" \
  || fail "ConfigMap patch failed"
# The pod-template checksum is derived from chart values, not the CM contents,
# so patching the CM does NOT roll the pod on its own — restart explicitly.
kubectl -n "$NS" rollout restart deploy/litellm-proxy || fail "rollout restart failed"
echo "ConfigMap patched, restart triggered"

step "2. wait for rollout (old pod keeps serving; new pod ~60-80s to become ready)"
kubectl -n "$NS" rollout status deploy/litellm-proxy --timeout=300s || fail "rollout did not complete"

step "3. gates: new key works (models + PONG), old key is dead"
[ "$(code "$GATEWAY/v1/models" -H "Authorization: Bearer $NEW_KEY")" = "200" ] \
  || fail "new key rejected by gateway"
PONG=$(curl -s --max-time 120 "$GATEWAY/v1/chat/completions" \
  -H "Authorization: Bearer $NEW_KEY" -H "Content-Type: application/json" \
  -d '{"model":"qwen3.8-27b","messages":[{"role":"user","content":"Reply with exactly: PONG"}],"max_tokens":256}')
echo "$PONG" | grep -q '"content":"[^"]*PONG' || fail "PONG check failed: $PONG"
echo "PONG with new key: OK"
OLD_CODE=$(code -X POST "$GATEWAY/v1/chat/completions" \
  -H "Authorization: Bearer $OLD_KEY" -H "Content-Type: application/json" \
  -d '{"model":"qwen3.8-27b","messages":[{"role":"user","content":"hi"}],"max_tokens":5}')
[ "$OLD_CODE" = "401" ] || fail "old key still accepted (HTTP $OLD_CODE)"
echo "old key -> 401: OK"

step "4. monitoring bearer token (litellm /metrics scrape)"
kubectl -n "$MONNS" patch secret litellm-bearer-token \
  -p "{\"data\":{\"token\":\"$(printf %s "$NEW_KEY" | base64 -w0)\"}}" || fail "monitoring secret patch"
sleep 35   # two 15s scrape cycles
curl -su admin:prometheus-admin "$PROM/api/v1/targets" | python3 -c '
import json, sys
ts = [t for t in json.load(sys.stdin)["data"]["activeTargets"]
      if "litellm" in (t["labels"].get("job") or "")]
assert ts, "no litellm scrape targets found"
bad = [f"{t[\"scrapeUrl\"]} ({t[\"labels\"].get(\"lastError\",\"?\")})" for t in ts if t["health"] != "up"]
assert not bad, f"litellm scrape targets not up: {bad}"
print(f"litellm scrape targets up: {len(ts)}")' || fail "litellm scrape target unhealthy"

step "5. npu-ops dashboard (key approvals) — secret + restart"
kubectl -n "$OPSNS" patch secret npu-ops-litellm \
  -p "{\"data\":{\"LITELLM_MASTER_KEY\":\"$(printf %s "$NEW_KEY" | base64 -w0)\"}}" || fail "npu-ops secret patch"
kubectl -n "$OPSNS" rollout restart deploy/npu-ops
kubectl -n "$OPSNS" rollout status deploy/npu-ops --timeout=300s || fail "npu-ops rollout"
ADM_PWD=$(kubectl -n "$OPSNS" get secret npu-ops-litellm -o jsonpath='{.data.NPU_OPS_ADMIN_PASSWORD}' | base64 -d)
TOKEN=$(curl -s -X POST "$OPS/api/auth/login" -H "Content-Type: application/json" \
  -d "{\"password\":\"$ADM_PWD\"}" | python3 -c 'import json,sys; print(json.load(sys.stdin)["token"])') \
  || fail "dashboard login failed"
[ "$(code "$OPS/api/keys/applies" -H "Authorization: Bearer $TOKEN")" = "200" ] \
  || fail "dashboard applies endpoint failed"
echo "dashboard login + applies: OK"
# mint + block a throwaway key through the same path the dashboard uses,
# then remove its row so the admin list stays clean
TEST_HASH=$(curl -s -X POST "$GATEWAY/key/generate" \
  -H "Authorization: Bearer $NEW_KEY" -H "Content-Type: application/json" \
  -d '{"user_id":"rotate-check"}' \
  | python3 -c 'import json,sys; print(json.load(sys.stdin)["token"])') \
  || fail "key/generate with new master key failed"
curl -s -X POST "$GATEWAY/key/block" -H "Authorization: Bearer $NEW_KEY" \
  -H "Content-Type: application/json" -d "{\"key\":\"$TEST_HASH\"}" >/dev/null \
  || fail "key/block failed"
kubectl -n "$NS" exec deploy/postgres -- psql -U litellm -d litellm -t \
  -c "DELETE FROM \"LiteLLM_VerificationToken\" WHERE user_id='rotate-check';" >/dev/null
echo "mint+revoke test key with new master key: OK"

step "6. write rollback script"
cat > "/tmp/rollback-litellm-key-$STAMP.sh" <<EOF
#!/usr/bin/env bash
# Rollback: restore old master key ($OLD_KEY) and both secrets.
set -euo pipefail
kubectl apply -f /tmp/litellm-config-backup-$STAMP.yaml
kubectl -n $MONNS patch secret litellm-bearer-token -p '{"data":{"token":"'"$(printf %s "$OLD_KEY" | base64 -w0)"'"}}'
kubectl -n $OPSNS patch secret npu-ops-litellm -p '{"data":{"LITELLM_MASTER_KEY":"'"$(printf %s "$OLD_KEY" | base64 -w0)"'"}}'
kubectl -n $OPSNS rollout restart deploy/npu-ops
echo "rolled back to old master key"
EOF
chmod +x "/tmp/rollback-litellm-key-$STAMP.sh"

step "DONE — all gates passed"
echo "NEW MASTER KEY (store it; shown only once):"
echo "$NEW_KEY"
echo
echo "Now lives in: litellm-config CM (ns $NS), npu-ops-litellm secret (ns $OPSNS),"
echo "              litellm-bearer-token secret (ns $MONNS). Never in the repo."
echo "Rollback (restores the old key everywhere): bash /tmp/rollback-litellm-key-$STAMP.sh"
