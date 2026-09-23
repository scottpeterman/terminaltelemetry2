import pytest

from terminaltelemetry2.paths import PKG_DATA
from terminaltelemetry2.widgets import WidgetError, load_widgets, parse_widget
from terminaltelemetry2.widgets.pipeline import Pipeline

WIDGETS, _ = load_widgets([PKG_DATA / "widgets"])
TERSE = """Interface               Admin Link Proto    Local                 Remote
ae0                     up    up
ae0.9                   up    up   inet     203.0.113.247/30
                                   inet6    2001:db8::1/126
                                            fe80::1/64
                                   mpls
ae0.13                  up    up   inet     203.0.113.235/30
xe-0/0/9                up    down
"""


def test_ports_one_row_per_junos_unit():
    from terminaltelemetry2.parsing import Parser
    parsed = Parser(PKG_DATA / "tfsm_templates.db").parse(
        "juniper_junos", "show interfaces terse", TERSE)
    assert parsed.error is None, parsed.error
    assert sum(r["INTERFACE"] == "ae0.9" for r in parsed.records) > 1   # template repeats it
    rows = Pipeline(WIDGETS["port_status"]).apply(parsed.records, 0.0)
    assert [r["port"] for r in rows] == ["ae0", "ae0.9", "ae0.13", "xe-0/0/9"]
    assert {r["port"]: r["status"] for r in rows}["xe-0/0/9"] == "down"


def test_unique_is_opt_in():
    # parallel OSPF adjacencies share a neighbor ID; they must both show
    assert not WIDGETS["ospf_neighbors"].unique and not WIDGETS["lldp_neighbors"].unique


def test_unique_needs_key():
    with pytest.raises(WidgetError):
        parse_widget({"widget": "w", "commands": {"x": "c"}, "fields": {"a": ["A"]},
                      "unique": True, "view": {"type": "table"}})
