#!/usr/bin/env python3
"""npu-node-agent: per-node control plane for the NPU dummy load.

Runs (privileged, hostPID, hostNetwork) on every NPU node. The host root is
mounted at /host. Exposes a tiny JSON API on :8083:

  GET  /state         -> dummy process state, script presence, node info
  POST /dummy/start   -> start the host-level HBM dummy load (npu_mem_load.py)
  POST /dummy/stop    -> SIGTERM the dummy load, wait for HBM release

The dummy load is a *host process* (invisible to the K8s device plugin), so
starting/stopping it requires host access. We run the host's own python3 via
`chroot /host` so all host library paths resolve exactly as on the host.
Stdlib only, no dependencies.
"""
import json
import os
import re
import signal
import socket
import subprocess
import threading
import time
import urllib.request
import urllib.error
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HOST = os.environ.get("NPU_HOST_ROOT", "/host")
STATE_DIR = os.environ.get("NPU_STATE_DIR", "/var/lib/npu-ops")
SCRIPT = "npu_mem_load.py"
PIDFILE = "/root/npu_mem_load.pid"
LOGFILE = "/root/npu_mem_load.log"
PORT = int(os.environ.get("NPU_AGENT_PORT", "8083"))
PCT = int(os.environ.get("NPU_DUMMY_PCT", "90"))
EXPORTER_URL = os.environ.get("NPU_EXPORTER_URL", "http://127.0.0.1:8082/metrics")
RRISE_TIMEOUT = int(os.environ.get("NPU_RISE_TIMEOUT", "150"))    # s to wait for HBM to rise
RSTOP_TIMEOUT = int(os.environ.get("NPU_STOP_TIMEOUT", "150"))   # s to wait for HBM to release
PYTHON_CANDIDATES = ["/usr/bin/python3", "/usr/local/bin/python3", "/bin/python3"]
# same candidate list as npu_mem_load.py (kept as a hint list; discovery below
# finds everything, including CANN installs in non-standard locations)
TOOLKIT_CANDIDATES = ["/usr/local/Ascend/ascend-toolkit/latest",
                      "/usr/local/Ascend/ascend-toolkit/8.0.RC1",
                      "/usr/local/Ascend/ascend-toolkit/8.0",
                      "/mnt/qjl/mycann/ascend-toolkit/latest",
                      "/usr/local/Ascend/ascend-toolkit/7.0.1.5",
                      "/usr/local/Ascend/ascend-toolkit/7.0"]
DRIVER_LIB64 = "/usr/local/Ascend/driver/lib64"

# host-python probe: not just CDLL (CANN ships *stub* libraries in devlib that
# load fine but cannot execute) - actually call aclrtGetDeviceCount
PROBE_PY = """
import ctypes
lib = ctypes.CDLL("libascendcl.so")
lib.aclrtGetDeviceCount.argtypes = [ctypes.POINTER(ctypes.c_uint32)]
lib.aclrtGetDeviceCount.restype = ctypes.c_int
n = ctypes.c_uint32(0)
rc = lib.aclrtGetDeviceCount(ctypes.byref(n))
print("RC=%d N=%d" % (rc, n.value))
raise SystemExit(0 if rc == 0 and n.value > 0 else 1)
"""

LOCK = threading.Lock()
MANAGED_FILE = os.path.join(STATE_DIR, "managed.json")


def log(msg):
    print("[%s] %s" % (time.strftime("%Y-%m-%dT%H:%M:%S"), msg), flush=True)


def host_path(p):
    return os.path.join(HOST, p.lstrip("/"))


def find_python():
    for cand in PYTHON_CANDIDATES:
        if os.path.exists(host_path(cand)):
            return cand
    return None


def proc_cmdline(pid):
    try:
        with open("/proc/%d/cmdline" % pid, "rb") as f:
            return f.read().replace(b"\0", b" ").decode("utf-8", "replace")
    except OSError:
        return ""


_DOCKER_CACHE = {"names": None, "ts": 0.0}


def docker_containers():
    """List host-level docker containers (invisible to k8s). [] if none,
    None if docker unavailable. Cached 5 min."""
    if _DOCKER_CACHE["names"] is not None and time.time() - _DOCKER_CACHE["ts"] < 300:
        return _DOCKER_CACHE["names"]
    names = None
    try:
        r = subprocess.run(
            ["chroot", HOST, "docker", "ps", "--format", "{{.Names}}"],
            capture_output=True, timeout=15,
            env={"PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
                 "HOME": "/root"})
        if r.returncode == 0:
            names = [l for l in r.stdout.decode().splitlines() if l.strip()]
    except (OSError, subprocess.TimeoutExpired):
        names = None
    _DOCKER_CACHE["names"], _DOCKER_CACHE["ts"] = names, time.time()
    return names


def proc_state(pid):
    """Process state letter ('Z' = zombie), or None if gone."""
    try:
        with open("/proc/%d/stat" % pid) as f:
            # comm field may contain spaces/parens; state is first char after the last ')'
            return f.read().rsplit(")", 1)[1].split()[0]
    except OSError:
        return None


def find_dummy_pid():
    """Return (pid, via) of a running npu_mem_load.py host process, or (None, reason)."""
    # 1) pidfile
    try:
        with open(host_path(PIDFILE)) as f:
            pid = int(f.read().strip() or 0)
        if pid and "npu_mem_load" in proc_cmdline(pid):
            return pid, "pidfile"
    except (OSError, ValueError):
        pass
    # 2) scan /proc (agent has hostPID)
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        try:
            pid = int(entry)
        except ValueError:
            continue
        cmd = proc_cmdline(pid)
        if "npu_mem_load" in cmd and SCRIPT in cmd:
            return pid, "proc-scan"
    return None, "none"


def proc_start_epoch(pid):
    """Approx process start time (epoch) from /proc stat field 22 (starttime, clock ticks)."""
    try:
        with open("/proc/%d/stat" % pid) as f:
            data = f.read()
        # comm field may contain spaces/parens; cut after the last ')'
        rest = data.rsplit(")", 1)[1].split()
        starttime_ticks = int(rest[19])  # field 22 overall
        clk = os.sysconf("SC_CLK_TCK")
        boot = 0
        try:
            with open("/proc/stat") as f:
                for line in f:
                    if line.startswith("btime "):
                        boot = int(line.split()[1])
                        break
        except OSError:
            pass
        return boot + starttime_ticks / clk
    except (OSError, ValueError, IndexError):
        return None


def managed_state():
    try:
        with open(MANAGED_FILE) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_managed(st):
    os.makedirs(STATE_DIR, exist_ok=True)
    tmp = MANAGED_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(st, f)
    os.replace(tmp, MANAGED_FILE)


def hbm_total_mb():
    """Sum of hbm_used_memory across devices from the local exporter; None if unavailable."""
    try:
        req = urllib.request.Request(EXPORTER_URL, headers={"User-Agent": "npu-node-agent"})
        with urllib.request.urlopen(req, timeout=10) as r:
            text = r.read().decode("utf-8", "replace")
        total = 0.0
        found = False
        for line in text.splitlines():
            if line.startswith("npu_chip_info_hbm_used_memory{"):
                try:
                    # sample line: name{labels} <value> <timestamp>
                    val = line.split("} ", 1)[1].split(" ")[0]
                    total += float(val)
                    found = True
                except (ValueError, IndexError):
                    pass
        return total if found else None
    except (urllib.error.URLError, OSError, ValueError):
        return None


def sync_script():
    """Install the bundled npu_mem_load.py to /root/ on the host (always overwrite:
    the managed script is the source of truth, keeps behaviour uniform)."""
    dst = host_path("/root/" + SCRIPT)
    src = os.path.join(os.path.dirname(os.path.abspath(__file__)), SCRIPT)
    with open(src, "rb") as f:
        content = f.read()
    old = None
    if os.path.exists(dst):
        try:
            with open(dst, "rb") as f:
                old = f.read()
        except OSError:
            pass
    if old == content:
        return False
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    with open(dst, "wb") as f:
        f.write(content)
    os.chmod(dst, 0o755)
    log("synced %s to host%s" % (SCRIPT, " (was missing)" if old is None else " (updated)"))
    return True


_LD_CACHE = {"path": None, "ts": 0.0}


def build_ldpath(force=False):
    """LD_LIBRARY_PATH for the chrooted script: every lib64 dir on this node
    that contains a libascendcl.so (CANN layouts vary: ascend-toolkit/latest,
    cann-X.Y.Z/aarch64-linux, /mnt/..., /opt/...) + the driver lib64.
    Cached 10 min (discovery runs host-python probes)."""
    if not force and _LD_CACHE["path"] is not None and time.time() - _LD_CACHE["ts"] < 600:
        return _LD_CACHE["path"]
    roots = ["/usr/local/Ascend", "/opt", "/usr/local/cann"]
    found = []
    for root in roots:
        hroot = host_path(root)
        if not os.path.isdir(hroot):
            continue
        for dirpath, dirnames, filenames in os.walk(hroot):
            depth = dirpath.count("/") - hroot.count("/")
            if depth >= 5:
                dirnames[:] = []
                continue
            dirnames[:] = [d for d in dirnames
                           if d not in ("share", "include", "samples", "python",
                                        "tools", "bash", "opp", "x86_64-linux")]
            if "libascendcl.so" in filenames:
                lib64 = os.path.relpath(dirpath, HOST)
                if lib64 not in found:
                    found.append(lib64)
    # verify each candidate in ISOLATION (its own lib64 + driver only) and only
    # accept stacks where aclrtGetDeviceCount actually executes (rejects stubs)
    py = find_python()
    if py and found:
        good = []
        for lib64 in found:
            env = {"LD_LIBRARY_PATH": lib64 + ":" + DRIVER_LIB64,
                   "PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
                   "HOME": "/root"}
            try:
                r = subprocess.run(["chroot", HOST, py, "-c", PROBE_PY],
                                   capture_output=True, timeout=90, env=env)
                if r.returncode == 0:
                    good.append(lib64)
                    log("toolkit ok: %s (%s)" % (lib64, (r.stdout or b"").decode().strip()))
                else:
                    log("toolkit rejected: %s (%s)" %
                        (lib64, (r.stdout or r.stderr or b"")[-140:].decode("utf-8", "replace").strip()))
            except (OSError, subprocess.TimeoutExpired):
                log("toolkit probe error: %s" % lib64)
        found = good
    if os.path.exists(host_path(DRIVER_LIB64)):
        found.append(DRIVER_LIB64)
    result = ":".join(found)
    _LD_CACHE["path"], _LD_CACHE["ts"] = result, time.time()
    return result


def wait_hbm(condition, timeout, interval=3):
    """Poll exporter until condition(hbm_mb) is true or timeout. Returns (met, final_mb)."""
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        last = hbm_total_mb()
        if last is not None and condition(last):
            return True, last
        time.sleep(interval)
    return False, last


class Api(BaseHTTPRequestHandler):
    server_version = "npu-node-agent/1.0"

    def _json(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):  # quieter default logging
        log("%s %s" % (self.address_string(), fmt % args))

    def do_GET(self):
        if self.path == "/health":
            self._json(200, {"ok": True, "node": os.environ.get("NODE_NAME", socket.gethostname())})
        elif self.path == "/state":
            pid, via = find_dummy_pid()
            st = managed_state()
            managed = bool(pid and st.get("pid") == pid)
            started_at = proc_start_epoch(pid) if pid else st.get("started_at")
            ldpath = build_ldpath()
            self._json(200, {
                "node": os.environ.get("NODE_NAME", socket.gethostname()),
                "ts": int(time.time()),
                "dummy": {
                    "running": pid is not None,
                    "pid": pid,
                    "detected_via": via,
                    "managed": managed,
                    "started_at": int(started_at) if started_at else None,
                },
                "script_present": os.path.exists(host_path("/root/" + SCRIPT)),
                "host_python": find_python(),
                "docker_containers": docker_containers(),
                "toolkit_libs": ldpath.split(":") if ldpath else [],
                "pct": PCT,
            })
        else:
            self._json(404, {"error": "not found"})

    def do_POST(self):
        if self.path == "/dummy/start":
            self._start()
        elif self.path == "/dummy/stop":
            self._stop()
        else:
            self._json(404, {"error": "not found"})

    def _start(self):
        with LOCK:
            pid, _ = find_dummy_pid()
            if pid:
                return self._json(409, {"error": "dummy load already running", "pid": pid})
            py = find_python()
            if not py:
                return self._json(500, {"error": "no host python3 found (tried %s)" % PYTHON_CANDIDATES})
            ldpath = build_ldpath(force=True)
            if not ldpath or ldpath == DRIVER_LIB64:
                return self._json(500, {"error": "no loadable CANN toolkit (libascendcl.so) found on host; see agent logs"})
            baseline = hbm_total_mb()
            if baseline is not None and baseline > 35 * 1024:
                # >1 NPU already loaded by something else (real workload, host
                # docker container, manual script) - a dummy load would fight it
                extra = [c for c in (docker_containers() or [])]
                hint = ("; host docker containers: %s" % ", ".join(extra[:4])) if extra else \
                       "; check for host processes (npu-smi info)"
                return self._json(409, {
                    "error": ("NPU HBM already in use (%.0f GiB held before start) - "
                              "not starting dummy load%s") % (baseline / 1024, hint)})
            cmd = ["chroot", HOST, "/usr/bin/env", "LD_LIBRARY_PATH=" + ldpath,
                   py, "/root/" + SCRIPT, "--pct", str(PCT)]
            synced = sync_script()
            log("starting dummy load: %s (baseline_hbm_mb=%s)" % (" ".join(cmd), baseline))
            logf = open(host_path(LOGFILE), "ab")
            proc = subprocess.Popen(
                cmd, stdout=logf, stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL, start_new_session=True,
            )
            logf.close()
            # give it a moment to die quickly (bad libs etc.)
            time.sleep(4)
            if proc.poll() is not None:
                return self._json(500, {
                    "error": "npu_mem_load.py exited early (code %s); see %s" % (proc.returncode, LOGFILE),
                })
            if baseline is None:
                ok, final = True, None
                note = "exporter unavailable, rise not verified"
            else:
                ok, final = wait_hbm(lambda mb: mb >= baseline + 5 * 1024, RRISE_TIMEOUT)
                note = None
            if not ok:
                # process alive but memory did not rise: report, but keep it (visible in logs)
                note = "hbm did not rise within %ss (final=%s); check %s" % (RRISE_TIMEOUT, final, LOGFILE)
            save_managed({"pid": proc.pid, "started_at": int(time.time()), "pct": PCT})
            log("dummy load started: pid=%s verified=%s %s" % (proc.pid, ok, note or ""))
            self._json(200, {
                "started": True, "pid": proc.pid, "pct": PCT,
                "script_synced": synced,
                "hbm_verified": ok if baseline is not None else None,
                "hbm_total_mb": final, "note": note,
            })

    def _stop(self):
        with LOCK:
            pid, via = find_dummy_pid()
            if not pid:
                return self._json(409, {"error": "no dummy load running"})
            before = hbm_total_mb()
            log("stopping dummy load pid=%s (detected via %s, hbm_before_mb=%s)" % (pid, via, before))
            try:
                os.kill(pid, signal.SIGTERM)
            except OSError as e:
                return self._json(500, {"error": "kill failed: %s" % e})
            # wait for the process to exit (it frees HBM in its SIGTERM handler).
            # A zombie means it has exited (parent just hasn't reaped it).
            deadline = time.time() + RSTOP_TIMEOUT
            exited = False
            while time.time() < deadline:
                st = proc_state(pid)
                if st is None or st == "Z":
                    exited = True
                    break
                time.sleep(2)
            if not exited:
                return self._json(504, {
                    "error": "pid %s still alive after %ss; it will be reaped or needs kill -9" % (pid, RSTOP_TIMEOUT),
                    "pid": pid,
                })
            # confirm HBM released (best effort)
            released, final = True, None
            if before is not None:
                deadline = time.time() + 30
                while time.time() < deadline:
                    final = hbm_total_mb()
                    if final is None:
                        break
                    if final <= (before - 10 * 1024) or final < 30 * 1024:  # ~baseline for 8x910B
                        break
                    time.sleep(2)
                released = final is None or final <= (before - 10 * 1024) or final < 30 * 1024
            try:
                os.remove(host_path(PIDFILE))
            except OSError:
                pass
            save_managed({})
            log("dummy load stopped: pid=%s hbm_released=%s (final_mb=%s)" % (pid, released, final))
            self._json(200, {
                "stopped": True, "pid": pid, "detected_via": via,
                "hbm_released": released, "hbm_total_mb": final,
            })


def main():
    os.makedirs(STATE_DIR, exist_ok=True)
    srv = ThreadingHTTPServer(("0.0.0.0", PORT), Api)
    log("npu-node-agent listening on :%d (host root at %s, pct=%s)" % (PORT, HOST, PCT))
    srv.serve_forever()


if __name__ == "__main__":
    main()
