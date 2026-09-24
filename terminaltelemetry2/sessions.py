"""
Session files (the folder/sessions YAML shared with the other terminal apps)
and the resolved connect target that the window is built from.

Accepted shapes:

    - folder_name: site1                # or 'folder'
      sessions:
        - display_name: router1.site1
          host: 10.1.1.1
          port: '22'
          DeviceType: arista_eos        # or device_type / platform
          Vendor: Arista
          Model: DCS-7280CR3
          credsid: 1                    # ignored: points at another app's store
          username: admin               # optional
          jump_host: bastion-site1      # optional; '', 'none', 'direct' = no override
          jump_port: 22                 # optional
          jump_username: scott          # optional

A top-level mapping with a 'folders' list (the VS Code extension variant) is
also accepted.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

import yaml

from .ssh.proxy import JumpHop, JumpSpec

_NO_JUMP = {"", "none", "direct", "null", "no"}


def normalize_platform(value: str) -> str:
    """Canonical platform id; aliases come from the platform packs."""
    from .platforms import registry
    return registry().normalize(value)


# ═══════════════════════════════════════════════════════════════════════════
# Session file
# ═══════════════════════════════════════════════════════════════════════════

@dataclass
class SessionEntry:
    folder: str
    name: str
    host: str
    port: int = 22
    platform_hint: str = ""        # raw DeviceType/device_type/platform value
    vendor: str = ""
    model: str = ""
    username: str = ""
    jump_host: str = ""
    jump_port: int = 22
    jump_username: str = ""

    def guess_platform(self, known: Iterable[str]) -> Optional[str]:
        """DeviceType hint, else pack vendor/model match (platforms.py)."""
        from .platforms import registry
        return registry().guess(self.platform_hint, self.vendor, self.model, known)

    def haystack(self) -> str:
        return " ".join((self.folder, self.name, self.host, self.platform_hint,
                         self.vendor, self.model)).lower()


def _int(value: Any, default: int) -> int:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


def _str(value: Any) -> str:
    return "" if value is None else str(value).strip()


def load_sessions(path: str | Path) -> List[SessionEntry]:
    p = Path(path).expanduser()
    with p.open("r", encoding="utf-8") as f:
        data = yaml.safe_load(f)
    if isinstance(data, dict):
        data = data.get("folders", [])
    if not isinstance(data, list):
        raise ValueError(f"{p}: expected a list of folders")

    out: List[SessionEntry] = []
    for i, folder in enumerate(data):
        if not isinstance(folder, dict):
            continue
        fname = _str(folder.get("folder_name") or folder.get("folder") or f"folder {i}")
        for s in folder.get("sessions") or []:
            if not isinstance(s, dict):
                continue
            host = _str(s.get("host"))
            if not host:
                continue
            jump = _str(s.get("jump_host"))
            out.append(SessionEntry(
                folder=fname,
                name=_str(s.get("display_name")) or host,
                host=host,
                port=_int(s.get("port"), 22),
                platform_hint=_str(s.get("platform") or s.get("device_type") or s.get("DeviceType")),
                vendor=_str(s.get("Vendor") or s.get("vendor")),
                model=_str(s.get("Model") or s.get("model")),
                username=_str(s.get("username")),
                jump_host="" if jump.lower() in _NO_JUMP else jump,
                jump_port=_int(s.get("jump_port"), 22),
                jump_username=_str(s.get("jump_username")),
            ))
    if not out:
        raise ValueError(f"{p}: no sessions with a host found")
    return out


# ═══════════════════════════════════════════════════════════════════════════
# Resolved connection
# ═══════════════════════════════════════════════════════════════════════════

@dataclass
class JumpTarget:
    host: str
    port: int = 22
    username: Optional[str] = None     # None -> device username
    password: Optional[str] = None     # None -> device password
    key_file: Optional[str] = None     # None -> device key


@dataclass
class ConnectTarget:
    host: str
    platform: str
    username: str
    port: int = 22
    password: Optional[str] = None
    key_file: Optional[str] = None
    key_passphrase: Optional[str] = None
    display_name: Optional[str] = None
    jump: Optional[JumpTarget] = None

    @property
    def label(self) -> str:
        if self.display_name and self.display_name != self.host:
            return f"{self.display_name} [{self.host}]"
        return self.host

    def jump_spec(self) -> Optional[JumpSpec]:
        """Bastion auth inherits the device's key and password unless the jump
        sets its own -- the common case is the same TACACS/AAA identity."""
        j = self.jump
        if j is None or not j.host:
            return None
        key_file = j.key_file or self.key_file
        key_content = None
        if key_file:
            key_path = Path(key_file).expanduser()
            if not key_path.is_file():
                raise ValueError(f"jump key file not found: {key_path}")
            key_content = key_path.read_text(encoding="utf-8")
        password = j.password or self.password
        if not key_content and not password:
            raise ValueError(f"jump host {j.host}: no key or password")
        return JumpSpec(hops=[JumpHop(
            name=j.host, host=j.host, port=j.port,
            username=j.username or self.username,
            password=password, key_content=key_content,
        )])


def parse_jump(spec: str) -> Tuple[Optional[str], str, int]:
    """'[user@]host[:port]' (IPv6 as [addr]:port) -> (user, host, port)."""
    user = None
    rest = spec.strip()
    if "@" in rest:
        user, rest = rest.rsplit("@", 1)
        user = user or None
    port = 22
    if rest.startswith("["):
        host, close, tail = rest[1:].partition("]")
        if not close or (tail and not tail.startswith(":")):
            raise ValueError(f"bad jump spec {spec!r}")
        if tail:
            port = int(tail[1:])
    elif rest.count(":") == 1:
        host, p = rest.split(":")
        port = int(p)
    else:
        host = rest
    if not host:
        raise ValueError(f"bad jump spec {spec!r}")
    return user, host, port


def jump_to_str(j: Optional[JumpTarget]) -> str:
    if j is None:
        return ""
    host = f"[{j.host}]" if ":" in j.host else j.host
    s = f"{j.username}@{host}" if j.username else host
    return s if j.port == 22 else f"{s}:{j.port}"
