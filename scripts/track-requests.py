#!/usr/bin/env python3
"""
Request tracking for the sir-llm-platform LiteLLM proxy, based on its
uvicorn access logs.

Whoever submits a request is identified by the source IP the proxy pod
sees: external clients hitting the NodePort show their real IP (kube-proxy
DNATs the destination, no SNAT), in-cluster clients show pod IPs.

Two modes:

    # Summary for the last hour, aggregated by client IP (default)
    python3 scripts/track-requests.py

    # Same, but last 24 hours / custom namespace or deployment
    python3 scripts/track-requests.py --since 24h
    KUBE_NS=other-ns python3 scripts/track-requests.py --deploy litellm-proxy

    # Live tail: one line per new request as it happens
    python3 scripts/track-requests.py --tail

    # Machine-readable summary
    python3 scripts/track-requests.py --since 6h --json

Notes:
  * Health/metrics endpoints are filtered out by default (--all disables).
  * Each access-log line = one request. Streaming (SSE) requests may be
    logged by uvicorn at response start, so counts are a lower bound.
  * Clients behind the same NAT share one IP — keys (a later upgrade)
    give per-user identity.

Only the Python standard library is used; kubectl must be on PATH with
access to the cluster.
"""

import argparse
import json
import os
import re
import subprocess
import sys
from collections import defaultdict

# u"IP:PORT - \"METHOD PATH HTTP/1.1\" STATUS" with an optional
# "2026-09-28T02:33:01.123456789Z " kubectl --timestamps prefix.
LINE_RE = re.compile(
    r'^(?P<ts>\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})?)?\s+'
    r'(?:\w+:\s+)?'
    r'(?P<ip>\S+?):(?P<port>\d+) - '
    r'"(?P<method>[A-Z]+) (?P<path>\S+)(?: [^"]*)?" '
    r'(?P<status>\d{3})'
)

INFER_PREFIXES = ("/v1/", "/raw/", "/262k/")
NOISE_PREFIXES = ("/health", "/metrics", "/ui", "/favicon", "/")


def parse_line(raw):
    m = LINE_RE.match(raw.strip())
    if not m:
        return None
    return {
        "ts": m.group("ts") or "",
        "ip": m.group("ip"),
        "method": m.group("method"),
        "path": m.group("path"),
        "status": int(m.group("status")),
    }


def is_interesting(req, all_paths):
    if all_paths:
        return True
    if req["method"] == "POST":
        return True
    return req["path"].startswith(INFER_PREFIXES)


def kubectl_logs(ns, deploy, since, follow, tail_lines=0):
    cmd = ["kubectl", "logs", "-n", ns, "deploy/" + deploy,
           "--timestamps"]
    if since:
        cmd.append("--since=" + since)
    if follow:
        cmd.append("-f")
        if tail_lines:
            cmd.append("--tail=" + str(tail_lines))
    return subprocess.run(cmd, stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE, text=True,
                          check=False) if not follow else subprocess.Popen(
                              cmd, stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, text=True)


def summarize(reqs):
    by_ip = defaultdict(lambda: {
        "count": 0,
        "endpoints": defaultdict(int),
        "first": None,
        "last": None,
        "errors": [],
    })
    for r in reqs:
        b = by_ip[r["ip"]]
        b["count"] += 1
        b["endpoints"][r["path"]] += 1
        if r["ts"]:
            if b["first"] is None or r["ts"] < b["first"]:
                b["first"] = r["ts"]
            if b["last"] is None or r["ts"] > b["last"]:
                b["last"] = r["ts"]
        if r["status"] >= 400:
            b["errors"].append(r)
    return by_ip


def fmt_ts(ts):
    # 2026-09-28T02:33:01.123456789Z -> 09-28 02:33:01
    m = re.match(r"\d{4}-(\d{2}-\d{2})T(\d{2}:\d{2}:\d{2})", ts)
    return f"{m.group(1)} {m.group(2)}" if m else (ts or "-")


def print_summary(by_ip, reqs, args):
    rows = sorted(by_ip.items(),
                  key=lambda kv: (-kv[1]["count"],
                                  kv[1]["last"] or ""))
    if not rows:
        print("No matching requests in the selected window.")
        return

    if args.json:
        out = {
            "window": args.since or "full",
            "clients": [
                {
                    "ip": ip,
                    "requests": b["count"],
                    "endpoints": dict(b["endpoints"]),
                    "first_seen": b["first"],
                    "last_seen": b["last"],
                    "errors": [
                        {k: r[k] for k in ("ts", "method", "path", "status")}
                        for r in b["errors"]
                    ],
                }
                for ip, b in rows
            ],
        }
        print(json.dumps(out, indent=2))
        return

    print(f"Request summary (last {args.since or 'full log window'})\n")
    header = (f"  {'CLIENT IP':<20} {'REQ':>4}  "
              f"{'ENDPOINTS':<42} {'FIRST SEEN':<16} LAST SEEN")
    print(header)
    print("  " + "-" * (len(header) - 2))
    for ip, b in rows:
        eps = ", ".join(f"{p} ({n})"
                        for p, n in sorted(b["endpoints"].items(),
                                           key=lambda kv: -kv[1]))
        print(f"  {ip:<20} {b['count']:>4}  {eps:<42} "
              f"{fmt_ts(b['first'] or ''):<16} {fmt_ts(b['last'] or '')}")

    errors = [r for b in by_ip.values() for r in b["errors"]]
    if errors:
        print(f"\nNon-2xx responses ({len(errors)}):")
        for r in errors[-20:]:
            print(f"  {fmt_ts(r['ts'])}  {r['ip']:<20} "
                  f"{r['method']} {r['path']}  {r['status']}")

    limit = args.limit
    recent = reqs[-limit:] if limit else []
    if recent:
        print(f"\nLast {len(recent)} requests:")
        for r in recent:
            print(f"  {fmt_ts(r['ts'])}  {r['ip']:<20} "
                  f"{r['method']:<6} {r['path']:<32} {r['status']}")


def tail(raw_lines, all_paths):
    for raw in raw_lines:
        r = parse_line(raw)
        if r is None or not is_interesting(r, all_paths):
            continue
        mark = " " if r["status"] < 400 else "!"
        print(f"{fmt_ts(r['ts']):<16} {mark} {r['ip']:<20} "
              f"{r['method']:<6} {r['path']:<32} {r['status']}",
              flush=True)


def main():
    ap = argparse.ArgumentParser(
        description="Track requests hitting the sir-llm-platform "
                    "LiteLLM proxy, by client IP.")
    ap.add_argument("--since", default="60m",
                    help="window for summary mode, e.g. 90s, 10m, 24h "
                         "(default: 60m; ignored with --tail)")
    ap.add_argument("--tail", action="store_true",
                    help="live mode: print each new request as it arrives")
    ap.add_argument("--tail-lines", type=int, default=50,
                    help="how many existing log lines to replay before "
                         "following in --tail mode (default 50, 0 for all)")
    ap.add_argument("--all", action="store_true",
                    help="include health/metrics/UI requests too")
    ap.add_argument("--json", action="store_true",
                    help="emit the summary as JSON (summary mode only)")
    ap.add_argument("--limit", type=int, default=10,
                    help="how many of the most recent requests to list "
                         "(default 10, 0 for none)")
    ap.add_argument("--namespace", default=os.environ.get("KUBE_NS",
                                                          "sir-llm-platform"))
    ap.add_argument("--deploy", default=os.environ.get("LITELLM_DEPLOY",
                                                       "litellm-proxy"))
    args = ap.parse_args()

    if args.tail:
        proc = kubectl_logs(args.namespace, args.deploy, None, True,
                            args.tail_lines)
        try:
            tail(iter(proc.stdout.readline, ""), args.all)
        except KeyboardInterrupt:
            pass
        finally:
            proc.terminate()
    else:
        res = kubectl_logs(args.namespace, args.deploy, args.since, False)
        if res.returncode != 0:
            sys.exit("kubectl failed:\n" + res.stderr.strip())
        reqs = [r for raw in res.stdout.splitlines()
                if (r := parse_line(raw))
                and is_interesting(r, args.all)]
        print_summary(summarize(reqs), reqs, args)


if __name__ == "__main__":
    main()
