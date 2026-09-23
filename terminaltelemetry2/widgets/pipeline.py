"""Parsed TextFSM records -> widget rows: alias mapping, counter rates,
computes, retained history, aggregates."""
from __future__ import annotations

from collections import deque
from typing import Any, Deque, Dict, List, Optional, Tuple

from .rules import to_number
from .schema import ViewSpec, WidgetDef

HIST_KEY = "__hist__"      # per-row payload: {field: [recent numeric samples]}


def _flatten(v: Any) -> Any:
    if isinstance(v, list):                     # TextFSM List values
        return ", ".join(str(x) for x in v if str(x).strip())
    return v


def _first(record: Dict[str, Any], aliases: List[str]) -> Any:
    for a in aliases:
        v = _flatten(record.get(a.lower()))
        if v is not None and str(v).strip() != "":
            return v
    return ""


class Pipeline:
    def __init__(self, defn: WidgetDef):
        self.defn = defn
        self._prev: Dict[Tuple[str, str], Tuple[float, float]] = {}
        self._hist: Dict[Tuple[str, str], Deque[float]] = {}

    def reset(self) -> None:
        self._prev.clear()
        self._hist.clear()

    def apply(self, records: List[Dict[str, Any]], ts: float) -> List[Dict[str, Any]]:
        d = self.defn
        rows: List[Dict[str, Any]] = []
        for rec in records:
            low = {str(k).lower(): v for k, v in rec.items()}
            row = {name: _first(low, aliases) for name, aliases in d.fields.items()}
            if not any(r.test(row) for r in d.drop):
                rows.append(row)
        if d.unique:
            # Opt-in: a template that emits one record per sub-line (Junos
            # 'show interfaces terse': one per family/address) repeats the key.
            # First record per key wins -- it carries the interface line itself.
            seen = set()
            rows = [r for r in rows if not (r.get(d.key) in seen or seen.add(r.get(d.key)))]
        if d.rates or d.deltas:
            self._derive(rows, ts)
        if d.computes:
            for row in rows:
                for name, expr in d.computes.items():
                    row[name] = expr.eval(row)          # references fields/rates/deltas/earlier computes
        if d.view.history_fields:
            self._retain(rows)
        return rows

    def _retain(self, rows: List[Dict[str, Any]]) -> None:
        """Keep a bounded ring of recent samples per (row key, field) for spark
        cells, and stamp each row with the current series. A blank/non-numeric
        sample is skipped rather than punched in as a zero. History for a row
        that stops appearing is dropped, so a churning key set can't grow the
        buffer without bound.
        """
        key = self.defn.key
        want = self.defn.view.history_fields          # {field: maxlen}
        seen = set()
        for row in rows:
            k = str(row.get(key, ""))
            stamp: Dict[str, List[float]] = {}
            for fld, n in want.items():
                slot = (k, fld)
                seen.add(slot)
                dq = self._hist.get(slot)
                if dq is None or dq.maxlen != n:      # maxlen change on a redefined widget
                    dq = deque(dq or (), maxlen=n)
                    self._hist[slot] = dq
                v = to_number(row.get(fld))
                if v is not None:
                    dq.append(v)
                stamp[fld] = list(dq)
            row[HIST_KEY] = stamp
        for slot in list(self._hist):
            if slot not in seen:
                del self._hist[slot]

    def _derive(self, rows: List[Dict[str, Any]], ts: float) -> None:
        """Rates and deltas from the previous poll, per (row key, source field).
        A counter that went backwards (clear, wrap, reboot) yields None for one
        sample rather than a negative value."""
        d = self.defn
        seen = set()
        sources = set(d.rates.values()) | set(d.deltas.values())
        for row in rows:
            k = str(row.get(d.key, ""))
            diffs: Dict[str, Tuple[Optional[float], float]] = {}
            for src in sources:
                cur = to_number(row.get(src))
                slot = (k, src)
                seen.add(slot)
                diff: Optional[float] = None
                dt = 0.0
                prev = self._prev.get(slot)
                if cur is not None:
                    if prev is not None and cur >= prev[0]:
                        diff, dt = cur - prev[0], ts - prev[1]
                    self._prev[slot] = (cur, ts)
                diffs[src] = (diff, dt)
            for rname, src in d.rates.items():
                diff, dt = diffs[src]
                row[rname] = diff / dt if diff is not None and dt > 0 else None
            for dname, src in d.deltas.items():
                row[dname] = diffs[src][0]
        for slot in list(self._prev):
            if slot not in seen:
                del self._prev[slot]


def aggregate(rows: List[Dict[str, Any]], view: ViewSpec) -> Optional[float]:
    sel = [r for r in rows if view.where is None or view.where.test(r)]
    if view.aggregate == "count":
        return float(len(sel))
    nums = [n for n in (to_number(r.get(view.field)) for r in sel) if n is not None]
    if not nums:
        return None
    return {"sum": sum, "min": min, "max": max}[view.aggregate](nums)
