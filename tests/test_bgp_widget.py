"""BGP widget schema against the custom arista_eos_show_bgp_summary fields.

The template parses fine (NEIGHBOR, REMOTE_AS, UP_DOWN, STATE="Estab",
PFX_RCVD). The bug was the widget: aliases named the stock ntc fields
(BGP_NEIGH / NEIGH_AS), so Neighbor and AS rendered blank, and the alert regex
was ^Establ, which the abbreviated "Estab" never matches, so every peer was
flagged down. These lock the field mapping and the established/down split.
"""
from terminaltelemetry2.paths import PKG_DATA
from terminaltelemetry2.widgets import Pipeline, load_widgets
from terminaltelemetry2.widgets.pipeline import aggregate
from terminaltelemetry2.widgets.rules import row_style

WIDGETS, ERRORS = load_widgets([PKG_DATA / "widgets"])

# records shaped like the custom template's output: two established (abbreviated
# "Estab"), one down, and one stock-style numeric prefix-count state.
RECS = [
    {"NEIGHBOR": "198.51.100.192",  "REMOTE_AS": "64501", "UP_DOWN": "221d22h",  "STATE": "Estab", "PFX_RCVD": "25"},
    {"NEIGHBOR": "203.0.113.66", "REMOTE_AS": "64502", "UP_DOWN": "320d05h",  "STATE": "Estab", "PFX_RCVD": "2"},
    {"NEIGHBOR": "10.0.0.9",      "REMOTE_AS": "65001", "UP_DOWN": "00:00:12", "STATE": "Idle",  "PFX_RCVD": "0"},
    {"NEIGHBOR": "10.0.0.10",     "REMOTE_AS": "65002", "UP_DOWN": "1d02h",    "STATE": "140"},   # numeric = up
]


def test_bgp_widgets_load_clean():
    assert ERRORS == []
    assert "bgp_peers" in WIDGETS and "bgp_down" in WIDGETS


def test_bgp_peers_columns_populate():
    defn = WIDGETS["bgp_peers"]
    rows = Pipeline(defn).apply(RECS, 0.0)
    by = {r["peer"]: r for r in rows}
    assert set(by) == {"198.51.100.192", "203.0.113.66", "10.0.0.9", "10.0.0.10"}  # NEIGHBOR -> peer
    assert by["198.51.100.192"]["asn"] == "64501"       # REMOTE_AS -> asn (was blank)
    assert by["198.51.100.192"]["up_down"] == "221d22h"
    assert by["198.51.100.192"]["state"] == "Estab"


def test_bgp_alert_only_on_down_peers():
    defn = WIDGETS["bgp_peers"]
    rows = Pipeline(defn).apply(RECS, 0.0)
    styles = {r["peer"]: row_style(defn.view.alerts, r) for r in rows}
    assert styles["198.51.100.192"] is None      # Estab -> not flagged
    assert styles["203.0.113.66"] is None
    assert styles["10.0.0.10"] is None          # numeric prefix count -> up
    assert styles["10.0.0.9"] == "alert"        # Idle -> flagged


def test_bgp_down_counts_only_unestablished():
    defn = WIDGETS["bgp_down"]
    rows = Pipeline(defn).apply(RECS, 0.0)
    assert aggregate(rows, defn.view) == 1.0    # only the Idle peer


JUNOS_SUMMARY = """\
Groups: 1 Peers: 2 Down peers: 1
Table          Tot Paths  Act Paths Suppressed    History Damp State    Pending
inet.0
                       0          0          0          0          0          0
Peer                     AS      InPkt     OutPkt    OutQ   Flaps Last Up/Dwn State|#Active/Received/Accepted/Damped...
192.0.2.2             64501       2954       3592       0       0  1d 2:55:25 0/0/0/0              0/0/0/0
192.0.2.9             64502          0          0       0       0        5:01 Active
"""


def test_junos_single_rib_established_is_not_down():
    from terminaltelemetry2.parsing import Parser
    recs = Parser(PKG_DATA / "tfsm_templates.db").parse(
        "juniper_junos", "show bgp summary", JUNOS_SUMMARY).records
    defn = WIDGETS["bgp_peers"]
    rows = {r["peer"]: r for r in Pipeline(defn).apply(recs, 0.0)}
    assert rows["192.0.2.2"]["up_down"] == "1d 2:55:25"          # LAST_UP_DOWN alias
    assert row_style(defn.view.alerts, rows["192.0.2.2"]) is None  # counts in State = Established
    assert row_style(defn.view.alerts, rows["192.0.2.9"]) == "alert"
    down = WIDGETS["bgp_down"]
    assert aggregate(Pipeline(down).apply(recs, 0.0), down.view) == 1
