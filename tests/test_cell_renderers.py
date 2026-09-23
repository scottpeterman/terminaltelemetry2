"""Cell renderers, computes, and retained history -- the pure-Python layer.

Qt painting (delegates) is exercised by the app, not here; these lock down the
definition language and the pipeline that feed the delegates.
"""
import pytest

from conftest import fixture_text
from terminaltelemetry2.parsing import Parser
from terminaltelemetry2.paths import PKG_DATA
from terminaltelemetry2.widgets import Expr, HIST_KEY, Pipeline, WidgetError, load_widgets, parse_widget
from terminaltelemetry2.widgets.pipeline import to_number
from terminaltelemetry2.widgets.compute import operand

WIDGETS, ERRORS = load_widgets([PKG_DATA / "widgets"])


def test_no_widget_load_errors():
    assert ERRORS == []
    assert "intf_util" in WIDGETS


# -- Expr ---------------------------------------------------------------------

def test_expr_basic_and_precedence():
    e = Expr("in_rate / (bw_kbit * 1000) * 100", {"in_rate", "bw_kbit"})
    assert e.names == {"in_rate", "bw_kbit"}
    assert e.eval({"in_rate": "500000000", "bw_kbit": "1000000"}) == pytest.approx(50.0)
    assert e.eval({"in_rate": 0, "bw_kbit": 1000000}) == 0.0


def test_expr_missing_or_nonnumeric_is_none():
    e = Expr("a * b", {"a", "b"})
    assert e.eval({"a": "5"}) is None            # b absent
    assert e.eval({"a": "5", "b": ""}) is None   # b blank
    assert e.eval({"a": "x", "b": "2"}) is None  # a non-numeric


def test_expr_divide_by_zero_is_none():
    assert Expr("a / b", {"a", "b"}).eval({"a": "1", "b": "0"}) is None


def test_expr_unknown_field_rejected_at_parse():
    with pytest.raises(ValueError, match="unknown field 'ghost'"):
        Expr("ghost * 2", {"real"})


@pytest.mark.parametrize("bad", ["a ** 2", "abs(a)", "a % 2", "a > 1", "a and b", "__import__('os')"])
def test_expr_rejects_non_arithmetic(bad):
    with pytest.raises(ValueError):
        Expr(bad, {"a", "b"})


# -- schema: computes + cells -------------------------------------------------

def test_intf_util_schema():
    w = WIDGETS["intf_util"]
    assert set(w.computes) == {"in_util", "out_util"}
    cells = w.view.cells
    assert cells["in_util"].kind == "bar"
    assert cells["in_util"].thresholds == [(70.0, "warn"), (90.0, "alert")]  # sorted ascending
    assert cells["in_util"].suffix == "%"
    assert cells["out_util"].max == 100.0 and cells["out_util"].min == 0.0   # min defaulted
    assert cells["link"].kind == "status" and len(cells["link"].rules) == 2
    assert cells["in_rate"].kind == "spark"
    assert w.view.history_fields == {"in_rate": 30}


def test_compute_can_reference_earlier_compute():
    w = parse_widget({
        "widget": "x", "commands": {"cisco_ios": "show foo"}, "key": "i",
        "fields": {"i": ["I"], "a": ["A"], "b": ["B"]},
        "computes": {"sum": "a + b", "avg": "sum / 2"},
        "view": {"type": "table", "columns": ["i", "avg"]},
    })
    assert set(w.computes) == {"sum", "avg"}


def test_compute_forward_reference_rejected():
    with pytest.raises(WidgetError, match="unknown field 'later'"):
        parse_widget({
            "widget": "x", "commands": {"cisco_ios": "show foo"},
            "fields": {"a": ["A"]},
            "computes": {"early": "later + 1", "later": "a * 2"},
            "view": {"type": "table", "columns": ["a"]},
        })


def test_bar_on_unknown_column_rejected():
    with pytest.raises(WidgetError, match="not a displayed column"):
        parse_widget({
            "widget": "x", "commands": {"cisco_ios": "show foo"},
            "fields": {"a": ["A"]},
            "view": {"type": "table", "columns": ["a"], "cells": {"ghost": {"type": "bar"}}},
        })


def test_spark_requires_key():
    with pytest.raises(WidgetError, match="spark renderers need a key"):
        parse_widget({
            "widget": "x", "commands": {"cisco_ios": "show foo"},
            "fields": {"a": ["A"]},
            "view": {"type": "table", "columns": ["a"], "cells": {"a": {"type": "spark"}}},
        })


# -- pipeline: computes -------------------------------------------------------

def _rec(intf, in_rate, out_rate, bw, err="0", link="up"):
    return {"INTERFACE": intf, "LINK_STATUS": link, "INPUT_RATE": in_rate,
            "OUTPUT_RATE": out_rate, "BANDWIDTH": bw, "INPUT_ERRORS": err}


def test_computes_utilization():
    rows = Pipeline(WIDGETS["intf_util"]).apply(
        [_rec("Gi0/1", "700000000", "50000000", "1000000")], 0.0)
    r = rows[0]
    assert r["in_util"] == pytest.approx(70.0)     # 700Mbps of 1Gbps
    assert r["out_util"] == pytest.approx(5.0)


def test_compute_none_when_bandwidth_missing():
    rows = Pipeline(WIDGETS["intf_util"]).apply(
        [_rec("Gi0/1", "700000000", "0", "")], 0.0)
    assert rows[0]["in_util"] is None              # blank BANDWIDTH -> blank cell, not a crash


# -- pipeline: retained history ----------------------------------------------

def test_history_accumulates_and_caps():
    p = Pipeline(WIDGETS["intf_util"])
    for rate in range(40):                         # more than history: 30
        rows = p.apply([_rec("Gi0/1", str(rate * 1_000_000), "0", "1000000")], float(rate))
    series = rows[0][HIST_KEY]["in_rate"]
    assert len(series) == 30                        # capped at declared history
    assert series[-1] == 39_000_000.0               # newest sample last
    assert series[0] == 10_000_000.0                # oldest 30 kept


def test_history_skips_blank_and_prunes_dropped_rows():
    p = Pipeline(WIDGETS["intf_util"])
    p.apply([_rec("Gi0/1", "1000000", "0", "1000000")], 0.0)
    r = p.apply([_rec("Gi0/1", "", "0", "1000000")], 1.0)[0]   # blank rate
    assert r[HIST_KEY]["in_rate"] == [1_000_000.0]             # blank not punched in as 0
    # Gi0/1 gone next poll -> its buffer is dropped, not leaked
    p.apply([_rec("Gi0/2", "2000000", "0", "1000000")], 2.0)
    assert all(k[0] != "Gi0/1" for k in p._hist)


# -- end to end: real IOS output ---------------------------------------------

def test_ios_fixture_through_util_widget(parser):
    recs = parser.parse("cisco_ios", "show interfaces",
                        fixture_text("cisco_ios_show_interfaces.raw")).records
    rows = Pipeline(WIDGETS["intf_util"]).apply(recs, 0.0)
    assert rows                                     # parsed and mapped
    by = {r["intf"]: r for r in rows}

    # BANDWIDTH arrives from the ntc template as '1000000 Kbit' -- a string with a
    # unit. Strict to_number() declines it (correctly: sort_key must not read a
    # number out of an interface name); the compute layer's operand() takes the
    # leading token, so the utilization math still resolves to a real number.
    g0 = by["GigabitEthernet0/0"]
    assert to_number(g0["bw_kbit"]) is None          # strict: unit blocks it
    assert operand(g0["bw_kbit"]) == 1_000_000.0      # tolerant: computes see 1e6

    # idle link (0 bits/sec) on a 1Gbps port -> 0.0%, a number and not None
    assert g0["in_util"] == 0.0
    assert g0["out_util"] == 0.0

    # a link with a non-zero rate yields a real, tiny, non-None utilization --
    # proves the derived value is computed, not defaulted to zero. (The fixture
    # lists GigabitEthernet0/2 twice, so intf isn't a unique key; scan the rows
    # for the one carrying traffic rather than trusting last-write-wins.)
    active = [r for r in rows if operand(r.get("out_rate")) not in (None, 0.0)]
    assert active, "expected at least one interface with a non-zero output rate"
    a = active[0]
    assert a["out_util"] is not None and a["out_util"] > 0.0
    expected = operand(a["out_rate"]) / (operand(a["bw_kbit"]) * 1000) * 100
    assert a["out_util"] == pytest.approx(expected)
