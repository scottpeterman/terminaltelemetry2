"""Platform packs. The hp_comware tests are the zero-code contract: a vendor
added by YAML alone, resolved through templates already in the DB, verified
against ntc-templates' own Comware captures (tests/fixtures/hp_comware,
Apache-2.0, networktocode/ntc-templates)."""
from pathlib import Path

import pytest

from conftest import PKG_DB
from terminaltelemetry2.monitor import COUNTER_COMMANDS
from terminaltelemetry2.parsing import Parser
from terminaltelemetry2.paths import PKG_DATA
from terminaltelemetry2.platforms import PackError, Platforms, load_platforms, parse_pack
from terminaltelemetry2.session import SHELL_PRIME, SHELL_PRIMES
from terminaltelemetry2.sessions import SessionEntry, normalize_platform
from terminaltelemetry2.widgets import load_widgets
from terminaltelemetry2.widgets.pipeline import Pipeline

HPE = Path(__file__).parent / "fixtures" / "hp_comware"
BUNDLED = ["arista_eos", "cisco_ios", "cisco_nxos", "hp_comware", "juniper_junos", "linux"]
TESTED = ["arista_eos", "cisco_ios", "cisco_nxos", "juniper_junos", "linux"]


@pytest.fixture(scope="module")
def packs():
    reg, errors = load_platforms([PKG_DATA / "platforms"])
    assert errors == []
    return reg


@pytest.fixture(scope="module")
def widgets():
    w, errors = load_widgets([PKG_DATA / "widgets"])
    assert errors == []
    return w


@pytest.fixture(scope="module")
def parser():
    return Parser(PKG_DB, [PKG_DATA / "templates"])


def _rows(parser, widgets, packs, name, raw_file):
    w = packs.apply(widgets, "hp_comware")[name]
    cmd = w.command_for("hp_comware")
    parsed = parser.parse("hp_comware", cmd, (HPE / raw_file).read_text(), w.template_for("hp_comware"))
    assert parsed.error is None, parsed.error
    return parsed, Pipeline(w).apply(parsed.records, 0.0)


# -- bundled packs ---------------------------------------------------------------

def test_bundled_packs_load(packs):
    assert set(BUNDLED) <= set(packs.packs) and len(packs.packs) >= 40
    assert sorted(p for p, v in packs.packs.items() if v.tested) == TESTED
    assert packs.conflicts == []


def test_legacy_tables_match_packs(packs):
    for plat, cmd in COUNTER_COMMANDS.items():
        c = packs.get(plat).counters
        assert (c.command, c.parser) == (cmd, "builtin")
    assert SHELL_PRIMES[packs.get("linux").shell] == SHELL_PRIME["linux"]


@pytest.mark.parametrize("alias,plat", [
    ("eos", "arista_eos"), ("IOS-XE", "cisco_ios"), ("nx-os", "cisco_nxos"),
    ("junos", "juniper_junos"), ("rocky", "linux"), ("comware", "hp_comware"),
    ("H3C", "hp_comware"), ("something_else", "something_else"),
])
def test_aliases(packs, alias, plat):
    assert packs.normalize(alias) == plat


def test_normalize_platform_uses_registry():
    assert normalize_platform("comware") == "hp_comware"


@pytest.mark.parametrize("vendor,model,plat", [
    ("Cisco", "N9K-C93180YC-FX", "cisco_nxos"),
    ("cisco", "C9300-48P", "cisco_ios"),              # vendor default: no model rules
    ("Arista Networks", "DCS-7050", "arista_eos"),
    ("HPE", "5940 48SFP+", "hp_comware"),
    ("Juniper", "QFX5120", "juniper_junos"),
    ("Dell", "S5248", None),
])
def test_vendor_model_inference(packs, vendor, model, plat):
    assert packs.guess(vendor=vendor, model=model) == plat


def test_session_entry_guess_honors_known(packs):
    e = SessionEntry("f", "n", "h", vendor="HPE", model="5940")
    assert e.guess_platform(["hp_comware", "arista_eos"]) == "hp_comware"
    assert e.guess_platform(["arista_eos"]) is None


def test_known_includes_pack_only_platform(packs, widgets):
    known = packs.known(widgets)
    assert "hp_comware" in known and set(BUNDLED) <= set(known)


# -- specialization --------------------------------------------------------------

def test_existing_platforms_unchanged(packs, widgets):
    for plat in ("arista_eos", "cisco_ios", "cisco_nxos", "juniper_junos", "linux"):
        assert packs.apply(widgets, plat) == widgets


def test_binding_overrides_and_prepends_aliases(packs, widgets):
    w = packs.apply(widgets, "hp_comware")["port_status"]
    assert w.command_for("hp_comware") == "display interface brief"
    assert w.fields["status"][0] == "LINK" and "STATUS" in w.fields["status"]
    assert w.command_for("arista_eos") == widgets["port_status"].command_for("arista_eos")
    assert widgets["port_status"].fields["status"][0] != "LINK"     # original untouched


def test_unbound_widgets_absent_on_new_platform(packs, widgets):
    spec = packs.apply(widgets, "hp_comware")
    assert spec["bgp_peers"].command_for("hp_comware") is None


def test_null_binding_removes_inline_command(widgets):
    reg = Platforms([parse_pack({"platform": "arista_eos", "bindings": {"bgp_peers": None}})])
    assert reg.apply(widgets, "arista_eos")["bgp_peers"].command_for("arista_eos") is None


def test_new_command_drops_stale_inline_template_and_requires(widgets):
    reg = Platforms([parse_pack({"platform": "linux",
                                 "bindings": {"bgp_peers": "vtysh -c 'show bgp summary'"}})])
    w = reg.apply(widgets, "linux")["bgp_peers"]
    assert w.template_for("linux") == "auto" and w.requires_for("linux") is None


def test_bad_bindings_warn_not_fail(widgets):
    reg = Platforms([parse_pack({"platform": "x_os", "bindings": {
        "nope": "show nope", "port_status": {"command": "show p", "fields": {"zzz": ["A"]}}}})])
    warn = []
    out = reg.apply(widgets, "x_os", warnings=warn)
    assert out["port_status"].command_for("x_os") == "show p"
    assert len(warn) == 2


@pytest.mark.parametrize("data,msg", [
    ({"platform": "Bad-Name"}, "lowercase"),
    ({"platform": "a_b", "counters": {"command": "show x"}}, "{intf}"),
    ({"platform": "a_b", "counters": {"command": "show {intf}", "rx": "(\\d+)"}}, "rx and tx"),
    ({"platform": "a_b", "counters": {"command": "show {intf}", "rx": "\\d+", "tx": "(\\d+)"}},
     "capture group"),
    ({"platform": "a_b", "session": {"shell": "fish"}}, "session.shell"),
    ({"platform": "a_b", "bindings": {"w": {"template": "py:nope"}}}, "python parser"),
    ({"platform": "a_b", "bindings": {"w": {"requires": "true"}}}, "requires needs a command"),
    ({"platform": "a_b", "bindings": {"w": {"comand": "x"}}}, "unknown keys"),
])
def test_pack_validation(data, msg):
    with pytest.raises(PackError, match=msg):
        parse_pack(data)


def test_user_pack_replaces_bundled(tmp_path):
    (tmp_path / "eos.yaml").write_text("platform: arista_eos\naliases: [ceos]\n")
    reg, errors = load_platforms([PKG_DATA / "platforms", tmp_path])
    assert errors == [] and reg.normalize("ceos") == "arista_eos"
    assert reg.normalize("eos") == "eos"                            # bundled pack replaced whole
    assert reg.get("arista_eos").counters is None


# -- HPE Comware, end to end, no code -------------------------------------------

def test_hpe_lldp(parser, widgets, packs):
    parsed, rows = _rows(parser, widgets, packs, "lldp_neighbors",
                         "hp_comware_display_lldp_neighbor-information_list.raw")
    assert parsed.template == "hp_comware_display_lldp_neighbor-information_list"
    assert rows and all(r["local"] and r["neighbor"] for r in rows)


def test_hpe_port_status(parser, widgets, packs):
    _, rows = _rows(parser, widgets, packs, "port_status", "hp_comware_display_interface_brief.raw")
    assert rows and all(r["port"] and r["status"] for r in rows)
    assert any(r["vlan"] for r in rows)


def test_hpe_intf_updown(parser, widgets, packs):
    _, rows = _rows(parser, widgets, packs, "intf_updown", "hp_comware_display_interface.raw")
    assert rows and all(r["intf"] and r["link"] for r in rows)


def test_hpe_version(parser, widgets, packs):
    _, rows = _rows(parser, widgets, packs, "version", "hp_comware_display_device_manuinfo.raw")
    assert rows[0]["model"] and rows[0]["serial"]


def test_hpe_counters_physical_and_l3(packs):
    c = packs.get("hp_comware").counters
    assert c.parse((HPE / "hp_comware_display_interface1.raw").read_text()) == \
        (1485686536611046, 1670292936644909)                        # "Input (total):"
    l3 = "Vlan-interface2000\nInput: 9103 packets, 611444 bytes, 0 drops\n" \
         "Output: 18587 packets, 1143610 bytes, 0 drops\n"
    assert c.parse(l3) == (611444, 1143610)
    with pytest.raises(ValueError, match="Wrong parameter"):
        c.parse("  ^\n % Wrong parameter found at '^' position.\n")


@pytest.mark.parametrize("buf,prompt", [
    ("banner\r\n<sw1>", "<sw1>"), ("x\n<HPE-5940-core>", "<HPE-5940-core>"),
    ("rtr1#", "rtr1#"), ("rtr1>", "rtr1>"), ("junos@r1>", "junos@r1>"),
])
def test_prompt_extraction_keeps_angle_brackets(buf, prompt):
    from terminaltelemetry2.ssh.client import SSHClient
    c = SSHClient.__new__(SSHClient)
    assert c._extract_prompt(buf) == prompt
    assert c._strip_echo_and_prompt(f"cmd\nout\n{prompt}", "cmd", prompt) == "out"


# -- session: paging forms, username suffix, ambiguity, checks ---------------------

def test_paging_forms():
    one = parse_pack({"platform": "a_b", "session": {"paging": "terminal length 0"}})
    seq = parse_pack({"platform": "a_b", "session": {"paging": ["x", "y"]}})
    none = parse_pack({"platform": "a_b", "session": {"paging": []}})
    shotgun = parse_pack({"platform": "a_b"})
    assert one.paging_config == "terminal length 0"
    assert seq.paging_config == ["x", "y"]
    assert none.paging_config == [] and shotgun.paging_config is None


def test_fortinet_and_mikrotik_packs(packs):
    assert packs.get("fortinet").paging_config[:3] == ["config global", "config system console",
                                                      "set output standard"]
    m = packs.get("mikrotik_routeros")
    assert m.paging_config == [] and m.username_suffix == "+ct511w4098h"


def test_build_ssh_config_applies_pack(packs):
    import argparse
    from terminaltelemetry2.app import build_ssh_config
    from terminaltelemetry2.platforms import set_registry
    from terminaltelemetry2.sessions import ConnectTarget
    set_registry(packs)
    try:
        args = argparse.Namespace(enable_command=None, paging_command=None, legacy_ssh=False)
        t = ConnectTarget(host="h", port=22, username="admin", password="p",
                          platform="mikrotik_routeros")
        cfg = build_ssh_config(t, args)
        assert cfg.username == "admin+ct511w4098h" and cfg.paging_disable_command == []
        args.paging_command = "no page"
        assert build_ssh_config(t, args).paging_disable_command == "no page"    # flag wins
    finally:
        set_registry(None)


def test_ambiguous_vendor_is_not_guessed():
    reg = Platforms([parse_pack({"platform": "x_one", "match": {"vendor": ["acme"]}}),
                     parse_pack({"platform": "x_two", "match": {"vendor": ["acme"]}})])
    assert reg.guess(vendor="Acme", model="z") is None


def test_alias_conflicts_recorded():
    reg = Platforms([parse_pack({"platform": "x_one", "aliases": ["dup", "x_two"]}),
                     parse_pack({"platform": "x_two", "aliases": ["dup"]})])
    assert len(reg.conflicts) == 2 and reg.normalize("x_two") == "x_two"


def test_check_report(packs, widgets, tmp_path):
    from terminaltelemetry2.platforms import check_report
    text, ok = check_report(packs, [], widgets, {"hp_comware": 16, "zyxel_os": 13})
    assert ok and "hp_comware" in text and "zyxel_os" in text
    (tmp_path / "bad.yaml").write_text("platform: Bad Name\n")
    (tmp_path / "ok.yaml").write_text("platform: x_os\nbindings: {nope: show nope}\n")
    reg, errors = load_platforms([tmp_path])
    text, ok = check_report(reg, errors, widgets, {})
    assert not ok and "load error" in text and "no such widget" in text


def test_check_platforms_cli_exit_code(tmp_path, monkeypatch):
    from terminaltelemetry2 import app
    monkeypatch.setenv("TERMINALTELEMETRY2_HOME", str(tmp_path))
    assert app.main(["--check-platforms"]) == 0
    (tmp_path / "platforms").mkdir(exist_ok=True)
    (tmp_path / "platforms" / "broken.yaml").write_text("platform: [not, a, string]\n")
    assert app.main(["--check-platforms"]) == 1


def test_read_timeout(packs):
    assert packs.get("linux").read_timeout == 30
    assert packs.get("arista_eos").read_timeout is None
    assert parse_pack({"platform": "x_os", "session": {"read_timeout": 12}}).read_timeout == 12
    with pytest.raises(PackError, match="read_timeout"):
        parse_pack({"platform": "x_os", "session": {"read_timeout": 0}})
    import argparse
    from terminaltelemetry2.app import build_ssh_config
    from terminaltelemetry2.platforms import set_registry
    from terminaltelemetry2.sessions import ConnectTarget
    set_registry(packs)
    try:
        args = argparse.Namespace(enable_command=None, paging_command=None, legacy_ssh=False)
        lx = build_ssh_config(ConnectTarget(host="h", username="u", password="p", platform="linux"), args)
        eos = build_ssh_config(ConnectTarget(host="h", username="u", password="p", platform="arista_eos"), args)
        assert lx.expect_prompt_timeout == 30000 and eos.expect_prompt_timeout == 3000
    finally:
        set_registry(None)


@pytest.mark.parametrize("cmd", ["configure", "configure terminal", "conf t", "config",
                                 "system-view", "configure private", "edit"])
def test_enable_rejects_config_mode(cmd):
    with pytest.raises(PackError, match="configuration mode"):
        parse_pack({"platform": "x_os", "session": {"enable": cmd}})


@pytest.mark.parametrize("cmd", ["enable", "enable 15", "en"])
def test_enable_accepts_privileged_exec(cmd):
    assert parse_pack({"platform": "x_os", "session": {"enable": cmd}}).enable == cmd
