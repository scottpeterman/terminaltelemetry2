"""Widget design from a template and a real sample -- no blank forms.

Start: a platform, a TextFSM template (its Value names) and sample output.
Everything else is suggested from what the bundled widgets already do:

  * field names    -- the alias vocabulary: a Value the bundled widgets
                      already map (INTERFACE -> intf, BGP_NEIGH -> peer) gets
                      that field name *and* that field's sibling aliases, so
                      the Platform Pack editor can bind other vendors exactly
  * field kinds    -- from the sample values and Value names: state, pct,
                      counter, time, id, number, text
  * key            -- the first id-like field that is unique and non-empty
  * view           -- one record: kv; many: table
  * patterns       -- the alert/cell/rate idioms the bundled widgets use,
                      offered per field kind with values taken from the sample
                      (healthy states are the ones the sample shows)
"""
from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

import yaml

from .packbuilder import command_tokens, template_values
from .parsing.parser import Parser
from .widgets.pipeline import Pipeline
from .widgets.rules import to_number
from .widgets.schema import MIN_INTERVAL, WidgetDef, parse_widget

# ═══════════════════════════════════════════════════════════════════════════
# Vocabulary from the bundled widgets
# ═══════════════════════════════════════════════════════════════════════════


class Vocabulary:
    """Value -> field names the bundled widgets use for it, and field name ->
    every alias any widget lists for it."""

    def __init__(self, widgets: Dict[str, WidgetDef]):
        self.names: Dict[str, Counter] = {}
        self.aliases: Dict[str, List[str]] = {}
        for w in widgets.values():
            for f, als in w.fields.items():
                for a in als:
                    self.names.setdefault(a, Counter())[f] += 1
                bucket = self.aliases.setdefault(f, [])
                for a in als:
                    if a not in bucket:
                        bucket.append(a)

    def field_name(self, value: str) -> str:
        c = self.names.get(value)
        if c:
            top = max(c.values())
            tied = sorted(n for n, k in c.items() if k == top)
            own = snake(value)                   # UPTIME: 'uptime' (version) over 'timer' (ospf)
            return own if own in tied else tied[0]
        return snake(value)

    def aliases_for(self, name: str, value: str, exclude: Sequence[str] = ()) -> List[str]:
        """The Value first, then the sibling aliases other widgets list for the
        same field name -- only when this Value is one of them (same meaning),
        and never another Value of this template (that is its own field)."""
        siblings = self.aliases.get(name, [])
        if value in siblings:
            return [value] + [a for a in siblings if a != value and a not in exclude]
        return [value]


def snake(value: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "_", value.lower()).strip("_")
    return s if s and not s[0].isdigit() else f"f_{s}"


# ═══════════════════════════════════════════════════════════════════════════
# Kinds
# ═══════════════════════════════════════════════════════════════════════════

KINDS = ("id", "state", "pct", "counter", "number", "time", "text")

_RX_PCT = re.compile(r"PCT|PERCENT|UTIL|USAGE|LOAD", re.I)
_RX_COUNTER = re.compile(r"PACKETS|PKTS|BYTES|OCTETS|ERRORS|ERR$|DROPS|DISCARDS|CRC|COUNT|"
                         r"RESETS|FLAPS|CHANGES|COLLISIONS|RUNTS|GIANTS|OVERRUN|UNDERRUN", re.I)
_RX_STATE = re.compile(r"STATE|STATUS|LINK|OPER|ADMIN|PROTOCOL|HEALTH|CONDITION", re.I)
_RX_TIME = re.compile(r"UPTIME|UP_TIME|TIME|AGE|SINCE|LAST|DURATION|UP_DOWN|EXPIRE", re.I)
_RX_ID = re.compile(r"INTERFACE|^PORT|NAME|NEIGH|PEER|^ID$|_ID$|ADDRESS|MAC|VLAN|^PID$|MOUNT|"
                    r"UNIT|PREFIX|NETWORK|SERIAL|HOST|CHASSIS", re.I)


def _nonempty(records: Sequence[dict], value: str) -> List[str]:
    out = []
    for r in records:
        v = r.get(value)
        if isinstance(v, list):
            v = ",".join(str(x) for x in v)
        if v not in (None, ""):
            out.append(str(v))
    return out


def _numeric(vals: Sequence[str]) -> bool:
    return bool(vals) and all(to_number(v.replace(",", "").rstrip("%")) is not None for v in vals)


def infer_kind(value: str, records: Sequence[dict]) -> str:
    vals = _nonempty(records, value)
    num = _numeric(vals)
    if num and (_RX_PCT.search(value) or any(v.endswith("%") for v in vals)):
        return "pct"
    if num and _RX_COUNTER.search(value):
        return "counter"
    if _RX_STATE.search(value) and not num:
        return "state"
    if _RX_TIME.search(value):
        return "time"
    if _RX_ID.search(value):
        return "id"
    if num:
        return "number"
    return "text"


# ═══════════════════════════════════════════════════════════════════════════
# Patterns -- the idioms the bundled widgets use
# ═══════════════════════════════════════════════════════════════════════════

# Deliberately not here: "active" (BGP Active = retrying, i.e. down) and
# "idle" -- healthy in some CLIs, a failure in others. Add them by hand.
HEALTHY_WORDS = ("up", "established", "estab", "full", "connected", "ok", "running",
                 "enabled", "online", "present", "normal", "forwarding", "master", "primary")


def healthy_regex(samples: Sequence[str]) -> str:
    """A 'this is fine' regex from what the sample shows: the healthy words
    that appear, else numeric (BGP prefixes-received), else the majority value."""
    words = sorted({w for s in samples for w in HEALTHY_WORDS
                    if re.match(rf"(?i){w}(\b|/)", s.strip())})
    numeric = any(to_number(s) is not None for s in samples)   # BGP: prefixes received = up
    if words:
        return "(?i)^(" + "|".join(words + ([r"\d"] if numeric else [])) + ")"
    if samples and sum(to_number(s) is not None for s in samples) > len(samples) / 2:
        return r"^\d"
    if samples:
        top = Counter(s.strip() for s in samples).most_common(1)[0][0]
        return "^" + re.escape(top) + "$"
    return "(?i)^up"


@dataclass(frozen=True)
class Pattern:
    id: str
    label: str                   # "{f}" is the field name
    kinds: Tuple[str, ...]
    param: str = ""              # what the parameter means, "" = none
    needs_key: bool = False


PATTERNS: Tuple[Pattern, ...] = (
    Pattern("alert_unhealthy", "Alert when {f} isn't healthy", ("state",), "healthy regex"),
    Pattern("mute_down", "Mute rows where {f} is down/disabled", ("state",), "down regex"),
    Pattern("alert_match", "Alert when {f} matches", ("state", "text"), "regex"),
    Pattern("status_cell", "Colour {f} (healthy green, else red)", ("state",), "healthy regex"),
    Pattern("alert_above", "Warn/alert when {f} is above", ("pct", "number", "counter"),
            "warn,alert"),
    Pattern("bar", "Bar for {f} with warn/alert thresholds", ("pct",), "warn,alert"),
    Pattern("alert_nonzero", "Alert when {f} > 0", ("counter", "number")),
    Pattern("rate", "Per-second rate of {f}", ("counter",), needs_key=True),
    Pattern("delta", "Changes in {f} since last poll", ("counter",), needs_key=True),
    Pattern("spark", "Sparkline of {f}", ("pct", "number", "counter"), needs_key=True),
    Pattern("drop_empty", "Drop rows with empty {f}", KINDS),
)
PATTERN_BY_ID = {p.id: p for p in PATTERNS}
DOWN_DEFAULT = "(?i)^(down|disabled|notconnect|inactive|admin)"


def default_param(p: Pattern, kind: str, samples: Sequence[str]) -> str:
    if p.id in ("alert_unhealthy", "status_cell"):
        return healthy_regex(samples)
    if p.id == "mute_down":
        return DOWN_DEFAULT
    if p.id == "alert_match":
        return "(?i)err"
    if p.id in ("alert_above", "bar"):
        return "70,90" if kind == "pct" else ""
    return ""


# ═══════════════════════════════════════════════════════════════════════════
# Spec
# ═══════════════════════════════════════════════════════════════════════════

@dataclass
class FieldSpec:
    value: str                   # TextFSM Value
    name: str                    # widget field name
    kind: str
    aliases: List[str]
    include: bool = True
    label: str = ""


@dataclass
class PatternUse:
    pattern: str
    field: str
    param: str = ""


@dataclass
class WidgetSpec:
    name: str
    title: str
    platform: str
    command: str
    template: str = ""           # "" = resolve by command
    interval: float = 30.0
    fields: List[FieldSpec] = field(default_factory=list)
    key: Optional[str] = None
    unique: bool = False
    view: str = "table"          # table | kv | stat
    columns: List[str] = field(default_factory=list)
    sort: Optional[str] = None
    sort_desc: bool = False
    limit: Optional[int] = None
    patterns: List[PatternUse] = field(default_factory=list)
    stat_aggregate: str = "count"
    stat_field: Optional[str] = None
    stat_where: Optional[PatternUse] = None     # alert_unhealthy / alert_match on a field
    stat_alert_above: Optional[float] = None
    monitor: Optional[str] = None

    # -- names ---------------------------------------------------------------------

    def included(self) -> List[FieldSpec]:
        return [f for f in self.fields if f.include]

    def derived(self) -> Dict[str, Tuple[str, str]]:
        """name -> (block, source) for rate/delta patterns."""
        out = {}
        for u in self.patterns:
            if u.pattern == "rate":
                out[f"{u.field}_ps"] = ("rates", u.field)
            elif u.pattern == "delta":
                out[f"{u.field}_chg"] = ("deltas", u.field)
        return out

    def known(self) -> List[str]:
        return [f.name for f in self.included()] + list(self.derived())

    # -- YAML -------------------------------------------------------------------------

    def to_data(self) -> dict:
        inc = self.included()
        names = {f.name for f in inc}
        d: dict = {"widget": self.name, "title": self.title or self.name,
                   "interval": int(self.interval) if float(self.interval).is_integer() else self.interval,
                   "commands": {self.platform: self.command}}
        if self.template and self.template != Parser.exact_template(self.platform, self.command):
            d["templates"] = {self.platform: self.template}
        if self.key and self.key in names:
            d["key"] = self.key
            if self.unique:
                d["unique"] = True
        d["fields"] = {f.name: list(f.aliases) for f in inc}
        rates, deltas = {}, {}
        for dn, (block, src) in self.derived().items():
            if src in names:
                (rates if block == "rates" else deltas)[dn] = src
        if rates:
            d["rates"] = rates
        if deltas:
            d["deltas"] = deltas
        drop = [{"field": u.field, "op": "empty"} for u in self.patterns
                if u.pattern == "drop_empty" and u.field in names]
        if drop:
            d["drop"] = drop

        known = [f.name for f in inc] + list(rates) + list(deltas)
        view: dict = {"type": self.view}
        labels = {f.name: f.label for f in inc if f.label}
        if labels:
            view["labels"] = labels
        alerts = []
        cells = {}
        for u in self.patterns:
            if u.field not in known:
                continue
            p = u.param.strip()
            if u.pattern == "alert_unhealthy" and p:
                alerts.append({"field": u.field, "op": "not_match", "value": p})
            elif u.pattern == "mute_down" and p:
                alerts.append({"field": u.field, "op": "match", "value": p, "style": "muted"})
            elif u.pattern == "alert_match" and p:
                alerts.append({"field": u.field, "op": "match", "value": p})
            elif u.pattern == "alert_nonzero":
                alerts.append({"field": u.field, "op": "gt", "value": 0})
            elif u.pattern == "alert_above":
                warn, alert = _two_numbers(p)
                if warn is not None:
                    alerts.append({"field": u.field, "op": "gt", "value": warn, "style": "warn"})
                if alert is not None:
                    alerts.append({"field": u.field, "op": "gt", "value": alert})
            elif u.pattern == "bar" and self.view == "table":
                warn, alert = _two_numbers(p)
                th = ([{"at": warn, "style": "warn"}] if warn is not None else []) + \
                     ([{"at": alert, "style": "alert"}] if alert is not None else [])
                cells[u.field] = {"type": "bar", "min": 0, "max": 100, "suffix": "%",
                                  **({"thresholds": th} if th else {})}
            elif u.pattern == "status_cell" and self.view == "table" and p:
                cells[u.field] = {"type": "status", "rules": [
                    {"field": u.field, "op": "match", "value": p, "style": "ok"},
                    {"field": u.field, "op": "not_match", "value": p, "style": "alert"}]}
            elif u.pattern == "spark" and self.view == "table":
                cells[u.field] = {"type": "spark", "history": 30}
        # Mute/alert order: the bundled widgets list alerts before muted styles.
        alerts.sort(key=lambda a: a.get("style") == "muted")

        if self.view == "table":
            cols = [c for c in (self.columns or known) if c in known]
            view["columns"] = cols
            cells = {k: v for k, v in cells.items() if k in cols}
            if self.sort in known:
                view["sort"] = self.sort
                if self.sort_desc:
                    view["sort_desc"] = True
                if self.limit:
                    view["limit"] = int(self.limit)
            if cells:
                view["cells"] = cells
        elif self.view == "kv":
            view["show"] = [c for c in (self.columns or known) if c in known]
        else:
            view["aggregate"] = self.stat_aggregate
            if self.stat_aggregate != "count" and self.stat_field in known:
                view["field"] = self.stat_field
            w = self.stat_where
            if w and w.field in known and w.param.strip():
                op = "not_match" if w.pattern == "alert_unhealthy" else "match"
                view["where"] = {"field": w.field, "op": op, "value": w.param.strip()}
            view["label"] = self.title or self.name
            if self.stat_alert_above is not None:
                view["alert_above"] = self.stat_alert_above
        if alerts and self.view != "stat":
            view["alerts"] = alerts
        d["view"] = view
        if self.monitor and self.view == "table" and self.monitor in view.get("columns", []):
            d["monitor"] = self.monitor
        return d

    def validate(self) -> WidgetDef:
        return parse_widget(self.to_data(), f"<design {self.name}>")

    def to_yaml(self) -> str:
        head = (f"# {self.title or self.name} -- written by the Widget Designer from "
                f"{self.template or 'auto'} on {self.platform}.\n"
                "# Bind other platforms in the Platform Pack editor (tt2 --packs).\n")
        return head + yaml.safe_dump(self.to_data(), sort_keys=False, default_flow_style=None,
                                     width=100, allow_unicode=True)


def _two_numbers(p: str) -> Tuple[Optional[float], Optional[float]]:
    parts = [x.strip() for x in p.split(",")]
    nums = [to_number(x) if x else None for x in parts] + [None, None]
    return nums[0], nums[1]


# ═══════════════════════════════════════════════════════════════════════════
# Suggest a spec from a template + sample
# ═══════════════════════════════════════════════════════════════════════════

def slug_for(command: str) -> str:
    toks = [t for t in re.split(r"[^a-z0-9]+", command.split("|")[0].lower())
            if t and t not in {"show", "display", "get", "sh", "dis", "run", "exec"}]
    return "_".join(toks)[:40] or "new_widget"


_ACRONYMS = {"ip", "bgp", "ospf", "lldp", "cdp", "vlan", "mac", "arp", "cpu", "vrf", "ipv6",
             "mpls", "ldp", "isis", "stp", "lacp", "vpn", "nat", "poe", "sfp", "dhcp", "ntp",
             "snmp", "aaa", "acl", "qos", "vrrp", "hsrp", "evpn", "vxlan", "nve", "rib", "fib"}


def title_for(command: str) -> str:
    return " ".join(w.upper() if w in _ACRONYMS else w.capitalize()
                    for w in slug_for(command).split("_")) or "New Widget"


_COL_ORDER = {"state": 1, "id": 2, "time": 3, "pct": 4, "counter": 5, "number": 6, "text": 7}


def default_columns(fields: Sequence[FieldSpec], key: Optional[str], records: Sequence[dict],
                    limit: int = 8) -> List[str]:
    """Key first, then ids, states, times, numbers, text -- skipping fields
    whose value never varies across rows (header Filldowns like router-id)."""
    def constant(f):
        vals = [str(r.get(f.value, "")) for r in records]
        return len(records) > 1 and len(set(vals)) == 1
    cand = [f for f in fields if f.include and f.name != key and not constant(f)]
    cand.sort(key=lambda f: _COL_ORDER.get(f.kind, 9))      # stable: template order within a kind
    return ([key] if key else []) + [f.name for f in cand][: limit - (1 if key else 0)]


def suggest_spec(vocab: Vocabulary, platform: str, command: str, template: str,
                 template_content: str, records: Sequence[dict],
                 taken_names: Sequence[str] = ()) -> WidgetSpec:
    values = template_values(template_content)
    fields: List[FieldSpec] = []
    used = set()
    for v in values:
        name = vocab.field_name(v)
        if name in used:                       # vocabulary name taken by an earlier Value
            name = snake(v)
        base, i = name, 2
        while name in used:
            name = f"{base}_{i}"
            i += 1
        used.add(name)
        others = [x for x in values if x != v]
        fields.append(FieldSpec(v, name, infer_kind(v, records),
                                vocab.aliases_for(name, v, exclude=others),
                                include=bool(_nonempty(records, v)) or not records))
    view = "kv" if len(records) == 1 else "table"
    key = None
    if len(records) > 1:
        for f in fields:
            vals = _nonempty(records, f.value)
            if f.include and f.kind == "id" and len(vals) == len(records) and len(set(vals)) == len(vals):
                key = f.name
                break
    patterns: List[PatternUse] = []
    for f in fields:
        if not f.include:
            continue
        samples = _nonempty(records, f.value)
        if f.kind == "state":
            patterns.append(PatternUse("alert_unhealthy", f.name,
                                       default_param(PATTERN_BY_ID["alert_unhealthy"], f.kind, samples)))
        elif f.kind == "pct":
            patterns.append(PatternUse("bar" if view == "table" else "alert_above", f.name, "70,90"))
        elif f.kind == "counter" and re.search(r"ERR|DROP|CRC|DISCARD", f.value, re.I):
            patterns.append(PatternUse("alert_nonzero", f.name))
    cols = default_columns(fields, key, records)
    name = slug_for(command)
    base, i = name, 2
    while name in taken_names:
        name = f"{base}_{i}"
        i += 1
    # right-click Monitor needs a displayed column holding an interface name
    monitor = next((f.name for f in fields if f.include and f.name in cols
                    and re.fullmatch(r"(LOCAL_)?(INTERFACE|PORT|INTF|IFNAME)(_NAME)?", f.value, re.I)),
                   None)
    return WidgetSpec(name=name, title=title_for(command), platform=platform, command=command,
                      template=template, fields=fields, key=key, view=view, columns=cols,
                      sort=key, patterns=patterns,
                      monitor=monitor if view == "table" else None)


def applicable(spec: WidgetSpec, f: FieldSpec) -> List[Pattern]:
    return [p for p in PATTERNS if f.kind in p.kinds and (spec.key or not p.needs_key)
            and not (p.id in ("bar", "status_cell", "spark") and spec.view != "table")]


# ═══════════════════════════════════════════════════════════════════════════
# Preview, save, layout
# ═══════════════════════════════════════════════════════════════════════════

@dataclass
class DesignPreview:
    widget: Optional[WidgetDef]
    rows: List[dict]
    records: int
    error: Optional[str] = None


def preview_spec(parser: Parser, spec: WidgetSpec, text: str) -> DesignPreview:
    try:
        w = spec.validate()
    except Exception as e:
        return DesignPreview(None, [], 0, str(e))
    if not text.strip():
        return DesignPreview(w, [], 0, "no sample output")
    parsed = parser.parse(spec.platform, spec.command, text, spec.template or "auto")
    if parsed.error:
        return DesignPreview(w, [], 0, parsed.error)
    pipe = Pipeline(w)
    try:
        rows = pipe.apply(parsed.records, 0.0)
        if w.rates or w.deltas:                 # a second identical poll: rates/deltas read 0, not blank
            rows = pipe.apply(parsed.records, max(w.interval, MIN_INTERVAL))
    except Exception as e:
        return DesignPreview(w, [], len(parsed.records), f"pipeline: {e}")
    return DesignPreview(w, rows, len(parsed.records))


def save_widget(spec: WidgetSpec, user_dir: Path) -> Path:
    spec.validate()
    user_dir.mkdir(parents=True, exist_ok=True)
    path = user_dir / f"{spec.name}.yaml"
    path.write_text(spec.to_yaml(), encoding="utf-8")
    return path


def add_to_layout(source: Path, widget: str, user_dir: Path, span: int = 1) -> Path:
    """Copy a layout into the user layouts dir (same layout name, so it
    replaces the bundled one) with `widget` appended as a new row. If the
    widget is already placed, the layout is copied unchanged."""
    data = yaml.safe_load(Path(source).read_text(encoding="utf-8")) or {}
    rows = data.setdefault("rows", [])
    placed = any((c if isinstance(c, str) else c.get("name")) == widget for r in rows for c in r)
    if not placed:
        rows.append([widget if span == 1 else {"name": widget, "span": span}])
    user_dir.mkdir(parents=True, exist_ok=True)
    out = user_dir / Path(source).name
    out.write_text(f"# {data.get('layout')} layout -- copied by the Widget Designer, "
                   f"with {widget} added.\n"
                   + yaml.safe_dump(data, sort_keys=False, default_flow_style=None, width=100),
                   encoding="utf-8")
    return out
