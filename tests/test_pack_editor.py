import shutil

import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication

from conftest import PKG_DB
from terminaltelemetry2.parsing import Parser
from terminaltelemetry2.paths import PKG_DATA
from terminaltelemetry2.platforms import load_platforms, set_registry
from terminaltelemetry2.widgets import load_widgets
from terminaltelemetry2.widgets.pack_editor import PackEditor, _COMMANDS, _NOPAGING, _SHOTGUN


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def ed(qapp, tmp_path):
    reg, _ = load_platforms([PKG_DATA / "platforms"])
    set_registry(reg)
    db = tmp_path / "t.db"
    shutil.copy2(PKG_DB, db)
    widgets, _ = load_widgets([PKG_DATA / "widgets"])
    e = PackEditor(Parser(db), widgets, ["default", "eos"], save_dir=tmp_path / "platforms",
                   platform="hp_comware")
    e._dirty = False
    yield e
    e._dirty = False
    e.close()
    set_registry(None)


def _row(table, name, col=0):
    return next(r for r in range(table.rowCount()) if table.item(r, col).data(Qt.UserRole) == name)


def test_loads_existing_pack(ed):
    assert ed.draft.platform == "hp_comware"
    assert ed.wtable.item(_row(ed.wtable, "lldp_neighbors"), 1).text().startswith("pack:")
    assert ed.s_paging_mode.currentText() == _COMMANDS
    assert ed.s_paging.toPlainText() == "screen-length disable"
    assert ed.c_mode.currentIndex() == 1 and ed.c_preset.currentText().startswith("Comware")


def test_existing_binding_previews_rows(ed):
    ed.select_widget("intf_updown")
    assert ed.cmd.text() == "display interface"
    link = ed.ftable.cellWidget(_row(ed.ftable, "link"), 2)
    assert link.currentData() == "LINE_STATUS"
    pv = ed.run_preview()
    assert pv.rows and pv.error is None


def test_new_platform_accept_confident_and_save(ed, tmp_path):
    ed.platform.setCurrentIndex(ed.platform.findData("cisco_asa"))
    assert ed.draft.platform == "cisco_asa" and ed.draft.bindings == {}
    taken = ed.accept_confident()
    assert {"version", "ospf_neighbors", "intf_updown"} <= set(taken)
    assert ed.detect_counters() and ed.test_counters() is not None
    path = ed.save(confirm=False)
    assert path == tmp_path / "platforms" / "cisco_asa.yaml"
    reg, errors = load_platforms([tmp_path / "platforms"])
    assert errors == []
    asa = reg.get("cisco_asa")
    assert set(taken) <= set(asa.bindings) and asa.counters.command.endswith("{intf}")
    assert asa.paging == ["terminal pager 0"]                       # session kept from bundled pack
    widgets, _ = load_widgets([PKG_DATA / "widgets"])
    assert "cisco_asa" in reg.known(widgets)


def test_pick_value_makes_overlay_and_bind(ed):
    ed.platform.setCurrentIndex(ed.platform.findData("hp_comware"))
    ed.select_widget("port_status")
    name_combo = ed.ftable.cellWidget(_row(ed.ftable, "name"), 2)
    name_combo.setCurrentIndex(name_combo.findData("DESCRIPTION"))
    b = ed.bind_current()
    assert b["fields"]["name"] == ["DESCRIPTION"]
    assert ed.draft.bindings["port_status"]["fields"]["name"] == ["DESCRIPTION"]


def test_candidate_click_rewires_everything(ed):
    ed.select_widget("lldp_neighbors")
    r = next(i for i, c in enumerate(ed._cands) if c.template.endswith("verbose"))
    ed.ctable.selectRow(r)
    assert ed.template.currentData().endswith("lldp_neighbor-information_verbose")
    assert "verbose" in ed.cmd.text()
    assert ed.sample_src.itemText(0).startswith("DB sample of")


def test_off_and_remove(ed):
    ed.select_widget("version")
    ed.set_off()
    assert ed.draft.bindings["version"] is None
    assert ed.wtable.item(_row(ed.wtable, "version"), 1).text() == "off"
    ed.clear_binding()
    assert "version" not in ed.draft.bindings


def test_session_modes_collect(ed):
    ed.s_paging_mode.setCurrentText(_SHOTGUN)
    assert ed.collect().paging is None
    ed.s_paging_mode.setCurrentText(_NOPAGING)
    assert ed.collect().paging == []
    ed.s_paging_known.setCurrentIndex(ed.s_paging_known.findText("no page"))
    ed._insert_paging(ed.s_paging_known.findText("no page"))
    assert ed.collect().paging == ["screen-length disable", "no page"] or \
        ed.collect().paging == ["no page"]


def test_validate_blocks_bad_pack(ed, tmp_path):
    ed.s_models.setText("([unclosed")
    assert ed.save(confirm=False) is None
    assert not (tmp_path / "platforms" / "hp_comware.yaml").exists()
    assert "match.model" in ed.status.text()


def test_ip_addresses_preview_explains_empty(ed):
    ed.platform.setCurrentIndex(ed.platform.findData("cisco_asa"))
    ed.select_widget("ip_addresses")
    pv = ed.run_preview()
    if pv and pv.records and not pv.rows:
        assert "filters dropped every row" in ed.pv_status.text()


def test_detect_fills_regexes_on_a_fresh_window(qapp, tmp_path):
    """Regression: Detect left rx/tx empty when the preset index didn't change."""
    reg, _ = load_platforms([PKG_DATA / "platforms"])
    set_registry(reg)
    widgets, _ = load_widgets([PKG_DATA / "widgets"])
    e = PackEditor(Parser(PKG_DB), widgets, ["default"], save_dir=tmp_path, platform="cisco_asa")
    try:
        assert e.c_preset.currentIndex() == 0
        assert e.detect_counters()
        assert e.c_rx.text() and e.c_tx.text()
        assert e.test_counters() is not None
        assert e.collect().counters["rx"] == e.c_rx.text()
    finally:
        e._dirty = False
        e.close()
        set_registry(None)


def test_unconfident_suggestion_not_shown_as_a_pick(ed):
    it = ed.wtable.item(_row(ed.wtable, "bgp_peers"), 2)
    assert it.text() == "no confident match" and "closest:" in it.toolTip()


def test_save_folds_other_packs_for_the_platform(qapp, tmp_path):
    """Regression: a merge overlay in the user dir loaded on top of the
    editor's saved pack and silently undid it (read_timeout 3 beat 20)."""
    pdir = tmp_path / "platforms"
    pdir.mkdir()
    (pdir / "linux-sudo.yaml").write_text(
        "platform: linux\nmerge: true\nsession: {read_timeout: 3}\n"
        "bindings:\n  containers: {sudo: true}\n")
    (pdir / "other.yaml").write_text("platform: x_os\n")          # other platforms untouched
    reg, _ = load_platforms([PKG_DATA / "platforms", pdir])
    set_registry(reg)
    widgets, _ = load_widgets([PKG_DATA / "widgets"])
    e = PackEditor(Parser(PKG_DB), widgets, ["default"], save_dir=pdir, platform="linux")
    try:
        assert e.s_read.value() == 3                                # starts from the effective pack
        e.s_read.setValue(20)
        e.save(confirm=False)
        assert sorted(p.name for p in pdir.iterdir()) == \
            ["linux-sudo.yaml.folded", "linux.yaml", "other.yaml"]
        reg, errors = load_platforms([PKG_DATA / "platforms", pdir])
        lx = reg.get("linux")
        assert errors == [] and lx.read_timeout == 20 and lx.bindings["containers"].sudo
        assert "folded in" in e.status.text()
    finally:
        e._dirty = False
        e.close()
        set_registry(None)


def test_enable_list_has_no_config_mode(ed):
    items = [ed.s_enable.itemText(i) for i in range(ed.s_enable.count())]
    assert items == ["", "enable", "enable 15"]
