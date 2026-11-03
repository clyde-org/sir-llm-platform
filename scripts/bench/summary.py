#!/usr/bin/env python3
"""
Per-phase summary for the sir-llm-platform benchmark.

Reads:
  - manifest.jsonl  (written by loadgen runs; phase windows + client-side stats)
  - *.summary.json  (loadgen per-run summaries)
  - /mnt/data/experiments/<dir>/run_summary.json  (harness runs, P2/P4a)

Pulls the matching Prometheus windows (router / vLLM / NPU / gateway) via
kubectl exec + wget against the in-cluster Prometheus, and writes:
  <out>/summary.md        combined markdown table
  <out>/summary-prom.json raw per-phase Prometheus values

Usage:
  python3 summary.py --report-dir bench/reports/2026-10-06
"""
import argparse
import json
import subprocess
import time
import urllib.parse
from pathlib import Path

NS = "sir-llm-platform"
PROMPOD = "prometheus-prometheus-kube-prometheus-prometheus-0"
NSL = f'{{namespace="{NS}"}}'

# (key, promql) — evaluated as instant query at phase-end + 30 s
QUERIES = {
    "router_queue_max": f"max_over_time(max(router_central_queue_length{NSL})[__W__:])",
    "router_qwait_p95": f"histogram_quantile(0.95, sum by(le)(rate(router_request_queue_wait_seconds_bucket{NSL}[3m])))",
    "router_admit_rps": f"sum(rate(router_admission_requests_total{NSL}[2m]))",
    "router_dispatch_rps": f"sum(rate(router_dispatch_dispatch_requests_total{NSL}[2m])) or sum(rate(router_dispatch_requests_total{NSL}[2m]))",
    "router_ttft_p95": f"histogram_quantile(0.95, sum by(le)(rate(router_request_ttft_seconds_bucket{NSL}[3m])))",
    "router_e2e_p95": f"histogram_quantile(0.95, sum by(le)(rate(router_request_e2e_seconds_bucket{NSL}[3m])))",
    "router_cpu_cores": f'sum(rate(process_cpu_seconds_total{{namespace="{NS}",pod=~"router-service-.*"}}[2m]))',
    "router_rss_gb": f'max(process_resident_memory_bytes{{namespace="{NS}",pod=~"router-service-.*"}})/1e9',
    "vllm_running": f"sum(vllm:num_requests_running{NSL})",
    "vllm_waiting": f"sum(vllm:num_requests_waiting{NSL})",
    "vllm_kv_pct_max": f"max(max by(pod)(vllm:kv_cache_usage_perc{NSL}))*100",
    "vllm_preempt": f"sum(increase(vllm:num_preemptions_total{NSL}[__W__]))",
    "vllm_ttft_p95": f"histogram_quantile(0.95, sum by(le)(rate(vllm:time_to_first_token_seconds_bucket{NSL}[3m])))",
    "vllm_itl_p95": f"histogram_quantile(0.95, sum by(le)(rate(vllm:inter_token_latency_seconds_bucket{NSL}[3m])))",
    "prefix_cache_hit_pct": f"sum(rate(vllm:prefix_cache_hits_total{NSL}[3m]))/sum(rate(vllm:prefix_cache_queries_total{NSL}[3m]))*100",
    "affinity_hits_rps": f"sum(rate(router_affinity_hits_total{NSL}[3m]))",
    "npu_aicore_pct_max": "max(avg_over_time(npu_chip_info_utilization[__W__:]))",
    "npu_temp_max": "max(max_over_time(npu_chip_info_temperature[__W__:]))",
    "gw_req_rps": f"sum(rate(litellm_proxy_total_requests_metric_total{NSL}[2m]))",
    "gw_failed": f"sum(increase(litellm_proxy_failed_requests_metric_total{NSL}[__W__]))",
}


def prom_query(expr, at_ts):
    enc = urllib.parse.quote(expr)
    cmd = [
        "kubectl", "-n", "monitoring", "exec", PROMPOD, "-c", "prometheus", "--",
        "wget", "-qO-", f"http://localhost:9090/api/v1/query?query={enc}&time={int(at_ts)}",
    ]
    try:
        raw = subprocess.run(cmd, capture_output=True, text=True, timeout=60).stdout
        d = json.loads(raw)
        if d.get("status") != "success":
            return None
        res = d["data"]["result"]
        return float(res[0]["value"][1]) if res else None
    except Exception:
        return None


def window_s(start_iso, end_iso):
    fmt = "%Y-%m-%dT%H:%M:%SZ"
    try:
        return (time.mktime(time.strptime(end_iso, fmt))
                - time.mktime(time.strptime(start_iso, fmt)))
    except Exception:
        return 0.0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--report-dir", required=True)
    args = ap.parse_args()
    rdir = Path(args.report_dir)
    manifest = rdir / "manifest.jsonl"
    if not manifest.exists():
        raise SystemExit(f"no {manifest}")

    phases = []
    for line in manifest.read_text().splitlines():
        if line.strip():
            phases.append(json.loads(line))
    phases.sort(key=lambda p: p.get("start", ""))

    out = {}
    for p in phases:
        start, end = p.get("start"), p.get("end")
        if not start or not end:
            continue
        ws = window_s(start, end)
        if ws <= 0:
            continue
        w = f"{int(ws) + 60}s"
        end_ts = time.mktime(time.strptime(end, "%Y-%m-%dT%H:%M:%SZ"))
        row = {"phase": p["tag"], "start": start, "end": end,
               "client": {k: p.get(k) for k in
                          ("ok", "errors", "throughput_rps", "aborted")}}
        for k, q in QUERIES.items():
            row[k] = prom_query(q.replace("__W__", w), end_ts + 30)
        out[p["tag"]] = row

    (rdir / "summary-prom.json").write_text(json.dumps(out, indent=2))

    # markdown table
    def f(x, spec=".3f"):
        return format(x, spec) if isinstance(x, (int, float)) else "-"
    lines = [
        "# Bench summary", "",
        "| phase | ok | err | client rps | router q(max) | qwait p95 (s) | admit (rps) | "
        "r-ttft p95 (s) | r-e2e p95 (s) | rcpu | rss GB | vllm run | kv % | preempt | "
        "v-ttft p95 (s) | prefix hit % | affinity (rps) | npu % | npu C | gw rps | gw fail |",
        "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for tag, r in out.items():
        c = r["client"]
        lines.append(
            f"| {tag} | {c.get('ok','-')} | {c.get('errors','-')} | "
            f"{f(c.get('throughput_rps'),'2f')} | {f(r['router_queue_max'],'0.f')} | "
            f"{f(r['router_qwait_p95'],'3f')} | {f(r['router_admit_rps'],'2f')} | "
            f"{f(r['router_ttft_p95'],'3f')} | {f(r['router_e2e_p95'],'2f')} | "
            f"{f(r['router_cpu_cores'],'2f')} | {f(r['router_rss_gb'],'2f')} | "
            f"{f(r['vllm_running'],'0.f')} | {f(r['vllm_kv_pct_max'],'1f')} | "
            f"{f(r['vllm_preempt'],'0.f')} | {f(r['vllm_ttft_p95'],'3f')} | "
            f"{f(r['prefix_cache_hit_pct'],'1f')} | {f(r['affinity_hits_rps'],'2f')} | "
            f"{f(r['npu_aicore_pct_max'],'0.f')} | {f(r['npu_temp_max'],'0.f')} | "
            f"{f(r['gw_req_rps'],'2f')} | {f(r['gw_failed'],'0.f')} |")
    (rdir / "summary.md").write_text("\n".join(lines) + "\n")
    print(f"wrote {rdir/'summary.md'} ({len(out)} phases)")


if __name__ == "__main__":
    main()
