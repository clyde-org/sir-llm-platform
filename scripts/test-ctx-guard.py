#!/usr/bin/env python3
"""
Context-guard / overflow contract tests for the sir-llm-platform LLM gateway.

Runs against the DEPLOYED LiteLLM endpoint from the outside (like any client),
so any platform user can run it:

    python3 scripts/test-ctx-guard.py

What it verifies (the router's CTX_GUARD + error-surfacing behaviour):

  OpenAI path (/v1/chat/completions) — used by pi, Open WebUI, BooM, etc.:
    1. over-context request        -> immediate 400, vLLM-shaped message
                                       "prompt is too long: N tokens > M maximum"
    2. over-context streaming      -> same 400 as JSON (NOT a fake SSE 200)
    3. over-context on 262k model  -> 400 with that pool's cap
    4. oversized max_tokens only   -> 400 (guard counts prompt + max_tokens)
    5. normal request (both pools) -> 200 with content
    6. near-cap request            -> 200 (guard must not false-reject; SLOW)
    7. overflow message is compatible with agent clients (pi's detector
       matches "prompt is too long")

  Anthropic path (/v1/messages) — used by Claude Code:
    8. over-context request        -> non-2xx with a descriptive
                                       "maximum context length" error
                                       (vLLM's own error, raw-forwarded)

Only the Python standard library is used. Environment overrides:

    CTX_TEST_URL          gateway base URL   (required — e.g. http://<NODE_IP>:30400)
    CTX_TEST_API_KEY      bearer key         (default sk-qwen38b-local)
    CTX_TEST_MODEL_131K   small-pool model   (default qwen3.8-27b)
    CTX_TEST_MODEL_262K   large-pool model   (default qwen3.8-27b-262k)
    CTX_TEST_CAP_131K     small-pool cap     (default 131072)
    CTX_TEST_CAP_262K     large-pool cap     (default 262144)
    CTX_TEST_SKIP_SLOW=1  skip the near-cap test #6 (runs a real ~115k-token
                          prefill, takes up to a minute on a busy pool)
"""

import json
import os
import re
import sys
import time
import urllib.error
import urllib.request

URL = os.environ.get("CTX_TEST_URL")
if not URL:
    sys.exit("CTX_TEST_URL is required (e.g. http://<NODE_IP>:30400)")
API_KEY = os.environ.get("CTX_TEST_API_KEY", "sk-qwen38b-local")
MODEL_131K = os.environ.get("CTX_TEST_MODEL_131K", "qwen3.8-27b")
MODEL_262K = os.environ.get("CTX_TEST_MODEL_262K", "qwen3.8-27b-262k")
CAP_131K = int(os.environ.get("CTX_TEST_CAP_131K", "131072"))
CAP_262K = int(os.environ.get("CTX_TEST_CAP_262K", "262144"))
SKIP_SLOW = os.environ.get("CTX_TEST_SKIP_SLOW", "0") == "1"

# Filler text: ~0.178 Qwen tokens/char (measured). Sizing:
FILLER_135K = "alpha beta gamma delta " * 33000   # ~135k tokens -> over 131k cap
FILLER_267K = "alpha beta gamma delta " * 66000   # ~267k tokens -> over 262k cap
FILLER_115K = "alpha beta gamma delta " * 27800   # ~115k tokens -> under 131k cap

PASS_RE = re.compile(r"prompt is too long", re.I)          # pi's overflow detector
CAP_131_RE = re.compile(r">\s*%d\s*maximum" % CAP_131K)
CAP_262_RE = re.compile(r">\s*%d\s*maximum" % CAP_262K)

_results = []


def check(name, cond, detail=""):
    status = "PASS" if cond else "FAIL"
    print(f"[{status}] {name}" + (f"\n       {str(detail)[:400]}" if detail and not cond else ""))
    _results.append((name, bool(cond)))


def post(path, body, stream=False, timeout=300):
    """POST JSON; returns (status, headers, body_text, elapsed_s)."""
    data = json.dumps(body).encode()
    req = urllib.request.Request(
        URL + path,
        data=data,
        headers={
            "Authorization": f"Bearer {API_KEY}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, dict(r.headers), r.read().decode("utf-8", "replace"), time.time() - t0
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read().decode("utf-8", "replace"), time.time() - t0


def chat_body(model, text, max_tokens=16, stream=False):
    b = {
        "model": model,
        "messages": [{"role": "user", "content": text}],
        "max_tokens": max_tokens,
    }
    if stream:
        b["stream"] = True
    return b


def main():
    print(f"gateway: {URL}  models: {MODEL_131K} / {MODEL_262K}")

    # 1. OpenAI non-stream overflow on 131k pool
    status, hdrs, body, dt = post("/v1/chat/completions",
                                  chat_body(MODEL_131K, FILLER_135K))
    msg = ""
    err_type = ""
    try:
        err = json.loads(body).get("error", {})
        msg, err_type = err.get("message", ""), err.get("type", "")
    except Exception:
        pass
    print(f"       (status={status} elapsed={dt:.1f}s) message: {msg[:150]}")
    check("1. 131k overflow -> 400", status == 400, f"status={status} body={body[:200]}")
    check("1b. vLLM-shaped message", PASS_RE.search(msg) is not None and CAP_131_RE.search(msg) is not None,
          f"message={msg[:200]}")
    check("1c. OpenAI error envelope", err_type == "invalid_request_error", f"error={err_type}")
    check("1d. immediate rejection (no queueing, <15s)", dt < 15, f"elapsed={dt:.1f}s")
    check("1e. agent-client compatible (pi detects overflow)", PASS_RE.search(msg) is not None,
          f"message={msg[:200]}")

    # 2. OpenAI STREAMING overflow on 131k pool (the original bug: fake SSE 200)
    status, hdrs, body, dt = post("/v1/chat/completions",
                                  chat_body(MODEL_131K, FILLER_135K, stream=True))
    ctype = hdrs.get("Content-Type", hdrs.get("content-type", ""))
    try:
        smsg = json.loads(body).get("error", {}).get("message", "")
    except Exception:
        smsg = ""
    print(f"       (status={status} content-type={ctype} elapsed={dt:.1f}s) message: {smsg[:150]}")
    check("2. 131k streaming overflow -> 400 JSON (not fake SSE 200)",
          status == 400 and "json" in ctype.lower(), f"status={status} ctype={ctype} body={body[:200]}")
    check("2b. streaming error message", PASS_RE.search(smsg) is not None and CAP_131_RE.search(smsg) is not None,
          f"message={smsg[:200]}")

    # 3. Overflow on 262k pool (per-model caps)
    status, hdrs, body, dt = post("/v1/chat/completions",
                                  chat_body(MODEL_262K, FILLER_267K))
    try:
        lmsg = json.loads(body).get("error", {}).get("message", "")
    except Exception:
        lmsg = ""
    print(f"       (status={status} elapsed={dt:.1f}s) message: {lmsg[:150]}")
    check("3. 262k overflow -> 400 with 262k cap",
          status == 400 and CAP_262_RE.search(lmsg) is not None, f"status={status} message={lmsg[:200]}")

    # 4. Small prompt + oversized max_tokens
    status, hdrs, body, dt = post("/v1/chat/completions",
                                  chat_body(MODEL_131K, "hello", max_tokens=200000))
    try:
        mmsg = json.loads(body).get("error", {}).get("message", "")
    except Exception:
        mmsg = ""
    check("4. oversized max_tokens -> 400",
          status == 400 and PASS_RE.search(mmsg) is not None, f"status={status} message={mmsg[:200]}")

    # 5. Normal requests still work (both pools)
    status, hdrs, body, dt = post("/v1/chat/completions",
                                  chat_body(MODEL_131K, "Reply with exactly: OK", max_tokens=32), timeout=180)
    ok = False
    detail = f"status={status} body={body[:200]}"
    try:
        content = json.loads(body)["choices"][0]["message"]["content"]
        ok = status == 200 and bool(content and content.strip())
        detail = f"status={status} content={content!r}"
    except Exception:
        pass
    check("5. 131k normal request -> 200 + content", ok, detail)

    status, hdrs, body, dt = post("/v1/chat/completions",
                                  chat_body(MODEL_262K, "Reply with exactly: OK", max_tokens=32), timeout=180)
    ok = False
    detail = f"status={status} body={body[:200]}"
    try:
        content = json.loads(body)["choices"][0]["message"]["content"]
        ok = status == 200 and bool(content and content.strip())
        detail = f"status={status} content={content!r}"
    except Exception:
        pass
    check("5b. 262k normal request -> 200 + content", ok, detail)

    # 6. Near-cap request must PASS (guard must not false-reject) — SLOW
    if SKIP_SLOW:
        print("[SKIP] 6. near-cap pass (CTX_TEST_SKIP_SLOW=1)")
    else:
        print("       (near-cap test: real ~115k-token prefill, up to ~1 min ...)")
        status, hdrs, body, dt = post("/v1/chat/completions",
                                      chat_body(MODEL_131K, FILLER_115K, max_tokens=16), timeout=600)
        ok = False
        detail = f"status={status} elapsed={dt:.1f}s body={body[:200]}"
        if status == 400:
            detail += " -> FALSE REJECTION: guard tokenization disagrees with vLLM by more than the margin"
        try:
            if status == 200:
                ok = bool(json.loads(body)["choices"])
        except Exception:
            pass
        check("6. 131k near-cap (~115k tokens) -> 200 (no false rejection)", ok, detail)

    # 7. Anthropic path (Claude Code): overflow -> non-2xx + descriptive error
    status, hdrs, body, dt = post(
        "/v1/messages",
        {"model": MODEL_131K, "max_tokens": 16,
         "messages": [{"role": "user", "content": FILLER_135K}]},
    )
    desc = "maximum context length" in body.lower() or "prompt is too long" in body.lower() \
        or "context" in body.lower()
    print(f"       (status={status} elapsed={dt:.1f}s) body: {body[:200]}")
    check("7. Anthropic path overflow -> non-2xx with descriptive error",
          300 <= status < 600 and desc, f"status={status} body={body[:300]}")

    # Summary
    failed = [n for n, ok in _results if not ok]
    print()
    if failed:
        print(f"RESULT: {len(failed)}/{len(_results)} FAILED: {failed}")
        sys.exit(1)
    print(f"RESULT: all {len(_results)} checks passed")


if __name__ == "__main__":
    main()
