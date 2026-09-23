"""Junos show interfaces extensive -> physical-port status.

The stock 5-field template misses channelized ports (xe-0/0/0:3) and has no
speed. This custom superset adds the optional :N channel and SPEED, scoped to
transport families. Authored against real MX10003 output.
"""
from conftest import fixture_text
from terminaltelemetry2.parsing import Parser
from terminaltelemetry2.paths import PKG_DATA

RAW = fixture_text("juniper_junos_show_interfaces_extensive.raw")


def _parse():
    return Parser(PKG_DATA / "tfsm_templates.db").parse(
        "juniper_junos", "show interfaces extensive", RAW)


def test_resolves_and_parses_channelized_port():
    r = _parse()
    assert r.error is None, r.error
    assert r.template == "juniper_junos_show_interfaces_extensive"
    by = {rec["INTERFACE"]: rec for rec in r.records}
    # channelized port the stock regex dropped, now captured with speed
    assert "xe-0/0/0:3" in by
    assert by["xe-0/0/0:3"]["LINK_STATUS"] == "Down"
    assert by["xe-0/0/0:3"]["SPEED"] == "10Gbps"
    assert by["xe-0/0/0:3"]["MTU"] == "1514"
    assert by["xe-0/0/0:3"]["DESCRIPTION"] == "Available"


def test_description_clears_between_records():
    r = _parse()
    by = {rec["INTERFACE"]: rec for rec in r.records}
    assert by["et-0/1/1"]["DESCRIPTION"] == ""   # no desc -> empty, not leaked
    assert by["et-0/1/1"]["SPEED"] == "100Gbps"


def test_non_transport_interfaces_skipped():
    r = _parse()
    names = {rec["INTERFACE"] for rec in r.records}
    assert "ae0" not in names                    # aggregate skipped by family scope
