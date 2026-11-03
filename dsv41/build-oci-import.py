#!/usr/bin/env python3
"""Build an OCI-layout tar from /mnt/data/dsv41-parallel (raw registry blobs
fetched by fetch-parallel.py) and distribute it to the k8s nodes via
`ctr -n k8s.io images import` (verified working with node containerd 2.2).

Usage: build-oci-import.py [--tar-only | --node node3 | --node node4 | --all]
"""
import json, os, subprocess, hashlib, sys

import sys as _s
REG = _s.argv[1] if len(_s.argv) > 1 else "quay.io/ascend/vllm-ascend:deepseek-v4.1-flash"
BLOBDIR = _s.argv[2] if len(_s.argv) > 2 else "/mnt/data/dsv41-parallel"
LAYOUT = _s.argv[3] if len(_s.argv) > 3 else "/mnt/data/dsv41-oci"
TAR = _s.argv[4] if len(_s.argv) > 4 else "/mnt/data/dsv41-oci.tar"
PROXY = "http://127.0.0.1:3128"
MY_IP = "7.191.128.118"
AGENT = {"node3": "npu-node-agent-5kzvd", "node4": "npu-node-agent-pvhxf"}

def sh(args, **kw):
    e = dict(os.environ, https_proxy=PROXY, http_proxy=PROXY)
    return subprocess.run(args, env=e, check=True, capture_output=True, **kw)

def build_layout():
    os.makedirs(f"{LAYOUT}/blobs/sha256", exist_ok=True)
    open(f"{LAYOUT}/oci-layout", "w").write(json.dumps({"imageLayoutVersion": "1.0.0"}))
    manifest_bytes = sh(["/tmp/crane", "manifest", REG, "--platform", "linux/arm64"]).stdout
    mdigest = hashlib.sha256(manifest_bytes).hexdigest()
    open(f"{LAYOUT}/blobs/sha256/{mdigest}", "wb").write(manifest_bytes)
    m = json.loads(manifest_bytes)

    cfgd = m["config"]["digest"]
    data = open(f"{BLOBDIR}/{cfgd}", "rb").read()
    assert hashlib.sha256(data).hexdigest() == cfgd.split(":")[1], "config hash mismatch"
    open(f"{LAYOUT}/blobs/sha256/{cfgd.split(':')[1]}", "wb").write(data)
    m["config"]["size"] = len(data)

    for l in m["layers"]:
        d = l["digest"]
        data = open(f"{BLOBDIR}/{d.split(':')[1]}.tar.gz", "rb").read()
        assert hashlib.sha256(data).hexdigest() == d.split(":")[1], f"layer hash mismatch {d}"
        open(f"{LAYOUT}/blobs/sha256/{d.split(':')[1]}", "wb").write(data)
        l["size"] = len(data)

    open(f"{LAYOUT}/index.json", "w").write(json.dumps({
        "schemaVersion": 2,
        "manifests": [{
            "mediaType": "application/vnd.oci.image.manifest.v1+json",
            "digest": f"sha256:{mdigest}",
            "size": len(manifest_bytes),
            "annotations": {"org.opencontainers.image.ref.name": REG},
        }],
    }))
    print(f"layout built: manifest sha256:{mdigest[:19]}..., {len(m['layers'])} layers")

def build_tar():
    subprocess.run(["tar", "-cf", TAR, "-C", LAYOUT, "oci-layout", "index.json", "blobs"], check=True)
    print(f"tar: {TAR} ({os.path.getsize(TAR)/1e9:.1f} GB)")

def node_import(node):
    agent = AGENT[node]
    size = os.path.getsize(TAR)
    k = lambda *a: subprocess.run(list(a), check=True, capture_output=True, text=True)
    print(f"--- {node}: transfer ({size/1e9:.1f} GB) + import")
    k("kubectl", "exec", "-n", "npu-ops", agent, "--", "chroot", "/host", "sh", "-c",
      f"curl -fsS --connect-timeout 10 -o /tmp/dsv41-oci.tar http://{MY_IP}:8899/dsv41-oci.tar && "
      f"test $(stat -c%s /tmp/dsv41-oci.tar) -eq {size} && echo TRANSFER-OK")
    k("kubectl", "exec", "-n", "npu-ops", agent, "--", "chroot", "/host", "sh", "-c",
      "ctr -n k8s.io images import /tmp/dsv41-oci.tar 2>&1 | grep -v DEPRECATION | tail -3 && "
      "crictl images | grep deepseek-v4.1-flash")

if __name__ == "__main__":
    args = sys.argv[1:]
    if "--tar-only" in args or not args:
        build_layout()
        build_tar()
    for node in AGENT:
        if node in args or "--all" in args:
            node_import(node)
