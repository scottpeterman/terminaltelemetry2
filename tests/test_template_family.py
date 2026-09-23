import shutil
from pathlib import Path

import pytest

from terminaltelemetry2.parsing import Parser

PKG_DB = Path(__file__).parent.parent / "terminaltelemetry2" / "data" / "tfsm_templates.db"
BASE = "juniper_junos_show_system_processes_extensive"
CMD = "show system processes extensive | no-more"
FIX = Path(__file__).parent / "fixtures" / "juniper_junos_show_system_processes_extensive.raw"
BAD = "Value X (zzz)\n\nStart\n  ^${X} -> Record\n"
NEW = "Value L1 (\\S+)\n\nStart\n  ^NEWFORMAT ${L1} -> Record\n"


@pytest.fixture
def parser(tmp_path):
    db = tmp_path / "t.db"
    shutil.copy2(PKG_DB, db)
    return Parser(db, [tmp_path / "overrides"])


def test_explicit_falls_through_to_newest_working_sibling(parser):
    parser.save_template_to_db(BASE + "2", BAD)
    parser.save_template_to_db(BASE + "3", NEW)
    assert parser.family(BASE) == [BASE, BASE + "3", BASE + "2"]
    r = parser.parse("juniper_junos", CMD, "NEWFORMAT 0.99\n", BASE)
    assert (r.template, r.records) == (BASE + "3", [{"L1": "0.99"}])
    assert parser.pinned("juniper_junos", CMD, BASE) == BASE + "3"
    # older gear still resolves to the base, past the pin
    r = parser.parse("juniper_junos", CMD, FIX.read_text(), BASE)
    assert r.template == BASE and r.records


def test_failure_names_sibling_count(parser):
    parser.save_template_to_db(BASE + "2", BAD)
    r = parser.parse("juniper_junos", CMD, "nothing matches\n", BASE)
    assert "1 sibling" in r.error


def test_override_file_siblings_and_suffix(parser, tmp_path):
    (tmp_path / "overrides").mkdir()
    (tmp_path / "overrides" / f"{BASE}7.textfsm").write_text(NEW)
    parser.reload_overrides()
    assert parser.family(BASE)[1] == BASE + "7"
    assert parser.next_in_family(BASE) == BASE + "8"
    assert parser.template_origin(BASE + "7") == "override"


def test_delete_guards(parser):
    parser.save_template_to_db(BASE + "2", BAD)
    with pytest.raises(ValueError):
        parser.delete_template_from_db(BASE, BASE)
    parser.delete_template_from_db(BASE + "2", BASE)
    assert parser.family(BASE) == [BASE]


def test_base_ending_in_digit_keeps_it(parser):
    assert parser.next_in_family("cisco_ios_show_ip_bgp_vpnv4") == "cisco_ios_show_ip_bgp_vpnv42"
