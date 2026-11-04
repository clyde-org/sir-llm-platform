# Auto-loaded by CPython at startup (PYTHONPATH=/dsv41-patch).
#
# Two source patches for the v0.30.0rc2 image (idempotent, no-op when the
# target file is already patched or unrelated to this model):
#
# 1. vllm_ascend/models/deepseek_v41/model.py
#    Normalizes the weight-name mapping to the plain `.scale` ->
#    `.weight_scale` rename (matches the deepseek_v4_fp8 ds_linear scheme:
#    dense/shared-expert linears use param `weight_scale`; routed experts
#    fuse to w13_weight_scale / w2_weight_scale via expert_params_mapping;
#    engram embed.scale has its own earlier special case ->
#    weight_scale_inv). Earlier patch revisions (weight_scale_inv fallback)
#    corrupted the shared-expert fusion; this revision migrates the file
#    back to the plain rename.
#
# 2. vllm_ascend/quantization/methods/w8a8/w8a8_mxfp8.py
#    Fixes an odd-scale-group-count crash in the LINEAR variant of
#    process_weights_after_loading. After repeat_interleave the scale's k
#    dim can be odd (e.g. a layer with k=288 -> 9 groups), and
#    `reshape(n_dim, k_dim // 2, 2)` then loses the last column:
#        RuntimeError: shape '[5120, 4, 2]' is invalid for input of size
#        46080
#    The MOE variant in the same file already handles this by zero-padding
#    the trailing pair (F.pad + reshape(k//2+1)); mirror that logic for the
#    linear path. The zero pair scales 32 nonexistent trailing elements, so
#    it is inert for the NPU kernel (same as the MoE path).
#
# 3. vllm/v1/engine/utils.py  (wait_for_engine_startup)
#    The 2-node DP handshake state machine is not fault tolerant: if a
#    headless worker restarts (or re-hands-shakes) after its first HELLO was
#    already processed, the head raises
#        RuntimeError: Unexpected HELLO message for remote engine 1 in
#        CoreEngineState.CONNECTED state.
#    and tears down the whole deployment. Tolerate a duplicate HELLO from an
#    already-CONNECTED engine: re-send the init message (idempotent
#    re-registration) without re-counting conn/start pending.
#
# 4. vllm/v1/engine/core.py  (startup_handshake)
#    Symmetrically, the worker side used to hard-fail after a single 5-minute
#    window without the init response (exit 0 -> k8s restart -> duplicate
#    HELLO on the head). Re-send HELLO on each timeout up to 1 hour so a
#    transient loss of the init response or a slow head does not crash-loop
#    the worker.
import glob
import os
import sys

# ---------------------------------------------------------------- model.py
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

def _patch_model_py(_path):
    with open(_path) as _f:
        _s = _f.read()
    if _PLAIN in _s and _REV1 not in _s and _REV2 not in _s:
        return  # already plain
    for _rev in (_REV1, _REV2):
        if _rev in _s:
            _s = _s.replace(_rev, _PLAIN, 1)
    _atomic_write(_path, _s)
    print(f"[dsv41-patch] normalized {_path}", file=sys.stderr)

# ----------------------------------------------------------- w8a8_mxfp8.py
_MXFP8_OLD = (
    "        n_dim, k_dim = layer.weight_scale.data.shape\n"
    "        layer.weight_scale.data = layer.weight_scale.data.reshape(n_dim, k_dim // 2, 2)\n"
)
_MXFP8_NEW = (
    "        n_dim, k_dim = layer.weight_scale.data.shape\n"
    "        if k_dim % 2 != 0:\n"
    "            # Odd scale-group count (k not a multiple of 64): mirror\n"
    "            # the MoE variant's zero-padded pair packing (dsv41-patch).\n"
    "            layer.weight_scale.data = F.pad(\n"
    "                layer.weight_scale.data, (0, 1), mode=\"constant\", value=0\n"
    "            ).reshape(n_dim, k_dim // 2 + 1, 2)\n"
    "        else:\n"
    "            layer.weight_scale.data = layer.weight_scale.data.reshape(n_dim, k_dim // 2, 2)\n"
)

def _patch_mxfp8(_path):
    with open(_path) as _f:
        _s = _f.read()
    if _MXFP8_NEW in _s:
        return  # already patched
    if _MXFP8_OLD in _s:
        _s = _s.replace(_MXFP8_OLD, _MXFP8_NEW, 1)
        _atomic_write(_path, _s)
        print(f"[dsv41-patch] patched odd-k scale packing in {_path}", file=sys.stderr)

# ------------------------------------------------------------ engine/utils.py
_HELLO_OLD = (
    "        if status == \"HELLO\" and engine.state == CoreEngineState.NEW:\n"
    "            # Send init message with DP config info.\n"
)
_HELLO_NEW = (
    "        if status == \"HELLO\" and engine.state in (\n"
    "            CoreEngineState.NEW, CoreEngineState.CONNECTED,\n"
    "        ):  # dsv41-patch: tolerate re-HELLO after worker restart\n"
    "            _dsv41_rehello = engine.state == CoreEngineState.CONNECTED\n"
    "            if _dsv41_rehello:\n"
    "                logger.warning(\n"
    "                    \"dsv41-patch: duplicate HELLO from %s engine %s; \"\n"
    "                    \"re-sending init message\",\n"
    "                    \"local\" if local else \"remote\", eng_index,\n"
    "                )\n"
    "            # Send init message with DP config info.\n"
)
_HELLO_COUNT_OLD = (
    "            handshake_socket.send_multipart((eng_identity, init_message), copy=False)\n"
    "            conn_pending[0 if local else 1] -= 1\n"
    "            start_pending[0 if local else 1] += 1\n"
    "            engine.state = CoreEngineState.CONNECTED\n"
)
_HELLO_COUNT_NEW = (
    "            handshake_socket.send_multipart((eng_identity, init_message), copy=False)\n"
    "            if not _dsv41_rehello:  # dsv41-patch: already counted\n"
    "                conn_pending[0 if local else 1] -= 1\n"
    "                start_pending[0 if local else 1] += 1\n"
    "            engine.state = CoreEngineState.CONNECTED\n"
)

def _patch_engine_utils(_path):
    with open(_path) as _f:
        _s = _f.read()
    if _HELLO_NEW in _s:
        return  # already patched
    changed = False
    if _HELLO_OLD in _s:
        _s = _s.replace(_HELLO_OLD, _HELLO_NEW, 1)
        changed = True
    if _HELLO_COUNT_OLD in _s:
        _s = _s.replace(_HELLO_COUNT_OLD, _HELLO_COUNT_NEW, 1)
        changed = True
    if changed:
        _atomic_write(_path, _s)
        print(f"[dsv41-patch] patched duplicate-HELLO tolerance in {_path}", file=sys.stderr)

# --------------------------------------------------------------- engine/core.py
_HS_OLD = (
    "        # Receive initialization message.\n"
    "        logger.debug(\"Waiting for init message from front-end.\")\n"
    "        if not handshake_socket.poll(timeout=HANDSHAKE_TIMEOUT_MINS * 60_000):\n"
    "            raise RuntimeError(\n"
    "                \"Did not receive response from front-end \"\n"
    "                f\"process within {HANDSHAKE_TIMEOUT_MINS} \"\n"
    "                f\"minutes\"\n"
    "            )\n"
    "        init_bytes = handshake_socket.recv()\n"
)
_HS_NEW = (
    "        # Receive initialization message.\n"
    "        logger.debug(\"Waiting for init message from front-end.\")\n"
    "        # dsv41-patch: re-send HELLO on each timeout instead of dying,\n"
    "        # so a lost init response or a slow head does not crash-loop a\n"
    "        # headless worker (head tolerates the duplicate HELLO).\n"
    "        _dsv41_deadline = time.monotonic() + 3600\n"
    "        while True:\n"
    "            if handshake_socket.poll(timeout=HANDSHAKE_TIMEOUT_MINS * 60_000):\n"
    "                init_bytes = handshake_socket.recv()\n"
    "                break\n"
    "            if time.monotonic() > _dsv41_deadline:\n"
    "                raise RuntimeError(\n"
    "                    \"Did not receive response from front-end \"\n"
    "                    f\"process within {HANDSHAKE_TIMEOUT_MINS} \"\n"
    "                    f\"minutes\"\n"
    "                )\n"
    "            logger.warning(\"dsv41-patch: no init response yet; re-sending HELLO\")\n"
    "            handshake_socket.send(\n"
    "                msgspec.msgpack.encode(\n"
    "                    {\n"
    "                        \"status\": \"HELLO\",\n"
    "                        \"local\": local_client,\n"
    "                        \"headless\": headless,\n"
    "                    }\n"
    "                )\n"
    "            )\n"
)

def _patch_engine_core(_path):
    with open(_path) as _f:
        _s = _f.read()
    if _HS_NEW in _s:
        return  # already patched
    if _HS_OLD in _s:
        _s = _s.replace(_HS_OLD, _HS_NEW, 1)
        _atomic_write(_path, _s)
        print(f"[dsv41-patch] patched HELLO retry in {_path}", file=sys.stderr)

def _atomic_write(_path, _s):
    _tmp = _path + ".dsv41patch"
    with open(_tmp, "w") as _f:
        _f.write(_s)
        _f.flush()
        os.fsync(_f.fileno())
    os.replace(_tmp, _path)

for _pat in (
    "/vllm-workspace/vllm-ascend/vllm_ascend/models/deepseek_v41/model.py",
    "/usr/local/lib/python*/site-packages/vllm_ascend/models/deepseek_v41/model.py",
    "/opt/conda/lib/python*/site-packages/vllm_ascend/models/deepseek_v41/model.py",
):
    for _path in glob.glob(_pat):
        try:
            _patch_model_py(_path)
        except OSError:
            pass

for _pat in (
    "/vllm-workspace/vllm-ascend/vllm_ascend/quantization/methods/w8a8/w8a8_mxfp8.py",
    "/usr/local/lib/python*/site-packages/vllm_ascend/quantization/methods/w8a8/w8a8_mxfp8.py",
    "/opt/conda/lib/python*/site-packages/vllm_ascend/quantization/methods/w8a8/w8a8_mxfp8.py",
):
    for _path in glob.glob(_pat):
        try:
            _patch_mxfp8(_path)
        except OSError:
            pass

for _pat in (
    "/vllm-workspace/vllm/vllm/v1/engine/utils.py",
    "/usr/local/lib/python*/site-packages/vllm/v1/engine/utils.py",
    "/opt/conda/lib/python*/site-packages/vllm/v1/engine/utils.py",
):
    for _path in glob.glob(_pat):
        try:
            _patch_engine_utils(_path)
        except OSError:
            pass

for _pat in (
    "/vllm-workspace/vllm/vllm/v1/engine/core.py",
    "/usr/local/lib/python*/site-packages/vllm/v1/engine/core.py",
    "/opt/conda/lib/python*/site-packages/vllm/v1/engine/core.py",
):
    for _path in glob.glob(_pat):
        try:
            _patch_engine_core(_path)
        except OSError:
            pass
