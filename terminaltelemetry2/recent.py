"""Last-used and recent connections for the Connect form.

~/.terminaltelemetry2/connections.yaml:

    last: {host: ..., port: 22, user: ..., platform: ..., key: ..., ...}
    recent: [ {...}, ... ]          # newest first, one per host/port/user/platform

Only the fields in FIELDS are ever written. Passwords, key passphrases and
jump passwords are never persisted -- they are dropped before the write, not
filtered on read. The file is created owner-only (0600).
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Dict, List, Optional

import yaml

from .paths import config_dir

FIELDS = ("host", "port", "user", "platform", "key", "jump", "jump_key",
          "layout", "enable_command", "legacy_ssh")
MAX_RECENT = 15


def path() -> Path:
    return config_dir() / "connections.yaml"


def _clean(entry: dict) -> dict:
    out = {}
    for k in FIELDS:
        v = entry.get(k)
        if v in (None, "", False):
            continue
        out[k] = int(v) if k == "port" else (bool(v) if k == "legacy_ssh" else str(v))
    return out


def _key(e: dict):
    return (e.get("host", "").lower(), int(e.get("port", 22)), e.get("user", ""), e.get("platform", ""))


def load() -> Dict[str, object]:
    """{'last': dict, 'recent': [dict]} -- empty on a missing or unreadable file."""
    try:
        data = yaml.safe_load(path().read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError):
        return {"last": {}, "recent": []}
    last = _clean(data.get("last") or {}) if isinstance(data.get("last"), dict) else {}
    recent = [_clean(e) for e in (data.get("recent") or []) if isinstance(e, dict) and e.get("host")]
    return {"last": last, "recent": recent}


def remember(entry: dict) -> Path:
    """Record a successful-looking connect as `last` and move it to the top of
    `recent` (deduped on host/port/user/platform, capped at MAX_RECENT)."""
    e = _clean(entry)
    cur = load()
    recent: List[dict] = [e] + [r for r in cur["recent"] if _key(r) != _key(e)]
    data = {"last": dict(e), "recent": [dict(r) for r in recent[:MAX_RECENT]]}   # copies: no YAML anchors
    p = path()
    tmp = p.with_suffix(".yaml.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write("# terminaltelemetry2 connections -- no passwords are stored here\n")
        yaml.safe_dump(data, fh, sort_keys=False, default_flow_style=False)
    os.replace(tmp, p)
    try:
        os.chmod(p, 0o600)
    except OSError:
        pass
    return p


def find(host: str) -> Optional[dict]:
    """Most recent entry for a host (case-insensitive), if any."""
    for r in load()["recent"]:
        if r.get("host", "").lower() == host.strip().lower():
            return r
    return None
