"""Safe per-row arithmetic for derived fields. AST-whitelisted; never eval()."""
from __future__ import annotations

import ast
import operator
import re
from typing import Any, Callable, Dict, Optional, Set

from .rules import to_number

_LEAD_NUM = re.compile(r"[-+]?\d[\d,]*(?:\.\d+)?")


def operand(value: Any) -> Optional[float]:
    """Number for arithmetic, tolerant of a trailing unit: real CLI fields arrive
    as '1000000 Kbit', '1500 bytes', '-3.4 dBm'. Takes the leading numeric token
    when to_number() (strict) declines. Scoped to computes on purpose -- the
    shared to_number() stays strict so sort_key doesn't start reading a leading
    number out of '10GigabitEthernet0/1' and break natural interface ordering.
    """
    n = to_number(value)
    if n is not None or value is None:
        return n
    m = _LEAD_NUM.match(str(value).strip())
    if not m:
        return None
    try:
        return float(m.group(0).replace(",", ""))
    except ValueError:
        return None

_BIN: Dict[type, Callable[[float, float], float]] = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
}
_UNARY: Dict[type, Callable[[float], float]] = {
    ast.UAdd: operator.pos,
    ast.USub: operator.neg,
}


class Expr:
    """A numeric expression over row fields: + - * /, unary +/-, parentheses,
    and numeric literals. Names resolve to row values via operand() (tolerant of
    a trailing unit like '1000000 Kbit'). A missing or non-numeric operand, or a
    divide-by-zero, makes the whole expression None
    (a blank cell) rather than raising -- the same discipline rates use for a
    counter that went backwards.

    Deliberately not an expression language: no calls, names-as-functions,
    attribute access, comparisons, or power. Validated once at parse time
    against the set of known fields, so a bad reference is a widget-load error,
    not a per-poll surprise.
    """

    __slots__ = ("text", "names", "_node")

    def __init__(self, text: str, known: Set[str]):
        self.text = text
        try:
            self._node = ast.parse(text, mode="eval").body
        except SyntaxError as e:
            raise ValueError(f"{text!r}: {e.msg}")
        self.names = self._check(self._node, known)

    @classmethod
    def _check(cls, node: ast.AST, known: Set[str]) -> Set[str]:
        if isinstance(node, ast.Name):
            if node.id not in known:
                raise ValueError(f"unknown field {node.id!r}")
            return {node.id}
        if isinstance(node, ast.Constant):
            if isinstance(node.value, bool) or not isinstance(node.value, (int, float)):
                raise ValueError("only numeric constants are allowed")
            return set()
        if isinstance(node, ast.BinOp):
            if type(node.op) not in _BIN:
                raise ValueError(f"operator {type(node.op).__name__} not allowed (use + - * /)")
            return cls._check(node.left, known) | cls._check(node.right, known)
        if isinstance(node, ast.UnaryOp):
            if type(node.op) not in _UNARY:
                raise ValueError(f"unary {type(node.op).__name__} not allowed")
            return cls._check(node.operand, known)
        raise ValueError(f"unsupported syntax: {type(node).__name__}")

    def eval(self, row: Dict[str, Any]) -> Optional[float]:
        return self._ev(self._node, row)

    def _ev(self, node: ast.AST, row: Dict[str, Any]) -> Optional[float]:
        if isinstance(node, ast.Constant):
            return float(node.value)
        if isinstance(node, ast.Name):
            return operand(row.get(node.id))
        if isinstance(node, ast.UnaryOp):
            v = self._ev(node.operand, row)
            return None if v is None else _UNARY[type(node.op)](v)
        if isinstance(node, ast.BinOp):
            a = self._ev(node.left, row)
            b = self._ev(node.right, row)
            if a is None or b is None:
                return None
            try:
                return _BIN[type(node.op)](a, b)
            except ZeroDivisionError:
                return None
        return None
