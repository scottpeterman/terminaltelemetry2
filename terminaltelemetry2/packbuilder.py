"""Pack building without blank forms: everything the Platform Pack editor
suggests comes from what already works.

  * Which template feeds a widget on a new platform: ranked by how much the
    template's command looks like the widget's commands on other platforms,
    and how many of the widget's used fields the template's Values cover via
    the widget's own alias vocabulary (exact) or a close name (fuzzy).
  * Which Value feeds each widget field: exact alias hits first, then fuzzy
    matches; unresolved fields are left for a pick list, never a text box.
  * The binding YAML: `template:` only when auto resolution wouldn't find the
    choice, `fields:` overlays only where the widget's aliases don't already
    pick the chosen Value.
  * Traffic counters: presets for the byte-counter formats vendors copy from
    each other, detected against the DB's own sample output.

Samples: nearly every DB template carries the vendor's sample output
(cli_content), so previews work before touching a device.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from functools import lru_cache
from difflib import SequenceMatcher
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

import yaml

from .parsing.parser import Parser
from .parsing.store import TemplateStore
from .platforms import PlatformPack, parse_pack
from .widgets.pipeline import Pipeline
from .widgets.schema import WidgetDef

# ═══════════════════════════════════════════════════════════════════════════
# Command similarity
# ═══════════════════════════════════════════════════════════════════════════

_VERBS = {"show", "display", "get", "sh", "dis", "run", "cat", "exec", "execute",
          "diagnose", "vtysh", "c", "json", "xml", "more", "no"}
_CANON = {"neighbour": "neighbor", "neighbors": "neighbor", "neighbours": "neighbor",
          "interfaces": "interface", "int": "interface", "intf": "interface",
          "routing": "route", "routes": "route", "peers": "peer", "processes": "process",
          "vlans": "vlan", "addresses": "address", "ipv4": "ip"}


def command_tokens(cmd: str) -> set:
    """Content words of a CLI command: pipes and shell noise dropped, verbs
    removed, plurals and vendor synonyms folded (neighbour/neighbors ->
    neighbor, interfaces -> interface)."""
    cmd = cmd.split("|")[0]
    out = set()
    for w in re.split(r"[^a-z0-9]+", cmd.lower()):
        if len(w) < 2 or w in _VERBS or w.isdigit():
            continue
        w = _CANON.get(w, w)
        if len(w) > 4 and w.endswith("s") and not w.endswith("ss"):
            w = _CANON.get(w[:-1], w[:-1])
        out.add(w)
    return out


# A widget about one of these is only fed by a command that names it.
PROTOCOLS = {"bgp", "ospf", "lldp", "cdp", "isdp", "isis", "arp", "rip", "eigrp", "radius",
             "mpls", "ldp", "vrrp", "hsrp", "stp", "lacp", "vpn", "ipsec"}
# Words that say how much, not what: sharing only these isn't topical.
_GENERIC_WORDS = {"summary", "brief", "detail", "all", "table", "status", "list", "information",
                  "info", "terse", "extensive", "verbose", "print", "neighbor", "ip", "system"}


def topical(candidate_cmd: str, reference_cmds: Iterable[str]) -> bool:
    """Candidate names the widget's protocol (when the widget has one) and
    shares a content word with some reference command."""
    ct = command_tokens(candidate_cmd)
    refs = [command_tokens(r) for r in reference_cmds]
    wanted = set().union(*refs) & PROTOCOLS if refs else set()
    if wanted and not (ct & wanted):
        return False
    return any((ct & r) - _GENERIC_WORDS for r in refs)


def command_similarity(a: str, b: str) -> float:
    """Dice coefficient over command_tokens."""
    ta, tb = command_tokens(a), command_tokens(b)
    if not ta or not tb:
        return 0.0
    return 2 * len(ta & tb) / (len(ta) + len(tb))


# ═══════════════════════════════════════════════════════════════════════════
# Fields
# ═══════════════════════════════════════════════════════════════════════════

_VALUE_RE = re.compile(r"^Value\s+(?:(?:Required|Filldown|Fillup|Key|List)(?:,\S+)?\s+)*(\w+)\s", re.M)


def template_values(content: str) -> List[str]:
    """Value names declared by a TextFSM template, in order."""
    return _VALUE_RE.findall(content or "")


def used_fields(w: WidgetDef) -> List[str]:
    """Widget fields that show up somewhere (key, columns, rate/delta sources,
    stat field, sort), in display order. These are what coverage counts."""
    order: List[str] = []

    def add(f):
        if f and f in w.fields and f not in order:
            order.append(f)
    add(w.key)
    for c in w.view.columns:
        add(c)
    for src in list(w.rates.values()) + list(w.deltas.values()):
        add(src)
    add(w.view.field)
    add(w.view.sort)
    if not order:                       # alert-only widgets (bgp_down): every field counts
        order = list(w.fields)
    return order


@dataclass
class FieldMatch:
    field: str
    value: Optional[str]                # chosen template Value, None = unresolved
    how: str                            # exact | fuzzy | picked | none
    score: float = 0.0


@lru_cache(maxsize=16384)
def _norm(s: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", s.upper())


@lru_cache(maxsize=16384)
def _tok(s: str) -> frozenset:
    return frozenset(t for t in re.split(r"[^A-Z0-9]+", s.upper()) if t)


# Words that qualify a Value without changing what it measures:
# DEVICE_SERIAL_NUMBER is still a serial number; HW_ADDRESS is not an IP address.
_GENERIC = {"DEVICE", "SYSTEM", "GLOBAL", "CURRENT", "OPER", "OPERATIONAL", "TOTAL", "CHASSIS"}


_IN = {"IN", "INPUT", "RX", "INBOUND", "INGRESS", "RECEIVED", "RCVD"}
_OUT = {"OUT", "OUTPUT", "TX", "OUTBOUND", "EGRESS", "SENT", "TRANSMITTED"}


def _direction(tokens: set) -> int:
    for t in tokens:
        if t in _IN or t.startswith("INPUT"):
            return 1
        if t in _OUT or t.startswith("OUTPUT"):
            return -1
    return 0


def _fuzzy(candidates: Sequence[str], value: str) -> float:
    return _fuzzy_cached(tuple(candidates), value)


@lru_cache(maxsize=65536)
def _fuzzy_cached(candidates: Tuple[str, ...], value: str) -> float:
    best = 0.0
    vt = _tok(value)
    for a in candidates:
        if _direction(_tok(a)) * _direction(vt) < 0:  # never map in-rate to OUT_RATE
            continue
        at = _tok(a)
        if vt and at and at < vt and not (vt - at) <= _GENERIC:
            continue                                 # ADDRESS vs HW_ADDRESS: a qualifier that changes meaning
        subset = bool(vt and at and (vt < at or at < vt))
        na, nv = _norm(a), _norm(value)
        if not subset:
            if 2 * min(len(na), len(nv)) < FUZZY_MIN * (len(na) + len(nv)):
                continue                             # lengths alone rule it out
            sm = SequenceMatcher(None, na, nv)
            if sm.quick_ratio() < FUZZY_MIN:
                continue                             # can't reach the threshold; skip the full diff
            r = sm.ratio()
        else:
            r = SequenceMatcher(None, na, nv).ratio()
        if vt and at:
            if vt < at:                              # LINK ~ LINK_STATE
                r = max(r, 0.8)
            elif at < vt:                            # SERIAL_NUMBER ~ DEVICE_SERIAL_NUMBER
                r = max(r, 0.8)
        best = max(best, r)
    return best


FUZZY_MIN = 0.72


def suggest_field_map(w: WidgetDef, values: Sequence[str]) -> Dict[str, FieldMatch]:
    """Every widget field -> best template Value. Exact alias hits win (in the
    widget's alias order, as the pipeline would pick); then fuzzy matches,
    assigned greedily so one Value isn't guessed for two fields."""
    out: Dict[str, FieldMatch] = {}
    vals = list(values)
    for f, aliases in w.fields.items():
        hit = next((a for a in aliases if a in vals), None)
        out[f] = FieldMatch(f, hit, "exact", 1.0) if hit else FieldMatch(f, None, "none")
    taken = {m.value for m in out.values() if m.value}
    pairs = []
    for f, m in out.items():
        if m.value:
            continue
        cands = list(w.fields[f]) + [f]
        for v in vals:
            s = _fuzzy(cands, v)
            if s >= FUZZY_MIN:
                pairs.append((s, f, v))
    for s, f, v in sorted(pairs, key=lambda p: (-p[0], p[1], p[2])):
        if out[f].value is None and v not in taken:
            out[f] = FieldMatch(f, v, "fuzzy", round(s, 2))
            taken.add(v)
    return out


def coverage(w: WidgetDef, mapping: Dict[str, FieldMatch]) -> float:
    used = used_fields(w)
    if not used:
        return 0.0
    return sum(1.0 if mapping[f].how in ("exact", "picked") else 0.6 if mapping[f].how == "fuzzy"
               else 0.0 for f in used) / len(used)


# ═══════════════════════════════════════════════════════════════════════════
# Template candidates
# ═══════════════════════════════════════════════════════════════════════════

@dataclass
class Candidate:
    template: str
    command: str
    score: float
    cmd_sim: float
    cover: float
    mapping: Dict[str, FieldMatch] = field(default_factory=dict)
    on_topic: bool = True

    @property
    def confident(self) -> bool:
        """Good enough to pre-select: on topic, most used fields resolve, and
        the command resembles the widget's elsewhere (or nearly all fields do)."""
        return self.on_topic and ((self.cover >= 0.5 and self.cmd_sim >= 0.3)
                                  or (self.cover >= 0.8 and self.cmd_sim >= 0.2))

    @property
    def reason(self) -> str:
        return f"command {self.cmd_sim:.0%} · fields {self.cover:.0%}"


@dataclass(frozen=True)
class CatalogEntry:
    template: str
    command: str
    values: Tuple[str, ...]


def platform_catalog(store: TemplateStore, platform: str) -> List[CatalogEntry]:
    """Every enabled template for a platform with its Value names -- read once,
    reused across widgets (the ranking is otherwise one DB read per pair)."""
    return [CatalogEntry(i.name, i.command, tuple(template_values(store.content(i.name) or "")))
            for i in store.list(platform=platform, enabled=True)]


def suggest_templates(store: TemplateStore, platform: str, w: WidgetDef,
                      limit: int = 8, catalog: Optional[List[CatalogEntry]] = None
                      ) -> List[Candidate]:
    """Enabled templates for `platform`, ranked for widget `w`. Pass a
    platform_catalog to rank many widgets without re-reading the DB."""
    # (command, weight): commands bound to python parsers are weaker evidence
    # of what a TextFSM template's command looks like
    ref = [(c, 0.5 if w.template_for(p).startswith("py:") else 1.0)
           for p, c in w.commands.items() if p != platform]
    out: List[Candidate] = []
    for info in (catalog if catalog is not None else platform_catalog(store, platform)):
        mapping = suggest_field_map(w, info.values)
        cov = coverage(w, mapping)
        sims = [(command_similarity(info.command, r), wt) for r, wt in ref] or [(0.0, 1.0)]
        sim = max(x for x, _ in sims)
        mean = sum(x * wt for x, wt in sims) / sum(wt for _, wt in sims)
        if sim == 0 and cov < 0.3:
            continue
        # best single-platform match, nudged by agreement across all platforms
        # (EOS: 'show ip bgp summary' beats 'show bgp summary' because 3 of 4 say 'ip')
        on_topic = topical(info.command, [r for r, _ in ref])
        score = 0.40 * sim + 0.05 * mean + 0.55 * cov - (0 if on_topic else 0.15)
        out.append(Candidate(info.template, info.command, round(score, 3),
                             round(sim, 2), round(cov, 2), mapping, on_topic))
    out.sort(key=lambda c: (-c.score, c.template))
    return out[:limit]


# ═══════════════════════════════════════════════════════════════════════════
# Bindings
# ═══════════════════════════════════════════════════════════════════════════

def make_binding(w: WidgetDef, platform: str, command: str, template: str,
                 values: Sequence[str], chosen: Dict[str, Optional[str]]) -> dict:
    """The pack binding for a widget: command always; template only if auto
    resolution (exact <platform>_<command> name) wouldn't pick it; a fields
    overlay only where the widget's own alias order wouldn't pick the chosen
    Value out of this template."""
    b: dict = {"command": command}
    if template and template != Parser.exact_template(platform, command):
        b["template"] = template
    overlay = {}
    for f, v in chosen.items():
        if not v or f not in w.fields:
            continue
        auto = next((a for a in w.fields[f] if a in values), None)
        if auto != v:
            overlay[f] = [v]
    if overlay:
        b["fields"] = overlay
    return b


@dataclass
class Preview:
    rows: List[dict]
    records: int
    template: Optional[str]
    error: Optional[str] = None


def preview(parser: Parser, spec: WidgetDef, platform: str, command: str,
            template: Optional[str], text: str) -> Preview:
    """Parse `text` as the poll would and run it through the widget's pipeline."""
    if not text.strip():
        return Preview([], 0, None, "no sample output")
    parsed = parser.parse(platform, command, text, template or "auto")
    if parsed.error:
        return Preview([], 0, parsed.template, parsed.error)
    try:
        rows = Pipeline(spec).apply(parsed.records, 0.0)
    except Exception as e:                          # a compute/alert on a missing field
        return Preview([], len(parsed.records), parsed.template, f"pipeline: {e}")
    return Preview(rows, len(parsed.records), parsed.template)


def sample_for(store: TemplateStore, template: str, platform: str, command: str) -> str:
    """Best offline sample: the template's own (ntc ships one), else the newest
    stored capture for the platform + command."""
    rec = store.get(template) if template else None
    if rec and rec.sample:
        return rec.sample
    s = store.samples(platform, command)
    return s[0].output if s else ""


# ═══════════════════════════════════════════════════════════════════════════
# Counters
# ═══════════════════════════════════════════════════════════════════════════

@dataclass(frozen=True)
class CounterPreset:
    name: str
    rx: str
    tx: str


# Byte-counter formats most vendors copy. Each regex: one capture group.
COUNTER_PRESETS: Tuple[CounterPreset, ...] = (
    CounterPreset("Cisco style: 'N packets input, N bytes'",
                  r"^\s*\d+ packets input, (\d+) bytes", r"^\s*\d+ packets output, (\d+) bytes"),
    CounterPreset("Comware / VRP: 'Input (total): N packets, N bytes'",
                  r"^\s*Input(?: \(total\))?:\s+\d+ packets, (\d+) bytes",
                  r"^\s*Output(?: \(total\))?:\s+\d+ packets, (\d+) bytes"),
    CounterPreset("Junos style: 'Input bytes : N'",
                  r"^\s*Input bytes\s*:\s*(\d+)", r"^\s*Output bytes\s*:\s*(\d+)"),
    CounterPreset("'N bytes input' / 'N bytes output'",
                  r"(\d+) bytes input", r"(\d+) bytes output"),
    CounterPreset("'RX bytes: N' / 'TX bytes: N'",
                  r"RX bytes[:\s]+(\d+)", r"TX bytes[:\s]+(\d+)"),
    CounterPreset("'InOctets N' / 'OutOctets N'",
                  r"In ?Octets\s*[:=]?\s*(\d+)", r"Out ?Octets\s*[:=]?\s*(\d+)"),
)


def try_preset(p: CounterPreset, text: str) -> Optional[Tuple[int, int]]:
    mi = re.search(p.rx, text, re.M | re.I)
    mo = re.search(p.tx, text, re.M | re.I)
    return (int(mi.group(1)), int(mo.group(1))) if mi and mo else None


def interface_commands(store: TemplateStore, platform: str) -> List[Tuple[str, str]]:
    """(template, command) for this platform's per-interface detail commands,
    best first -- where byte counters live."""
    out = []
    for info in store.list(platform=platform, enabled=True):
        t = command_tokens(info.command)
        if "interface" not in t:
            continue
        noise = t & {"brief", "status", "terse", "description", "summary", "ip", "counters",
                     "bound", "switchport", "trunk", "transceiver", "capabilities"}
        out.append((len(noise), len(t), info.name, info.command))
    out.sort()
    return [(n, c) for _, _, n, c in out]


def detect_counters(store: TemplateStore, platform: str
                    ) -> List[Tuple[str, str, CounterPreset, Tuple[int, int]]]:
    """(counter command, template, preset, parsed values) that work on the
    DB's own sample output for this platform. The command gets ' {intf}'."""
    hits = []
    for tname, cmd in interface_commands(store, platform):
        text = sample_for(store, tname, platform, cmd)
        for p in COUNTER_PRESETS:
            v = try_preset(p, text)
            if v:
                hits.append((f"{cmd} {{intf}}", tname, p, v))
    return hits


# ═══════════════════════════════════════════════════════════════════════════
# Draft
# ═══════════════════════════════════════════════════════════════════════════

@dataclass
class PackDraft:
    """An editable pack. `bindings[w] is None` = widget off on this platform."""
    platform: str
    title: str = ""
    aliases: List[str] = field(default_factory=list)
    vendors: List[str] = field(default_factory=list)
    models: List[str] = field(default_factory=list)
    tested: bool = False
    paging: Optional[List[str]] = None
    enable: Optional[str] = None
    username_suffix: Optional[str] = None
    shell: Optional[str] = None
    layout: Optional[str] = None
    counters: Optional[dict] = None
    read_timeout: Optional[float] = None
    bindings: Dict[str, Optional[dict]] = field(default_factory=dict)

    @classmethod
    def from_pack(cls, p: PlatformPack) -> "PackDraft":
        counters = None
        if p.counters is not None:
            counters = {"command": p.counters.command, "parser": p.counters.parser}
            if p.counters.parser == "regex":
                counters.update(rx=p.counters.rx.pattern, tx=p.counters.tx.pattern)
        bindings: Dict[str, Optional[dict]] = {}
        for w, b in p.bindings.items():
            if b.removed:
                bindings[w] = None
                continue
            d = {}
            for k in ("command", "template", "requires"):
                if getattr(b, k):
                    d[k] = getattr(b, k)
            if b.fields:
                d["fields"] = {k: list(v) for k, v in b.fields.items()}
            if b.sudo:
                d["sudo"] = True
            bindings[w] = d
        return cls(p.platform, p.title if p.title != p.platform else "", list(p.aliases),
                   list(p.vendors), [m.pattern for m in p.models], p.tested,
                   None if p.paging is None else list(p.paging), p.enable,
                   p.username_suffix, p.shell, p.layout, counters,
                   read_timeout=p.read_timeout, bindings=bindings)

    def to_data(self) -> dict:
        d: dict = {"platform": self.platform}
        if self.title:
            d["title"] = self.title
        if self.aliases:
            d["aliases"] = list(self.aliases)
        match = {}
        if self.vendors:
            match["vendor"] = list(self.vendors)
        if self.models:
            match["model"] = list(self.models)
        if match:
            d["match"] = match
        d["tested"] = bool(self.tested)
        sess = {}
        if self.paging is not None:
            sess["paging"] = self.paging[0] if len(self.paging) == 1 else list(self.paging)
        for k in ("enable", "username_suffix", "shell", "read_timeout"):
            if getattr(self, k):
                v = getattr(self, k)
                sess[k] = int(v) if isinstance(v, float) and v.is_integer() else v
        if sess:
            d["session"] = sess
        if self.layout:
            d["layout"] = self.layout
        if self.counters:
            d["counters"] = dict(self.counters)
        d["bindings"] = {k: (None if v is None else dict(v)) for k, v in sorted(self.bindings.items())}
        return d

    def validate(self) -> PlatformPack:
        """parse_pack on the draft; raises PackError with the problem."""
        return parse_pack(self.to_data(), f"<draft {self.platform}>")

    def to_yaml(self) -> str:
        head = (f"# {self.title or self.platform} -- platform pack written by the Platform Pack editor.\n"
                "# Format: terminaltelemetry2/platforms.py. Validate: tt2 --check-platforms\n")
        return head + yaml.safe_dump(self.to_data(), sort_keys=False, default_flow_style=False,
                                     width=1000, allow_unicode=True)
