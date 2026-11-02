"""
Startup fix for nightly-releases-v0.30.0-openeuler (2026-10-26 build) vLLM-core
circular-import regression:

    File ".../vllm/utils/platform_utils.py", line 45, in is_pin_memory_available
        from vllm.platforms import current_platform
    ImportError: cannot import name 'current_platform' from 'vllm.platforms'

vllm/platforms/__init__.py in this build exposes current_platform lazily via a
module-level PEP 562 __getattr__. During the vllm package bootstrap chain
(vllm/__init__ -> env_override -> utils.torch_utils:75 PIN_MEMORY=... ->
utils.platform_utils:45) the vllm.platforms module can be only partially
initialized, so the from-import fails hard.

Patch: make is_pin_memory_available() exception-safe. On NPU pinned host
memory is a CUDA concept; returning False is semantically correct and only
affects the CUDA fast path. Idempotent.
"""
import sys

P = "/vllm-workspace/vllm/vllm/utils/platform_utils.py"

OLD = '''@cache
def is_pin_memory_available() -> bool:
    from vllm.platforms import current_platform

    return current_platform.is_pin_memory_available()'''

NEW = '''@cache
def is_pin_memory_available() -> bool:
    try:
        import vllm.platforms as _p
        return _p.current_platform.is_pin_memory_available()
    except Exception:
        return False'''

try:
    s = open(P).read()
    if "dsv4f-import-fix" in s:
        print("dsv4f-import-fix: already patched")
        sys.exit(0)
    if OLD not in s:
        print("dsv4f-import-fix: pattern not found; checking current shape...")
        import re
        m = re.search(r"@cache\ndef is_pin_memory_available\(\).*?return current_platform\.is_pin_memory_available\(\)", s, re.S)
        if not m:
            print("dsv4f-import-fix: ABORT, cannot locate function (leaving file untouched)")
            sys.exit(0)
        old = m.group(0)
    else:
        old = OLD
    # marker comment for idempotency
    new = NEW.replace("def is_pin_memory_available() -> bool:",
                      "def is_pin_memory_available() -> bool:  # dsv4f-import-fix", 1)
    s = s.replace(old, new, 1)
    open(P, "w").write(s)
    print("dsv4f-import-fix: patched is_pin_memory_available()")
except Exception as e:
    # never block boot on the fix itself
    print(f"dsv4f-import-fix: ERROR {e!r} (continuing unpatched)")
    sys.exit(0)
