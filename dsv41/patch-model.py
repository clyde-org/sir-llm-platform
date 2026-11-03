#!/usr/bin/env python3
"""Runtime patch for vllm_ascend deepseek_v41 model.py.

The 1017 checkpoint (block-FP8, scale_fmt=ue8m0) stores per-tensor scales as
`.scale` keys. The image's name-mapping renames them to `.weight_scale`, but
the fp8_block quantization backend names the linear scale parameter
`weight_scale_inv`. The mapping already special-cases `engram.embed.scale`
but not `engram.wkv.scale` (KeyError at load time).

This patch makes the `.scale` rename resolve against the actual parameter
dict: prefer `.weight_scale`, fall back to `.weight_scale_inv`.

Runs as root before `vllm serve`; edits the image file in place (container
upper layer). Idempotent.
"""
import sys

PATH = "/vllm-workspace/vllm-ascend/vllm_ascend/models/deepseek_v41/model.py"
OLD = (
    '            if name.endswith(".scale"):\n'
    '                name = name.replace(".scale", ".weight_scale")\n'
)
NEW = (
    '            if name.endswith(".scale"):\n'
    '                _ws = name.replace(".scale", ".weight_scale")\n'
    '                name = _ws if _ws in params_dict else name.replace(".scale", ".weight_scale_inv")\n'
)


def main() -> int:
    try:
        s = open(PATH).read()
    except FileNotFoundError:
        print(f"patch: {PATH} not found", file=sys.stderr)
        return 1
    if NEW in s:
        print("patch: already applied")
        return 0
    if OLD not in s:
        print("patch: target block not found (image layout changed?)", file=sys.stderr)
        return 1
    s = s.replace(OLD, NEW, 1)
    open(PATH, "w").write(s)
    print("patch: applied ue8m0 scale-name fallback to", PATH)
    return 0


if __name__ == "__main__":
    sys.exit(main())
