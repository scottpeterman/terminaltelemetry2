import pytest

from terminaltelemetry2.sessions import (
    ConnectTarget, JumpTarget, jump_to_str, load_sessions, parse_jump,
)

KNOWN = ["arista_eos", "cisco_ios", "cisco_nxos", "juniper_junos"]

YAML = """
- folder_name: site1
  sessions:
  - {display_name: edge1, host: 10.1.1.1, port: '22', DeviceType: arista_eos}
  - {display_name: tor1, host: 10.1.1.2, Vendor: Juniper, jump_host: none}
  - {display_name: nx1, host: 10.1.1.3, Vendor: Cisco, Model: N9K-C93180YC,
     jump_host: bastion, jump_port: '2222', jump_username: scott}
  - {display_name: nohost}
- folder: site2
  sessions:
  - {host: 10.2.2.1, device_type: cisco_xe, port: 830}
  - {display_name: lnx, host: 10.2.2.2, DeviceType: Linux}
"""


@pytest.fixture
def entries(tmp_path):
    p = tmp_path / "s.yaml"
    p.write_text(YAML)
    return load_sessions(p)


def test_load_and_guess(entries):
    by = {e.name: e for e in entries}
    assert len(entries) == 5                       # host-less entry skipped
    assert by["edge1"].guess_platform(KNOWN) == "arista_eos"
    assert by["tor1"].guess_platform(KNOWN) == "juniper_junos"
    assert by["tor1"].jump_host == ""              # 'none' means no override
    assert by["nx1"].guess_platform(KNOWN) == "cisco_nxos"
    assert (by["nx1"].jump_host, by["nx1"].jump_port) == ("bastion", 2222)
    assert by["10.2.2.1"].folder == "site2" and by["10.2.2.1"].port == 830
    assert by["10.2.2.1"].guess_platform(KNOWN) == "cisco_ios"
    assert by["lnx"].guess_platform(KNOWN) is None


@pytest.mark.parametrize("spec,want", [
    ("h", (None, "h", 22)),
    ("u@h:2200", ("u", "h", 2200)),
    ("[2001:db8::1]:2022", (None, "2001:db8::1", 2022)),
    ("u@2001:db8::1", ("u", "2001:db8::1", 22)),
])
def test_parse_jump(spec, want):
    assert parse_jump(spec) == want


@pytest.mark.parametrize("spec", ["", "u@", "[::1", "[::1]x"])
def test_parse_jump_bad(spec):
    with pytest.raises(ValueError):
        parse_jump(spec)


def test_jump_spec_inherits_device_auth(tmp_path):
    key = tmp_path / "k"
    key.write_text("KEY")
    t = ConnectTarget(host="d", platform="arista_eos", username="me", password="pw",
                      key_file=str(key), jump=JumpTarget(host="b", port=2222))
    hop = t.jump_spec().hops[0]
    assert (hop.host, hop.port, hop.username, hop.password, hop.key_content) == \
        ("b", 2222, "me", "pw", "KEY")
    assert jump_to_str(t.jump) == "b:2222"


def test_jump_spec_own_auth():
    t = ConnectTarget(host="d", platform="arista_eos", username="me", password="pw",
                      jump=JumpTarget(host="b", username="j", password="jp"))
    hop = t.jump_spec().hops[0]
    assert (hop.username, hop.password, hop.key_content) == ("j", "jp", None)
    assert ConnectTarget(host="d", platform="x", username="u", password="p").jump_spec() is None
