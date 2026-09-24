import shutil

import pytest
from PySide6.QtGui import QAction
from PySide6.QtWidgets import QApplication

from conftest import PKG_DB
from terminaltelemetry2 import about
from terminaltelemetry2.paths import PKG_DATA


def test_every_component_has_its_license_text():
    for c in about.components():
        for f in c.files:
            assert (about.LICENSE_DIR / f).is_file(), (c.name, f)
        if c.files:
            assert "missing" not in c.texts().split("\n", 1)[0], c.name
    names = {c.name for c in about.components()}
    assert {"Qt", "PySide6 / Shiboken6", "paramiko", "TextFSM", "ntc-templates", "PyYAML"} <= names


def test_qt_notice_is_lgpl_with_sources():
    n = about.qt_notice()
    assert "LGPL" in n and "download.qt.io" in n and "QtForPython" in n
    qt = next(c for c in about.components() if c.name == "Qt")
    assert qt.license == "LGPL-3.0-only" and "LGPL-3.0.txt" in qt.files
    assert "GNU LESSER GENERAL PUBLIC LICENSE" in qt.texts()


def test_app_license_matches_repo():
    from pathlib import Path
    repo_license = (Path(__file__).parents[1] / "LICENSE").read_text()
    assert (about.LICENSE_DIR / "GPL-3.0.txt").read_text() == repo_license


def test_licenses_packaged():
    import tomllib
    from pathlib import Path
    cfg = tomllib.loads((Path(__file__).parents[1] / "pyproject.toml").read_text())
    assert "data/licenses/*.txt" in cfg["tool"]["setuptools"]["package-data"]["terminaltelemetry2"]


def test_cli_licenses(capsys):
    from terminaltelemetry2 import app
    assert app.main(["--licenses"]) == 0
    out = capsys.readouterr().out
    assert "GPL-3.0-or-later" in out and "LGPL" in out and "ntc-templates" in out


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


def _help(win):
    menu = win.menuBar().actions()[0].menu()
    return [(a.text(), a.menuRole()) for a in menu.actions()]


def test_help_menu_on_every_window(qapp, tmp_path):
    from terminaltelemetry2.layout import load_layouts
    from terminaltelemetry2.parsing import Parser
    from terminaltelemetry2.widgets import load_widgets
    from terminaltelemetry2.widgets.pack_editor import PackEditor
    from terminaltelemetry2.widgets.template_manager import TemplateManager
    from terminaltelemetry2.widgets.widget_designer import WidgetDesigner
    db = tmp_path / "t.db"
    shutil.copy2(PKG_DB, db)
    p = Parser(db)
    widgets, _ = load_widgets([PKG_DATA / "widgets"])
    layouts, _ = load_layouts([PKG_DATA / "layouts"])
    wins = [TemplateManager(p), PackEditor(p, widgets, ["default"], save_dir=tmp_path),
            WidgetDesigner(p, widgets, layouts, widget_dir=tmp_path, layout_dir=tmp_path)]
    want = [("About terminaltelemetry2", QAction.AboutRole), ("About Qt", QAction.AboutQtRole),
            ("Third-party licenses...", QAction.NoRole)]
    for w in wins:
        assert _help(w) == want, type(w).__name__
        w._dirty = False
        w.close()


def test_dialogs_build(qapp):
    from terminaltelemetry2.widgets.about_dialog import AboutDialog, LicensesDialog
    d = AboutDialog()
    l = LicensesDialog()
    for row in range(l.items.count()):
        l.items.setCurrentRow(row)
        assert l.text.toPlainText()
    assert "<a href='https://download.qt.io/official_releases/qt/'>" in \
        (l.items.setCurrentRow(1) or l.info.text())
