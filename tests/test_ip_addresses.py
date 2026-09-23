from pathlib import Path

from terminaltelemetry2.parsing import Parser
from terminaltelemetry2.paths import PKG_DATA
from terminaltelemetry2.widgets import load_widgets
from terminaltelemetry2.widgets.pipeline import Pipeline

FIX = Path(__file__).parent / "fixtures"
WIDGETS, ERRS = load_widgets([PKG_DATA / "widgets"])
PARSER = Parser(PKG_DATA / "tfsm_templates.db", [PKG_DATA / "templates"])


def rows(platform, command, raw):
    w = WIDGETS["ip_addresses"]
    parsed = PARSER.parse(platform, command, raw, w.template_for(platform))
    assert parsed.error is None, parsed.error
    return [(r["intf"], r["family"], r["address"], r["subnet"], r["status"])
            for r in Pipeline(w).apply(parsed.records, 0.0)]


def test_loads():
    assert not ERRS


def test_eos_fixture_v4():
    got = rows("arista_eos", "show interfaces", (FIX / "arista_eos_show_interfaces.raw").read_text())
    assert ("Ethernet1", "Internet", "172.16.1.1/24", "", "up") in got
    assert len(got) == 4


def test_eos_v6_global_not_link_local():
    raw = """Ethernet2 is up, line protocol is up (connected)
  Hardware is Ethernet, address is 7483.ef37.8b31
  Description: PNI:EXAMPLE:AS64500:00000001
  Internet address is 198.51.100.23/31
  Broadcast address is 255.255.255.255
  IPv6 link-local address is fe80::7683:efff:fe37:8b31/64
  IPv6 global unicast address(es):
    2001:db8:100:a::b, subnet is 2001:db8:100:a::a/127
  IP MTU 9202 bytes (default), Ethernet MRU 10200 bytes, BW 10000000 kbit
Ethernet3 is down, line protocol is notpresent (notconnect)
  Hardware is Ethernet, address is 7483.ef37.8b34
"""
    assert rows("arista_eos", "show interfaces", raw) == [
        ("Ethernet2", "Internet", "198.51.100.23/31", "", "up"),
        ("Ethernet2", "IPv6", "2001:db8:100:a::b", "2001:db8:100:a::a/127", "up"),
    ]


def test_ios_fixture():
    got = rows("cisco_ios", "show interfaces", (FIX / "cisco_ios_show_interfaces.raw").read_text())
    assert any(a == "10.255.0.16/16" and f == "Internet" for _, f, a, _, _ in got)


def test_nxos_capital_a_and_bare_status():
    raw = """Vlan10 is up, line protocol is up, autostate enabled
  Hardware is EtherSVI, address is  00de.fb12.3456
  Internet Address is 10.10.10.1/24
Ethernet1/1 is up
admin state is up, Dedicated Interface
  Internet Address is 192.0.2.1/31
"""
    assert rows("cisco_nxos", "show interface", raw) == [
        ("Vlan10", "Internet", "10.10.10.1/24", "", "up"),
        ("Ethernet1/1", "Internet", "192.0.2.1/31", "", "up"),
    ]


def test_junos_terse_families_and_internal_noise():
    raw = """Interface               Admin Link Proto    Local                 Remote
pfe-0/0/0.16383         up    up   inet
                                   inet6
pfh-0/0/0.16383         up    up   inet     10.0.0.1/8
xe-0/0/0.1001           up    up   aenet    --> ae0.1001
ae0.13                  up    up   inet     203.0.113.234/31
                                   inet6    2001:db8:0:1009:0:1:4:1/112
                                            fe80::5e5e:ab00:d01:47c0/64
                                   multiservice
bme0.0                  up    up   inet     128.0.0.1/2
                                            128.0.0.4/2
lo0.0                   up    up   inet     203.0.113.253      --> 0/0
lo0.16384               up    up   inet     127.0.0.1           --> 0/0
xe-0/0/9.0              up    down inet     192.0.2.9/31
"""
    got = rows("juniper_junos", "show interfaces terse", raw)
    assert got == [
        ("ae0.13", "inet", "203.0.113.234/31", "", "up"),
        ("ae0.13", "inet6", "2001:db8:0:1009:0:1:4:1/112", "", "up"),
        ("lo0.0", "inet", "203.0.113.253", "", "up"),
        ("xe-0/0/9.0", "inet", "192.0.2.9/31", "", "down"),
    ]


def test_ifaddr_templates_stay_out_of_the_auto_sweep():
    # Interface Up/Down resolves 'show interfaces' under AUTO; the sweep matches
    # names containing every term, so ours must lack at least one.
    for name in ("arista_eos_ifaddr", "cisco_ios_ifaddr", "cisco_nxos_ifaddr"):
        assert "show" not in name and "interface" not in name


def test_shared_command_widgets_keep_their_own_templates():
    # Regression: the pin cache was keyed on (platform, command). IP Addresses
    # (explicit arista_eos_ifaddr) pinned 'show interfaces', and Interface
    # Up/Down (AUTO) then parsed with that pin -> one row per address.
    p = Parser(PKG_DATA / "tfsm_templates.db", [PKG_DATA / "templates"])
    raw = (FIX / "arista_eos_show_interfaces.raw").read_text()
    a = p.parse("arista_eos", "show interfaces", raw, WIDGETS["ip_addresses"].template_for("arista_eos"))
    b = p.parse("arista_eos", "show interfaces", raw, WIDGETS["intf_updown"].template_for("arista_eos"))
    assert a.template == "arista_eos_ifaddr"
    assert b.template == "arista_eos_show_interfaces"
    # and again with both pins in place, in the other order
    b = p.parse("arista_eos", "show interfaces", raw, "auto")
    a = p.parse("arista_eos", "show interfaces", raw, "arista_eos_ifaddr")
    assert (a.template, b.template) == ("arista_eos_ifaddr", "arista_eos_show_interfaces")
    assert p.pinned("arista_eos", "show interfaces") == "arista_eos_show_interfaces"
    assert p.pinned("arista_eos", "show interfaces", "arista_eos_ifaddr") == "arista_eos_ifaddr"
