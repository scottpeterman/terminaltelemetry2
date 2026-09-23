"""Every bundled EOS widget against ntc fixtures, plus the view behaviors they rely on."""
import pytest
from conftest import fixture_text

from terminaltelemetry2.paths import PKG_DATA
from terminaltelemetry2.widgets import Pipeline, load_widgets, row_style
from terminaltelemetry2.widgets.views import KVView, TableView, make_view

WIDGETS, _ = load_widgets([PKG_DATA / "widgets"])


def rows_for(parser, name, fixture=None, ts=0.0, pipeline=None, text=None):
    w = WIDGETS[name]
    cmd = w.command_for("arista_eos")
    text = text or fixture_text(fixture or f"arista_eos_{cmd.replace(' ', '_')}.raw")
    r = parser.parse("arista_eos", cmd, text, w.template_for("arista_eos"))
    assert r.records, f"{name}: {r.error}"
    return (pipeline or Pipeline(w)).apply(r.records, ts)


def test_ospf(parser):
    rows = rows_for(parser, "ospf_neighbors")
    assert rows[0]["neighbor"] == "3.3.3.3" and rows[0]["state"] == "FULL/BDR"
    alerts = WIDGETS["ospf_neighbors"].view.alerts
    assert all(row_style(alerts, r) is None for r in rows)
    for bad in ("INIT", "EXSTART/DR", "Loading"):
        assert row_style(alerts, {"state": bad}) == "alert"
    for good in ("2WAY/DROTHER", "Full", "2 Ways"):
        assert row_style(alerts, {"state": good}) is None


def test_lldp(parser):
    rows = rows_for(parser, "lldp_neighbors")
    assert rows[0] == {"local": "Et1", "neighbor": "localhost", "remote": "Ethernet1", "caps": ""}


def test_version_kv_hides_empty(qapp, parser):
    rows = rows_for(parser, "version")
    assert rows[0]["model"] == "vEOS" and rows[0]["version"] == "4.14.7M"
    view = make_view(WIDGETS["version"])
    assert isinstance(view, KVView)
    view.update_rows(rows)
    assert view.values["version"].text() == "4.14.7M"
    assert not view.form.isRowVisible(view.values["hostname"])      # EOS template has no hostname
    assert view.form.isRowVisible(view.values["model"])


def test_system_and_top_share_one_parse(qapp, parser):
    assert WIDGETS["system"].command_for("arista_eos") == WIDGETS["top_procs"].command_for("arista_eos")
    sysrow = rows_for(parser, "system")[0]
    assert sysrow["cpu_idle"] == "96.3" and sysrow["cpu_5s"] == ""
    top = make_view(WIDGETS["top_procs"])
    rows = rows_for(parser, "top_procs")
    top.update_rows(rows)
    t = top.table
    cpus = [float(t.item(r, 1).text()) for r in range(t.rowCount())]
    assert t.rowCount() == min(12, len(rows)) and cpus == sorted(cpus, reverse=True)


def test_intf_updown_flap_delta(parser):
    w = WIDGETS["intf_updown"]
    p = Pipeline(w)
    text = fixture_text("arista_eos_show_interfaces.raw")
    first = rows_for(parser, "intf_updown", text=text, pipeline=p, ts=0.0)
    e1 = next(r for r in first if r["intf"] == "Ethernet1")
    assert e1["link"] == "up" and e1["changes"] == "1" and e1["flaps"] is None
    flapped = text.replace("1 link status changes since last clear",
                           "3 link status changes since last clear", 1)
    assert flapped != text
    second = rows_for(parser, "intf_updown", text=flapped, pipeline=p, ts=60.0)
    e1 = next(r for r in second if r["intf"] == "Ethernet1")
    assert e1["flaps"] == 2.0 and row_style(w.view.alerts, e1) == "alert"


def test_table_default_sort_is_ascending_natural(qapp):
    w = WIDGETS["port_status"]
    v = TableView(w)
    v.update_rows([{"port": p, "name": "", "status": "connected", "vlan": "", "speed": "", "type": ""}
                   for p in ("Po1", "Et10", "Ma1", "Et9", "Et2")])
    assert [v.table.item(r, 0).text() for r in range(5)] == ["Et2", "Et9", "Et10", "Ma1", "Po1"]


def test_drop_rules_and_integer_deltas():
    from terminaltelemetry2.widgets.views import format_value
    p = Pipeline(WIDGETS["lldp_neighbors"])
    rows = p.apply([{"LOCAL_INTERFACE": "---------", "NEIGHBOR_NAME": "------", "NEIGHBOR_INTERFACE": "---"},
                    {"LOCAL_INTERFACE": "Et1", "NEIGHBOR_NAME": "agg2.lab1", "NEIGHBOR_INTERFACE": "Ethernet1"}], 0.0)
    assert [r["local"] for r in rows] == ["Et1"]
    assert format_value(0.0) == "0" and format_value(2.0) == "2" and format_value(12.5) == "12.5"
