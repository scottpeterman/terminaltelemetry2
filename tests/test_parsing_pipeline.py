from conftest import PKG_DB, fixture_text

from terminaltelemetry2.parsing import Parser
from terminaltelemetry2.paths import PKG_DATA
from terminaltelemetry2.widgets import Pipeline, aggregate, load_widgets, row_style

WIDGETS, _ = load_widgets([PKG_DATA / "widgets"])
BGP = fixture_text("arista_eos_show_ip_bgp_summary.raw")
BGP_DOWN = BGP.replace("02:59:41 7", "00:00:12 Active")
CMD = "show ip bgp summary"


def test_exact_then_pinned(parser):
    first = parser.parse("arista_eos", CMD, BGP)
    assert first.method in ("exact", "pinned") and first.template == "arista_eos_show_ip_bgp_summary"
    assert len(first.records) == 2
    second = parser.parse("arista_eos", CMD, BGP)
    assert second.method == "pinned" and second.records == first.records


def test_stock_template_fails_on_down_peer_and_no_vendor_guess():
    stock = Parser(PKG_DB)                       # no overrides
    r = stock.parse("arista_eos", CMD, BGP_DOWN)
    assert not r.records and r.error             # error, not a wrong table
    guess = Parser(PKG_DB, vendor_fallback=True).parse("arista_eos", CMD, BGP_DOWN)
    assert guess.method == "vendor" and guess.template != "arista_eos_show_ip_bgp_summary"


def test_override_parses_down_peer(parser):
    r = parser.parse("arista_eos", CMD, BGP_DOWN, "arista_eos_show_ip_bgp_summary")
    assert [x["STATE"] for x in r.records] == ["", "Active"]


def test_explicit_template_and_garbage(parser):
    r = parser.parse("arista_eos", CMD, BGP, "arista_eos_show_ip_bgp_summary")
    assert r.method == "explicit" and len(r.records) == 2
    bad = parser.parse("arista_eos", "show nothing", "% Invalid input detected at '^' marker.")
    assert not bad.records and bad.error


def test_bgp_widget_mapping_alerts_and_stat(parser):
    recs = parser.parse("arista_eos", CMD, BGP_DOWN).records
    rows = Pipeline(WIDGETS["bgp_peers"]).apply(recs, 0.0)
    assert [r["peer"] for r in rows] == ["10.17.254.78", "10.17.254.2"]
    assert rows[0]["state"] == "7" and rows[1]["state"] == "Active"
    alerts = WIDGETS["bgp_peers"].view.alerts
    assert row_style(alerts, rows[1]) == "alert" and row_style(alerts, rows[0]) is None
    assert row_style(alerts, {"state": "Establ"}) is None      # Junos
    stat = WIDGETS["bgp_down"]
    assert aggregate(Pipeline(stat).apply(recs, 0.0), stat.view) == 1.0


def test_rates_and_counter_clear():
    p = Pipeline(WIDGETS["intf_counters"])
    rec = lambda n, e: [{"INTERFACE": "Gi0/1", "LINK_STATUS": "up", "INPUT_PACKETS": str(n),
                         "OUTPUT_PACKETS": "0", "INPUT_ERRORS": str(e), "OUTPUT_ERRORS": "0"}]
    assert p.apply(rec(1000, 0), 100.0)[0]["in_pps"] is None           # no history yet
    r = p.apply(rec(4000, 30), 130.0)[0]
    assert r["in_pps"] == 100.0 and r["in_err_ps"] == 1.0
    assert p.apply(rec(10, 0), 160.0)[0]["in_pps"] is None             # cleared counters
    assert p.apply(rec(310, 0), 190.0)[0]["in_pps"] == 10.0


def test_ios_interfaces_parse_feeds_counters(parser):
    r = parser.parse("cisco_ios", "show interfaces", fixture_text("cisco_ios_show_interfaces.raw"))
    rows = Pipeline(WIDGETS["intf_counters"]).apply(r.records, 0.0)
    assert r.template == "cisco_ios_show_interfaces"
    assert rows and rows[0]["intf"].startswith("GigabitEthernet") and rows[0]["in_pkts"] == "324"
