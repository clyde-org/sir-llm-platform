"""Aggregation helpers: prometheus text parsing + per-node card building."""
import re
import time
from typing import Optional

# ---------------------------------------------------------------- prom parse

_LINE = re.compile(r'^([a-zA-Z_:][a-zA-Z0-9_:]*)\{([^}]*)\}\s+([-+0-9.eE]+)')
_LBL = re.compile(r'([a-zA-Z_][a-zA-Z0-9_]*)="((?:[^"\\]|\\.)*)"')


def parse_prom(text):
    """Parse prometheus text exposition -> {metric: [(labels_dict, value), ...]}."""
    out = {}
    for line in text.splitlines():
        if not line or line.startswith("#"):
            continue
        m = _LINE.match(line)
        if not m:
            continue
        name, lbls, val = m.group(1), m.group(2), m.group(3)
        labels = dict(_LBL.findall(lbls))
        try:
            v = float(val)
        except ValueError:
            continue
        out.setdefault(name, []).append((labels, v))
    return out


def npu_devices_from_exporter(text):
    """Build {device_id: {util, hbm_used_mb, hbm_total_mb, proc_pid, model}} from exporter text."""
    p = parse_prom(text)
    devs = {}

    def d(id_):
        return devs.setdefault(id_, {"util": 0.0, "hbm_used_mb": 0.0, "hbm_total_mb": 0.0,
                                     "proc_pid": "", "model": ""})

    for name, key in (("npu_chip_info_utilization", "util"),
                      ("npu_chip_info_hbm_used_memory", "hbm_used_mb"),
                      ("npu_chip_info_hbm_total_memory", "hbm_total_mb")):
        for labels, v in p.get(name, []):
            id_ = labels.get("id")
            if id_ is not None:
                d(id_)[key] = v
    for labels, v in p.get("npu_chip_info_process_info", []):
        id_ = labels.get("id")
        if id_ is not None:
            d(id_)["proc_pid"] = labels.get("process_id", "") or ""
            d(id_)["proc_hbm_mb"] = v
    for labels, v in p.get("npu_chip_info_name", []):
        id_ = labels.get("id")
        if id_ is not None:
            d(id_)["model"] = str(v)
    return devs


# ------------------------------------------------------------- NPU pod parse

NPU_RESOURCE_RE = re.compile(r'(huawei\.com/)?[Aa]scend')


def pod_npu_count(pod_spec):
    """Total NPU devices requested by a pod (any huawei.com/Ascend* resource)."""
    total = 0
    for c in (pod_spec.get("containers") or []) + (pod_spec.get("initContainers") or []):
        res = (c.get("resources") or {})
        for section in ("requests", "limits"):
            for k, v in (res.get(section) or {}).items():
                if NPU_RESOURCE_RE.search(k):
                    try:
                        total = max(total, int(v))
                    except (TypeError, ValueError):
                        pass
            break  # requests preferred (limits identical for NPU allocs)
    return total


def parse_device_list(ann_value):
    """'Ascend910-0,Ascend910-5' -> [0, 5]"""
    ids = []
    for part in (ann_value or "").split(","):
        part = part.strip()
        if not part:
            continue
        m = re.search(r'-(\d+)$', part)
        if m:
            ids.append(int(m.group(1)))
    return sorted(set(ids))


ABSENT = "absent"  # sentinel: pod does not use NPUs
VISIBLE_ENVS = ("ASCEND_RT_VISIBLE_DEVICES", "ASCEND_VISIBLE_DEVICES")


def pod_npu_devices(pod):
    """Device ids a pod can use.

    Returns:
      ABSENT        - no NPU usage detected
      [0, 3, ...]   - explicit device list (device-plugin annotation, or
                      ASCEND[_RT]_VISIBLE_DEVICES env on a direct-access pod)
      None          - uses ALL devices (empty ASCEND[_RT]_VISIBLE_DEVICES)
    """
    ann = (pod.get("metadata", {}) or {}).get("annotations", {}) or {}
    for key in ("huawei.com/AscendReal", "huawei.com/Ascend910", "huawei.com/kltDev"):
        if ann.get(key):
            ids = parse_device_list(ann[key])
            if ids:
                return ids
    spec = pod.get("spec", {}) or {}
    for c in spec.get("containers") or []:
        for e in c.get("env") or []:
            if e.get("name") in VISIBLE_ENVS:
                v = (e.get("value") or "").strip()
                if not v:
                    return None  # all devices
                ids = sorted({int(x) for x in v.split(",")
                              if x.strip().lstrip("-").isdigit()})
                return ids or None
    return ABSENT


def is_npu_pod(pod):
    """True if the pod uses NPUs (device-plugin request OR direct visibility env)."""
    if pod_npu_count(pod.get("spec", {}) or {}) > 0:
        return True
    return pod_npu_devices(pod) != ABSENT


def pod_owner(pod):
    labels = pod.get("metadata", {}).get("labels", {}) or {}
    return labels.get("platform/owner") or labels.get("owner") or ""


def pod_age_hours(pod):
    ts = (pod.get("metadata", {}).get("creationTimestamp")
          or pod.get("metadata", {}).get("creation_timestamp"))
    if ts is None:
        return None
    if isinstance(ts, str):
        ts = ts.replace("Z", "")
        try:
            t = time.mktime(time.strptime(ts[:19], "%Y-%m-%dT%H:%M:%S"))
        except ValueError:
            return None
    else:  # datetime (kubernetes client to_dict)
        t = ts.timestamp() if hasattr(ts, "timestamp") else time.mktime(ts.timetuple())
    return max(0.0, (time.time() - t) / 3600.0)
