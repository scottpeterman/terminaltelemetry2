import pytest
"""Junos system/top_procs via show system processes extensive | no-more.

One custom template feeds both widgets with the same Value names as the Arista
top template (no widget alias changes). The "| no-more" pipe would mangle the
auto-derived template name, so the widgets name the template explicitly.
Authored against a real MX10003 capture.
"""
from conftest import fixture_text
from terminaltelemetry2.parsing import Parser
from terminaltelemetry2.paths import PKG_DATA
from terminaltelemetry2.widgets import Pipeline, aggregate, load_widgets

WIDGETS, _ = load_widgets([PKG_DATA / "widgets"])
RAW = fixture_text("juniper_junos_show_system_processes_extensive.raw")
TPL = "juniper_junos_show_system_processes_extensive"


def _parse():
    # mirror the widget: piped command sent to device, explicit template used
    return Parser(PKG_DATA / "tfsm_templates.db").parse(
        "juniper_junos", "show system processes extensive | no-more", RAW, template=TPL)


def test_widget_wiring():
    for name in ("system", "top_procs"):
        w = WIDGETS[name]
        assert w.command_for("juniper_junos") == "show system processes extensive | no-more"
        assert w.template_for("juniper_junos") == TPL          # explicit, decoupled from pipe


def test_template_resolves_and_parses():
    parsed = _parse()
    assert parsed.error is None, parsed.error
    assert parsed.template == TPL
    assert parsed.records


def test_system_globals_populate():
    parsed = _parse()
    r = Pipeline(WIDGETS["system"]).apply(parsed.records, 0.0)[0]
    assert r["load1"] == "0.53" and r["load5"] == "0.43"
    assert r["cpu_user"] == "1.5" and r["cpu_sys"] == "1.1" and r["cpu_idle"] == "97.3"
    assert r["tasks"] == "514"
    assert r["mem_used"] == "186M" and r["mem_free"] == "36G"


def test_top_procs_rows_and_idle_dropped():
    parsed = _parse()
    rows = Pipeline(WIDGETS["top_procs"]).apply(parsed.records, 0.0)
    cmds = [r["command"] for r in rows]
    assert not any(c.startswith("idle") for c in cmds)          # idle threads dropped
    assert "mib2d" in cmds and "rpd{rpd}" in cmds and "snmpd" in cmds
    assert any(c == "intr{swi6: task queue}" for c in cmds)     # spaces-in-command survives


def test_junos_13x_format_spans():
    # JUNOS 13.3 (MX80): "processes" not "threads", a THR column instead of C,
    # no CPU line. The base template spans it via the Process_THR header path.
    raw = fixture_text("juniper_junos_show_system_processes_extensive_13x.raw")
    parsed = Parser(PKG_DATA / "tfsm_templates.db").parse(
        "juniper_junos", "show system processes extensive | no-more", raw, template=TPL)
    assert parsed.error is None, parsed.error
    r = Pipeline(WIDGETS["system"]).apply(parsed.records, 0.0)[0]
    assert r["load1"] == "0.85" and r["tasks"] == "140" and r["zombies"] == "1"
    assert r["mem_used"] == "1121M" and r["mem_free"] == "74M"
    assert r["cpu_idle"] == "60.21"                     # no CPU line on 13.x -> idle process WCPU
    procs = Pipeline(WIDGETS["top_procs"]).apply(parsed.records, 0.0)
    by = {p["command"]: p for p in procs if not p["command"].startswith("idle")}
    assert by["mib2d"]["rss"] == "27032K"               # RES, not the SIZE column
    assert "swi2: netisr 0" in {p["command"] for p in procs}   # spaces-in-command


def test_junos_qfx5100_plain_layout():
    # QFX5100 (Junos 21.4 on the old FreeBSD base): no C and no THR column,
    # "processes" line, no CPU line, STATE values with spaces ("PCI Sc",
    # "long p") and "-". Parsed via the Process_Plain header path.
    raw = fixture_text("juniper_junos_show_system_processes_extensive_qfx.raw")
    parsed = Parser(PKG_DATA / "tfsm_templates.db").parse(
        "juniper_junos", "show system processes extensive | no-more", raw, template=TPL)
    assert parsed.error is None, parsed.error
    assert len(parsed.records) == 18
    r = Pipeline(WIDGETS["system"]).apply(parsed.records, 0.0)[0]
    assert r["load1"] == "1.04" and r["load5"] == "0.79"
    assert r["tasks"] == "172" and r["zombies"] == "1"
    assert r["mem_used"] == "982M" and r["mem_free"] == "130M"
    procs = Pipeline(WIDGETS["top_procs"]).apply(parsed.records, 0.0)
    by = {p["command"]: p for p in procs}
    assert by["fxpc"]["rss"] == "591M"
    assert "PCI Scan Thread" in by and "wkupdaemon" in by and "g_down" in by


@pytest.mark.parametrize("fixture,idle,busy", [
    ("juniper_junos_show_system_processes_extensive_qfx.raw", "68.21", 31.79),
    ("juniper_junos_show_system_processes_extensive_13x.raw", "60.21", 39.79),
    ("juniper_junos_show_system_processes_extensive.raw", "97.3", 2.7),   # CPU line wins
])
def test_system_cpu_without_cpu_line(fixture, idle, busy):
    parsed = Parser(PKG_DATA / "tfsm_templates.db").parse(
        "juniper_junos", "show system processes extensive | no-more",
        fixture_text(fixture), template=TPL)
    r = Pipeline(WIDGETS["system"]).apply(parsed.records, 0.0)[0]
    assert r["cpu_idle"] == idle
    assert r["cpu_busy"] == pytest.approx(busy)
