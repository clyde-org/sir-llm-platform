#!/usr/bin/env python3
"""
Build prompt pools for the benchmark from real datasets.

Outputs (JSONL) into --out-dir. Each row:
  {"messages": [{"role","content"}, ...], "tokens": N, "turns": T}
  - lmsys-512.jsonl : LAST USER turn of LMSYS-Chat-1M (single-message chat),
                      400-640 tokens
  - lmsys-2k/8k/16k/32k.jsonl : FULL conversations (agent-style history,
                      like Claude Code / pi send), tiered by total tokens
  - codeflow.jsonl : CodeFlowBench-2505 problem descriptions

Two passes over LMSYS: pass 1 char-length prefilter (fast), pass 2 tokenizes
only candidates (capped) with the Qwen3-14B tokenizer (same family as the
served Qwen3.8-27B; close enough for tiering).

Usage:
  python3 build_pools.py --out-dir /mnt/data/code/sir-llm-platform/bench/data
"""
import argparse
import ast
import json
import os
import random
import time

from datasets import load_from_disk
from transformers import PreTrainedTokenizerFast

LMSYS_PATH = "/mnt/data/datasets/lmsys_chat_1m"
CODEFLOW_PATH = "/mnt/data/datasets/codeflowbench"
TOKENIZER = "/mnt/data/models/Qwen3-14B"

# tier: (char_lo, char_hi, tok_lo, tok_hi, n_sample, source)
# source: "last_user" = last user turn only; "full" = full conversation
TIERS = {
    "512": (1400, 2300, 400, 640, 300, "last_user"),
    "2k": (5000, 9500, 1500, 2600, 300, "full"),
    "8k": (22000, 33000, 6500, 9500, 200, "full"),
    "16k": (45000, 66000, 13000, 19000, 100, "full"),
    "32k": (98000, 180000, 28000, 52000, 50, "full"),
}
CAP_FACTOR = 4


def parse_conv(conv):
    if isinstance(conv, str):
        try:
            conv = ast.literal_eval(conv)
        except Exception:
            return None
    if not isinstance(conv, list):
        return None
    turns = [t for t in conv if isinstance(t, dict) and t.get("content")]
    return turns or None


def last_user_msgs(turns):
    users = [t for t in turns if t.get("role") == "user"]
    if not users:
        return None
    return [{"role": "user", "content": users[-1]["content"]}]


def full_msgs(turns):
    """Conversation as a prompt: drop trailing assistant turn(s) so the
    model is expected to answer the last user turn."""
    msgs = [{"role": t.get("role", "user"), "content": t["content"]}
            for t in turns if t.get("role") in ("user", "assistant")]
    while msgs and msgs[-1]["role"] != "user":
        msgs.pop()
    return msgs or None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out-dir", required=True)
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--skip-codeflow", action="store_true")
    args = ap.parse_args()
    os.makedirs(args.out_dir, exist_ok=True)
    rng = random.Random(args.seed)

    tok = PreTrainedTokenizerFast.from_pretrained(
        TOKENIZER, local_files_only=True, extra_special_tokens={})
    try:
        tok.model_max_length = int(1e9)
    except Exception:
        pass

    print(f"loading LMSYS from {LMSYS_PATH} ...", flush=True)
    ds = load_from_disk(LMSYS_PATH)
    n = len(ds)
    print(f"loaded {n} rows", flush=True)

    # Pass 1: char prefilter -> candidate row ids per tier
    cands = {t: [] for t in TIERS}
    caps = {t: CAP_FACTOR * TIERS[t][4] for t in TIERS}
    t0 = time.time()
    for i in range(n):
        if all(len(cands[t]) >= caps[t] for t in TIERS):
            break
        turns = parse_conv(ds[i]["conversation"])
        if turns is None:
            continue
        for t, (clo, chi, _, _, _, src) in TIERS.items():
            if len(cands[t]) >= caps[t]:
                continue
            if src == "last_user":
                um = last_user_msgs(turns)
                L = len(um[0]["content"]) if um else 0
            else:
                L = sum(len(m["content"]) for m in
                        [{"content": x["content"]} for x in turns])
            if clo <= L < chi:
                cands[t].append(i)
    print(f"pass1 done in {time.time()-t0:.0f}s: "
          + ", ".join(f"{t}={len(v)}" for t, v in cands.items()), flush=True)

    # Pass 2: tokenize candidates, filter, sample
    for t, (clo, chi, tlo, thi, n_sample, src) in TIERS.items():
        out = []
        idxs = cands[t]
        rng.shuffle(idxs)
        for i in idxs:
            if len(out) >= n_sample:
                break
            turns = parse_conv(ds[i]["conversation"])
            msgs = last_user_msgs(turns) if src == "last_user" else full_msgs(turns)
            if not msgs:
                continue
            text = " ".join(m["content"] for m in msgs)
            nt = len(tok.encode(text, add_special_tokens=False))
            if tlo <= nt < thi:
                out.append({"messages": msgs, "tokens": nt,
                            "turns": len(msgs)})
        path = os.path.join(args.out_dir, f"lmsys-{t}.jsonl")
        with open(path, "w") as f:
            for row in out:
                f.write(json.dumps(row) + "\n")
        lens = [r["tokens"] for r in out]
        med = sorted(lens)[len(lens)//2] if lens else 0
        print(f"{path}: {len(out)} prompts (median {med} tok)", flush=True)

    if not args.skip_codeflow:
        print(f"loading CodeFlowBench from {CODEFLOW_PATH} ...", flush=True)
        cds = load_from_disk(CODEFLOW_PATH)
        rows = []
        for ex in cds:
            problem = (ex.get("problem-description") or "").strip()
            sols = ex.get("solutions") or []
            if not problem or not sols or not sols[0].get("content"):
                continue
            nt = len(tok.encode(problem, add_special_tokens=False))
            if nt >= 100:
                rows.append({"messages": [{"role": "user", "content": problem}],
                             "tokens": nt, "turns": 1})
        rng.shuffle(rows)
        rows = rows[:500]
        path = os.path.join(args.out_dir, "codeflow.jsonl")
        with open(path, "w") as f:
            for row in rows:
                f.write(json.dumps(row) + "\n")
        med = sorted(r["tokens"] for r in rows)[len(rows)//2] if rows else 0
        print(f"{path}: {len(rows)} prompts (median {med} tok)", flush=True)


if __name__ == "__main__":
    main()
