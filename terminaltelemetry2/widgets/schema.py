"""
Widget definition language.

    widget: bgp_peers
    title: BGP Peers
    interval: 30
    commands:                   # platform -> CLI command
      arista_eos: show ip bgp summary
    templates:                  # optional platform -> DB template name ("auto" default),
      arista_eos: auto          #   or py:<name> for a python parser (parsing/pyparsers.py)
    requires:                   # optional platform -> shell test run once per session;
      linux: command -v vtysh   #   false -> widget shows "not on this host", never polled
    key: peer                   # row identity; required when rates are used
    fields:                     # widget field -> TextFSM field aliases, first non-empty wins
      peer: [BGP_NEIGH, BGP_NEIGHBOR, PEER_IP]
    rates:                      # derived per-second rate: name -> source widget field
      in_pps: in_pkts
    deltas:                     # derived change since last poll: name -> source widget field
      flaps: link_changes
    drop:                       # rows matching any rule are discarded before the view
      - {field: peer, op: match, value: "^-+$"}
    view:
      type: table               # table | stat | kv
      columns: [peer, asn]
      labels: {asn: AS}
      sort: peer
      sort_desc: false
      limit: 15                 # optional: top-N after sorting (needs sort)
      alerts:                   # first matching rule styles the row
        - {field: state, op: not_numeric}
        - when: [{field: a, op: gt, value: 0}, {field: b, op: eq, value: x}]
          style: warn
    # stat view:
    #   aggregate: count | sum | min | max
    #   field: <field>          (sum/min/max)
    #   where: <condition or when-list>
    #   label: Peers down
    #   alert_above: 0
    # kv view (first record as label/value pairs; empty values hidden):
    #   show: [model, version, serial]
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

import yaml

from .compute import Expr
from .rules import NO_VALUE_OPS, OPS, STYLES, Condition, Rule

from ..parsing.pyparsers import PREFIX as PY_PREFIX, PY_PARSERS, is_py

MAX_COMMAND_LEN = 1000

log = logging.getLogger(__name__)

VIEW_TYPES = {"table", "stat", "kv"}
AGGREGATES = {"count", "sum", "min", "max"}
CELL_TYPES = {"text", "bar", "status", "spark"}
MIN_INTERVAL = 5.0


class WidgetError(ValueError):
    pass


@dataclass
class CellSpec:
    """Per-column sub-renderer. Closed set; the user picks a kind and binds it,
    never draws. Fields are shared across kinds (following ViewSpec's own style);
    only the ones a kind uses are read.
        bar    -> min, max, suffix, thresholds [(at, style), ...] value>=at
        status -> rules [(Rule, label), ...]  first match wins; no match = plain
        spark  -> history (samples), optional min/max (else autoscale)
    """
    kind: str
    min: Optional[float] = None
    max: Optional[float] = None
    suffix: str = ""
    thresholds: List[Tuple[float, str]] = field(default_factory=list)
    rules: List[Tuple[Rule, str]] = field(default_factory=list)
    history: int = 0


@dataclass
class ViewSpec:
    type: str
    columns: List[str] = field(default_factory=list)
    labels: Dict[str, str] = field(default_factory=dict)
    sort: Optional[str] = None
    sort_desc: bool = False
    limit: Optional[int] = None
    alerts: List[Rule] = field(default_factory=list)
    cells: Dict[str, CellSpec] = field(default_factory=dict)
    history_fields: Dict[str, int] = field(default_factory=dict)
    aggregate: str = "count"
    field: Optional[str] = None
    where: Optional[Rule] = None
    label: str = ""
    alert_above: Optional[float] = None

    def label_for(self, name: str) -> str:
        return self.labels.get(name, name)


@dataclass
class WidgetDef:
    name: str
    title: str
    interval: float
    commands: Dict[str, str]
    templates: Dict[str, str]
    key: Optional[str]
    fields: Dict[str, List[str]]
    rates: Dict[str, str]
    view: ViewSpec
    deltas: Dict[str, str] = field(default_factory=dict)
    computes: Dict[str, Expr] = field(default_factory=dict)
    drop: List[Rule] = field(default_factory=list)
    source: str = "<memory>"
    monitor: Optional[str] = None      # table column holding an interface name -> right-click Monitor
    unique: bool = False               # collapse rows sharing a key (first wins)
    requires: Dict[str, str] = field(default_factory=dict)   # platform -> shell test; gates polling

    def command_for(self, platform: str) -> Optional[str]:
        return self.commands.get(platform)

    def requires_for(self, platform: str) -> Optional[str]:
        return self.requires.get(platform)

    def template_for(self, platform: str) -> str:
        return self.templates.get(platform, "auto")


def _err(src: str, msg: str) -> WidgetError:
    return WidgetError(f"{src}: {msg}")


def _condition(d: Any, known: set, src: str, where: str) -> Condition:
    if not isinstance(d, dict):
        raise _err(src, f"{where}: condition must be a mapping")
    f, op = d.get("field"), d.get("op")
    if f not in known:
        raise _err(src, f"{where}: unknown field {f!r}")
    if op not in OPS:
        raise _err(src, f"{where}: unknown op {op!r} (allowed: {', '.join(sorted(OPS))})")
    if op not in NO_VALUE_OPS and "value" not in d:
        raise _err(src, f"{where}: op {op!r} needs a value")
    return Condition(f, op, d.get("value"))


def _rule(d: Any, known: set, src: str, where: str) -> Rule:
    if not isinstance(d, dict):
        raise _err(src, f"{where}: rule must be a mapping")
    style = d.get("style", "alert")
    if style not in STYLES:
        raise _err(src, f"{where}: unknown style {style!r} (allowed: {', '.join(sorted(STYLES))})")
    if "when" in d:
        conds = d["when"]
        if not isinstance(conds, list) or not conds:
            raise _err(src, f"{where}.when must be a non-empty list")
        return Rule(tuple(_condition(c, known, src, f"{where}.when[{i}]")
                          for i, c in enumerate(conds)), style)
    return Rule((_condition(d, known, src, where),), style)


def _str_map(v: Any, src: str, name: str) -> Dict[str, str]:
    if v is None:
        return {}
    if not isinstance(v, dict):
        raise _err(src, f"{name} must be a mapping")
    return {str(k): str(val) for k, val in v.items()}


def _cell(d: Any, col: str, known: set, src: str) -> CellSpec:
    if not isinstance(d, dict):
        raise _err(src, f"view.cells.{col}: renderer must be a mapping")
    kind = d.get("type", "text")
    if kind not in CELL_TYPES:
        raise _err(src, f"view.cells.{col}.type must be one of {sorted(CELL_TYPES)}")
    spec = CellSpec(kind=kind)
    if kind == "bar":
        spec.min = 0.0 if d.get("min") is None else _num(d["min"], src, f"view.cells.{col}.min")
        spec.max = 100.0 if d.get("max") is None else _num(d["max"], src, f"view.cells.{col}.max")
        if spec.max == spec.min:
            raise _err(src, f"view.cells.{col}: max must differ from min")
        spec.suffix = str(d.get("suffix", ""))
        raw = d.get("thresholds") or []
        if not isinstance(raw, list):
            raise _err(src, f"view.cells.{col}.thresholds must be a list")
        out: List[Tuple[float, str]] = []
        for i, t in enumerate(raw):
            if not isinstance(t, dict) or "at" not in t:
                raise _err(src, f"view.cells.{col}.thresholds[{i}] needs 'at' and 'style'")
            style = str(t.get("style", "warn"))
            if style not in STYLES:
                raise _err(src, f"view.cells.{col}.thresholds[{i}]: unknown style {style!r}")
            out.append((_num(t["at"], src, f"view.cells.{col}.thresholds[{i}].at"), style))
        spec.thresholds = sorted(out)                   # ascending; last satisfied wins
    elif kind == "status":
        raw = d.get("rules") or []
        if not isinstance(raw, list) or not raw:
            raise _err(src, f"view.cells.{col}.rules must be a non-empty list")
        rules: List[Tuple[Rule, str]] = []
        for i, r in enumerate(raw):
            label = str(r.get("text", "")) if isinstance(r, dict) else ""
            rules.append((_rule(r, known, src, f"view.cells.{col}.rules[{i}]"), label))
        spec.rules = rules
    elif kind == "spark":
        spec.history = _int(d.get("history", 30), src, f"view.cells.{col}.history")
        if spec.history < 2:
            raise _err(src, f"view.cells.{col}.history must be >= 2")
        if d.get("min") is not None:
            spec.min = _num(d["min"], src, f"view.cells.{col}.min")
        if d.get("max") is not None:
            spec.max = _num(d["max"], src, f"view.cells.{col}.max")
    return spec


def _num(v: Any, src: str, where: str) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        raise _err(src, f"{where} must be a number")


def _int(v: Any, src: str, where: str) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        raise _err(src, f"{where} must be an integer")


def parse_widget(data: Any, source: str = "<memory>") -> WidgetDef:
    if not isinstance(data, dict):
        raise _err(source, "top level must be a mapping")
    name = data.get("widget")
    if not name or not isinstance(name, str):
        raise _err(source, "widget: name is required")
    src = f"{source} [{name}]"

    commands = _str_map(data.get("commands"), src, "commands")
    if not commands:
        raise _err(src, "commands: at least one platform -> command is required")
    for plat, cmd in commands.items():
        if len(cmd) > MAX_COMMAND_LEN:
            raise _err(src, f"commands.{plat}: {len(cmd)} chars; keep under {MAX_COMMAND_LEN} "
                            "(busybox ash and some device CLIs truncate long input lines)")
    requires = _str_map(data.get("requires"), src, "requires")
    for plat in requires:
        if plat not in commands:
            raise _err(src, f"requires.{plat}: no command for that platform")
    templates = _str_map(data.get("templates"), src, "templates")
    for plat, tname in templates.items():
        if is_py(tname) and tname[len(PY_PREFIX):] not in PY_PARSERS:
            raise _err(src, f"templates.{plat}: unknown python parser {tname!r}; "
                            f"known: {', '.join(sorted(PY_PARSERS))}")

    try:
        interval = float(data.get("interval", 30))
    except (TypeError, ValueError):
        raise _err(src, "interval must be a number (seconds)")
    if interval < MIN_INTERVAL:
        raise _err(src, f"interval must be >= {MIN_INTERVAL:g}s")

    raw_fields = data.get("fields")
    if not isinstance(raw_fields, dict) or not raw_fields:
        raise _err(src, "fields: at least one field mapping is required")
    fields: Dict[str, List[str]] = {}
    for fname, aliases in raw_fields.items():
        if isinstance(aliases, str):
            aliases = [aliases]
        if not isinstance(aliases, list) or not aliases:
            raise _err(src, f"fields.{fname} must be a TextFSM field or list of them")
        fields[str(fname)] = [str(a) for a in aliases]

    rates = _str_map(data.get("rates"), src, "rates")
    deltas = _str_map(data.get("deltas"), src, "deltas")
    for block, derived in (("rates", rates), ("deltas", deltas)):
        for dname, source_field in derived.items():
            if dname in fields or (block == "deltas" and dname in rates):
                raise _err(src, f"{block}.{dname} collides with another field of the same name")
            if source_field not in fields:
                raise _err(src, f"{block}.{dname}: source {source_field!r} is not a field")

    raw_computes = data.get("computes")
    computes: Dict[str, Expr] = {}
    if raw_computes is not None:
        if not isinstance(raw_computes, dict):
            raise _err(src, "computes must be a mapping name -> expression")
        avail = set(fields) | set(rates) | set(deltas)
        for cname, expr_text in raw_computes.items():
            cname = str(cname)
            if cname in avail:
                raise _err(src, f"computes.{cname} collides with another field of the same name")
            if not isinstance(expr_text, str):
                raise _err(src, f"computes.{cname} must be a string expression")
            try:
                computes[cname] = Expr(expr_text, avail)    # references earlier fields/computes
            except ValueError as e:
                raise _err(src, f"computes.{cname}: {e}")
            avail.add(cname)

    key = data.get("key")
    if key is not None and key not in fields:
        raise _err(src, f"key {key!r} is not a field")
    if (rates or deltas) and not key:
        raise _err(src, "rates/deltas need a key (row identity across polls)")

    unique = data.get("unique", False)
    if not isinstance(unique, bool):
        raise _err(src, "unique must be true or false")
    if unique and not key:
        raise _err(src, "unique needs a key (the field rows are collapsed on)")

    known = set(fields) | set(rates) | set(deltas) | set(computes)
    raw_drop = data.get("drop") or []
    if not isinstance(raw_drop, list):
        raise _err(src, "drop must be a list")
    drop = [_rule(r, set(fields), src, f"drop[{i}]") for i, r in enumerate(raw_drop)]
    v = data.get("view")
    if not isinstance(v, dict):
        raise _err(src, "view is required")
    vtype = v.get("type")
    if vtype not in VIEW_TYPES:
        raise _err(src, f"view.type must be one of {sorted(VIEW_TYPES)}")

    view = ViewSpec(type=vtype, labels=_str_map(v.get("labels"), src, "view.labels"))
    alerts = v.get("alerts") or []
    if not isinstance(alerts, list):
        raise _err(src, "view.alerts must be a list")
    view.alerts = [_rule(a, known, src, f"view.alerts[{i}]") for i, a in enumerate(alerts)]

    if vtype == "table":
        cols = v.get("columns") or list(fields) + list(rates) + list(deltas)
        if not isinstance(cols, list):
            raise _err(src, "view.columns must be a list")
        for c in cols:
            if c not in known:
                raise _err(src, f"view.columns: unknown field {c!r}")
        view.columns = [str(c) for c in cols]
        sort = v.get("sort")
        if sort is not None and sort not in known:
            raise _err(src, f"view.sort: unknown field {sort!r}")
        view.sort = sort
        view.sort_desc = bool(v.get("sort_desc", False))
        if v.get("limit") is not None:
            try:
                view.limit = int(v["limit"])
            except (TypeError, ValueError):
                raise _err(src, "view.limit must be an integer")
            if view.limit < 1:
                raise _err(src, "view.limit must be >= 1")
            if not sort:
                raise _err(src, "view.limit needs view.sort (top-N of what?)")
        raw_cells = v.get("cells") or {}
        if not isinstance(raw_cells, dict):
            raise _err(src, "view.cells must be a mapping column -> renderer")
        for cname, cdef in raw_cells.items():
            if cname not in view.columns:
                raise _err(src, f"view.cells: {cname!r} is not a displayed column")
            spec = _cell(cdef, str(cname), known, src)
            view.cells[str(cname)] = spec
            if spec.kind == "spark":
                view.history_fields[str(cname)] = spec.history
        if view.history_fields and not key:
            raise _err(src, "spark renderers need a key (row identity across polls for history)")
    elif vtype == "kv":
        show = v.get("show") or list(fields) + list(rates) + list(deltas)
        if not isinstance(show, list):
            raise _err(src, "view.show must be a list")
        for c in show:
            if c not in known:
                raise _err(src, f"view.show: unknown field {c!r}")
        view.columns = [str(c) for c in show]
    else:
        agg = v.get("aggregate", "count")
        if agg not in AGGREGATES:
            raise _err(src, f"view.aggregate must be one of {sorted(AGGREGATES)}")
        view.aggregate = agg
        f = v.get("field")
        if agg != "count":
            if f not in known:
                raise _err(src, f"view.field: {agg} needs a known field, got {f!r}")
            view.field = f
        if "where" in v:
            view.where = _rule(v["where"], known, src, "view.where")
        view.label = str(v.get("label", data.get("title", name)))
        if v.get("alert_above") is not None:
            try:
                view.alert_above = float(v["alert_above"])
            except (TypeError, ValueError):
                raise _err(src, "view.alert_above must be a number")

    monitor = data.get("monitor")
    if monitor is not None:
        if vtype != "table":
            raise _err(src, "monitor: only table views have rows to right-click")
        if monitor not in view.columns:
            raise _err(src, f"monitor: {monitor!r} is not a displayed column")

    return WidgetDef(
        name=name, title=str(data.get("title", name)), interval=interval,
        commands=commands, templates=templates, requires=requires, key=key, fields=fields,
        rates=rates, view=view, deltas=deltas, computes=computes, drop=drop, source=source,
        monitor=monitor, unique=unique,
    )


def load_widget(path: Path) -> WidgetDef:
    with open(path, "r", encoding="utf-8") as fh:
        try:
            data = yaml.safe_load(fh)
        except yaml.YAMLError as e:
            raise WidgetError(f"{path}: YAML error: {e}")
    return parse_widget(data, str(path))


def load_widgets(dirs: Iterable[Path]) -> Tuple[Dict[str, WidgetDef], List[str]]:
    """Load every *.yaml under dirs; later dirs override earlier by widget name.
    Bad files are skipped and reported, never fatal."""
    widgets: Dict[str, WidgetDef] = {}
    errors: List[str] = []
    for d in dirs:
        if not Path(d).is_dir():
            continue
        for p in sorted(Path(d).glob("*.yaml")):
            try:
                w = load_widget(p)
            except WidgetError as e:
                errors.append(str(e))
                log.warning("%s", e)
                continue
            widgets[w.name] = w
    return widgets, errors
