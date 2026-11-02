"""Diagnostic v5: exact target shapes + storage descriptor details + clone retry."""
import traceback

_fired = {"n": 0}
# w13 [E, 2I, K/2] = (32, 4096, 2048); w2 [E, K, I/2] = (32, 4096, 1024)
# plus transposed/other orderings just in case
TARGETS = {(32, 4096, 2048), (32, 4096, 1024), (32, 2048, 4096), (32, 1024, 4096)}


def _dump(tag, t):
    import torch_npu
    print(f"[diag5] {tag} shape={tuple(t.shape)} dtype={t.dtype}", flush=True)
    try:
        print(f"[diag5] {tag} numel={t.numel()} storage_bytes={t.untyped_storage().nbytes()}", flush=True)
    except Exception as e:  # noqa: BLE001
        print(f"[diag5] {tag} storage err {e}", flush=True)
    try:
        print(f"[diag5] {tag} acl_format={int(torch_npu.get_npu_format(t))}", flush=True)
    except Exception as e:  # noqa: BLE001
        print(f"[diag5] {tag} acl err {e}", flush=True)
    tb = traceback.format_stack()
    for line in tb[-7:-1]:
        print("[diag5] caller: " + line.strip()[:170], flush=True)


def _install():
    import torch_npu

    orig = torch_npu.npu_format_cast

    def wrapped(*args, **kwargs):
        t = args[0] if args else kwargs.get("tensor")
        try:
            shape = tuple(t.shape)
            if shape in TARGETS and _fired["n"] < 4:
                _fired["n"] += 1
                _dump("INPUT", t)
                # try a clone first to see if clean storage changes the outcome
                try:
                    tc = t.clone()
                    _dump("CLONE", tc)
                except Exception as e:  # noqa: BLE001
                    print(f"[diag5] clone err {e}", flush=True)
        except Exception:  # noqa: BLE001
            traceback.print_exc()
        return orig(*args, **kwargs)

    torch_npu.npu_format_cast = wrapped
    print("[diag5] wrapped", flush=True)


try:
    _install()
except Exception:  # noqa: BLE001
    traceback.print_exc()
    print("[diag5] install FAILED", flush=True)
