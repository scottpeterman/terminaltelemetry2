import shutil

import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication

from conftest import PKG_DB, fixture_text
from terminaltelemetry2.parsing import Parser
from terminaltelemetry2.widgets.template_manager import LiveCapture, TemplateManager

BGP = "arista_eos_show_ip_bgp_summary"
V6 = "arista_eos_show_ipv6_bgp_summary"
CMD = "show ip bgp summary"


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def mgr(qapp, tmp_path):
    db = tmp_path / "t.db"
    shutil.copy2(PKG_DB, db)
    out = fixture_text("arista_eos_show_ip_bgp_summary.raw")
    live = [LiveCapture("spine1", "arista_eos", CMD, out)]
    m = TemplateManager(Parser(db, [tmp_path / "overrides"]), captures=lambda: live)
    fired = []
    m.changed.connect(lambda: fired.append(1))
    m.fired = fired
    yield m
    m.close()


def _row(table, name, col=1):
    return next(r for r in range(table.rowCount()) if table.item(r, col).data(Qt.UserRole) == name)


def test_platform_filter_and_search(mgr):
    mgr.select_platform("hp_comware")
    assert mgr.table.rowCount() == 16
    mgr.search.setText("lldp")
    names = [mgr.table.item(r, 1).text() for r in range(mgr.table.rowCount())]
    assert names and all("lldp" in n for n in names)


def test_checkbox_disables_and_emits(mgr):
    mgr.select_platform("arista_eos")
    r = _row(mgr.table, BGP)
    mgr.table.item(r, 0).setCheckState(Qt.Unchecked)
    assert mgr.store.get(BGP).enabled is False
    assert mgr.fired
    assert "45/46" in mgr.platform_list.currentItem().text()


def test_live_capture_rank_and_resolution(mgr):
    mgr.select_platform("arista_eos")
    mgr.select_names([BGP])
    mgr._on_select()
    mgr.load_live(next(iter(mgr._captures())))
    mgr.run_rank()
    names = [mgr.rank_table.item(r, 1).text() for r in range(mgr.rank_table.rowCount())]
    assert names[:2] == [V6, BGP]                       # the interference, visible
    assert f"<b>{BGP}</b> (exact" in mgr.rank_resolves.text()
    # disable the exact template from the rank view: auto falls to the sweep -> v6
    mgr.rank_table.item(_row(mgr.rank_table, BGP), 0).setCheckState(Qt.Unchecked)
    mgr.run_rank()
    assert f"<b>{V6}</b> (scored" in mgr.rank_resolves.text()


def test_clone_edit_save_and_vendor_base_protection(mgr):
    mgr.select_platform("arista_eos")
    mgr.select_names([BGP])
    mgr._on_select()
    assert mgr._current == BGP and not mgr.ed_save.isEnabled()      # ntc: sibling only
    mgr.load_live(next(iter(mgr._captures())))
    assert mgr.test_editor().ok
    dst = mgr.clone_selected()
    assert dst == BGP + "2" and mgr._current == dst and mgr.ed_save.isEnabled()
    mgr.editor.setPlainText(mgr.editor.toPlainText() + "\n")
    assert mgr.save_editor() == dst
    assert mgr.store.get(dst).sample                                   # capture stored with it
    assert mgr.save_editor(as_sibling=True) == BGP + "3"


def test_bad_template_not_saved(mgr):
    mgr.select_platform("arista_eos")
    mgr.select_names([BGP])
    mgr._on_select()
    dst = mgr.clone_selected()
    mgr.editor.setPlainText("Value A (\\S+\n\nStart\n  ^${A} -> Record\n")
    assert mgr.test_editor().error_kind == "syntax"
    assert mgr.save_editor() is None
    assert mgr.store.content(dst) == mgr.store.content(BGP)


def test_samples_and_regress(mgr):
    mgr.load_live(next(iter(mgr._captures())))
    assert mgr.save_sample("lab spine") is not None
    assert mgr.save_sample() is None                                   # dedup
    assert mgr.sample_table.rowCount() == 1
    mgr.open_template(BGP)
    mgr.run_regress()
    assert "1/1 samples parse" in mgr.regress_label.text()


def test_delete_refuses_vendor_rows(mgr):
    dst = mgr.store.clone(BGP)
    refused = mgr.delete([dst, BGP])
    assert refused == [BGP]
    assert mgr.store.get(dst) is None and mgr.store.get(BGP) is not None


def test_file_override_is_flagged(mgr, tmp_path):
    d = tmp_path / "overrides"
    d.mkdir(exist_ok=True)
    (d / f"{BGP}.textfsm").write_text("Value A (\\S+)\n\nStart\n  ^${A} -> Record\n")
    mgr.select_platform("arista_eos")
    assert mgr.table.item(_row(mgr.table, BGP), 4).text() == "file"
    mgr.open_template(BGP)
    assert "shadowed" in mgr.ed_name.text()


def test_capture_follows_command_across_templates(mgr):
    """Regression: the first template's sample stuck for every later one."""
    s = mgr.store
    arp = "juniper_junos_show_arp_no-resolve"
    lldp = "juniper_junos_show_lldp_neighbors"
    mgr.open_template(arp)
    assert mgr.capture.toPlainText() == s.get(arp).sample
    mgr.open_template(lldp)                                           # different command
    assert mgr.capture.toPlainText() == s.get(lldp).sample
    assert mgr.ed_status.text().endswith("records") or "record" in mgr.ed_status.text()
    mgr.open_template(arp)                                            # and back
    assert mgr.capture.toPlainText() == s.get(arp).sample


def test_stored_sample_used_when_template_has_none(mgr):
    t = "juniper_junos_show_bgp_summary"                               # no sample of its own
    mgr.store.add_sample("juniper_junos", "show bgp summary", "BGP-CAPTURE", "edge")
    mgr.open_template("juniper_junos_show_arp_no-resolve")
    mgr.open_template(t)
    assert mgr.capture.toPlainText() == "BGP-CAPTURE"


def test_capture_kept_across_siblings_and_hand_edits(mgr):
    mgr.load_live(next(iter(mgr._captures())))
    mgr.capture.setPlainText(mgr.capture.toPlainText() + "\n")     # hand edit claims it
    edited = mgr.capture.toPlainText()
    dst = mgr.store.clone(BGP)
    mgr.open_template(BGP)
    mgr.open_template(dst)                                            # same command: kept
    assert mgr.capture.toPlainText() == edited


def test_no_capture_says_so(mgr):
    mgr.open_template("juniper_junos_show_arp_no-resolve")
    mgr.open_template("arista_eos_show_ip_route_v2")                  # no sample anywhere
    assert mgr.capture.toPlainText() == ""
    assert "no capture for show ip route v2" in mgr.ed_status.text()
