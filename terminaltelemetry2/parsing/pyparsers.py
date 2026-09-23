"""
Python parsers: the non-TextFSM path for widget polls.

A widget selects one per platform through its `templates:` map with a `py:`
prefix:

    templates:
      linux: py:linux_links

Each parser takes the cleaned command output and returns records -- a list of
flat dicts -- in the same vocabulary the TextFSM templates use (INTERFACE,
LINK_STATUS, NEIGHBOR, REMOTE_AS ...). Everything downstream (fields aliases,
rates, deltas, computes, drop rules, alerts, views) is unchanged.

Contract:
  * return [] for a legitimately empty answer (no containers, no failed units)
  * raise ValueError when the output isn't what the command should produce
    (tool missing, permission denied) -- the message becomes the tile error

Linux commands run through the telemetry shell like any other poll. Tools that
need root use `sudo -n X 2>/dev/null || X`: passwordless sudo when available,
else the plain command with its stderr intact, so a permission failure surfaces
as an error instead of an empty table.
"""
from __future__ import annotations

import json
import re
from typing import Callable, Dict, List, Optional

PREFIX = "py:"

Records = List[Dict[str, object]]
PY_PARSERS: Dict[str, Callable[[str], Records]] = {}


def register(name: str):
    def deco(fn: Callable[[str], Records]) -> Callable[[str], Records]:
        PY_PARSERS[name] = fn
        return fn
    return deco


def is_py(template: Optional[str]) -> bool:
    return bool(template) and template.startswith(PREFIX)


def get(template: str) -> Callable[[str], Records]:
    name = template[len(PREFIX):]
    try:
        return PY_PARSERS[name]
    except KeyError:
        raise KeyError(f"no python parser {name!r}; known: {', '.join(sorted(PY_PARSERS))}")


# ═══════════════════════════════════════════════════════════════════════════
# helpers
# ═══════════════════════════════════════════════════════════════════════════

def _first_line(text: str) -> str:
    return next((ln.strip() for ln in text.splitlines() if ln.strip()), "")


def _json(text: str, opener: str = "[{"):
    """Parse the JSON document in `text`, skipping any leading noise (a sudo
    lecture, a warning line). Raises ValueError with the first output line."""
    starts = [i for i in (text.find(c) for c in opener) if i >= 0]
    if not starts:
        raise ValueError(_first_line(text) or "no JSON in output")
    try:
        return json.loads(text[min(starts):])
    except ValueError as e:
        raise ValueError(f"bad JSON ({e}): {_first_line(text)[:120]}")


def _sections(text: str) -> Dict[str, str]:
    """Split compound-command output on '@name' marker lines."""
    out: Dict[str, List[str]] = {}
    cur = ""
    for line in text.splitlines():
        m = re.fullmatch(r"@(\w+)\s*", line)
        if m:
            cur = m.group(1)
            out.setdefault(cur, [])
        else:
            out.setdefault(cur, []).append(line)
    return {k: "\n".join(v) for k, v in out.items()}


_TOP_UNITS = {"KiB": 1, "MiB": 1024, "GiB": 1024 ** 2, "TiB": 1024 ** 3}


def _last_top_frame(text: str) -> List[str]:
    """`top -b -n2` prints two frames; the first frame's CPU figures are
    since-boot averages, so only the last frame is read."""
    lines = text.splitlines()
    starts = [i for i, ln in enumerate(lines) if ln.startswith("top - ")]
    if not starts:
        raise ValueError(_first_line(text) or "no top output")
    return lines[starts[-1]:]


# ═══════════════════════════════════════════════════════════════════════════
# system / processes  --  LC_ALL=C top -b -n2 -d0.5 -w 512
# ═══════════════════════════════════════════════════════════════════════════

@register("linux_top_summary")
def linux_top_summary(text: str) -> Records:
    rec: Dict[str, object] = {}
    for ln in _last_top_frame(text)[:8]:
        if ln.startswith("top - "):
            m = re.search(r"load average:\s*([\d.]+),\s*([\d.]+),\s*([\d.]+)", ln)
            if m:
                rec["GLOBAL_LOAD_AVERAGE_1_MINUTES"] = m.group(1)
                rec["GLOBAL_LOAD_AVERAGE_5_MINUTES"] = m.group(2)
                rec["GLOBAL_LOAD_AVERAGE_15_MINUTES"] = m.group(3)
        elif ln.startswith("Tasks:"):
            for n, what in re.findall(r"(\d+)\s+(total|zombie)", ln):
                rec["GLOBAL_TASKS_TOTAL" if what == "total" else "GLOBAL_TASKS_ZOMBIE"] = n
        elif ln.startswith("%Cpu(s):"):
            for v, k in re.findall(r"([\d.]+)\s+(us|sy|id|wa)\b", ln):
                rec[{"us": "GLOBAL_CPU_PERCENT_USER", "sy": "GLOBAL_CPU_PERCENT_SYSTEM",
                     "id": "GLOBAL_CPU_PERCENT_IDLE", "wa": "GLOBAL_CPU_PERCENT_IOWAIT"}[k]] = v
        else:
            m = re.match(r"(KiB|MiB|GiB|TiB) Mem\s*:", ln)
            if m:
                mult = _TOP_UNITS[m.group(1)]
                for v, k in re.findall(r"([\d.]+)\s+(total|free|used)\b", ln):
                    rec[f"GLOBAL_MEM_{k.upper()}"] = str(int(float(v) * mult))
    if "GLOBAL_LOAD_AVERAGE_1_MINUTES" not in rec:
        raise ValueError("no load average in top output")
    return [rec]


@register("linux_top_procs")
def linux_top_procs(text: str) -> Records:
    frame = _last_top_frame(text)
    hdr = next((i for i, ln in enumerate(frame) if ln.split()[:1] == ["PID"]), None)
    if hdr is None:
        raise ValueError("no process table in top output")
    recs: Records = []
    for ln in frame[hdr + 1:]:
        p = ln.split(None, 11)
        if len(p) < 12 or not p[0].isdigit():
            continue
        recs.append({
            "PID": p[0], "USER": p[1], "PRIORITY": p[2], "NICE": p[3],
            "VIRTUAL_MEMORY_SIZE": p[4], "RESIDENT_MEMORY_SIZE": p[5],
            "SHARED_MEMORY_SIZE": p[6], "STATE": p[7], "PERCENT_CPU": p[8],
            "PERCENT_MEMORY": p[9], "CPU_TIME": p[10], "COMMAND": p[11].strip(),
        })
    return recs


# ═══════════════════════════════════════════════════════════════════════════
# version  --  compound KEY=value
# ═══════════════════════════════════════════════════════════════════════════

@register("linux_version")
def linux_version(text: str) -> Records:
    kv: Dict[str, str] = {}
    for ln in text.splitlines():
        k, sep, v = ln.partition("=")
        if sep and re.fullmatch(r"[A-Z_]+", k):
            kv[k] = v.strip().strip('"')
    if "KERNEL" not in kv:
        raise ValueError(_first_line(text) or "no version output")
    up = kv.get("UPTIME_S", "").split(".")[0]
    uptime = ""
    if up.isdigit():
        s = int(up)
        uptime = f"{s // 86400}d {s % 86400 // 3600}h {s % 3600 // 60}m"
    model = " ".join(x for x in (kv.get("VENDOR", ""), kv.get("PRODUCT", "")) if x)
    return [{
        "HOSTNAME": kv.get("HOST", ""),
        "OS": kv.get("PRETTY_NAME", ""),
        "VERSION": f"{kv.get('PRETTY_NAME', '')} / {kv['KERNEL']}".strip(" /"),
        "MODEL": model or kv.get("ARCH", ""),
        "UPTIME": uptime,
        "TOTAL_MEMORY": kv.get("MEMTOTAL", "").split()[0] if kv.get("MEMTOTAL") else "",
        "FREE_MEMORY": kv.get("MEMAVAIL", "").split()[0] if kv.get("MEMAVAIL") else "",
    }]


# ═══════════════════════════════════════════════════════════════════════════
# interfaces  --  ip -j -s link show; @cc; carrier_changes
# ═══════════════════════════════════════════════════════════════════════════

def _operstate(ln: dict) -> str:
    """tun/wg/vmnet/dummy report UNKNOWN while carrying traffic; LOWER_UP is the
    carrier bit, so UNKNOWN + LOWER_UP reads as up."""
    st = str(ln.get("operstate", "UNKNOWN")).lower()
    if st == "unknown" and "LOWER_UP" in (ln.get("flags") or []):
        return "up"
    return st


@register("linux_links")
def linux_links(text: str) -> Records:
    sec = _sections(text)
    links = _json(sec.get("", text), "[")
    changes: Dict[str, str] = {}
    for ln in sec.get("cc", "").splitlines():
        m = re.match(r"/sys/class/net/([^/]+)/carrier_changes:(\d+)", ln.strip())
        if m:
            changes[m.group(1)] = m.group(2)
    recs: Records = []
    for ln in links:
        name = ln.get("ifname", "")
        if not name or name == "lo":
            continue
        st = ln.get("stats64") or {}
        rx, tx = st.get("rx") or {}, st.get("tx") or {}
        flags = ln.get("flags") or []
        recs.append({
            "INTERFACE": name,
            "LINK_STATUS": _operstate(ln),
            "PROTOCOL_STATUS": "up" if "UP" in flags else "admin down",
            "DESCRIPTION": ln.get("ifalias", ""),
            "MTU": str(ln.get("mtu", "")),
            "ADDRESS": ln.get("address", ""),
            "LINK_STATUS_CHANGE": changes.get(name, ""),
            "INPUT_PACKETS": str(rx.get("packets", "")),
            "OUTPUT_PACKETS": str(tx.get("packets", "")),
            "INPUT_BYTES": str(rx.get("bytes", "")),
            "OUTPUT_BYTES": str(tx.get("bytes", "")),
            "INPUT_ERRORS": str(rx.get("errors", "")),
            "OUTPUT_ERRORS": str(tx.get("errors", "")),
            "INPUT_DROPS": str(rx.get("dropped", "")),
            "OUTPUT_DROPS": str(tx.get("dropped", "")),
        })
    return recs


@register("linux_addrs")
def linux_addrs(text: str) -> Records:
    recs: Records = []
    for ln in _json(text, "["):
        name = ln.get("ifname", "")
        if not name or name == "lo":
            continue
        status = _operstate(ln)
        for a in ln.get("addr_info") or []:
            if not a.get("local"):
                continue
            recs.append({
                "INTERFACE": name,
                "FAMILY": a.get("family", ""),
                "ADDRESS": f"{a['local']}/{a.get('prefixlen', '')}",
                "LINK_STATUS": status,
            })
    return recs


# ═══════════════════════════════════════════════════════════════════════════
# storage  --  df -PkT
# ═══════════════════════════════════════════════════════════════════════════

@register("linux_df")
def linux_df(text: str) -> Records:
    lines = [ln for ln in text.splitlines() if ln.strip()]
    if not lines or not lines[0].startswith("Filesystem"):
        raise ValueError(_first_line(text) or "no df output")
    recs: Records = []
    for ln in lines[1:]:
        # mount points may contain spaces: fixed 6 leading columns, rest is the mount
        p = ln.split(None, 6)
        if len(p) < 7:
            continue
        recs.append({
            "FILESYSTEM": p[0], "FSTYPE": p[1], "SIZE_KB": p[2], "USED_KB": p[3],
            "AVAIL_KB": p[4], "USE_PERCENT": p[5].rstrip("%"), "MOUNT": p[6],
        })
    return recs


# ═══════════════════════════════════════════════════════════════════════════
# FRR BGP  --  vtysh -c 'show bgp summary json'
# ═══════════════════════════════════════════════════════════════════════════

@register("frr_bgp_summary")
def frr_bgp_summary(text: str) -> Records:
    data = _json(text, "{")
    recs: Records = []
    seen = set()
    # Default VRF: {"ipv4Unicast": {...}, "ipv6Unicast": {...}}; each AFI carries
    # "peers". A peer active in two AFIs appears twice -- first AFI wins.
    for afi_name, afi in (data.items() if isinstance(data, dict) else []):
        if not isinstance(afi, dict):
            continue
        for peer, p in (afi.get("peers") or {}).items():
            if peer in seen or not isinstance(p, dict):
                continue
            seen.add(peer)
            pfx = p.get("pfxRcd", p.get("prefixReceivedCount", ""))
            recs.append({
                "NEIGHBOR": peer,
                "REMOTE_AS": str(p.get("remoteAs", "")),
                "UP_DOWN": p.get("peerUptime", ""),
                "STATE": p.get("state", ""),
                "PFX_RCVD": str(pfx),
                "DESCRIPTION": p.get("desc", p.get("hostname", "")),
                "AFI": afi_name,
            })
    return recs


# ═══════════════════════════════════════════════════════════════════════════
# LLDP  --  lldpctl -f json
# ═══════════════════════════════════════════════════════════════════════════

def _lldp_iface(local: str, info: dict) -> Optional[Dict[str, object]]:
    chassis = info.get("chassis") or {}
    port = info.get("port") or {}
    # chassis is {sysname: {...}} when the neighbor sends a name, else {"id": {...}}
    if "id" in chassis and isinstance(chassis["id"], dict):
        name, cdata = chassis["id"].get("value", ""), chassis
    elif chassis:
        name, cdata = next(iter(chassis.items()))
    else:
        return None
    caps = cdata.get("capability") if isinstance(cdata, dict) else None
    if isinstance(caps, dict):
        caps = [caps]
    enabled = [c.get("type", "") for c in caps or [] if isinstance(c, dict) and c.get("enabled")]
    pid = port.get("id")
    return {
        "LOCAL_INTERFACE": local,
        "NEIGHBOR_NAME": name,
        "NEIGHBOR_INTERFACE": pid.get("value", "") if isinstance(pid, dict) else str(pid or ""),
        "NEIGHBOR_DESCRIPTION": port.get("descr", ""),
        "CAPABILITIES": ",".join(enabled),
    }


@register("linux_lldp")
def linux_lldp(text: str) -> Records:
    data = _json(text, "{")
    ifaces = (data.get("lldp") or {}).get("interface") or {}
    # one neighbor: {"eth0": {...}}; several: [{"eth0": {...}}, {"eth1": {...}}]
    items = ifaces if isinstance(ifaces, list) else [ifaces]
    recs: Records = []
    for entry in items:
        if not isinstance(entry, dict):
            continue
        for local, info in entry.items():
            if isinstance(info, dict):
                r = _lldp_iface(local, info)
                if r:
                    recs.append(r)
    return recs


# ═══════════════════════════════════════════════════════════════════════════
# containers  --  docker ps -a --format '{{json .}}'
# ═══════════════════════════════════════════════════════════════════════════

@register("docker_ps")
def docker_ps(text: str) -> Records:
    recs: Records = []
    bad = None
    for ln in text.splitlines():
        s = ln.strip()
        if not s:
            continue
        if not s.startswith("{"):
            bad = bad or s
            continue
        try:
            d = json.loads(s)
        except ValueError:
            bad = bad or s
            continue
        recs.append({
            "ID": d.get("ID", ""), "NAME": d.get("Names", ""), "IMAGE": d.get("Image", ""),
            "STATE": d.get("State", ""), "STATUS": d.get("Status", ""),
            "PORTS": d.get("Ports", ""),
        })
    if not recs and bad:
        raise ValueError(bad[:160])
    return recs


# ═══════════════════════════════════════════════════════════════════════════
# systemd  --  systemctl list-units --failed --no-legend --plain --no-pager
# ═══════════════════════════════════════════════════════════════════════════

@register("systemd_failed")
def systemd_failed(text: str) -> Records:
    recs: Records = []
    for ln in text.splitlines():
        # systemd >= 245 prefixes failed units with a status bullet on some outputs
        p = ln.replace("\u25cf", " ").lstrip(" *").split(None, 4)
        if not p:
            continue
        if len(p) < 4 or "." not in p[0]:
            raise ValueError(ln.strip()[:160])
        recs.append({"UNIT": p[0], "LOAD": p[1], "ACTIVE": p[2], "SUB": p[3],
                     "DESCRIPTION": p[4] if len(p) > 4 else ""})
    return recs
