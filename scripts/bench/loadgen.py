#!/usr/bin/env python3
"""
Bench load generator for the sir-llm-platform gateway.

Stdlib only. Designed to run inside a throwaway in-cluster pod (or anywhere
that can reach the LiteLLM gateway).

Subcommands:
  open    open-loop: Poisson arrivals at a fixed rate (finds saturation)
  closed  closed-loop: fixed concurrency (finds batching sweet spot)
  grid    Phase-1 latency matrix (sequential, N=1, both pools)
  warmup  quick smoke / warm-up burst

Outputs (all under --out-dir):
  <tag>.csv        per-request rows (ttft/tpot/e2e/tokens/status/err)
  <tag>.summary.json
  manifest.jsonl   one line per run (phase, config, window, files)

Examples:
  loadgen.py open   --tag p2-r16  --rate 16 --duration 180 \
      --model qwen3.8-27b --input-tokens 1024 --output-tokens 1024
  loadgen.py closed --tag p3-c64  --conc 64 --count 192 \
      --model qwen3.8-27b --input-tokens 1024 --output-tokens 1024
  loadgen.py grid   --tag p1      --models qwen3.8-27b qwen3.8-27b-262k
"""

import argparse
import csv
import http.client
import json
import os
import random
import socket
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

# ~0.178 tokens/char measured for the filler in scripts/test-ctx-guard.py
CHARS_PER_TOKEN = 5.5
WORDS = ["alpha", "beta", "gamma", "delta", "epsilon", "zeta", "eta", "theta",
         "iota", "kappa", "lambda", "mu", "nu", "xi", "omicron", "upsilon"]

CSV_FIELDS = [
    "ts", "tag", "model", "path", "seq", "status", "ok", "err",
    "ttft_s", "tpot_s", "e2e_s", "prompt_tokens", "completion_tokens",
    "stream", "est",
]


def now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def pct(sorted_vals, q):
    if not sorted_vals:
        return None
    k = (len(sorted_vals) - 1) * q
    f, c = int(k), min(int(k) + 1, len(sorted_vals) - 1)
    return sorted_vals[f] + (sorted_vals[c] - sorted_vals[f]) * (k - f)


class PromptPool:
    """Real prompts from a dataset JSONL: one JSON object per line with a
    'messages' field (list of {role, content}) or 'text' (single user turn).
    Cycled per sequence number."""
    def __init__(self, path):
        self.items = []
        with open(path) as f:
            for line in f:
                line = line.strip()
                if line:
                    self.items.append(json.loads(line))
        if not self.items:
            raise SystemExit(f"empty prompt pool {path}")

    def __len__(self):
        return len(self.items)

    def get(self, seq):
        it = self.items[seq % len(self.items)]
        if "messages" in it:
            return it["messages"]
        return it["text"]


def make_prompt(target_tokens, seed, shared_prefix_tokens=0):
    """Prompt of ~target_tokens tokens. Filler text varies per seed so that
    KV-block hashes differ across requests (no accidental cache hits).
    shared_prefix_tokens: leading static text shared across all requests."""
    rng = random.Random(seed)
    parts = []
    if shared_prefix_tokens:
        parts.append("SYSTEM: You are a helpful assistant. " +
                     "alpha beta gamma delta " * shared_prefix_tokens)
    n = max(0, target_tokens - shared_prefix_tokens)
    words = [rng.choice(WORDS) for _ in range(n)]
    parts.append("USER: " + " ".join(words) + " SUMMARIZE.")
    return "".join(parts)


def do_request(args, prompt, model, max_tokens, seq):
    """One chat-completion (or Anthropic messages) request. `prompt` is a
    string (single user turn) or a list of {role, content} message dicts.
    Returns dict of measured fields."""
    is_anthropic = args.anthropic
    stream = args.stream
    messages = prompt if isinstance(prompt, list) else [
        {"role": "user", "content": prompt}]
    total_chars = sum(len(m.get("content") or "") for m in messages)
    t0 = time.time()
    ttft = None
    status = 0
    err = ""
    prompt_tokens = None
    completion_tokens = None
    n_deltas = 0
    host = args.host
    port = args.port
    try:
        conn = http.client.HTTPConnection(host, port, timeout=120)
        if is_anthropic:
            body = {
                "model": model,
                "max_tokens": max_tokens,
                "messages": messages,
            }
            if stream:
                body["stream"] = True
            headers = {
                "authorization": f"Bearer {args.api_key}",
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            }
        else:
            body = {
                "model": model,
                "max_tokens": max_tokens,
                "stream": stream,
                "messages": messages,
            }
            if args.ignore_eos:
                body["ignore_eos"] = True
            if stream:
                body["stream_options"] = {"include_usage": True}
            headers = {
                "authorization": f"Bearer {args.api_key}",
                "content-type": "application/json",
            }
        headers["user-agent"] = f"bench/{args.tag}"
        conn.request("POST", args.path, body=json.dumps(body).encode(),
                     headers=headers)
        resp = conn.getresponse()
        status = resp.status
        if status == 200 and stream:
            while True:
                line = resp.readline()
                if not line:
                    break
                line = line.decode("utf-8", "replace").strip()
                if not line.startswith("data:"):
                    continue
                payload = line[5:].strip()
                if payload == "[DONE]":
                    break
                try:
                    ev = json.loads(payload)
                except Exception:
                    continue
                if is_anthropic:
                    et = ev.get("type")
                    if et == "message_start":
                        prompt_tokens = (ev.get("message", {})
                                         .get("usage", {})
                                         .get("input_tokens"))
                    elif et == "content_block_delta":
                        dt = ev.get("delta", {}).get("type")
                        if dt in ("text_delta", "thinking_delta"):
                            if ttft is None:
                                ttft = time.time() - t0
                            n_deltas += 1
                    elif et == "message_delta":
                        completion_tokens = ev.get("usage", {}).get("output_tokens")
                    elif et == "error":
                        err = json.dumps(ev.get("error"))[:200]
                        break
                else:
                    ch = ev.get("choices") or []
                    if ch:
                        d = ch[0].get("delta", {})
                        # Qwen3 thinking models stream reasoning_content;
                        # count it for TTFT/token count like content.
                        if d.get("content") or d.get("reasoning_content"):
                            if ttft is None:
                                ttft = time.time() - t0
                            n_deltas += 1
                    if ev.get("usage"):
                        u = ev["usage"]
                        prompt_tokens = u.get("prompt_tokens")
                        ct = u.get("completion_tokens") or 0
                        rt = (u.get("completion_tokens_details") or {}).get(
                            "reasoning_tokens") or 0
                        completion_tokens = ct + rt
        elif status == 200:
            raw = resp.read()
            try:
                j = json.loads(raw)
                if is_anthropic:
                    prompt_tokens = j.get("usage", {}).get("input_tokens")
                    completion_tokens = j.get("usage", {}).get("output_tokens")
                else:
                    u = j.get("usage") or {}
                    prompt_tokens = u.get("prompt_tokens")
                    completion_tokens = u.get("completion_tokens")
            except Exception as e:
                err = f"badjson:{e}"
        else:
            err = resp.read()[:200].decode("utf-8", "replace")
        conn.close()
    except (socket.timeout, TimeoutError) as e:
        err = f"timeout:{type(e).__name__}"
    except (http.client.HTTPException, ConnectionError, OSError) as e:
        err = f"{type(e).__name__}:{str(e)[:120]}"
    t1 = time.time()
    e2e = t1 - t0
    ok = status == 200 and not err
    completion = completion_tokens if completion_tokens is not None else (
        n_deltas if stream else None)
    if ttft is not None and completion:
        tpot = max(0.0, (e2e - ttft) / max(1, completion - 1))
    else:
        tpot = None
    if prompt_tokens is None:
        prompt_tokens = int(total_chars / CHARS_PER_TOKEN)
    return {
        "ts": now_iso(),
        "tag": args.tag,
        "model": model,
        "path": args.path,
        "seq": seq,
        "status": status,
        "ok": ok,
        "err": err,
        "ttft_s": round(ttft, 4) if ttft is not None else "",
        "tpot_s": round(tpot, 5) if tpot is not None else "",
        "e2e_s": round(e2e, 3),
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion if completion else "",
        "stream": stream,
        "est": prompt_tokens is not None and total_chars / CHARS_PER_TOKEN
               == prompt_tokens,
    }


class Recorder:
    def __init__(self, args):
        self.args = args
        self.pool = None
        if getattr(args, "prompts_jsonl", None):
            self.pool = PromptPool(args.prompts_jsonl)
            print(f"[pool] {len(self.pool)} prompts from {args.prompts_jsonl}",
                  flush=True)
        os.makedirs(args.out_dir, exist_ok=True)
        self.csv_path = os.path.join(args.out_dir, f"{args.tag}.csv")
        self.rows = []
        self.lock = threading.Lock()
        self.csvf = open(self.csv_path, "w", newline="")
        self.csvw = csv.DictWriter(self.csvf, fieldnames=CSV_FIELDS)
        self.csvw.writeheader()
        self.csvf.flush()
        self.stop = threading.Event()
        self.aborted = ""

    def add(self, row):
        with self.lock:
            self.rows.append(row)
            self.csvw.writerow(row)
            self.csvf.flush()
            if row["ok"]:
                return
            recent = self.rows[-50:]
            bad = sum(1 for r in recent if not r["ok"])
            if len(recent) >= 20 and bad / len(recent) > self.args.max_err_rate:
                self.aborted = (f"error-rate {bad}/{len(recent)} "
                                f"over last {len(recent)} > {self.args.max_err_rate}")
                self.stop.set()

    def close(self):
        self.csvf.close()


def summarize(rows, duration):
    ok_rows = [r for r in rows if r["ok"]]
    def vals(key):
        return sorted(float(r[key]) for r in ok_rows if r[key] != "")
    ttft, e2e, tpot = vals("ttft_s"), vals("e2e_s"), vals("tpot_s")
    out_tokens = sum(int(r["completion_tokens"]) for r in ok_rows
                     if r["completion_tokens"] != "")
    return {
        "requests": len(rows),
        "ok": len(ok_rows),
        "errors": len(rows) - len(ok_rows),
        "error_rate": round((len(rows) - len(ok_rows)) / max(1, len(rows)), 4),
        "throughput_rps": round(len(ok_rows) / duration, 4) if duration else None,
        "out_tokens_per_s": round(out_tokens / duration, 1) if duration else None,
        "ttft_s": {
            "p50": round(pct(ttft, .5), 4) if ttft else None,
            "p95": round(pct(ttft, .95), 4) if ttft else None,
            "p99": round(pct(ttft, .99), 4) if ttft else None,
        },
        "tpot_s": {
            "p50": round(pct(tpot, .5), 5) if tpot else None,
            "p95": round(pct(tpot, .95), 5) if tpot else None,
            "p99": round(pct(tpot, .99), 5) if tpot else None,
        },
        "e2e_s": {
            "p50": round(pct(e2e, .5), 3) if e2e else None,
            "p95": round(pct(e2e, .95), 3) if e2e else None,
            "p99": round(pct(e2e, .99), 3) if e2e else None,
        },
        "top_errors": _top_errors(rows),
    }


def _top_errors(rows):
    errs = {}
    for r in rows:
        if not r["ok"]:
            key = f"{r['status']}:{r['err'][:60]}"
            errs[key] = errs.get(key, 0) + 1
    return sorted(errs.items(), key=lambda kv: -kv[1])[:5]


def emit(args, rec, duration, extra=None):
    s = summarize(rec.rows, duration)
    s.update({
        "tag": args.tag,
        "start": rec.start_iso,
        "end": now_iso(),
        "duration_s": round(duration, 1),
        "config": {
            "mode": args.mode, "model": args.model, "path": args.path,
            "input_tokens": args.input_tokens, "output_tokens": args.output_tokens,
            "shared_prefix_tokens": args.shared_prefix_tokens,
            "rate": getattr(args, "rate", None),
            "conc": getattr(args, "conc", None),
            "stream": args.stream, "anthropic": args.anthropic,
        },
        "aborted": rec.aborted,
        "csv": rec.csv_path,
    })
    if extra:
        s.update(extra)
    sp = os.path.join(args.out_dir, f"{args.tag}.summary.json")
    with open(sp, "w") as f:
        json.dump(s, f, indent=2)
    with open(os.path.join(args.out_dir, "manifest.jsonl"), "a") as f:
        f.write(json.dumps({k: s[k] for k in
                            ("tag", "start", "end", "duration_s", "config",
                             "ok", "errors", "throughput_rps", "aborted", "csv")})
                + "\n")
    print(json.dumps(s, indent=2), flush=True)
    return s


def run_open(args, rec):
    rec.start_iso = now_iso()
    t_end = time.time() + args.duration
    seq = 0
    with ThreadPoolExecutor(max_workers=args.max_inflight) as ex:
        futures = set()
        next_t = time.time()
        while not rec.stop.is_set() and time.time() < t_end and seq < args.count:
            gap = random.expovariate(args.rate)
            next_t += gap
            sleep = next_t - time.time()
            if sleep > 0:
                if rec.stop.wait(sleep):
                    break
            for f in list(futures):
                if f.done():
                    futures.discard(f)
            if len(futures) >= args.max_inflight:
                # generator backpressure: wait for one to finish (should be rare)
                rec.stop.wait(0.05)
                continue
            seq += 1
            model = args.models[seq % len(args.models)]
            prompt = _prompt_for(args, rec.pool, seq, args.shared_prefix_tokens)
            futures.add(ex.submit(_work, args, rec, seq, model, prompt))
        for f in futures:
            f.result(timeout=args.timeout)
    emit(args, rec, time.time() + (t_end - t_end) or _dur(rec))


def _dur(rec):
    return getattr(rec, "wall", 0.0)


def run_closed(args, rec):
    rec.start_iso = now_iso()
    counter = {"n": 0}
    clk = threading.Lock()
    def worker():
        while not rec.stop.is_set():
            with clk:
                if counter["n"] >= args.count:
                    return
                counter["n"] += 1
                seq = counter["n"]
            model = args.models[(seq - 1) % len(args.models)]
            prompt = _prompt_for(args, rec.pool, seq, args.shared_prefix_tokens)
            _work(args, rec, seq, model, prompt)
    t0 = time.time()
    threads = [threading.Thread(target=worker, daemon=True)
               for _ in range(args.conc)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=args.timeout)
    emit(args, rec, time.time() - t0)


def _work(args, rec, seq, model, prompt):
    row = do_request(args, prompt, model, args.output_tokens, seq)
    rec.add(row)
    if seq % 10 == 0:
        print(f"[{rec.tag}] seq={seq} ok={row['ok']} "
              f"e2e={row['e2e_s']}s err={row['err'][:60]}", flush=True)


def _prompt_for(args, pool, seq, shared_tokens):
    if pool is not None:
        return pool.get(seq)
    return make_prompt(args.input_tokens, seed=10_000 + seq,
                       shared_prefix_tokens=shared_tokens)


def run_grid(args, rec):
    """Phase-1 latency matrix, sequential N=1."""
    rec.start_iso = now_iso()
    t0 = time.time()
    seq = 0
    for model in args.models:
        for itok in args.inputs:
            for otok in args.outputs:
                reps = args.reps_small if itok <= 8192 else args.reps_large
                for rep in range(1, reps + 1):
                    if rec.stop.is_set():
                        break
                    seq += 1
                    tag = f"{args.tag}-{model.split('.')[-1]}-in{itok}-out{otok}"
                    prompt = make_prompt(itok, seed=30_000 + seq)
                    row = do_request_with(args, rec, tag, seq, model, prompt,
                                          itok, otok)
                    print(f"[{tag}] r{rep} ok={row['ok']} "
                          f"ttft={row['ttft_s']} e2e={row['e2e_s']}s "
                          f"ptok={row['prompt_tokens']}", flush=True)
    emit(args, rec, time.time() - t0, extra={"mode": "grid"})


def do_request_with(args, rec, tag, seq, model, prompt, itok, otok):
    """do_request with a per-cell tag override."""
    args.tag = tag
    row = do_request(args, prompt, model, otok, seq)
    row["input_target"] = itok
    rec.add(row)
    return row


def run_warmup(args, rec):
    rec.start_iso = now_iso()
    t0 = time.time()
    for i, model in enumerate(args.models):
        prompt = make_prompt(512, seed=40_000 + i)
        row = do_request(args, prompt, model, 512, i + 1)
        rec.add(row)
        print(f"[warmup {model}] ok={row['ok']} e2e={row['e2e_s']}s "
              f"err={row['err'][:80]}", flush=True)
    emit(args, rec, time.time() - t0)


def parse_args():
    p = argparse.ArgumentParser(description=__doc__)
    sub = p.add_subparsers(dest="mode", required=True)
    for name in ("open", "closed", "grid", "warmup"):
        sp = sub.add_parser(name)
        _common(sp)
    sub.choices["open"].add_argument("--rate", type=float, required=True)
    sub.choices["open"].add_argument("--duration", type=float, default=180)
    sub.choices["open"].add_argument("--count", type=int, default=0,
                                     help="max requests (0 = unlimited)")
    sub.choices["closed"].add_argument("--conc", type=int, required=True)
    sub.choices["closed"].add_argument("--count", type=int, required=True)
    sub.choices["grid"].add_argument("--inputs", type=int, nargs="+",
                                     default=[512, 2048, 8192, 32768, 65536, 100000])
    sub.choices["grid"].add_argument("--outputs", type=int, nargs="+",
                                     default=[128, 512, 1024])
    sub.choices["grid"].add_argument("--reps-small", type=int, default=3)
    sub.choices["grid"].add_argument("--reps-large", type=int, default=1)
    a = p.parse_args()
    a.mode = a.mode
    if a.base_url.startswith("http://"):
        rest = a.base_url[7:]
        a.host, _, a.port = rest.partition(":")
        a.port = int(a.port or 80)
    else:
        a.host, a.port = a.base_url, 4000
    if not getattr(a, "count", 0):
        a.count = int(getattr(a, "rate", 0) * getattr(a, "duration", 180) * 2) + 10 \
            if a.mode == "open" else 0
    if a.anthropic and a.path == "/v1/chat/completions":
        a.path = "/v1/messages"
    return a


def _common(sp):
    sp.add_argument("--base-url", default="http://litellm-proxy.sir-llm-platform:4000")
    sp.add_argument("--api-key", default="YOUR-API-KEY")
    sp.add_argument("--tag", required=True)
    sp.add_argument("--model", default="qwen3.8-27b")
    sp.add_argument("--models", nargs="+", default=None,
                    help="model rotation (grid uses these)")
    sp.add_argument("--path", default="/v1/chat/completions")
    sp.add_argument("--anthropic", action="store_true",
                    help="Anthropic /v1/messages API")
    sp.add_argument("--input-tokens", type=int, default=1024)
    sp.add_argument("--output-tokens", type=int, default=1024)
    sp.add_argument("--shared-prefix-tokens", type=int, default=0)
    sp.add_argument("--prompts-jsonl", default=None,
                    help="JSONL file of real prompts ({text, tokens} per line); "
                         "overrides synthetic filler generation")
    sp.add_argument("--no-stream", action="store_true")
    sp.add_argument("--no-ignore-eos", action="store_true")
    sp.add_argument("--timeout", type=int, default=1200,
                    help="max wait per request at join time (s)")
    sp.add_argument("--max-inflight", type=int, default=4096)
    sp.add_argument("--max-err-rate", type=float, default=0.05,
                    help="abort when last-50 error fraction exceeds this")
    sp.add_argument("--out-dir", default="/work/out")


def main():
    args = parse_args()
    if args.models is None:
        args.models = [args.model]
    args.stream = not args.no_stream
    args.ignore_eos = not args.no_ignore_eos
    rec = Recorder(args)
    rec.start_iso = now_iso()
    rec.wall = 0.0
    try:
        if args.mode == "open":
            run_open(args, rec)
        elif args.mode == "closed":
            run_closed(args, rec)
        elif args.mode == "grid":
            run_grid(args, rec)
        elif args.mode == "warmup":
            run_warmup(args, rec)
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)
    finally:
        rec.close()
        if rec.aborted:
            print(f"ABORTED: {rec.aborted}", file=sys.stderr)
            sys.exit(2)


if __name__ == "__main__":
    main()
