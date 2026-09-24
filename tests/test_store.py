import shutil
import sqlite3

import pytest

from conftest import PKG_DB, fixture_text
from terminaltelemetry2.parsing import Parser
from terminaltelemetry2.parsing.store import (
    SCHEMA_VERSION, TemplateStore, migrate, schema_version, split_name,
)

GOOD = "Value A (\\S+)\n\nStart\n  ^${A} -> Record\n"
BGP = "arista_eos_show_ip_bgp_summary"
BGP_CMD = "show ip bgp summary"


@pytest.fixture
def db(tmp_path):
    p = tmp_path / "t.db"
    shutil.copy2(PKG_DB, p)
    return p


def _v1(path, rows):
    with sqlite3.connect(path) as c:
        c.execute("CREATE TABLE templates(id INT, cli_command TEXT, cli_content TEXT, "
                  "textfsm_content TEXT, textfsm_hash TEXT, source TEXT, created TEXT)")
        c.executemany("INSERT INTO templates VALUES (?,?,?,?,?,?,?)", rows)


def test_split_name():
    assert split_name("hp_comware_display_lldp_neighbor-information_list") == \
        ("hp_comware", "display lldp neighbor-information list")
    assert split_name("fortinet_get_system_status") == ("fortinet", "get system status")
    assert split_name("linux_ip_addr") == ("linux", "ip addr")
    assert split_name("du -h /var/log") == ("", "du -h /var/log")
    assert split_name("show_system_info") == ("", "show system info")


def test_bundled_db_is_current():
    assert schema_version(str(PKG_DB)) == SCHEMA_VERSION


def test_migrate_v1(tmp_path):
    p = tmp_path / "old.db"
    _v1(p, [
        (1, "cisco_ios_show_version", "", GOOD, None, "ntc", "2024"),
        (2, "cisco_ios_show_version2", "", GOOD, None, "custom", "2024"),
        (3, "dupe_x_show_y", "", "old", None, "ntc", "2024"),
        (3, "dupe_x_show_y", "", GOOD, None, None, "2025"),       # dup name, dup id
        (4, "empty_row", "", None, None, "ntc", ""),              # no content: dropped
    ])
    assert migrate(str(p)) is True
    assert migrate(str(p)) is False                                # idempotent
    assert (tmp_path / "old.db.v1.bak").exists()
    s = TemplateStore(p)
    names = {i.name: i for i in s.list()}
    assert set(names) == {"cisco_ios_show_version", "cisco_ios_show_version2", "dupe_x_show_y"}
    assert s.content("dupe_x_show_y") == GOOD                      # last row won
    assert names["dupe_x_show_y"].source == "unknown"
    sib = names["cisco_ios_show_version2"]
    assert (sib.platform, sib.command) == ("cisco_ios", "show version")   # inherits base


def test_disable_removes_from_exact_and_sweep(db):
    p = Parser(db)
    out = fixture_text("arista_eos_show_ip_bgp_summary.raw")
    assert p.parse("arista_eos", BGP_CMD, out).template == BGP
    p.store.set_enabled([BGP], False)
    p.reload_overrides()
    r = p.parse("arista_eos", BGP_CMD, out)
    assert r.template != BGP
    assert p.template_content(BGP) is not None                     # still readable for the lab
    assert BGP not in [x.name for x in p.store.rank(p.clean_output(out), BGP, include_disabled=False)]


def test_disabled_sibling_leaves_family(db):
    p = Parser(db)
    p.save_template_to_db(BGP + "2", GOOD)
    assert p.family(BGP) == [BGP, BGP + "2"]
    p.store.set_enabled([BGP + "2"], False)
    p.reload_overrides()
    assert p.family(BGP) == [BGP]
    assert p.next_sibling_name(BGP) == BGP + "3"                   # disabled still owns its number


def test_clone_and_delete_rules(db):
    s = TemplateStore(db)
    dst = s.clone(BGP)
    assert dst == BGP + "2"
    rec = s.get(dst)
    assert (rec.source, rec.platform, rec.command) == ("custom", "arista_eos", BGP_CMD)
    assert rec.content == s.content(BGP)
    assert set(s.duplicates(rec.content)) == {BGP, dst}
    with pytest.raises(ValueError, match="disable"):
        s.delete(BGP)
    s.delete(dst)
    assert s.get(dst) is None


def test_save_rejects_uncompilable(db):
    s = TemplateStore(db)
    with pytest.raises(ValueError, match="compile"):
        s.save("x_y_show_z", "Value A (\\S+\n\nStart\n  ^${A} -> Record\n")
    assert s.get("x_y_show_z") is None


def test_rank_shows_interference_and_disable_fixes_it(db):
    # The sweep drops terms <= 2 chars, so 'ip' never filters and the IPv6
    # template competes on IPv4 output -- and edges it out on score.
    p = Parser(db)
    clean = p.clean_output(fixture_text("arista_eos_show_ip_bgp_summary.raw"))
    v6 = "arista_eos_show_ipv6_bgp_summary"
    ranked = p.store.rank(clean, BGP)
    assert all(a.score >= b.score for a, b in zip(ranked, ranked[1:]))
    assert [r.name for r in ranked[:2]] == [v6, BGP]
    p.store.set_enabled([v6], False)
    assert p.store.rank(clean, BGP, include_disabled=False)[0].name == BGP
    assert [r.enabled for r in p.store.rank(clean, BGP)[:1]] == [False]   # still visible


def test_samples_dedupe_and_regress(db):
    p = Parser(db)
    out = fixture_text("arista_eos_show_ip_bgp_summary.raw")
    assert p.store.add_sample("arista_eos", BGP_CMD, out, "lab spine") is not None
    assert p.store.add_sample("arista_eos", BGP_CMD, out) is None           # identical capture
    res = p.store.regress(BGP, cleaner=p.clean_output)
    assert len(res) == 1 and res[0].records > 0 and res[0].error is None


def test_export_import_roundtrip(db, tmp_path):
    s = TemplateStore(db)
    [path] = s.export([BGP], tmp_path / "out")
    name = s.import_file(path, name="arista_eos_show_ip_bgp_summary_mine")
    assert s.content(name) == s.content(BGP)


def test_clone_keeps_commands_ending_in_digits(db):
    s = TemplateStore(db)
    dst = s.clone("arista_eos_show_ip_route_v2")
    assert s.get(dst).command == "show ip route v2"


def test_new_sibling_inherits_base_command(db):
    s = TemplateStore(db)
    s.save(BGP + "7", GOOD)
    assert (s.get(BGP + "7").platform, s.get(BGP + "7").command) == ("arista_eos", BGP_CMD)
    s.save("arista_eos_show_ip_route_v2", s.content("arista_eos_show_ip_route_v2"), allow_overwrite=True)
    assert s.get("arista_eos_show_ip_route_v2").command == "show ip route v2"   # not a sibling of '..._v'
