# Auto-loaded by CPython at startup (PYTHONPATH=/dsv41-patch).
#
# Normalizes the weight-name mapping in vllm_ascend deepseek_v41 model.py to
# the plain `.scale` -> `.weight_scale` rename.
#
# Rationale (with --quantization deepseek_v4_fp8):
#   - dense/shared-expert linears use the ds_linear scheme (param
#     `weight_scale`); shared experts w1/w3 fuse to gate_up_proj.weight_scale
#     AFTER this block, so the name must stay plain here;
#   - routed experts fuse to w13_weight_scale / w2_weight_scale via
#     expert_params_mapping (ds_w4a8_moe MXFP4 scheme);
#   - engram embed.scale is handled by an earlier special case
#     (weight_scale_inv); engram wkv is a LinearBase -> ds_linear ->
#     weight_scale.
# Earlier patch revisions (weight_scale_inv fallback) corrupted the
# shared-expert fusion; this revision migrates the file back to the plain
# rename. Idempotent; no-op on unrelated python processes.
import glob

_PLAIN = (
    '            if name.endswith(".scale"):\n'
    '                name = name.replace(".scale", ".weight_scale")\n'
)
_REV1 = (
    '            if name.endswith(".scale"):\n'
    '                _ws = name.replace(".scale", ".weight_scale")\n'
    '                name = _ws if _ws in params_dict else name.replace(".scale", ".weight_scale_inv")\n'
)
_REV2 = (
    '            if name.endswith(".scale"):\n'
    '                if ".mlp.experts." in name:\n'
    '                    name = name.replace(".scale", ".weight_scale")\n'
    '                else:\n'
    '                    _ws = name.replace(".scale", ".weight_scale")\n'
    '                    name = _ws if _ws in params_dict else name.replace(".scale", ".weight_scale_inv")\n'
)

for _pat in (
    "/vllm-workspace/vllm-ascend/vllm_ascend/models/deepseek_v41/model.py",
    "/usr/local/lib/python*/site-packages/vllm_ascend/models/deepseek_v41/model.py",
    "/opt/conda/lib/python*/site-packages/vllm_ascend/models/deepseek_v41/model.py",
):
    for _path in glob.glob(_pat):
        try:
            with open(_path) as _f:
                _s = _f.read()
            if _PLAIN in _s and _REV1 not in _s and _REV2 not in _s:
                continue  # already plain
            _changed = False
            for _rev in (_REV1, _REV2):
                if _rev in _s:
                    _s = _s.replace(_rev, _PLAIN, 1)
                    _changed = True
            if _changed:
                with open(_path, "w") as _f:
                    _f.write(_s)
                print(f"[dsv41-patch] normalized {_path}", file=__import__("sys").stderr)
        except OSError:
            pass
