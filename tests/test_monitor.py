from pathlib import Path

import pytest

from terminaltelemetry2.monitor import (
    COUNTER_COMMANDS, CounterError, RateTracker, fmt_bps, nice_ceil, parse_octets,
)

FIX = Path(__file__).parent / "fixtures"


def test_ios_eos_text_fixtures():
    assert parse_octets("cisco_ios", (FIX / "cisco_ios_show_interfaces.raw").read_text()) == (48614, 62737)
    assert parse_octets("arista_eos", (FIX / "arista_eos_show_interfaces.raw").read_text()) == (31440, 33221)


def test_junos_text_with_vc_banner():
    text = ("Physical interface: et-0/0/50, Enabled, Physical link is Up\n"
            "  Traffic statistics:\n   Input  bytes  :      1000   8 bps\n"
            "   Output bytes  :      2000   16 bps\n"
            "  Logical interface et-0/0/50.0\n    Traffic statistics:\n"
            "     Input  bytes  :       5\n     Output bytes  :       6\n\n{master:1}\n")
    assert parse_octets("juniper_junos", text) == (1000, 2000)


def test_nxos_text():
    text = ("Ethernet1/1 is up\n  RX\n    10 unicast packets  0 multicast packets\n"
            "    10 input packets  1280 bytes\n  TX\n    4 output packets  512 bytes\n")
    assert parse_octets("cisco_nxos", text) == (1280, 512)


def test_eos_json():
    text = '{"interfaces": {"Ethernet1": {"interfaceCounters": {"inOctets": 10, "outOctets": 20}}}}'
    assert parse_octets("arista_eos", text) == (10, 20)


def test_nxos_json_row_list_and_strings():
    text = ('{"TABLE_interface": {"ROW_interface": [{"interface": "Ethernet1/1",'
            ' "eth_inbytes": "30", "eth_outbytes": 40, "eth_inrate1_bits": "9"}]}}')
    assert parse_octets("cisco_nxos", text) == (30, 40)


def test_junos_xml_physical_not_logical():
    xml = """junk
<rpc-reply xmlns:junos="http://xml.juniper.net/junos/14.1X53/junos">
<interface-information xmlns="http://xml.juniper.net/junos/14.1X53/junos-interface" junos:style="normal">
<physical-interface><name>et-0/0/50</name>
<logical-interface><name>et-0/0/50.0</name>
  <traffic-statistics><input-bytes>1</input-bytes><output-bytes>2</output-bytes></traffic-statistics>
</logical-interface>
<traffic-statistics junos:style="brief"><input-bytes>
 700 </input-bytes><output-bytes>800</output-bytes></traffic-statistics>
</physical-interface></interface-information></rpc-reply>
{master:1}"""
    assert parse_octets("juniper_junos", xml) == (700, 800)


@pytest.mark.parametrize("text", [
    "% Invalid input detected at '^' marker.",
    "error: device et-0/0/99 not found\n\n{master:1}",
    "",
])
def test_errors_surface(text):
    with pytest.raises(CounterError):
        parse_octets("juniper_junos", text)


def test_tracker_rates_and_rebaseline():
    t = RateTracker()
    assert t.add(100.0, 0, 0) is None
    assert t.add(105.0, 625_000, 1_250_000) == (1_000_000.0, 2_000_000.0)
    assert t.add(110.0, 10, 10) is None          # clear counters -> rebaseline
    assert t.add(115.0, 10 + 625, 10) == (1000.0, 0.0)
    assert len(t.points) == 2


def test_format():
    assert fmt_bps(0) == "0 bps"
    assert fmt_bps(999) == "999 bps"
    assert fmt_bps(1_500_000) == "1.5 Mbps"
    assert fmt_bps(9.87e9) == "9.87 Gbps"
    assert nice_ceil(0) == 1000
    assert nice_ceil(3.2e6) == 5e6
    assert nice_ceil(1e9) == 1e9


def test_every_platform_has_command():
    assert set(COUNTER_COMMANDS) == {"arista_eos", "cisco_ios", "cisco_nxos", "juniper_junos", "linux"}


def test_junos_ae_unit_bundle_totals():
    # ae0.1001 on an MX80 13.3: no "Input bytes" lines, a Bundle: table instead.
    # Bytes is the third column; Link: rows are per-member and must not be used.
    text = (FIX / "juniper_junos_show_interfaces_ae_unit_detail.raw").read_text()
    assert parse_octets("juniper_junos", text) == (33847690742431279, 4742381468105927)


def test_junos_physical_ae_prefers_own_block_over_unit_bundle():
    text = ("Physical interface: ae0, Enabled, Physical link is Up\n"
            "  Traffic statistics:\n   Input  bytes  :   111   8 bps\n"
            "   Output bytes  :   222   8 bps\n"
            "  Logical interface ae0.1001\n"
            "    Statistics        Packets        pps         Bytes          bps\n"
            "    Bundle:\n        Input : 1 0 999 0\n        Output: 1 0 888 0\n")
    assert parse_octets("juniper_junos", text) == (111, 222)
