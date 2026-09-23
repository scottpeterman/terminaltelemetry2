"""Linux platform: python parsers, the Parser hook, widget wiring, monitor."""
import json

import pytest

from terminaltelemetry2.monitor import COUNTER_COMMANDS, parse_octets
from terminaltelemetry2.parsing import Parser
from terminaltelemetry2.parsing.pyparsers import PY_PARSERS
from terminaltelemetry2.paths import PKG_DATA
from terminaltelemetry2.widgets import WidgetError, load_widgets, parse_widget
from terminaltelemetry2.widgets.pipeline import Pipeline

WIDGETS, ERRORS = load_widgets([PKG_DATA / "widgets"])
P = lambda name: PY_PARSERS[name]

TOP = """\
top - 10:00:00 up 2 days,  1:00,  1 user,  load average: 0.50, 0.40, 0.30
Tasks: 100 total,   1 running,  99 sleeping,   0 stopped,   0 zombie
%Cpu(s):  0.3 us,  0.1 sy,  0.0 ni, 99.6 id,  0.0 wa,  0.0 hi,  0.0 si,  0.0 st
MiB Mem :   4000.0 total,   3000.0 free,    500.0 used,    500.0 buff/cache
MiB Swap:      0.0 total,      0.0 free,      0.0 used.   3400.0 avail Mem

    PID USER      PR  NI    VIRT    RES    SHR S  %CPU  %MEM     TIME+ COMMAND
      1 root      20   0   21620   4748   4352 S   0.0   0.1   0:00.77 systemd

top - 10:00:01 up 2 days,  1:00,  1 user,  load average: 0.52, 0.41, 0.30
Tasks: 101 total,   2 running,  98 sleeping,   0 stopped,   1 zombie
%Cpu(s): 12.5 us,  2.5 sy,  0.0 ni, 85.0 id,  0.0 wa,  0.0 hi,  0.0 si,  0.0 st
MiB Mem :   4000.0 total,   2900.0 free,    600.0 used,    500.0 buff/cache
MiB Swap:      0.0 total,      0.0 free,      0.0 used.   3300.0 avail Mem

    PID USER      PR  NI    VIRT    RES    SHR S  %CPU  %MEM     TIME+ COMMAND
   4242 app       20   0 1234567  98765   1234 R  88.0   2.4  12:34.56 java -Xmx2g -jar svc.jar
     57 root     -51   0       0      0      0 S   1.0   0.0   0:01.00 irq/57-eth0
"""

LINKS = json.dumps([
    {"ifname": "lo", "flags": ["LOOPBACK", "UP"], "operstate": "UNKNOWN"},
    {"ifname": "eth0", "flags": ["BROADCAST", "UP", "LOWER_UP"], "mtu": 9000,
     "operstate": "UP", "address": "00:00:5e:00:53:01", "ifalias": "uplink to agg1",
     "stats64": {"rx": {"bytes": 1000, "packets": 10, "errors": 0, "dropped": 1},
                 "tx": {"bytes": 2000, "packets": 20, "errors": 3, "dropped": 0}}},
    {"ifname": "eth1", "flags": ["BROADCAST"], "operstate": "DOWN",
     "stats64": {"rx": {"packets": 0}, "tx": {"packets": 0}}},
]) + "\n@cc\n/sys/class/net/eth0/carrier_changes:4\n/sys/class/net/eth1/carrier_changes:0\n"

FRR = json.dumps({
    "ipv4Unicast": {"routerId": "192.0.2.1", "as": 64500, "peers": {
        "192.0.2.10": {"remoteAs": 64501, "state": "Established", "peerUptime": "1d02h03m",
                       "pfxRcd": 12, "desc": "transit-a"},
        "192.0.2.11": {"remoteAs": 64502, "state": "Active", "peerUptime": "never", "pfxRcd": 0},
    }},
    "ipv6Unicast": {"peers": {
        "192.0.2.10": {"remoteAs": 64501, "state": "Established", "pfxRcd": 3},
        "2001:db8::2": {"remoteAs": 64503, "state": "Established", "peerUptime": "00:10:00",
                        "pfxRcd": 7},
    }},
})

LLDP_ONE = json.dumps({"lldp": {"interface": {"eth0": {"via": "LLDP",
    "chassis": {"agg1.lab1": {"id": {"type": "mac", "value": "00:00:5e:00:53:aa"},
                              "capability": [{"type": "Bridge", "enabled": True},
                                             {"type": "Router", "enabled": True},
                                             {"type": "Wlan", "enabled": False}]}},
    "port": {"id": {"type": "ifname", "value": "Ethernet1"}, "descr": "to server"}}}}})

LLDP_MANY = json.dumps({"lldp": {"interface": [
    {"eth0": {"chassis": {"agg1.lab1": {"capability": {"type": "Bridge", "enabled": True}}},
              "port": {"id": {"value": "Ethernet1"}}}},
    {"eth1": {"chassis": {"id": {"type": "mac", "value": "00:00:5e:00:53:bb"}},
              "port": {"id": {"value": "Ethernet2"}}}},
]}})


def test_widgets_load_and_linux_is_a_platform():
    assert ERRORS == []
    linux = {n for n, w in WIDGETS.items() if "linux" in w.commands}
    assert {"version", "system", "top_procs", "intf_updown", "ip_addresses",
            "bgp_peers", "lldp_neighbors", "storage", "containers", "services_failed"} <= linux


def test_unknown_python_parser_rejected_at_load():
    with pytest.raises(WidgetError, match="unknown python parser"):
        parse_widget({"widget": "x", "commands": {"linux": "true"},
                      "templates": {"linux": "py:nope"}, "fields": {"a": ["A"]},
                      "view": {"type": "kv", "show": ["a"]}})


def test_top_reads_last_frame_only():
    s = P("linux_top_summary")(TOP)[0]
    assert s["GLOBAL_CPU_PERCENT_IDLE"] == "85.0" and s["GLOBAL_TASKS_ZOMBIE"] == "1"
    assert s["GLOBAL_LOAD_AVERAGE_1_MINUTES"] == "0.52"
    assert s["GLOBAL_MEM_USED"] == str(600 * 1024)            # MiB -> KiB
    procs = P("linux_top_procs")(TOP)
    assert [p["PID"] for p in procs] == ["4242", "57"]
    assert procs[0]["COMMAND"] == "java -Xmx2g -jar svc.jar" and procs[1]["PRIORITY"] == "-51"


def test_system_widget_computes_busy():
    rows = Pipeline(WIDGETS["system"]).apply(P("linux_top_summary")(TOP), 0.0)
    assert len(rows) == 1 and float(rows[0]["cpu_busy"]) == pytest.approx(15.0)


def test_links_counters_and_flaps():
    recs = P("linux_links")(LINKS)
    by = {r["INTERFACE"]: r for r in recs}
    assert set(by) == {"eth0", "eth1"}                        # lo skipped
    assert by["eth0"]["LINK_STATUS"] == "up" and by["eth0"]["LINK_STATUS_CHANGE"] == "4"
    assert by["eth0"]["DESCRIPTION"] == "uplink to agg1" and by["eth0"]["OUTPUT_ERRORS"] == "3"
    assert by["eth1"]["PROTOCOL_STATUS"] == "admin down"
    p = Pipeline(WIDGETS["intf_counters"])
    p.apply(recs, 0.0)
    bumped = LINKS.replace('"packets": 10,', '"packets": 110,')
    rows = {r["intf"]: r for r in p.apply(P("linux_links")(bumped), 10.0)}
    assert rows["eth0"]["in_pps"] == pytest.approx(10.0)


def test_addrs_and_unknown_operstate():
    raw = json.dumps([
        {"ifname": "lo", "addr_info": [{"family": "inet", "local": "127.0.0.1", "prefixlen": 8}]},
        {"ifname": "wg0", "flags": ["POINTOPOINT", "UP", "LOWER_UP"], "operstate": "UNKNOWN",
         "addr_info": [{"family": "inet", "local": "198.51.100.1", "prefixlen": 31}]},
        {"ifname": "eth0", "operstate": "UP", "addr_info": [
            {"family": "inet6", "local": "2001:db8::5", "prefixlen": 64},
            {"family": "inet6", "local": "fe80::1", "prefixlen": 64}]},
    ])
    rows = Pipeline(WIDGETS["ip_addresses"]).apply(P("linux_addrs")(raw), 0.0)
    got = {(r["intf"], r["address"], r["status"]) for r in rows}
    assert got == {("wg0", "198.51.100.1/31", "up"), ("eth0", "2001:db8::5/64", "up")}


def test_df_mount_with_spaces():
    raw = ("Filesystem     Type 1024-blocks    Used Available Capacity Mounted on\n"
           "/dev/sda1      ext4    1000000  950000     50000      95% /\n"
           "/dev/sdb1      xfs     2000000  100000   1900000       5% /mnt/My Data\n")
    recs = {r["MOUNT"]: r for r in P("linux_df")(raw)}
    assert recs["/"]["USE_PERCENT"] == "95" and "/mnt/My Data" in recs


def test_frr_bgp_dedupes_across_afis():
    recs = P("frr_bgp_summary")(FRR)
    by = {r["NEIGHBOR"]: r for r in recs}
    assert set(by) == {"192.0.2.10", "192.0.2.11", "2001:db8::2"}
    assert by["192.0.2.10"]["PFX_RCVD"] == "12" and by["192.0.2.10"]["AFI"] == "ipv4Unicast"
    rows = Pipeline(WIDGETS["bgp_down"]).apply(recs, 0.0)
    assert {r["peer"] for r in rows} == {"192.0.2.10", "192.0.2.11", "2001:db8::2"}


def test_lldp_single_and_list_shapes():
    one = P("linux_lldp")(LLDP_ONE)
    assert one == [{"LOCAL_INTERFACE": "eth0", "NEIGHBOR_NAME": "agg1.lab1",
                    "NEIGHBOR_INTERFACE": "Ethernet1", "NEIGHBOR_DESCRIPTION": "to server",
                    "CAPABILITIES": "Bridge,Router"}]
    many = {r["LOCAL_INTERFACE"]: r for r in P("linux_lldp")(LLDP_MANY)}
    assert many["eth0"]["NEIGHBOR_NAME"] == "agg1.lab1" and many["eth0"]["CAPABILITIES"] == "Bridge"
    assert many["eth1"]["NEIGHBOR_NAME"] == "00:00:5e:00:53:bb"


def test_docker_and_systemd_empty_is_valid_error_is_raised():
    assert P("docker_ps")("") == []
    line = json.dumps({"ID": "abc", "Names": "web", "Image": "nginx", "State": "running",
                       "Status": "Up 2 hours", "Ports": "0.0.0.0:80->80/tcp"})
    assert P("docker_ps")(line)[0]["NAME"] == "web"
    with pytest.raises(ValueError, match="permission denied"):
        P("docker_ps")("permission denied while trying to connect to the Docker daemon socket")
    assert P("systemd_failed")("") == []
    rec = P("systemd_failed")("\u25cf nginx.service loaded failed failed A high performance web server\n")[0]
    assert rec["UNIT"] == "nginx.service" and rec["ACTIVE"] == "failed"
    with pytest.raises(ValueError):
        P("systemd_failed")("-bash: systemctl: command not found")


def test_parser_hook_routes_py_templates():
    parser = Parser(PKG_DATA / "tfsm_templates.db")
    ok = parser.parse("linux", "docker ps", "", "py:docker_ps")
    assert ok.error is None and ok.records == [] and ok.method == "python"
    got = parser.parse("linux", "x", FRR, "py:frr_bgp_summary")
    assert got.template == "py:frr_bgp_summary" and len(got.records) == 3


def test_monitor_sysfs_counters():
    assert "{intf}" in COUNTER_COMMANDS["linux"]
    assert parse_octets("linux", "123\n456\n") == (123, 456)
    from terminaltelemetry2.monitor import CounterError
    with pytest.raises(CounterError, match="No such file"):
        parse_octets("linux", "cat: /sys/class/net/x/statistics/rx_bytes: No such file or directory")


# ── shell normalization, capability gate, operstate ──────────────────────────

def test_probe_roundtrip_in_posix_sh():
    import subprocess
    from terminaltelemetry2.controller import build_probe, parse_probe
    tests = ["command -v sh", "command -v definitely-not-a-tool-tt2", "test -d /"]
    out = subprocess.run(["sh", "-c", build_probe(tests)], capture_output=True, text=True).stdout
    assert parse_probe(out, 3) == {0: True, 1: False, 2: True}
    assert parse_probe("@tt2cap0=1\n", 3) is None            # partial read -> keep old gates


def test_requires_validation_and_command_cap():
    base = {"widget": "x", "fields": {"a": ["A"]}, "view": {"type": "kv", "show": ["a"]}}
    with pytest.raises(WidgetError, match="no command for that platform"):
        parse_widget({**base, "commands": {"linux": "true"}, "requires": {"arista_eos": "true"}})
    with pytest.raises(WidgetError, match="keep under"):
        parse_widget({**base, "commands": {"linux": "echo " + "x" * 1000}})
    w = parse_widget({**base, "commands": {"linux": "true"}, "requires": {"linux": "command -v x"}})
    assert w.requires_for("linux") == "command -v x" and w.requires_for("cisco_ios") is None


def test_gated_widgets_declare_requirements():
    assert WIDGETS["bgp_peers"].requires_for("linux") == WIDGETS["bgp_down"].requires_for("linux")
    for name in ("lldp_neighbors", "containers", "services_failed"):
        assert WIDGETS[name].requires_for("linux")
    assert WIDGETS["storage"].requires_for("linux") is None


def test_unknown_operstate_with_carrier_is_up():
    raw = json.dumps([
        {"ifname": "vmnet8", "flags": ["BROADCAST", "UP", "LOWER_UP"], "operstate": "UNKNOWN"},
        {"ifname": "tun9", "flags": ["POINTOPOINT", "UP"], "operstate": "UNKNOWN"},
    ])
    by = {r["INTERFACE"]: r["LINK_STATUS"] for r in P("linux_links")(raw)}
    assert by == {"vmnet8": "up", "tun9": "unknown"}


def test_osc_sequences_stripped():
    from terminaltelemetry2.ssh.client import filter_ansi_sequences as f
    assert f("\x1b]0;user@host: ~\x07user@host:~$ ") == "user@host:~$ "
    assert f("\x1b]133;A\x1b\\tt2$ ") == "tt2$ "              # ST-terminated shell mark


def test_linux_prime_is_shell_agnostic_exec():
    from terminaltelemetry2.session import SENTINEL, SHELL_PRIME
    prime = SHELL_PRIME["linux"]
    assert prime.startswith("exec env ") and prime.endswith(" /bin/sh")
    assert f"PS1='{SENTINEL} '" in prime and "-u PROMPT_COMMAND" in prime
