"""Platform packs -- everything tt2 knows about a platform, in one YAML file.

    platform: hp_comware            # canonical id; also the template-name prefix
    title: HPE Comware
    aliases: [comware, h3c]         # --platform / session-file DeviceType values
    match:                          # session-file Vendor/Model inference
      vendor: [hpe, h3c]            #   case-insensitive substrings of Vendor
      model: ["^59", "^FF"]         #   regexes on Model; a model match beats a
                                    #   vendor-only pack for the same vendor
    tested: false                   # true once run against real gear
    session:
      paging: screen-length disable # a command or a list run in order;
                                    # null/absent = the paging shotgun,
                                    # [] = send nothing (paging handled otherwise)
      enable: null                  # command to enter privileged mode, if any
      username_suffix: null         # appended to the login name (MikroTik: +ct511w4098h)
      read_timeout: null            # seconds a command may stay silent before the read
                                    # gives up (default 3; Linux ships 30 -- docker/top
                                    # on a busy host can pause longer than a CLI does)
      shell: null                   # posix = replace the login shell (Linux hosts)
    layout: null                    # preferred layout name (else layout platforms:)
    counters:                       # traffic monitor; {intf} is substituted
      command: display interface {intf}
      rx: '^\\s*Input(?: \\(total\\))?:\\s+\\d+ packets, (\\d+) bytes'
      tx: '^\\s*Output(?: \\(total\\))?:\\s+\\d+ packets, (\\d+) bytes'
      # or  parser: builtin   (EOS/IOS/NX-OS/Junos/Linux formats in monitor.py)
    bindings:                       # widget name -> how this platform feeds it
      lldp_neighbors:
        command: display lldp neighbor-information list
      port_status:
        command: display interface brief
        fields: {status: [LINK], vlan: [PVID]}   # aliases tried before the widget's
      intf_counters: null           # null = not on this platform, even if inline
      containers: {sudo: true}      # keep the widget's command/parser, run it via sudo -n

A user pack with `merge: true` layers onto the pack of the same platform
loaded before it (bundled) instead of replacing it: bindings merge per
widget, aliases/vendors/models add, anything else it sets wins. E.g.
~/.terminaltelemetry2/platforms/linux-sudo.yaml:

    platform: linux
    merge: true
    bindings:
      containers: {sudo: true}
      bgp_peers: {sudo: true}

A binding overrides the widget's inline commands/templates/requires for its
platform; widgets keep inline entries for platforms that have no binding, so
existing widget files work unchanged. User packs (~/.terminaltelemetry2/
platforms/*.yaml) replace a bundled pack of the same `platform:` whole.
"""
from __future__ import annotations

import dataclasses
import logging
import re
import shlex
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Pattern, Tuple

import yaml

from .parsing.pyparsers import PREFIX as PY_PREFIX, PY_PARSERS, is_py
from .widgets.schema import MAX_COMMAND_LEN, WidgetDef

log = logging.getLogger(__name__)

SHELLS = {"posix"}
_PLATFORM_ID = re.compile(r"^[a-z0-9]+(?:_[a-z0-9]+)*$")


class PackError(ValueError):
    pass


@dataclass
class Binding:
    command: Optional[str] = None       # None with removed=False: keep the widget's inline command
    template: Optional[str] = None
    requires: Optional[str] = None
    fields: Dict[str, List[str]] = field(default_factory=dict)
    removed: bool = False               # `widget: null` in the pack
    sudo: bool = False                  # run the (pack or inline) command via `sudo -n`


@dataclass
class Counters:
    command: str
    parser: str = "regex"               # regex | builtin
    rx: Optional[Pattern] = None
    tx: Optional[Pattern] = None

    def parse(self, text: str) -> Tuple[int, int]:
        """(rx_octets, tx_octets) via the rx/tx regexes; first match each.
        Raises ValueError with the first output line on no match."""
        mi, mo = self.rx.search(text), self.tx.search(text)
        if not (mi and mo):
            # first line with words: CLI errors lead with a bare caret line
            first = next((ln.strip() for ln in text.splitlines()
                          if re.search(r"[A-Za-z]", ln)), "")
            raise ValueError(first or "no byte counters in output")
        return int(mi.group(1)), int(mo.group(1))


@dataclass
class PlatformPack:
    platform: str
    title: str = ""
    aliases: List[str] = field(default_factory=list)
    vendors: List[str] = field(default_factory=list)
    models: List[Pattern] = field(default_factory=list)
    paging: Optional[List[str]] = None   # None = the paging shotgun; [] = send nothing
    enable: Optional[str] = None
    username_suffix: Optional[str] = None
    read_timeout: Optional[float] = None    # seconds of silence per command; None = default
    shell: Optional[str] = None
    layout: Optional[str] = None
    counters: Optional[Counters] = None
    bindings: Dict[str, Binding] = field(default_factory=dict)
    tested: bool = False
    merge: bool = False                 # layer onto the pack loaded before it (see module doc)
    present: frozenset = frozenset()    # keys the YAML actually set (merge needs to know)
    source: str = "<memory>"

    @property
    def paging_config(self) -> Optional[object]:
        """SSHClientConfig.paging_disable_command: None, a command, or a list."""
        if self.paging is None:
            return None
        return self.paging[0] if len(self.paging) == 1 else list(self.paging)


# ═══════════════════════════════════════════════════════════════════════════
# Parsing
# ═══════════════════════════════════════════════════════════════════════════

def _err(src: str, msg: str) -> PackError:
    return PackError(f"{src}: {msg}")


def _opt_str(v, src: str, where: str) -> Optional[str]:
    if v is None:
        return None
    if not isinstance(v, (str, int, float)):
        raise _err(src, f"{where} must be a string")
    s = str(v).strip()
    return s or None


def _str_list(v, src: str, where: str) -> List[str]:
    if v is None:
        return []
    if isinstance(v, str):
        v = [v]
    if not isinstance(v, list):
        raise _err(src, f"{where} must be a string or list of strings")
    return [str(x).strip() for x in v if str(x).strip()]


def _regex(pat: str, src: str, where: str, groups: Optional[int] = None) -> Pattern:
    try:
        rx = re.compile(pat, re.M)
    except re.error as e:
        raise _err(src, f"{where}: bad regex: {e}")
    if groups is not None and rx.groups != groups:
        raise _err(src, f"{where}: needs exactly {groups} capture group(s), has {rx.groups}")
    return rx


def _command(v, src: str, where: str) -> Optional[str]:
    cmd = _opt_str(v, src, where)
    if cmd and len(cmd) > MAX_COMMAND_LEN:
        raise _err(src, f"{where}: {len(cmd)} chars; keep under {MAX_COMMAND_LEN}")
    return cmd


def _template(v, src: str, where: str) -> Optional[str]:
    t = _opt_str(v, src, where)
    if t and is_py(t) and t[len(PY_PREFIX):] not in PY_PARSERS:
        raise _err(src, f"{where}: unknown python parser {t!r}; known: {', '.join(sorted(PY_PARSERS))}")
    return t


def parse_pack(data, source: str = "<memory>") -> PlatformPack:
    if not isinstance(data, dict):
        raise _err(source, "top level must be a mapping")
    plat = str(data.get("platform") or "").strip()
    if not _PLATFORM_ID.match(plat):
        raise _err(source, f"platform: {plat!r} must be lowercase words joined by '_' "
                           "(it prefixes template names, e.g. hp_comware)")
    src = f"{source} [{plat}]"

    match = data.get("match") or {}
    if not isinstance(match, dict):
        raise _err(src, "match must be a mapping")
    models = []
    for pat in _str_list(match.get("model"), src, "match.model"):
        try:
            models.append(re.compile(pat, re.I))
        except re.error as e:
            raise _err(src, f"match.model: bad regex {pat!r}: {e}")

    sess = data.get("session") or {}
    if not isinstance(sess, dict):
        raise _err(src, "session must be a mapping")
    shell = _opt_str(sess.get("shell"), src, "session.shell")
    if shell and shell not in SHELLS:
        raise _err(src, f"session.shell: {shell!r}; known: {', '.join(sorted(SHELLS))}")

    counters = None
    raw_c = data.get("counters")
    if raw_c is not None:
        if not isinstance(raw_c, dict):
            raise _err(src, "counters must be a mapping")
        cmd = _command(raw_c.get("command"), src, "counters.command")
        if not cmd or "{intf}" not in cmd:
            raise _err(src, "counters.command is required and must contain {intf}")
        parser = str(raw_c.get("parser") or "regex").strip()
        if parser == "builtin":
            counters = Counters(cmd, "builtin")
        elif parser == "regex":
            if not raw_c.get("rx") or not raw_c.get("tx"):
                raise _err(src, "counters: rx and tx regexes are required (or parser: builtin)")
            counters = Counters(cmd, "regex",
                                _regex(str(raw_c["rx"]), src, "counters.rx", groups=1),
                                _regex(str(raw_c["tx"]), src, "counters.tx", groups=1))
        else:
            raise _err(src, f"counters.parser: {parser!r}; use regex or builtin")

    bindings: Dict[str, Binding] = {}
    raw_b = data.get("bindings") or {}
    if not isinstance(raw_b, dict):
        raise _err(src, "bindings must be a mapping widget -> binding")
    for wname, b in raw_b.items():
        where = f"bindings.{wname}"
        if b is None:
            bindings[str(wname)] = Binding(removed=True)
            continue
        if isinstance(b, str):                      # shorthand: widget: <command>
            b = {"command": b}
        if not isinstance(b, dict):
            raise _err(src, f"{where} must be a mapping, a command string, or null")
        unknown = set(b) - {"command", "template", "requires", "fields", "sudo"}
        if unknown:
            raise _err(src, f"{where}: unknown keys {sorted(unknown)}")
        flds: Dict[str, List[str]] = {}
        raw_f = b.get("fields") or {}
        if not isinstance(raw_f, dict):
            raise _err(src, f"{where}.fields must be a mapping field -> aliases")
        for fname, aliases in raw_f.items():
            al = _str_list(aliases, src, f"{where}.fields.{fname}")
            if not al:
                raise _err(src, f"{where}.fields.{fname}: give at least one TextFSM field")
            flds[str(fname)] = al
        binding = Binding(command=_command(b.get("command"), src, f"{where}.command"),
                          template=_template(b.get("template"), src, f"{where}.template"),
                          requires=_opt_str(b.get("requires"), src, f"{where}.requires"),
                          fields=flds, sudo=_bool(b.get("sudo", False), src, f"{where}.sudo"))
        if binding.requires and not binding.command:
            raise _err(src, f"{where}: requires needs a command in the same binding")
        bindings[str(wname)] = binding

    present = {k for k in ("title", "aliases", "tested", "layout", "counters") if k in data}
    present |= {f"match.{k}" for k in ("vendor", "model") if k in match}
    present |= {f"session.{k}" for k in ("paging", "enable", "username_suffix", "shell",
                                         "read_timeout") if k in sess}
    return PlatformPack(
        platform=plat, title=str(data.get("title") or plat),
        aliases=[a.lower() for a in _str_list(data.get("aliases"), src, "aliases")],
        vendors=[v.lower() for v in _str_list(match.get("vendor"), src, "match.vendor")],
        models=models,
        paging=(None if sess.get("paging") is None else
                [c for c in (_command(x, src, "session.paging")
                             for x in _str_list(sess.get("paging"), src, "session.paging")) if c]),
        enable=_enable(sess.get("enable"), src),
        username_suffix=_opt_str(sess.get("username_suffix"), src, "session.username_suffix"),
        read_timeout=_read_timeout(sess.get("read_timeout"), src),
        shell=shell, layout=_opt_str(data.get("layout"), src, "layout"),
        counters=counters, bindings=bindings, tested=bool(data.get("tested", False)),
        merge=_bool(data.get("merge", False), src, "merge"), present=frozenset(present),
        source=source,
    )


_CONFIG_MODE = re.compile(r"^\s*(conf(ig(ure)?)?(\s+(t(erminal)?|private|exclusive|session\b.*))?|"
                          r"system-view|sys|edit)\s*$", re.I)


def _enable(v, src: str) -> Optional[str]:
    """session.enable must reach privileged EXEC, never configuration mode --
    every poll would run inside config mode (and some platforms lock it)."""
    cmd = _command(v, src, "session.enable")
    if cmd and _CONFIG_MODE.match(cmd):
        raise _err(src, f"session.enable: {cmd!r} enters configuration mode; use the command "
                        "that reaches privileged EXEC (e.g. 'enable'), or leave it empty")
    return cmd


def _read_timeout(v, src: str) -> Optional[float]:
    if v is None:
        return None
    try:
        t = float(v)
    except (TypeError, ValueError):
        raise _err(src, "session.read_timeout must be a number of seconds")
    if not 1 <= t <= 600:
        raise _err(src, "session.read_timeout must be between 1 and 600 seconds")
    return t


def _bool(v, src: str, where: str) -> bool:
    if isinstance(v, bool):
        return v
    raise _err(src, f"{where} must be true or false")


_SHELL_META = re.compile(r"[;&|<>`$()\n]")


def sudo_wrap(cmd: str) -> str:
    """Run `cmd` via passwordless sudo. `-n` never prompts: without NOPASSWD
    it fails at once ("a password is required") instead of hanging the
    telemetry shell. Compound commands (; | && redirects) run in one `sh -c`
    so sudo covers all of them, not just the first word."""
    if cmd.startswith("sudo "):
        return cmd
    if _SHELL_META.search(cmd):
        return "sudo -n sh -c " + shlex.quote(cmd)
    return "sudo -n " + cmd


def merge_packs(base: PlatformPack, over: PlatformPack) -> PlatformPack:
    """`over` (merge: true) layered on `base`: per-widget bindings replace,
    lists add, any other field `over` actually sets wins."""
    m = dataclasses.replace(base, bindings={**base.bindings, **over.bindings},
                            source=f"{base.source} + {over.source}", merge=False)
    for attr, key in (("aliases", "aliases"), ("vendors", "match.vendor")):
        if key in over.present:
            setattr(m, attr, getattr(base, attr) + [x for x in getattr(over, attr)
                                                    if x not in getattr(base, attr)])
    if "match.model" in over.present:
        m.models = base.models + over.models
    for attr, key in (("title", "title"), ("tested", "tested"), ("layout", "layout"),
                      ("counters", "counters"), ("paging", "session.paging"),
                      ("enable", "session.enable"), ("username_suffix", "session.username_suffix"),
                      ("read_timeout", "session.read_timeout"),
                      ("shell", "session.shell")):
        if key in over.present:
            setattr(m, attr, getattr(over, attr))
    return m


def load_pack(path: Path) -> PlatformPack:
    try:
        data = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    except yaml.YAMLError as e:
        raise PackError(f"{path}: YAML error: {e}")
    return parse_pack(data, str(path))


# ═══════════════════════════════════════════════════════════════════════════
# Registry
# ═══════════════════════════════════════════════════════════════════════════

class Platforms:
    def __init__(self, packs: Iterable[PlatformPack] = ()):
        self.packs: Dict[str, PlatformPack] = {}
        for p in packs:
            self.packs[p.platform] = p
        self._alias: Dict[str, str] = {}
        self.conflicts: List[str] = []
        for p in self.packs.values():
            for a in p.aliases:
                if a in self.packs and a != p.platform:
                    self.conflicts.append(f"alias {a!r} of {p.platform} is another pack's platform id; ignored")
                    continue
                if a in self._alias and self._alias[a] != p.platform:
                    self.conflicts.append(f"alias {a!r} claimed by {self._alias[a]} and {p.platform}; "
                                          f"{p.platform} wins")
                self._alias[a] = p.platform
        for c in self.conflicts:
            log.warning("%s", c)

    def get(self, platform: str) -> Optional[PlatformPack]:
        return self.packs.get(platform)

    def normalize(self, value: str) -> str:
        v = str(value).strip().lower()
        return self._alias.get(v, v)

    def guess(self, hint: str = "", vendor: str = "", model: str = "",
              known: Optional[Iterable[str]] = None) -> Optional[str]:
        """Session-file inference: an explicit hint (normalized) if known, else
        vendor substring match, with a model regex match beating a pack that
        claims the vendor without model rules."""
        known_set = set(known) if known is not None else set(self.packs)
        if hint:
            p = self.normalize(hint)
            if p in known_set:
                return p
        v = vendor.strip().lower()
        if not v:
            return None
        cands = [p for p in self.packs.values()
                 if p.platform in known_set and any(x in v for x in p.vendors)]
        by_model = [p for p in cands if p.models and any(rx.search(model or "") for rx in p.models)]
        if by_model:
            return by_model[0].platform
        default = [p for p in cands if not p.models]
        if len(default) > 1:                     # two packs claim the vendor outright: don't pick
            log.warning("vendor %r matches %s; set DeviceType in the session file",
                        vendor, ", ".join(sorted(p.platform for p in default)))
            return None
        return default[0].platform if default else None

    def apply(self, widgets: Dict[str, WidgetDef], platform: str,
              warnings: Optional[List[str]] = None) -> Dict[str, WidgetDef]:
        """Widgets specialized for one platform: the pack's bindings override
        inline commands/templates/requires, and binding field aliases are tried
        before the widget's own. Unbound widgets pass through untouched."""
        pack = self.packs.get(platform)
        if pack is None:
            return dict(widgets)
        out = dict(widgets)
        for wname, b in pack.bindings.items():
            w = widgets.get(wname)
            if w is None:
                _warn(warnings, f"{pack.source}: bindings.{wname}: no such widget")
                continue
            commands, templates, requires = dict(w.commands), dict(w.templates), dict(w.requires)
            if b.removed:
                for d in (commands, templates, requires):
                    d.pop(platform, None)
                out[wname] = dataclasses.replace(w, commands=commands, templates=templates,
                                                 requires=requires)
                continue
            if b.command:
                commands[platform] = b.command
                # a new command invalidates an inline template/requires written for the old one
                templates.pop(platform, None)
                requires.pop(platform, None)
            if platform not in commands:
                _warn(warnings, f"{pack.source}: bindings.{wname}: no command "
                                f"(widget has none inline for {platform})")
                continue
            if b.sudo:
                commands[platform] = sudo_wrap(commands[platform])
                if len(commands[platform]) > MAX_COMMAND_LEN:
                    _warn(warnings, f"{pack.source}: bindings.{wname}: command too long "
                                    f"with sudo ({len(commands[platform])} chars)")
            if b.template:
                templates[platform] = b.template
            if b.requires:
                requires[platform] = b.requires
            fields = {k: list(v) for k, v in w.fields.items()}
            for fname, aliases in b.fields.items():
                if fname not in fields:
                    _warn(warnings, f"{pack.source}: bindings.{wname}.fields.{fname}: "
                                    f"not a field of {wname} ({', '.join(fields)})")
                    continue
                fields[fname] = aliases + [a for a in fields[fname] if a not in aliases]
            out[wname] = dataclasses.replace(w, commands=commands, templates=templates,
                                             requires=requires, fields=fields)
        return out

    def known(self, widgets: Dict[str, WidgetDef]) -> List[str]:
        """Platforms with at least one widget command, after bindings."""
        plats = {p for w in widgets.values() for p in w.commands}
        plats |= set(self.packs)
        return sorted(p for p in plats
                      if any(p in w.commands for w in self.apply(widgets, p).values()))


def check_report(reg: "Platforms", errors: List[str], widgets: Dict[str, WidgetDef],
                 template_counts: Dict[str, int]) -> Tuple[str, bool]:
    """Human-readable pack report for `tt2 --check-platforms`. ok is False
    when any pack failed to load, conflicts, or has a binding that doesn't
    resolve to a widget/field."""
    lines = []
    hdr = f"{'platform':<22}{'tested':<8}{'widgets':>8}{'templates':>10}  {'counters':<9}paging"
    lines += [hdr, "-" * len(hdr)]
    warns: List[str] = []
    for name in sorted(reg.packs):
        p = reg.packs[name]
        spec = reg.apply(widgets, name, warnings=warns)
        n_w = sum(1 for w in spec.values() if w.command_for(name))
        if p.shell:
            paging = f"(shell prime: {p.shell})"
        elif p.paging is None:
            paging = "(shotgun)"
        elif not p.paging:
            paging = "(none)" + (f"  user+{p.username_suffix[1:]}" if p.username_suffix else "")
        else:
            paging = " ; ".join(p.paging)
        counters = "-" if p.counters is None else p.counters.parser
        lines.append(f"{name:<22}{('yes' if p.tested else 'no'):<8}{n_w:>8}"
                     f"{template_counts.get(name, 0):>10}  {counters:<9}{paging}")
    no_pack = sorted(k for k, v in template_counts.items() if k and v and k not in reg.packs)
    lines.append("")
    lines.append(f"{len(reg.packs)} packs; {sum(1 for p in reg.packs.values() if p.tested)} tested on gear")
    if no_pack:
        lines.append(f"DB platforms with templates but no pack ({len(no_pack)}): {', '.join(no_pack)}")
    problems = [f"load error: {e}" for e in errors] + reg.conflicts + warns
    if problems:
        lines.append("")
        lines += [f"PROBLEM: {m}" for m in problems]
    return "\n".join(lines), not problems


def _warn(sink: Optional[List[str]], msg: str) -> None:
    log.warning("%s", msg)
    if sink is not None:
        sink.append(msg)


def load_platforms(dirs: Iterable[Path]) -> Tuple[Platforms, List[str]]:
    """Every *.yaml under dirs; later dirs replace earlier by `platform:`.
    Bad files are skipped and reported, never fatal."""
    packs: Dict[str, PlatformPack] = {}
    errors: List[str] = []
    for d in dirs:
        if not Path(d).is_dir():
            continue
        loaded = []
        for p in sorted(Path(d).glob("*.yaml")):
            try:
                loaded.append(load_pack(p))
            except PackError as e:
                errors.append(str(e))
                log.warning("%s", e)
        # whole packs first, then merge packs on top -- file names don't decide order
        for pack in [x for x in loaded if not x.merge] + [x for x in loaded if x.merge]:
            base = packs.get(pack.platform)
            packs[pack.platform] = merge_packs(base, pack) if (pack.merge and base) \
                else dataclasses.replace(pack, merge=False)
    return Platforms(packs.values()), errors


_REGISTRY: Optional[Platforms] = None
_REGISTRY_ERRORS: List[str] = []


def registry() -> Platforms:
    """Process-wide packs from the bundled + user dirs, loaded once."""
    global _REGISTRY, _REGISTRY_ERRORS
    if _REGISTRY is None:
        from .paths import platform_dirs
        _REGISTRY, _REGISTRY_ERRORS = load_platforms(platform_dirs())
    return _REGISTRY


def registry_errors() -> List[str]:
    """Pack files that failed to load (and were skipped) in registry()."""
    registry()
    return list(_REGISTRY_ERRORS)


def set_registry(reg: Optional[Platforms]) -> None:
    """Replace (or with None, force a reload of) the process-wide registry."""
    global _REGISTRY, _REGISTRY_ERRORS
    _REGISTRY, _REGISTRY_ERRORS = reg, []
