import pytest

from conftest import PKG_DB
from terminaltelemetry2 import packbuilder as pb
from terminaltelemetry2.parsing import Parser
from terminaltelemetry2.parsing.store import TemplateStore
from terminaltelemetry2.paths import PKG_DATA
from terminaltelemetry2.platforms import Platforms, load_platforms, parse_pack
from terminaltelemetry2.widgets import load_widgets

TESTED = ("arista_eos", "cisco_ios", "cisco_nxos", "juniper_junos")


@pytest.fixture(scope="module")
def W():
    w, _ = load_widgets([PKG_DATA / "widgets"])
    return w


@pytest.fixture(scope="module")
def store():
    return TemplateStore(PKG_DB)


@pytest.fixture(scope="module")
def parser():
    return Parser(PKG_DB)


def _top(store, plat, w):
    c = [x for x in pb.suggest_templates(store, plat, w, limit=3) if x.confident]
    return c[0] if c else None


# -- tokens -------------------------------------------------------------------------

def test_command_tokens():
    assert pb.command_tokens("display lldp neighbor-information list") == \
        {"lldp", "neighbor", "information", "list"}
    assert pb.command_tokens("show system processes extensive | no-more") == \
        {"system", "process", "extensive"}
    assert pb.command_tokens("show interfaces") == pb.command_tokens("show interface")


# -- ranking: blind test against the bindings that already work ------------------

def test_blind_ranking_recovers_existing_bindings(W, store):
    """Hide each tested platform's own binding; the engine must rank the
    template actually in use first nearly always, and top-3 always."""
    ranks = []
    for plat in TESTED:
        for name, w in W.items():
            cmd = w.commands.get(plat)
            if not cmd:
                continue
            t = w.templates.get(plat) or Parser.exact_template(plat, cmd)
            if t.startswith("py:") or t.endswith("_ifaddr") or store.get(t) is None:
                continue
            names = [c.template for c in pb.suggest_templates(store, plat, w, limit=10)]
            ranks.append(names.index(t) + 1 if t in names else 99)
    assert len(ranks) >= 40
    assert sum(r == 1 for r in ranks) >= len(ranks) - 3
    assert all(r <= 4 for r in ranks)


def test_rebuilds_hand_written_comware_pack(W, store):
    lldp = _top(store, "hp_comware", W["lldp_neighbors"])
    assert lldp.template == "hp_comware_display_lldp_neighbor-information_list"
    assert {f for f, m in lldp.mapping.items() if m.how == "exact"} >= {"local", "neighbor", "remote"}
    up = _top(store, "hp_comware", W["intf_updown"])
    assert up.template == "hp_comware_display_interface"
    assert up.mapping["link"].value == "LINE_STATUS"          # the overlay written by hand
    ports = _top(store, "hp_comware", W["port_status"])
    assert ports.template == "hp_comware_display_interface_brief"
    assert ports.mapping["status"].value == "LINK"            # the overlay written by hand


def test_no_confident_guess_when_nothing_fits(W, store):
    assert _top(store, "hp_comware", W["bgp_peers"]) is None
    assert _top(store, "hp_comware", W["version"]) is None     # no display version template


def test_off_topic_commands_never_confident(W, store):
    for plat in ("fortinet", "vmware_nsxv", "ubiquiti_edgerouter"):
        t = _top(store, plat, W["ospf_down"])
        assert t is None or "ospf" in t.command


# -- field mapping guards ---------------------------------------------------------

def test_fuzzy_never_crosses_direction(W):
    m = pb.suggest_field_map(W["intf_util"], ["ONEMIN_OUT_RATE", "INTERFACE"])
    assert m["in_rate"].value is None


def test_fuzzy_rejects_discriminating_qualifiers(W):
    m = pb.suggest_field_map(W["ip_addresses"], ["HW_ADDRESS", "INTERFACE"])
    assert m["address"].value is None                          # a MAC is not an IP
    m = pb.suggest_field_map(W["version"], ["DEVICE_SERIAL_NUMBER"])
    assert (m["serial"].value, m["serial"].how) == ("DEVICE_SERIAL_NUMBER", "fuzzy")


def test_exact_follows_widget_alias_order(W):
    m = pb.suggest_field_map(W["port_status"], ["INTERFACE", "PORT"])
    assert m["port"].value == "PORT"                           # PORT is first in the widget


# -- bindings -------------------------------------------------------------------------

def test_make_binding_minimal(W):
    w = W["intf_updown"]
    vals = ["INTERFACE", "LINE_STATUS", "PROTOCOL_STATUS", "DESCRIPTION"]
    b = pb.make_binding(w, "hp_comware", "display interface", "hp_comware_display_interface",
                        vals, {"intf": "INTERFACE", "link": "LINE_STATUS", "proto": "PROTOCOL_STATUS"})
    assert b == {"command": "display interface", "fields": {"link": ["LINE_STATUS"]}}


def test_make_binding_keeps_template_auto_cannot_find(W):
    b = pb.make_binding(W["lldp_neighbors"], "hp_comware", "display lldp neighbor-information list",
                        "hp_comware_display_lldp_neighbor-information_list", [], {})
    assert b["template"] == "hp_comware_display_lldp_neighbor-information_list"


def test_confident_suggestions_preview_with_rows(W, store, parser):
    """End to end offline: confident suggestion -> binding -> the widget's
    own pipeline on the DB sample output yields rows."""
    for plat, name in (("cisco_asa", "ospf_neighbors"), ("cisco_asa", "version"),
                       ("huawei_vrp", "lldp_neighbors"), ("fortinet", "bgp_peers"),
                       ("hp_procurve", "lldp_neighbors"), ("mikrotik_routeros", "bgp_peers")):
        top = _top(store, plat, W[name])
        assert top is not None, (plat, name)
        vals = pb.template_values(store.content(top.template))
        b = pb.make_binding(W[name], plat, top.command, top.template, vals,
                            {f: m.value for f, m in top.mapping.items()})
        spec = Platforms([parse_pack({"platform": plat, "bindings": {name: b}})]).apply(W, plat)[name]
        pv = pb.preview(parser, spec, plat, b["command"], b.get("template") or top.template,
                        pb.sample_for(store, top.template, plat, top.command))
        assert pv.error is None and pv.rows, (plat, name, pv.error)


# -- counters -----------------------------------------------------------------------

@pytest.mark.parametrize("plat,preset", [
    ("hp_comware", "Comware"), ("cisco_asa", "Cisco style"), ("huawei_vrp", "Comware"),
])
def test_detect_counters_from_db_samples(store, plat, preset):
    hits = pb.detect_counters(store, plat)
    assert hits and hits[0][2].name.startswith(preset) and hits[0][0].endswith("{intf}")
    assert all(isinstance(v, int) and v >= 0 for v in hits[0][3])   # some samples are idle ports


def test_presets_compile_with_one_group():
    import re
    for p in pb.COUNTER_PRESETS:
        assert re.compile(p.rx).groups == 1 and re.compile(p.tx).groups == 1


# -- drafts ---------------------------------------------------------------------------

def test_every_bundled_pack_roundtrips_through_draft():
    reg, errors = load_platforms([PKG_DATA / "platforms"])
    assert errors == []
    for name, pack in reg.packs.items():
        again = pb.PackDraft.from_pack(pack).validate()
        for attr in ("platform", "aliases", "vendors", "paging", "enable", "username_suffix",
                     "shell", "layout", "tested", "read_timeout"):
            assert getattr(again, attr) == getattr(pack, attr), (name, attr)
        assert [m.pattern for m in again.models] == [m.pattern for m in pack.models]
        assert again.bindings.keys() == pack.bindings.keys()
        for w, b in pack.bindings.items():
            assert again.bindings[w] == b, (name, w)
        if pack.counters:
            assert again.counters.command == pack.counters.command
            assert again.counters.parser == pack.counters.parser
            if pack.counters.parser == "regex":
                assert again.counters.rx.pattern == pack.counters.rx.pattern


def test_draft_yaml_is_loadable(tmp_path):
    d = pb.PackDraft("x_os", title="X", aliases=["xos"], paging=["a", "b"],
                     counters={"command": "show int {intf}", "rx": r"in (\d+)", "tx": r"out (\d+)"},
                     bindings={"version": {"command": "show ver"}, "bgp_peers": None})
    (tmp_path / "x_os.yaml").write_text(d.to_yaml())
    reg, errors = load_platforms([tmp_path])
    assert errors == []
    p = reg.get("x_os")
    assert p.paging == ["a", "b"] and p.bindings["bgp_peers"].removed
    assert p.counters.parse("in 5\nout 7") == (5, 7)
