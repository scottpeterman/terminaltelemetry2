"""Condition evaluation for alerts and stat filters. No eval()."""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Dict, Optional, Tuple

OPS = {
    "eq", "ne", "lt", "le", "gt", "ge",
    "match", "not_match", "numeric", "not_numeric", "empty", "not_empty",
}
NO_VALUE_OPS = {"numeric", "not_numeric", "empty", "not_empty"}
STYLES = {"alert", "warn", "ok", "muted"}


def to_number(value: Any) -> Optional[float]:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    s = str(value).strip().replace(",", "")
    if not s:
        return None
    try:
        return float(s)
    except ValueError:
        return None


@dataclass(frozen=True)
class Condition:
    field: str
    op: str
    value: Any = None

    def test(self, row: Dict[str, Any]) -> bool:
        v = row.get(self.field)
        s = "" if v is None else str(v)
        op = self.op
        if op == "empty":
            return s.strip() == ""
        if op == "not_empty":
            return s.strip() != ""
        if op == "numeric":
            return to_number(v) is not None
        if op == "not_numeric":
            return to_number(v) is None
        if op == "match":
            return re.search(str(self.value), s) is not None
        if op == "not_match":
            return re.search(str(self.value), s) is None
        a, b = to_number(v), to_number(self.value)
        if op in ("eq", "ne"):
            same = (a == b) if (a is not None and b is not None) else (s == str(self.value))
            return same if op == "eq" else not same
        if a is None or b is None:
            return False
        return {"lt": a < b, "le": a <= b, "gt": a > b, "ge": a >= b}[op]


@dataclass(frozen=True)
class Rule:
    """All conditions must hold (AND)."""
    conditions: Tuple[Condition, ...]
    style: str = "alert"

    def test(self, row: Dict[str, Any]) -> bool:
        return all(c.test(row) for c in self.conditions)


def row_style(rules, row: Dict[str, Any]) -> Optional[str]:
    """First matching rule wins."""
    for r in rules:
        if r.test(row):
            return r.style
    return None
