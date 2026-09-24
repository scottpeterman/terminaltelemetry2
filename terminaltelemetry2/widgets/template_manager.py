"""Template Manager -- the template DB as a browsable, testable workspace.

    platforms | templates (enable checkbox, filters) | workspace
                                                      context: platform, command, capture
                                                      tabs:    Template | Rank | Samples

One window per app, over one DB. Every write emits `changed`; the app wires
that to each device window's Parser.reload_overrides() so pins and cached
content re-resolve on the next poll.

Captures come from three places: live polls in open device windows (the
`captures` callable), stored samples, or paste. Nothing is auto-saved to the
sample corpus -- counter-bearing output hashes differently every poll, so
auto-capture would grow without bound. "Save as sample" is explicit.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable, List, Optional

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QAction, QFont, QKeySequence, QTextCursor
from PySide6.QtWidgets import (
    QAbstractItemView, QComboBox, QFileDialog, QHBoxLayout, QHeaderView, QInputDialog,
    QLabel, QLineEdit, QListWidget, QListWidgetItem, QMainWindow, QMenu, QMessageBox,
    QPlainTextEdit, QPushButton, QSplitter, QTableWidget, QTableWidgetItem, QTabWidget,
    QToolButton, QVBoxLayout, QWidget,
)

from ..parsing import Parser
from ..parsing.store import TemplateInfo
from .lab import LabResult, run_textfsm

_OK = "#3aa35a"
_ERR = "#d9534f"
_MUTED = "#8a8a8a"
_ALL = "\x00all"
_NONE = ""                                        # the no-platform bucket


@dataclass
class LiveCapture:
    label: str                                    # device label
    platform: str
    command: str
    output: str


def _mono() -> QFont:
    f = QFont("monospace")
    f.setStyleHint(QFont.Monospace)
    f.setPointSize(9)
    return f


def _item(text: str, data=None, align_right: bool = False) -> QTableWidgetItem:
    it = QTableWidgetItem(text)
    it.setFlags(it.flags() & ~Qt.ItemIsEditable)
    if data is not None:
        it.setData(Qt.UserRole, data)
    if align_right:
        it.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
    return it


def _check(on: bool, data) -> QTableWidgetItem:
    it = QTableWidgetItem()
    it.setFlags(Qt.ItemIsUserCheckable | Qt.ItemIsEnabled | Qt.ItemIsSelectable)
    it.setCheckState(Qt.Checked if on else Qt.Unchecked)
    it.setData(Qt.UserRole, data)
    return it


class TemplateManager(QMainWindow):
    changed = Signal()
    packs_requested = Signal(str)             # platform to open in the Platform Pack editor
    design_requested = Signal(str, str)       # (platform, template) for the Widget Designer

    COLS = ("On", "Template", "Command", "Source", "File", "Updated", "Note")

    def __init__(self, parser: Parser,
                 captures: Optional[Callable[[], Iterable[LiveCapture]]] = None,
                 parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.parser = parser
        self.store = parser.store
        self._captures = captures or (lambda: [])
        self._current: Optional[str] = None       # template in the editor
        self.setWindowTitle("Template Manager - terminaltelemetry2")
        self.resize(1600, 950)

        # -- left: platforms -----------------------------------------------------
        self.platform_list = QListWidget()
        self.platform_list.currentItemChanged.connect(lambda *_: self.refresh_templates())

        # -- middle: filters + template table ------------------------------------
        self.search = QLineEdit()
        self.search.setPlaceholderText("filter: terms AND-ed over name, command, note")
        self.search.textChanged.connect(lambda *_: self.refresh_templates())
        self.source_filter = QComboBox()
        self.source_filter.addItem("any source", None)
        for s in ("ntc", "custom", "chatgpt", "unknown"):
            self.source_filter.addItem(s, s)
        self.source_filter.currentIndexChanged.connect(lambda *_: self.refresh_templates())
        self.state_filter = QComboBox()
        for label, v in (("enabled + disabled", None), ("enabled", True), ("disabled", False)):
            self.state_filter.addItem(label, v)
        self.state_filter.currentIndexChanged.connect(lambda *_: self.refresh_templates())

        self.table = QTableWidget(0, len(self.COLS))
        self.table.setHorizontalHeaderLabels(self.COLS)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.table.verticalHeader().setVisible(False)
        self.table.setSortingEnabled(True)
        hh = self.table.horizontalHeader()
        hh.setSectionResizeMode(QHeaderView.Interactive)
        hh.setStretchLastSection(True)
        self.table.itemChanged.connect(self._on_item_changed)
        self.table.itemSelectionChanged.connect(self._on_select)
        self.table.setContextMenuPolicy(Qt.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self._table_menu)
        self.count_label = QLabel("")

        btns = QHBoxLayout()
        for text, fn, tip in (
                ("Enable", lambda: self.set_enabled_selected(True), "Put back in resolution"),
                ("Disable", lambda: self.set_enabled_selected(False),
                 "Remove from sweep, families and exact resolution"),
                ("Clone", lambda: self.clone_selected(), "Copy to the next numbered sibling"),
                ("Import...", self._import_dialog, "Import .textfsm files as custom templates"),
                ("Export...", self._export_dialog, "Write selected templates as .textfsm files")):
            b = QPushButton(text)
            b.setToolTip(tip)
            b.clicked.connect(fn)
            btns.addWidget(b)
        design = QPushButton("New widget...")
        design.setToolTip("Design a widget from the selected template (Widget Designer)")
        design.clicked.connect(lambda: self.design_requested.emit(
            self.ctx_platform.text().strip() or (self.selected_platform() or ""), self._current or ""))
        btns.addWidget(design)
        packs = QPushButton("Platform packs...")
        packs.setToolTip("Bind widgets to this platform's templates (Platform Pack editor)")
        packs.clicked.connect(lambda: self.packs_requested.emit(self.selected_platform() or ""))
        btns.addWidget(packs)
        btns.addStretch(1)
        btns.addWidget(self.count_label)

        mid = QWidget()
        ml = QVBoxLayout(mid)
        ml.setContentsMargins(0, 0, 0, 0)
        fl = QHBoxLayout()
        fl.addWidget(self.search, 1)
        fl.addWidget(self.source_filter)
        fl.addWidget(self.state_filter)
        ml.addLayout(fl)
        ml.addWidget(self.table, 1)
        ml.addLayout(btns)

        # -- right: context + tabs -------------------------------------------------
        self.ctx_platform = QLineEdit()
        self.ctx_platform.setPlaceholderText("platform")
        self.ctx_command = QLineEdit()
        self.ctx_command.setPlaceholderText("command")
        self.load_btn = QToolButton()
        self.load_btn.setText("Load capture")
        self.load_btn.setPopupMode(QToolButton.InstantPopup)
        self.load_menu = QMenu(self.load_btn)
        self.load_menu.aboutToShow.connect(self._fill_load_menu)
        self.load_btn.setMenu(self.load_menu)
        save_sample = QPushButton("Save as sample")
        save_sample.setToolTip("Store this capture in the corpus for platform + command")
        save_sample.clicked.connect(lambda: self.save_sample())
        ctx = QHBoxLayout()
        ctx.addWidget(self.ctx_platform)
        ctx.addWidget(self.ctx_command, 2)
        ctx.addWidget(self.load_btn)
        ctx.addWidget(save_sample)

        self.capture = QPlainTextEdit()
        self.capture.setFont(_mono())
        self.capture.setLineWrapMode(QPlainTextEdit.NoWrap)
        self.capture.setPlaceholderText("raw CLI output -- load a capture or paste")
        # (platform, command) the capture text belongs to. Set when a capture
        # is loaded; a hand edit/paste claims it for the current context.
        self._capture_key: tuple = ("", "")
        self._loading_capture = False
        self.capture.textChanged.connect(self._on_capture_edited)

        self.tabs = QTabWidget()
        self.tabs.addTab(self._build_editor_tab(), "Template")
        self.tabs.addTab(self._build_rank_tab(), "Rank")
        self.tabs.addTab(self._build_samples_tab(), "Samples")

        cap_box = QWidget()
        cl = QVBoxLayout(cap_box)
        cl.setContentsMargins(0, 0, 0, 0)
        cl.addLayout(ctx)
        cl.addWidget(self.capture, 1)
        right = QSplitter(Qt.Vertical)
        right.addWidget(cap_box)
        right.addWidget(self.tabs)
        right.setSizes([300, 600])

        split = QSplitter(Qt.Horizontal)
        split.addWidget(self.platform_list)
        split.addWidget(mid)
        split.addWidget(right)
        split.setSizes([220, 620, 760])
        self.setCentralWidget(split)
        from .about_dialog import add_help_menu
        add_help_menu(self)

        refresh = QAction("Refresh", self)
        refresh.setShortcut(QKeySequence("F5"))
        refresh.triggered.connect(self.refresh)
        self.addAction(refresh)

        self.refresh()

    # ═══════════════════════════════════════════════════════════════════════
    # Tabs
    # ═══════════════════════════════════════════════════════════════════════

    def _build_editor_tab(self) -> QWidget:
        self.ed_name = QLabel("(no template)")
        self.ed_name.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.editor = QPlainTextEdit()
        self.editor.setFont(_mono())
        self.editor.setLineWrapMode(QPlainTextEdit.NoWrap)
        self.ed_status = QLabel("")
        self.ed_status.setWordWrap(True)
        self.ed_result = QTableWidget(0, 0)
        self.ed_result.setEditTriggers(QTableWidget.NoEditTriggers)
        self.ed_result.verticalHeader().setVisible(False)

        test = QPushButton("Test")
        test.clicked.connect(self.test_editor)
        revert = QPushButton("Revert")
        revert.clicked.connect(lambda: self.open_template(self._current) if self._current else None)
        self.ed_save = QPushButton("Save")
        self.ed_save.setToolTip("Update this custom template in place")
        self.ed_save.clicked.connect(lambda: self.save_editor())
        self.ed_sibling = QPushButton("Save as sibling")
        self.ed_sibling.setToolTip("Write to the next numbered sibling; the base is untouched")
        self.ed_sibling.clicked.connect(lambda: self.save_editor(as_sibling=True))
        row = QHBoxLayout()
        row.addWidget(test)
        row.addWidget(revert)
        row.addStretch(1)
        row.addWidget(self.ed_sibling)
        row.addWidget(self.ed_save)

        split = QSplitter(Qt.Vertical)
        split.addWidget(self.editor)
        res = QWidget()
        rl = QVBoxLayout(res)
        rl.setContentsMargins(0, 0, 0, 0)
        rl.addWidget(self.ed_status)
        rl.addWidget(self.ed_result, 1)
        split.addWidget(res)
        split.setSizes([380, 220])

        w = QWidget()
        l = QVBoxLayout(w)
        l.addWidget(self.ed_name)
        l.addWidget(split, 1)
        l.addLayout(row)
        return w

    def _build_rank_tab(self) -> QWidget:
        self.rank_resolves = QLabel("")
        self.rank_resolves.setWordWrap(True)
        self.rank_table = QTableWidget(0, 6)
        self.rank_table.setHorizontalHeaderLabels(("On", "Template", "Score", "Records", "Fields", "Error"))
        self.rank_table.verticalHeader().setVisible(False)
        self.rank_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.rank_table.horizontalHeader().setStretchLastSection(True)
        self.rank_table.itemChanged.connect(self._on_rank_item_changed)
        self.rank_table.itemDoubleClicked.connect(
            lambda it: self._open_from(self.rank_table, it.row()))
        run = QPushButton("Rank")
        run.setToolTip("Score every sweep candidate for platform + command against the capture")
        run.clicked.connect(self.run_rank)
        top = QHBoxLayout()
        top.addWidget(run)
        top.addWidget(self.rank_resolves, 1)
        w = QWidget()
        l = QVBoxLayout(w)
        l.addLayout(top)
        l.addWidget(self.rank_table, 1)
        l.addWidget(QLabel("Double-click to open. Unchecking disables the template in the DB."))
        return w

    def _build_samples_tab(self) -> QWidget:
        self.sample_table = QTableWidget(0, 4)
        self.sample_table.setHorizontalHeaderLabels(("Id", "Label", "Created", "Lines"))
        self.sample_table.verticalHeader().setVisible(False)
        self.sample_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.sample_table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.sample_table.horizontalHeader().setStretchLastSection(True)
        self.sample_table.itemDoubleClicked.connect(lambda it: self.load_sample_row(it.row()))
        self.regress_label = QLabel("")
        self.regress_label.setWordWrap(True)
        load = QPushButton("Load")
        load.clicked.connect(lambda: self.load_sample_row(self.sample_table.currentRow()))
        delete = QPushButton("Delete")
        delete.clicked.connect(self._delete_sample)
        regress = QPushButton("Regress template")
        regress.setToolTip("Run the open template against every sample for platform + command")
        regress.clicked.connect(self.run_regress)
        row = QHBoxLayout()
        for b in (load, delete, regress):
            row.addWidget(b)
        row.addStretch(1)
        w = QWidget()
        l = QVBoxLayout(w)
        l.addWidget(self.sample_table, 1)
        l.addLayout(row)
        l.addWidget(self.regress_label)
        return w

    # ═══════════════════════════════════════════════════════════════════════
    # Browse
    # ═══════════════════════════════════════════════════════════════════════

    def refresh(self) -> None:
        self.refresh_platforms()
        self.refresh_templates()
        self.refresh_samples()

    def refresh_platforms(self) -> None:
        """Rebuild the platform list (counts), keeping the selection; does not
        touch the template table."""
        keep = self.selected_platform()
        self.platform_list.blockSignals(True)
        self.platform_list.clear()
        plats = self.store.platforms()
        total = sum(n for _, n, _ in plats)
        on = sum(e or 0 for _, _, e in plats)
        first = QListWidgetItem(f"(all)   {on}/{total}")
        first.setData(Qt.UserRole, _ALL)
        self.platform_list.addItem(first)
        for plat, n, e in plats:
            it = QListWidgetItem(f"{plat or '(no platform)'}   {e or 0}/{n}")
            it.setData(Qt.UserRole, plat)
            self.platform_list.addItem(it)
        row = next((i for i in range(self.platform_list.count())
                    if self.platform_list.item(i).data(Qt.UserRole) == keep), 0)
        self.platform_list.setCurrentRow(row)
        self.platform_list.blockSignals(False)

    def selected_platform(self) -> Optional[str]:
        it = self.platform_list.currentItem()
        if it is None:
            return None
        v = it.data(Qt.UserRole)
        return None if v == _ALL else v

    def select_platform(self, platform: Optional[str]) -> None:
        want = _ALL if platform is None else platform
        for i in range(self.platform_list.count()):
            if self.platform_list.item(i).data(Qt.UserRole) == want:
                self.platform_list.setCurrentRow(i)
                return

    def _override_names(self) -> set:
        out = set()
        for d in self.parser.override_dirs:
            if d.is_dir():
                out.update(p.stem for p in d.glob("*.textfsm"))
        return out

    def refresh_templates(self) -> None:
        rows = self.store.list(platform=self.selected_platform(), text=self.search.text(),
                               source=self.source_filter.currentData(),
                               enabled=self.state_filter.currentData())
        files = self._override_names()
        sel = set(self.selected_names())
        self.table.setSortingEnabled(False)
        self.table.blockSignals(True)
        self.table.setRowCount(len(rows))
        for r, t in enumerate(rows):
            self.table.setItem(r, 0, _check(t.enabled, t.name))
            self.table.setItem(r, 1, _item(t.name, t.name))
            self.table.setItem(r, 2, _item(t.command))
            self.table.setItem(r, 3, _item(t.source))
            self.table.setItem(r, 4, _item("file" if t.name in files else ""))
            self.table.setItem(r, 5, _item(t.updated))
            self.table.setItem(r, 6, _item(t.note))
        self.table.blockSignals(False)
        self.table.setSortingEnabled(True)
        self.table.resizeColumnsToContents()
        if sel:
            self.select_names(sel)
        n_on = sum(t.enabled for t in rows)
        self.count_label.setText(f"{len(rows)} shown, {n_on} enabled")

    def selected_names(self) -> List[str]:
        rows = sorted({i.row() for i in self.table.selectedIndexes()})
        return [self.table.item(r, 1).data(Qt.UserRole) for r in rows if self.table.item(r, 1)]

    def select_names(self, names: Iterable[str]) -> None:
        names = set(names)
        self.table.blockSignals(True)
        self.table.clearSelection()
        mode = self.table.selectionMode()
        self.table.setSelectionMode(QAbstractItemView.MultiSelection)
        for r in range(self.table.rowCount()):
            if self.table.item(r, 1).data(Qt.UserRole) in names:
                self.table.selectRow(r)
        self.table.setSelectionMode(mode)
        self.table.blockSignals(False)

    def _on_item_changed(self, it: QTableWidgetItem) -> None:
        # A checkbox edit updates the DB and the counts only; rebuilding the
        # table from inside its own itemChanged would delete the live item.
        if it.column() != 0:
            return
        self.store.set_enabled([it.data(Qt.UserRole)], it.checkState() == Qt.Checked)
        self.parser.reload_overrides()
        self.changed.emit()
        self.refresh_platforms()
        n = self.table.rowCount()
        on = sum(self.table.item(r, 0).checkState() == Qt.Checked for r in range(n))
        self.count_label.setText(f"{n} shown, {on} enabled")

    def _on_select(self) -> None:
        names = self.selected_names()
        if len(names) == 1 and names[0] != self._current:
            self.open_template(names[0])

    def _table_menu(self, pos) -> None:
        names = self.selected_names()
        if not names:
            return
        m = QMenu(self)
        m.addAction("Enable", lambda: self.set_enabled_selected(True))
        m.addAction("Disable", lambda: self.set_enabled_selected(False))
        m.addSeparator()
        if len(names) == 1:
            m.addAction("Clone to sibling", lambda: self.clone_selected())
            m.addAction("Rename...", lambda: self._rename_dialog(names[0]))
            m.addAction("Edit note...", lambda: self._note_dialog(names[0]))
            m.addAction("Set platform / command...", lambda: self._meta_dialog(names[0]))
            m.addAction("Rank for this command", lambda: self._rank_for(names[0]))
        m.addAction("Export...", self._export_dialog)
        m.addSeparator()
        m.addAction("Delete...", self._delete_dialog)
        m.exec(self.table.viewport().mapToGlobal(pos))

    # ═══════════════════════════════════════════════════════════════════════
    # Actions (dialog-free cores, testable headless)
    # ═══════════════════════════════════════════════════════════════════════

    def set_enabled_selected(self, on: bool) -> int:
        names = self.selected_names()
        n = self.store.set_enabled(names, on)
        self._changed()
        self._say_status(f"{'enabled' if on else 'disabled'} {n} template(s)")
        return n

    def clone_selected(self) -> Optional[str]:
        names = self.selected_names()
        if len(names) != 1:
            self._say_status("select one template to clone")
            return None
        try:
            dst = self.store.clone(names[0])
        except (KeyError, ValueError) as e:
            self._say_status(str(e))
            return None
        self._changed()
        self.select_names([dst])
        self.open_template(dst)
        self._say_status(f"cloned {names[0]} -> {dst}")
        return dst

    def delete(self, names: List[str], force: bool = False) -> List[str]:
        """Delete; returns the names refused (non-custom without force)."""
        refused = []
        for n in names:
            try:
                self.store.delete(n, force=force)
            except ValueError:
                refused.append(n)
        if self._current in names and self._current not in refused:
            self._current = None
            self.editor.clear()
            self.ed_name.setText("(no template)")
        self._changed()
        return refused

    def open_template(self, name: str) -> None:
        rec = self.store.get(name)
        if rec is None:
            return
        self._current = name
        self.editor.setPlainText(rec.content)
        state = "" if rec.enabled else "  [disabled]"
        shadow = name in self._override_names()
        self.ed_name.setText(f"<b>{name}</b>   {rec.source}{state}"
                             + ("   <span style='color:#d9534f'>shadowed by a file override"
                                "</span>" if shadow else ""))
        custom = rec.source == "custom"
        self.ed_save.setEnabled(custom)
        self.ed_save.setToolTip("Update this custom template in place" if custom else
                                f"{rec.source} template: save as a sibling instead")
        key = (rec.platform, rec.command)  # siblings carry their base's command
        self.ctx_platform.setText(key[0])
        self.ctx_command.setText(key[1])
        self.ed_result.setRowCount(0)
        self.ed_result.setColumnCount(0)
        self._say(self.ed_status, "", _MUTED)
        if key != self._capture_key:
            # Different command: the old capture can't test this template.
            # Prefer the template's own sample, then the newest stored one.
            stored = self.store.samples(*key)
            text = rec.sample or (stored[0].output if stored else "")
            self.set_capture(text, *key)
        self.refresh_samples()
        if self.capture.toPlainText().strip():
            self.test_editor()

    def test_editor(self) -> LabResult:
        res = run_textfsm(self.editor.toPlainText(),
                          self.parser.clean_output(self.capture.toPlainText()))
        if res.ok:
            _fill(self.ed_result, res.header, res.records)
            n = len(res.records)
            self._say(self.ed_status, f"{n} record{'' if n == 1 else 's'}", _OK if n else _MUTED)
        else:
            self.ed_result.setRowCount(0)
            self.ed_result.setColumnCount(0)
            if res.error_kind == "state" and res.input_line:
                self._say(self.ed_status, f"State Error at template line {res.rule_line}: "
                                          f"no rule matched -> {res.input_line}", _ERR)
                self._goto_line(self.editor, res.rule_line or 1)
            elif res.error_kind == "syntax":
                self._say(self.ed_status, f"template won't compile: {res.error}", _ERR)
            else:
                self._say(self.ed_status, res.error or "parse failed", _ERR)
        return res

    def save_editor(self, as_sibling: bool = False) -> Optional[str]:
        if not self._current:
            return None
        content = self.editor.toPlainText()
        name = self.store.next_sibling(self._current) if as_sibling else self._current
        sample = self.parser.clean_output(self.capture.toPlainText()) or None
        try:
            self.store.save(name, content, sample=sample)
        except ValueError as e:
            self._say(self.ed_status, str(e), _ERR)
            return None
        self._changed()
        self.select_names([name])
        self.open_template(name)
        self._say(self.ed_status, f"saved {name}", _OK)
        return name

    # -- rank ------------------------------------------------------------------

    def _rank_for(self, name: str) -> None:
        self.open_template(name)
        self.tabs.setCurrentIndex(1)
        self.run_rank()

    def run_rank(self) -> None:
        plat, cmd = self.ctx_platform.text().strip(), self.ctx_command.text().strip()
        raw = self.capture.toPlainText()
        if not raw.strip() or not cmd:
            self.rank_resolves.setText("need a command and a capture")
            return
        flt = Parser.exact_template(plat, cmd)
        rows = self.store.rank(self.parser.clean_output(raw), flt)
        self.rank_table.blockSignals(True)
        self.rank_table.setRowCount(len(rows))
        winner = next((r.name for r in rows if r.enabled and r.records), None)
        for i, r in enumerate(rows):
            self.rank_table.setItem(i, 0, _check(r.enabled, r.name))
            name = _item(r.name, r.name)
            if r.name == winner:
                f = name.font()
                f.setBold(True)
                name.setFont(f)
            self.rank_table.setItem(i, 1, name)
            self.rank_table.setItem(i, 2, _item(f"{r.score:.1f}", align_right=True))
            self.rank_table.setItem(i, 3, _item(str(r.records), align_right=True))
            self.rank_table.setItem(i, 4, _item(str(r.fields), align_right=True))
            self.rank_table.setItem(i, 5, _item(r.error or ""))
        self.rank_table.blockSignals(False)
        self.rank_table.resizeColumnsToContents()
        # What a widget with `template: auto` actually resolves to: exact name
        # first, sweep after. Fresh pins so this reflects the DB right now.
        self.parser.reload_overrides()
        p = self.parser.parse(plat, cmd, raw)
        if p.error:
            self.rank_resolves.setText(f"auto resolves to nothing: {p.error}  "
                                       f"({len(rows)} candidates for {flt})")
        else:
            self.rank_resolves.setText(
                f"auto resolves to <b>{p.template}</b> ({p.method}"
                + (f", score {p.score}" if p.score is not None else "")
                + f", {len(p.records)} records); sweep winner {winner or '-'}; "
                  f"{len(rows)} candidates for {flt}")

    def _on_rank_item_changed(self, it: QTableWidgetItem) -> None:
        if it.column() != 0:
            return
        self.store.set_enabled([it.data(Qt.UserRole)], it.checkState() == Qt.Checked)
        self._changed()
        QTimer.singleShot(0, self.run_rank)       # not from inside the table's own signal

    def _open_from(self, table: QTableWidget, row: int) -> None:
        it = table.item(row, 1)
        if it is not None:
            self.open_template(it.data(Qt.UserRole))
            self.tabs.setCurrentIndex(0)

    # -- samples / captures ------------------------------------------------------

    def save_sample(self, label: str = "") -> Optional[int]:
        plat, cmd = self.ctx_platform.text().strip(), self.ctx_command.text().strip()
        raw = self.capture.toPlainText()
        if not plat or not cmd or not raw.strip():
            self._say_status("sample needs platform, command and a capture")
            return None
        sid = self.store.add_sample(plat, cmd, raw, label)
        self._say_status(f"saved sample {sid}" if sid else "identical sample already stored")
        self.refresh_samples()
        return sid

    def refresh_samples(self) -> None:
        plat, cmd = self.ctx_platform.text().strip(), self.ctx_command.text().strip()
        samples = self.store.samples(plat, cmd) if cmd else []
        self.sample_table.setRowCount(len(samples))
        for i, s in enumerate(samples):
            self.sample_table.setItem(i, 0, _item(str(s.id), s.id, align_right=True))
            self.sample_table.setItem(i, 1, _item(s.label))
            self.sample_table.setItem(i, 2, _item(s.created))
            self.sample_table.setItem(i, 3, _item(str(s.output.count("\n") + 1), align_right=True))
        self.sample_table.resizeColumnsToContents()
        self.regress_label.setText("")

    def load_sample_row(self, row: int) -> None:
        it = self.sample_table.item(row, 0) if row >= 0 else None
        if it is None:
            return
        sid = it.data(Qt.UserRole)
        s = next((x for x in self.store.samples() if x.id == sid), None)
        if s is not None:
            self.set_capture(s.output, s.platform, s.command)
            if self._current:
                self.test_editor()

    def _delete_sample(self) -> None:
        it = self.sample_table.item(self.sample_table.currentRow(), 0)
        if it is not None:
            self.store.delete_sample(it.data(Qt.UserRole))
            self.refresh_samples()

    def run_regress(self) -> None:
        if not self._current:
            self.regress_label.setText("open a template first")
            return
        plat, cmd = self.ctx_platform.text().strip(), self.ctx_command.text().strip()
        res = self.store.regress(self._current, cleaner=self.parser.clean_output,
                                 platform=plat, command=cmd)
        if not res:
            self.regress_label.setText(f"no samples for {plat} / {cmd}")
            return
        bad = [r for r in res if r.error or not r.records]
        parts = [f"#{r.sample_id} {r.label or ''}: " + (r.error or f"{r.records} records")
                 for r in res]
        color = _ERR if bad else _OK
        self.regress_label.setStyleSheet(f"color: {color};")
        self.regress_label.setText(f"{self._current}: {len(res) - len(bad)}/{len(res)} samples "
                                   f"parse\n" + "\n".join(parts))

    def _fill_load_menu(self) -> None:
        self.load_menu.clear()
        live = list(self._captures())
        if live:
            self.load_menu.addSection("Live polls")
            for c in live:
                a = self.load_menu.addAction(f"{c.label}  ({c.platform})  {c.command}")
                a.triggered.connect(lambda _=False, c=c: self.load_live(c))
        plat, cmd = self.ctx_platform.text().strip(), self.ctx_command.text().strip()
        stored = self.store.samples(plat, cmd) if cmd else []
        if stored:
            self.load_menu.addSection(f"Samples: {cmd}")
            for s in stored[:25]:
                a = self.load_menu.addAction(f"#{s.id} {s.label or s.created}")
                a.triggered.connect(lambda _=False, s=s: self._load_stored(s))
        if not live and not stored:
            a = self.load_menu.addAction("no live polls or samples; paste output instead")
            a.setEnabled(False)

    def set_capture(self, text: str, platform: str, command: str) -> None:
        """Replace the capture and record which platform + command it is."""
        self._loading_capture = True
        try:
            self.capture.setPlainText(text)
        finally:
            self._loading_capture = False
        self._capture_key = (platform, command)
        if not text.strip():
            self._say(self.ed_status, f"no capture for {command or 'this command'}: "
                                      "Load capture or paste output", _MUTED)
            self.ed_result.setRowCount(0)
            self.ed_result.setColumnCount(0)

    def _on_capture_edited(self) -> None:
        if not self._loading_capture:
            self._capture_key = (self.ctx_platform.text().strip(), self.ctx_command.text().strip())

    def _load_stored(self, s) -> None:
        self.set_capture(s.output, s.platform, s.command)
        if self._current:
            self.test_editor()

    def load_live(self, c: LiveCapture) -> None:
        self.ctx_platform.setText(c.platform)
        self.ctx_command.setText(c.command)
        self.set_capture(c.output, c.platform, c.command)
        self.refresh_samples()
        if self._current:
            self.test_editor()

    # ═══════════════════════════════════════════════════════════════════════
    # Dialogs
    # ═══════════════════════════════════════════════════════════════════════

    def _rename_dialog(self, name: str) -> None:
        new, ok = QInputDialog.getText(self, "Rename template", "New name", text=name)
        if ok and new.strip() and new.strip() != name:
            try:
                self.store.rename(name, new.strip())
            except (KeyError, ValueError) as e:
                QMessageBox.warning(self, "Rename", str(e))
                return
            if self._current == name:
                self._current = new.strip()
            self._changed()

    def _note_dialog(self, name: str) -> None:
        rec = self.store.get(name)
        note, ok = QInputDialog.getText(self, "Note", name, text=rec.note if rec else "")
        if ok:
            self.store.set_meta(name, note=note)
            self._changed()

    def _meta_dialog(self, name: str) -> None:
        rec = self.store.get(name)
        if rec is None:
            return
        plat, ok = QInputDialog.getText(self, "Platform", name, text=rec.platform)
        if not ok:
            return
        cmd, ok = QInputDialog.getText(self, "Command", name, text=rec.command)
        if ok:
            self.store.set_meta(name, platform=plat.strip(), command=cmd.strip())
            self._changed()

    def _delete_dialog(self) -> None:
        names = self.selected_names()
        if not names:
            return
        if QMessageBox.question(self, "Delete", f"Delete {len(names)} template(s) from the DB?"
                                ) != QMessageBox.Yes:
            return
        refused = self.delete(names)
        if refused and QMessageBox.question(
                self, "Delete",
                f"{len(refused)} are ntc/chatgpt templates. Disabling keeps them recoverable.\n"
                f"Delete them anyway?") == QMessageBox.Yes:
            self.delete(refused, force=True)

    def _import_dialog(self) -> None:
        paths, _ = QFileDialog.getOpenFileNames(self, "Import templates", "",
                                                "TextFSM (*.textfsm *.template);;All (*)")
        errors = []
        for p in paths:
            try:
                self.store.import_file(p)
            except (ValueError, OSError) as e:
                errors.append(f"{Path(p).name}: {e}")
        self._changed()
        if errors:
            QMessageBox.warning(self, "Import", "\n".join(errors))

    def _export_dialog(self) -> None:
        names = self.selected_names()
        if not names:
            return
        d = QFileDialog.getExistingDirectory(self, "Export to")
        if d:
            self.store.export(names, d)
            self._say_status(f"exported {len(names)} to {d}")

    # ═══════════════════════════════════════════════════════════════════════
    # Helpers
    # ═══════════════════════════════════════════════════════════════════════

    def _changed(self) -> None:
        self.parser.reload_overrides()
        self.changed.emit()
        keep = self.table.verticalScrollBar().value()
        self.refresh()
        self.table.verticalScrollBar().setValue(keep)

    def _say_status(self, text: str) -> None:
        self.statusBar().showMessage(text, 8000)

    @staticmethod
    def _say(label: QLabel, text: str, color: str) -> None:
        label.setText(text)
        label.setStyleSheet(f"color: {color};")

    @staticmethod
    def _goto_line(edit: QPlainTextEdit, line_1based: int) -> None:
        block = edit.document().findBlockByLineNumber(max(0, line_1based - 1))
        if block.isValid():
            cur = QTextCursor(block)
            cur.select(QTextCursor.LineUnderCursor)
            edit.setTextCursor(cur)
            edit.centerCursor()


def _fill(table: QTableWidget, header: List[str], records: List[dict]) -> None:
    table.setColumnCount(len(header))
    table.setHorizontalHeaderLabels(header)
    table.setRowCount(len(records))
    for r, rec in enumerate(records):
        for c, col in enumerate(header):
            table.setItem(r, c, _item(str(rec.get(col, ""))))
    table.resizeColumnsToContents()
