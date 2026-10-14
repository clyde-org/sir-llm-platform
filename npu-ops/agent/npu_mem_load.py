#!/usr/bin/env python3
"""Load NPU HBM memory to a target percentage (default 90%) and hold it.

This is the *managed* copy used by npu-ops (npu-node-agent installs it to
/root/npu_mem_load.py when missing). It is a host-level process: invisible to
the K8s device plugin, allocates HBM via libascendcl and holds it until
SIGTERM (which frees everything).

Usage:
  python3 /root/npu_mem_load.py                     # load all NPUs to 90%, hold forever
  python3 /root/npu_mem_load.py --pct 90            # custom target percentage
  python3 /root/npu_mem_load.py --devices 0,3       # only these devices
  python3 /root/npu_mem_load.py --status            # only show current HBM usage
  kill $(cat /root/npu_mem_load.pid)                # release all memory (SIGTERM)
  kill -9 $(cat /root/npu_mem_load.pid)             # force release (driver reclaims)
"""
import argparse
import ctypes
import os
import signal
import sys
import time

HBM = 0  # ACL_MEM_HBM_DEVICE
CHUNK = 1 << 30  # allocate in 1 GiB blocks


def _probe_runtime(lib64):
    """True if libascendcl.so in this dir is a working *runtime* (not a stub).

    Runs in a SUBPROCESS on purpose: dlopen only honours LD_LIBRARY_PATH as
    seen at process startup, so the candidate dir must be set in the child's
    env. CDLL alone is also not enough: CANN ships stub libraries in 'devlib'
    that load fine but fail at execution (aclrtGetDeviceCount -> 100039)."""
    import subprocess
    probe = ("import ctypes, sys\n"
             "lib = ctypes.CDLL('libascendcl.so')\n"
             "lib.aclrtGetDeviceCount.argtypes = [ctypes.POINTER(ctypes.c_uint32)]\n"
             "lib.aclrtGetDeviceCount.restype = ctypes.c_int\n"
             "n = ctypes.c_uint32(0)\n"
             "rc = lib.aclrtGetDeviceCount(ctypes.byref(n))\n"
             "sys.exit(0 if rc == 0 and n.value > 0 else 1)\n")
    so = os.path.join(lib64, "libascendcl.so")
    if not os.path.exists(so):
        return False, "no libascendcl.so"
    env = dict(os.environ)
    env["LD_LIBRARY_PATH"] = lib64 + os.pathsep + "/usr/local/Ascend/driver/lib64"
    try:
        r = subprocess.run([sys.executable, "-c", probe],
                           capture_output=True, timeout=90, env=env)
        if r.returncode == 0:
            return True, "ok"
        out = ((r.stdout or b"") + (r.stderr or b"")).decode("utf-8", "replace").strip()
        return False, out[-120:]
    except (OSError, subprocess.TimeoutExpired) as e:
        return False, str(e)


def find_toolkit():
    # if a previous (pre-re-exec) pass already found a working runtime, trust it
    cached = os.environ.get("_NPU_LOAD_TOOLKIT", "")
    if cached and os.path.exists(os.path.join(cached, "libascendcl.so")):
        lib64 = cached
        print("using CANN runtime (cached): %s" % lib64, flush=True)
        return lib64.replace("/lib64", "") if lib64.endswith("/lib64") else os.path.dirname(lib64), lib64
    # NOTE: aclrtGetMemInfo(HBM) returns (FREE bytes, TOTAL bytes) on these CANN
    # versions; the runtime pool is ~29.5G of the 32G chip (driver keeps ~2.5G).
    candidates = ["/usr/local/Ascend/ascend-toolkit/latest",
                  "/usr/local/Ascend/ascend-toolkit/8.0.RC1",
                  "/usr/local/Ascend/ascend-toolkit/8.0",
                  "/mnt/qjl/mycann/ascend-toolkit/latest",
                  "/usr/local/Ascend/ascend-toolkit/7.0.1.5",
                  "/usr/local/Ascend/ascend-toolkit/7.0"]
    # generic discovery: CANN installs vary (cann-8.5.2/aarch64-linux, /opt/...),
    # so also walk common roots for any dir holding libascendcl.so
    seen = []
    for base in candidates:
        lib64 = os.path.realpath(base) + "/lib64"
        if lib64 not in seen:
            seen.append(lib64)
    for root in ("/usr/local/Ascend", "/usr/local/cann", "/opt"):
        if not os.path.isdir(root):
            continue
        for dirpath, dirnames, filenames in os.walk(root):
            depth = dirpath.count(os.sep) - root.count(os.sep)
            if depth >= 5:
                dirnames[:] = []
                continue
            dirnames[:] = [d for d in dirnames
                           if d not in ("share", "include", "samples", "python",
                                        "tools", "bash", "x86_64-linux")]
            if "libascendcl.so" in filenames and dirpath not in seen:
                seen.append(dirpath)
    # prefer real runtime dirs (named lib64) over stub dirs (devlib),
    # then verify with an actual runtime call
    seen.sort(key=lambda p: (os.path.basename(p) != "lib64", p))
    for lib64 in seen:
        ok, why = _probe_runtime(lib64)
        if ok:
            os.environ["LD_LIBRARY_PATH"] = (lib64 + os.pathsep +
                                             "/usr/local/Ascend/driver/lib64" + os.pathsep +
                                             os.environ.get("LD_LIBRARY_PATH", ""))
            print("using CANN runtime: %s" % lib64, flush=True)
            return lib64.replace("/lib64", "") if lib64.endswith("/lib64") else os.path.dirname(lib64), lib64
        print("skip %s: %s" % (lib64, why), flush=True)
    sys.exit("ERROR: no working CANN runtime found (libascendcl.so with functional aclrtGetDeviceCount)")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pct", type=int, default=90, help="target HBM usage %% (default 90)")
    ap.add_argument("--devices", type=str, default="",
                    help="comma-separated device ids (default: all), e.g. 0,3")
    ap.add_argument("--status", action="store_true", help="show usage and exit")
    args = ap.parse_args()

    toolkit, lib64 = find_toolkit()
    os.environ["ASCEND_HOME_PATH"] = toolkit
    os.environ["LD_LIBRARY_PATH"] = (lib64 + os.pathsep +
                                     "/usr/local/Ascend/driver/lib64" + os.pathsep +
                                     os.environ.get("LD_LIBRARY_PATH", ""))
    try:
        lib = ctypes.CDLL(os.path.join(lib64, "libascendcl.so"))
    except OSError:
        # The running process's linker didn't see LD_LIBRARY_PATH (it is only
        # read at process startup). Re-exec once with the env set; the guard
        # var prevents a loop if the failure is real.
        if os.environ.get("_NPU_LOAD_REEXEC") != lib64:
            env = dict(os.environ)
            env["_NPU_LOAD_REEXEC"] = lib64
            env["_NPU_LOAD_TOOLKIT"] = lib64
            os.execve(sys.executable, [sys.executable] + sys.argv, env)
        raise
    lib.aclrtSetDevice.argtypes = [ctypes.c_uint32]
    lib.aclrtSetDevice.restype = ctypes.c_int
    lib.aclrtMalloc.argtypes = [ctypes.POINTER(ctypes.c_void_p), ctypes.c_size_t, ctypes.c_int]
    lib.aclrtMalloc.restype = ctypes.c_int
    lib.aclrtFree.argtypes = [ctypes.c_void_p]
    lib.aclrtFree.restype = ctypes.c_int
    lib.aclrtGetMemInfo.argtypes = [ctypes.c_int, ctypes.POINTER(ctypes.c_uint64),
                                    ctypes.POINTER(ctypes.c_uint64)]
    lib.aclrtGetMemInfo.restype = ctypes.c_int
    lib.aclrtGetDeviceCount.argtypes = [ctypes.POINTER(ctypes.c_uint32)]
    lib.aclrtGetDeviceCount.restype = ctypes.c_int

    count = ctypes.c_uint32(0)
    if lib.aclrtGetDeviceCount(ctypes.byref(count)) != 0:
        sys.exit("ERROR: aclrtGetDeviceCount failed")
    n = count.value

    def meminfo(dev):
        """Return (used, total) in bytes for the device HBM pool."""
        if lib.aclrtSetDevice(dev) != 0:
            sys.exit("ERROR: aclrtSetDevice(%d) failed" % dev)
        free, total = ctypes.c_uint64(0), ctypes.c_uint64(0)
        if lib.aclrtGetMemInfo(HBM, ctypes.byref(free), ctypes.byref(total)) != 0:
            sys.exit("ERROR: aclrtGetMemInfo(%d) failed" % dev)
        return total.value - free.value, total.value

    if args.status:
        for dev in range(n):
            used, total = meminfo(dev)
            print("NPU %d: %6.1f GiB / %6.1f GiB (%.1f%%)" %
                  (dev, used / 2 ** 30, total / 2 ** 30, 100.0 * used / total), flush=True)
        return

    if args.devices.strip():
        devs = [int(d) for d in args.devices.split(",") if d.strip() != ""]
        bad = [d for d in devs if d < 0 or d >= n]
        if bad:
            sys.exit("ERROR: device ids out of range: %s (have 0..%d)" % (bad, n - 1))
    else:
        devs = list(range(n))

    allocs = []
    for dev in devs:
        if lib.aclrtSetDevice(dev) != 0:
            print("NPU %d: ERROR set device, skipping" % dev, flush=True)
            continue
        used, total = meminfo(dev)
        target = total * args.pct // 100
        to_alloc = target - used
        if to_alloc <= 0:
            print("NPU %d: already at %.1f%%, skipping" % (dev, 100.0 * used / total), flush=True)
            continue
        got = 0
        while to_alloc > 0:
            sz = min(CHUNK, to_alloc)
            ptr = ctypes.c_void_p()
            if lib.aclrtMalloc(ctypes.byref(ptr), sz, HBM) != 0 or not ptr:
                print("NPU %d: malloc of %d MiB failed, stopping this device" %
                      (dev, sz >> 20), flush=True)
                break
            allocs.append((dev, ptr, sz))
            got += sz
            to_alloc -= sz
        now, tot = meminfo(dev)
        print("NPU %d: allocated %7.1f GiB -> now %6.1f/%6.1f GiB (%.1f%%)" %
              (dev, got / 2 ** 30, now / 2 ** 30, tot / 2 ** 30, 100.0 * now / tot), flush=True)

    if not allocs:
        print("Nothing to allocate.", flush=True)
        return

    pid = os.getpid()
    try:
        with open("/root/npu_mem_load.pid", "w") as f:
            f.write(str(pid))
    except OSError:
        pass
    total_alloc = sum(s for _, _, s in allocs)
    print("Holding %.1f GiB across %d NPUs (pid %d). "
          "Release with: kill %d" % (total_alloc / 2 ** 30, len(devs), pid, pid), flush=True)

    stop = {"flag": False}

    def _term(signum, frame):
        stop["flag"] = True
    signal.signal(signal.SIGINT, _term)
    signal.signal(signal.SIGTERM, _term)
    while not stop["flag"]:
        time.sleep(5)

    print("Releasing...", flush=True)
    for dev, ptr, sz in allocs:
        lib.aclrtSetDevice(dev)
        lib.aclrtFree(ptr)
    print("Done.", flush=True)


if __name__ == "__main__":
    main()
