"""Platform Pack editor -- build or extend a platform pack from pick lists.

    [platform v]  Validate  Save                         saved to ...
    Widgets | Session | Counters | YAML

Widgets: every widget with what feeds it on this platform now (inline widget
command, pack binding, off) and the best template the engine found. Select one
to see the ranked candidates, the field map (a Value pick list per field,
pre-filled from the widget's alias vocabulary), and a live preview through the
widget's own pipeline against the DB's sample output, a stored capture, or a
live poll. Nothing is a blank regex box.

Saves to ~/.terminaltelemetry2/platforms/<platform>.yaml (replacing any
bundled pack of that platform whole). New device windows pick it up.
"""
from __future__ import annotations

import dataclasses
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Optional

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QAbstractItemView, QCheckBox, QComboBox, QFormLayout, QHBoxLayout, QHeaderView, QLabel,
    QLineEdit, QMainWindow, QMessageBox, QPlainTextEdit, QPushButton, QSpinBox, QSplitter,
    QTableWidget, QTabWidget, QVBoxLayout, QWidget,
)

from .. import packbuilder as pb
from ..parsing import Parser
from ..platforms import PackError, Platforms, parse_pack, registry, set_registry
from ..widgets.schema import WidgetDef
from .template_manager import LiveCapture, _ERR, _MUTED, _OK, _item, _mono

_NONE = "(none)"
_SHOTGUN, _NOPAGING, _COMMANDS = "Paging shotgun (try common commands)", "None needed", "Commands"


def _combo(items: Iterable[str], editable: bool = False, current: str = "") -> QComboBox:
    c = QComboBox()
    c.setEditable(editable)
    for i in items:
        c.addItem(i)
    if current:
        i = c.findText(current)
        if i >= 0:
            c.setCurrentIndex(i)
        elif editable:
            c.setEditText(current)
    return c


def _csv(text: str) -> List[str]:
    return [x.strip() for x in text.split(",") if x.strip()]


class PackEditor(QMainWindow):
    saved = Signal(str)                        # path written

    def __init__(self, parser: Parser, widgets: Dict[str, WidgetDef], layouts: Iterable[str],
                 captures: Optional[Callable[[], Iterable[LiveCapture]]] = None,
                 save_dir: Optional[Path] = None, platform: str = "",
                 parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.parser = parser
        self.store = parser.store
        self.widgets = widgets
        self.layout_names = sorted(layouts)
        self._captures = captures or (lambda: [])
        if save_dir is None:
            from ..paths import platform_dirs
            save_dir = platform_dirs()[-1]
        self.save_dir = Path(save_dir)
        self.draft: Optional[pb.PackDraft] = None
        self._cands: List[pb.Candidate] = []
        self._widget: Optional[str] = None
        self._values: List[str] = []
        self._dirty = False
        self._catalog: Optional[List[pb.CatalogEntry]] = None
        self.setWindowTitle("Platform Pack editor - terminaltelemetry2")
        self.resize(1500, 950)

        # -- top bar ---------------------------------------------------------------
        self.platform = QComboBox()
        self.platform.setMinimumWidth(320)
        self.platform.currentIndexChanged.connect(lambda *_: self.load(self.platform.currentData()))
        validate = QPushButton("Validate")
        validate.clicked.connect(self.run_validate)
        save = QPushButton("Save pack")
        save.clicked.connect(lambda: self.save())
        self.status = QLabel("")
        self.status.setWordWrap(True)
        top = QHBoxLayout()
        top.addWidget(QLabel("Platform"))
        top.addWidget(self.platform)
        top.addWidget(validate)
        top.addWidget(save)
        top.addWidget(self.status, 1)

        self.tabs = QTabWidget()
        self.tabs.addTab(self._build_widgets_tab(), "Widgets")
        self.tabs.addTab(self._build_session_tab(), "Session")
        self.tabs.addTab(self._build_counters_tab(), "Counters")
        self.yaml_view = QPlainTextEdit()
        self.yaml_view.setReadOnly(True)
        self.yaml_view.setFont(_mono())
        self.tabs.addTab(self.yaml_view, "YAML")
        self.tabs.currentChanged.connect(lambda i: self.refresh_yaml() if i == 3 else None)

        root = QWidget()
        rl = QVBoxLayout(root)
        rl.addLayout(top)
        rl.addWidget(self.tabs, 1)
        self.setCentralWidget(root)
        from .about_dialog import add_help_menu
        add_help_menu(self)

        self._fill_platforms(platform)

    def set_widgets(self, widgets: Dict[str, WidgetDef]) -> None:
        """Pick up widgets saved since the editor opened (Widget Designer)."""
        self.widgets = dict(widgets)
        if self.draft is not None:
            self.refresh_widgets()

    # ═══════════════════════════════════════════════════════════════════════
    # Widgets tab
    # ═══════════════════════════════════════════════════════════════════════

    def _build_widgets_tab(self) -> QWidget:
        self.wtable = QTableWidget(0, 3)
        self.wtable.setHorizontalHeaderLabels(("Widget", "Now", "Suggestion"))
        self.wtable.verticalHeader().setVisible(False)
        self.wtable.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.wtable.setSelectionMode(QAbstractItemView.SingleSelection)
        self.wtable.horizontalHeader().setStretchLastSection(True)
        self.wtable.itemSelectionChanged.connect(self._on_widget_selected)
        accept = QPushButton("Accept confident suggestions")
        accept.setToolTip("Bind every unbound widget whose best suggestion is confident and "
                          "previews with rows on the sample output")
        accept.clicked.connect(lambda: self.accept_confident())
        left = QWidget()
        ll = QVBoxLayout(left)
        ll.setContentsMargins(0, 0, 0, 0)
        ll.addWidget(self.wtable, 1)
        ll.addWidget(accept)

        self.w_title = QLabel("select a widget")
        self.w_title.setWordWrap(True)
        self.ctable = QTableWidget(0, 3)
        self.ctable.setHorizontalHeaderLabels(("Template", "Command", "Why"))
        self.ctable.verticalHeader().setVisible(False)
        self.ctable.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.ctable.setSelectionMode(QAbstractItemView.SingleSelection)
        self.ctable.horizontalHeader().setStretchLastSection(True)
        self.ctable.itemSelectionChanged.connect(self._on_candidate_selected)
        self.show_all = QCheckBox("all templates for this platform")
        self.show_all.toggled.connect(lambda *_: self._fill_candidates())

        self.cmd = QLineEdit()
        self.cmd.setPlaceholderText("command sent to the device")
        self.cmd.editingFinished.connect(self.run_preview)
        self.template = QComboBox()
        self.template.setEditable(False)
        self.template.currentIndexChanged.connect(lambda *_: self._on_template_changed())
        self.sudo = QCheckBox("run with sudo -n (passwordless sudo on the device)")
        self.sudo.setToolTip("Keeps the command and parser; wraps the command in sudo -n, which "
                             "fails fast instead of prompting when NOPASSWD isn't set up")

        self.ftable = QTableWidget(0, 4)
        self.ftable.setHorizontalHeaderLabels(("Field", "Shown", "Template Value", "Match"))
        self.ftable.verticalHeader().setVisible(False)
        self.ftable.horizontalHeader().setSectionResizeMode(2, QHeaderView.Stretch)

        self.sample_src = QComboBox()
        self.sample_src.currentIndexChanged.connect(lambda *_: self._on_sample_source())
        self.sample = QPlainTextEdit()
        self.sample.setFont(_mono())
        self.sample.setLineWrapMode(QPlainTextEdit.NoWrap)
        self.sample.setPlaceholderText("sample output -- from the DB, a stored capture, a live poll, or paste")
        self.pv_status = QLabel("")
        self.pv_status.setWordWrap(True)
        self.pv_table = QTableWidget(0, 0)
        self.pv_table.verticalHeader().setVisible(False)
        self.pv_table.setEditTriggers(QTableWidget.NoEditTriggers)
        preview = QPushButton("Preview")
        preview.clicked.connect(self.run_preview)

        bind = QPushButton("Bind")
        bind.setToolTip("Write this command, template and field map into the pack")
        bind.clicked.connect(lambda: self.bind_current())
        off = QPushButton("Off on this platform")
        off.setToolTip("Hide the widget on this platform even if its file has a command")
        off.clicked.connect(lambda: self.set_off())
        clear = QPushButton("Remove from pack")
        clear.setToolTip("Drop the pack's binding; the widget file's own command (if any) applies")
        clear.clicked.connect(lambda: self.clear_binding())

        form = QFormLayout()
        form.addRow("Command", self.cmd)
        form.addRow("Template", self.template)
        form.addRow("", self.sudo)
        cands_box = QWidget()
        cb = QVBoxLayout(cands_box)
        cb.setContentsMargins(0, 0, 0, 0)
        cb.addWidget(QLabel("Candidates (best first)"))
        cb.addWidget(self.ctable, 1)
        cb.addWidget(self.show_all)

        src_row = QHBoxLayout()
        src_row.addWidget(QLabel("Sample"))
        src_row.addWidget(self.sample_src, 1)
        src_row.addWidget(preview)
        pv_box = QWidget()
        pl = QVBoxLayout(pv_box)
        pl.setContentsMargins(0, 0, 0, 0)
        pl.addLayout(src_row)
        split_s = QSplitter(Qt.Vertical)
        split_s.addWidget(self.sample)
        res = QWidget()
        rl = QVBoxLayout(res)
        rl.setContentsMargins(0, 0, 0, 0)
        rl.addWidget(self.pv_status)
        rl.addWidget(self.pv_table, 1)
        split_s.addWidget(res)
        pl.addWidget(split_s, 1)

        fields_box = QWidget()
        fl = QVBoxLayout(fields_box)
        fl.setContentsMargins(0, 0, 0, 0)
        fl.addLayout(form)
        fl.addWidget(self.ftable, 1)

        mid = QSplitter(Qt.Horizontal)
        mid.addWidget(cands_box)
        mid.addWidget(fields_box)
        mid.setSizes([420, 520])

        right_split = QSplitter(Qt.Vertical)
        right_split.addWidget(mid)
        right_split.addWidget(pv_box)
        right_split.setSizes([420, 460])

        btns = QHBoxLayout()
        btns.addStretch(1)
        btns.addWidget(clear)
        btns.addWidget(off)
        btns.addWidget(bind)
        right = QWidget()
        rr = QVBoxLayout(right)
        rr.setContentsMargins(0, 0, 0, 0)
        rr.addWidget(self.w_title)
        rr.addWidget(right_split, 1)
        rr.addLayout(btns)

        split = QSplitter(Qt.Horizontal)
        split.addWidget(left)
        split.addWidget(right)
        split.setSizes([430, 1070])
        return split

    # ═══════════════════════════════════════════════════════════════════════
    # Session / counters tabs
    # ═══════════════════════════════════════════════════════════════════════

    def _build_session_tab(self) -> QWidget:
        self.s_title = QLineEdit()
        self.s_aliases = QLineEdit()
        self.s_aliases.setPlaceholderText("comma separated, e.g. comware, h3c")
        self.s_vendors = QLineEdit()
        self.s_vendors.setPlaceholderText("session-file Vendor substrings, e.g. hpe, h3c")
        self.s_models = QLineEdit()
        self.s_models.setPlaceholderText("optional: Model patterns that pick this pack over a vendor sibling")
        self.s_tested = QCheckBox("tested against real gear")
        self.s_paging_mode = _combo((_SHOTGUN, _NOPAGING, _COMMANDS))
        self.s_paging = QPlainTextEdit()
        self.s_paging.setFont(_mono())
        self.s_paging.setPlaceholderText("one command per line, sent in order")
        self.s_paging.setMaximumHeight(110)
        self.s_paging_known = QComboBox()
        self.s_paging_known.activated.connect(self._insert_paging)
        self.s_paging_mode.currentIndexChanged.connect(
            lambda *_: self.s_paging.setEnabled(self.s_paging_mode.currentText() == _COMMANDS))
        # Privileged EXEC only. Never a config-mode command (configure,
        # system-view): every poll would then run inside configuration mode.
        self.s_enable = _combo(("", "enable", "enable 15"), editable=True)
        self.s_enable.setToolTip("Command that raises the session to privileged EXEC "
                                 "(e.g. 'enable'). Leave empty when the login already lands "
                                 "there. Config-mode commands are rejected.")
        self.s_suffix = QLineEdit()
        self.s_suffix.setPlaceholderText("rarely needed (MikroTik: +ct511w4098h)")
        self.s_shell = _combo(("", "posix"))
        self.s_read = QSpinBox()
        self.s_read.setRange(0, 600)
        self.s_read.setSuffix(" s")
        self.s_read.setSpecialValueText("default (3 s)")
        self.s_read.setToolTip("How long a command may stay silent before the read gives up")
        self.s_layout = _combo([""] + self.layout_names)

        paging_row = QHBoxLayout()
        paging_row.addWidget(self.s_paging_mode)
        paging_row.addWidget(QLabel("insert known:"))
        paging_row.addWidget(self.s_paging_known, 1)

        form = QFormLayout()
        form.setFieldGrowthPolicy(QFormLayout.AllNonFixedFieldsGrow)   # macOS default keeps them narrow
        form.addRow("Title", self.s_title)
        form.addRow("Aliases", self.s_aliases)
        form.addRow("Vendor match", self.s_vendors)
        form.addRow("Model match", self.s_models)
        form.addRow("", self.s_tested)
        form.addRow("Paging", paging_row)
        form.addRow("", self.s_paging)
        form.addRow("Enable command", self.s_enable)
        form.addRow("Username suffix", self.s_suffix)
        form.addRow("Shell", self.s_shell)
        form.addRow("Read timeout", self.s_read)
        form.addRow("Preferred layout", self.s_layout)
        w = QWidget()
        l = QVBoxLayout(w)
        l.addLayout(form)
        l.addStretch(1)
        return w

    def _build_counters_tab(self) -> QWidget:
        self.c_mode = _combo(("None (no traffic monitor)", "Preset", "Builtin parser (EOS/IOS/NX-OS/Junos/Linux)"))
        self.c_preset = QComboBox()
        for p in pb.COUNTER_PRESETS:
            self.c_preset.addItem(p.name, p)
        self.c_preset.addItem("Custom", None)
        self.c_preset.currentIndexChanged.connect(lambda *_: self._on_preset())
        self.c_cmd = QComboBox()
        self.c_cmd.setEditable(True)
        self.c_rx = QLineEdit()
        self.c_tx = QLineEdit()
        for e in (self.c_rx, self.c_tx):
            e.setFont(_mono())
        detect = QPushButton("Detect from DB samples")
        detect.setToolTip("Try every preset against this platform's interface samples in the DB")
        detect.clicked.connect(lambda: self.detect_counters())
        test = QPushButton("Test")
        test.clicked.connect(lambda: self.test_counters())
        self.c_sample = QPlainTextEdit()
        self.c_sample.setFont(_mono())
        self.c_sample.setLineWrapMode(QPlainTextEdit.NoWrap)
        self.c_sample.setPlaceholderText("output of the counter command for one interface")
        self.c_result = QLabel("")
        form = QFormLayout()
        form.setFieldGrowthPolicy(QFormLayout.AllNonFixedFieldsGrow)
        form.addRow("Mode", self.c_mode)
        form.addRow("Preset", self.c_preset)
        form.addRow("Command", self.c_cmd)
        form.addRow("rx bytes", self.c_rx)
        form.addRow("tx bytes", self.c_tx)
        row = QHBoxLayout()
        row.addWidget(detect)
        row.addWidget(test)
        row.addWidget(self.c_result, 1)
        w = QWidget()
        l = QVBoxLayout(w)
        l.addLayout(form)
        l.addLayout(row)
        l.addWidget(self.c_sample, 1)
        return w

    # ═══════════════════════════════════════════════════════════════════════
    # Load
    # ═══════════════════════════════════════════════════════════════════════

    def _fill_platforms(self, select: str = "") -> None:
        reg = registry()
        db = {p: n for p, n, _ in self.store.platforms() if p}
        names = sorted(set(db) | set(reg.packs))
        self.platform.blockSignals(True)
        self.platform.clear()
        for p in names:
            pack = reg.get(p)
            tag = "no pack" if pack is None else ("tested" if pack.tested else "pack")
            self.platform.addItem(f"{p}   ({db.get(p, 0)} templates, {tag})", p)
        self.platform.blockSignals(False)
        i = max(0, self.platform.findData(select)) if select else 0
        self.platform.setCurrentIndex(i)
        self.load(self.platform.currentData())

    def load(self, platform: Optional[str]) -> None:
        if not platform:
            return
        pack = registry().get(platform)
        self.draft = pb.PackDraft.from_pack(pack) if pack else pb.PackDraft(platform)
        self._catalog = pb.platform_catalog(self.store, platform)
        self._dirty = False
        self._load_session()
        self._load_counters()
        self.refresh_widgets()
        self._say(f"{platform}: " + ("editing its pack" if pack else "no pack yet -- building a new one"),
                  _MUTED)

    def _load_session(self) -> None:
        d = self.draft
        self.s_title.setText(d.title)
        self.s_aliases.setText(", ".join(d.aliases))
        self.s_vendors.setText(", ".join(d.vendors))
        self.s_models.setText(", ".join(d.models))
        self.s_tested.setChecked(d.tested)
        mode = _SHOTGUN if d.paging is None else _NOPAGING if not d.paging else _COMMANDS
        self.s_paging_mode.setCurrentText(mode)
        self.s_paging.setPlainText("\n".join(d.paging or []))
        self.s_paging.setEnabled(mode == _COMMANDS)
        known = sorted({c for p in registry().packs.values() for c in (p.paging or [])})
        self.s_paging_known.clear()
        self.s_paging_known.addItem("")
        for c in known:
            self.s_paging_known.addItem(c)
        self.s_enable.setEditText(d.enable or "")
        self.s_suffix.setText(d.username_suffix or "")
        self.s_shell.setCurrentText(d.shell or "")
        self.s_read.setValue(int(d.read_timeout or 0))
        self.s_layout.setCurrentText(d.layout or "")

    def _insert_paging(self, idx: int) -> None:
        c = self.s_paging_known.itemText(idx)
        if not c:
            return
        self.s_paging_mode.setCurrentText(_COMMANDS)
        lines = [x for x in self.s_paging.toPlainText().splitlines() if x.strip()]
        if c not in lines:
            lines.append(c)
        self.s_paging.setPlainText("\n".join(lines))
        self.s_paging_known.setCurrentIndex(0)

    def _load_counters(self) -> None:
        c = self.draft.counters
        self.c_cmd.clear()
        for _, cmd in pb.interface_commands(self.store, self.draft.platform)[:12]:
            self.c_cmd.addItem(f"{cmd} {{intf}}")
        self.c_sample.clear()
        self.c_result.setText("")
        if not c:
            self.c_mode.setCurrentIndex(0)
            self.c_rx.clear()
            self.c_tx.clear()
            return
        self.c_cmd.setEditText(c["command"])
        if c.get("parser") == "builtin":
            self.c_mode.setCurrentIndex(2)
            return
        self.c_mode.setCurrentIndex(1)
        idx = next((i for i, p in enumerate(pb.COUNTER_PRESETS)
                    if p.rx == c.get("rx") and p.tx == c.get("tx")), len(pb.COUNTER_PRESETS))
        self.c_preset.setCurrentIndex(idx)
        self._on_preset()
        self.c_rx.setText(c.get("rx", ""))
        self.c_tx.setText(c.get("tx", ""))

    # ═══════════════════════════════════════════════════════════════════════
    # Widgets: list, candidates, fields, preview
    # ═══════════════════════════════════════════════════════════════════════

    def _now(self, name: str) -> str:
        plat = self.draft.platform
        if name in self.draft.bindings:
            b = self.draft.bindings[name]
            if b is None:
                return "off"
            cmd = b.get("command") or self.widgets[name].command_for(self.draft.platform) or "?"
            return f"pack: {cmd}" + ("  [sudo]" if b.get("sudo") else "")
        cmd = self.widgets[name].command_for(plat)
        return f"widget file: {cmd}" if cmd else ""

    def refresh_widgets(self) -> None:
        keep = self._widget
        self._best: Dict[str, Optional[pb.Candidate]] = {}
        self.wtable.blockSignals(True)
        self.wtable.setRowCount(len(self.widgets))
        for r, name in enumerate(sorted(self.widgets)):
            w = self.widgets[name]
            cands = pb.suggest_templates(self.store, self.draft.platform, w, limit=1, catalog=self._catalog)
            best = cands[0] if cands else None
            self._best[name] = best
            self.wtable.setItem(r, 0, _item(f"{w.title}  ({name})", name))
            now = self._now(name)
            it = _item(now or "-")
            if not now:
                it.setForeground(Qt.gray)
            self.wtable.setItem(r, 1, it)
            if best is not None and best.confident:
                sug = _item(f"{best.template.replace(self.draft.platform + '_', '')}  ({best.reason})")
            else:
                sug = _item("no confident match" if best is not None else "")
                sug.setForeground(Qt.gray)
                if best is not None:
                    sug.setToolTip(f"closest: {best.template} ({best.reason})")
            self.wtable.setItem(r, 2, sug)
        self.wtable.blockSignals(False)
        self.wtable.resizeColumnsToContents()
        if keep:
            self.select_widget(keep)

    def select_widget(self, name: str) -> None:
        for r in range(self.wtable.rowCount()):
            if self.wtable.item(r, 0).data(Qt.UserRole) == name:
                self.wtable.selectRow(r)
                return

    def _on_widget_selected(self) -> None:
        rows = {i.row() for i in self.wtable.selectedIndexes()}
        if not rows:
            return
        name = self.wtable.item(rows.pop(), 0).data(Qt.UserRole)
        self._widget = name
        w = self.widgets[name]
        used = pb.used_fields(w)
        self.w_title.setText(f"<b>{w.title}</b> ({name}) -- shows: {', '.join(used)}"
                             f"<br><span style='color:{_MUTED}'>now: {self._now(name) or 'not on this platform'}</span>")
        self._fill_candidates()
        self._fill_sample_sources()
        b = self.draft.bindings.get(name)
        if b:                                   # an existing pack binding: show it, not a suggestion
            self._load_binding(b)
        elif w.command_for(self.draft.platform) or not self._cands:
            # Already fed by the widget file (e.g. Linux python parsers): show that,
            # not the top template suggestion.
            self.sudo.setChecked(False)
            self.cmd.setText(w.command_for(self.draft.platform) or "")
            self._set_template_choices([c.template for c in self._cands])
            self._fill_fields({})
            self._fill_sample_sources()
            self.run_preview()
        else:
            self.sudo.setChecked(False)
            self.ctable.selectRow(0)

    def _fill_candidates(self) -> None:
        if not self._widget:
            return
        w = self.widgets[self._widget]
        limit = 500 if self.show_all.isChecked() else 10
        self._cands = pb.suggest_templates(self.store, self.draft.platform, w, limit=limit, catalog=self._catalog)
        if self.show_all.isChecked():           # everything, even unscored
            seen = {c.template for c in self._cands}
            for info in self.store.list(platform=self.draft.platform, enabled=True):
                if info.name not in seen:
                    m = pb.suggest_field_map(w, pb.template_values(self.store.content(info.name) or ""))
                    self._cands.append(pb.Candidate(info.name, info.command, 0.0, 0.0,
                                                    pb.coverage(w, m), m, False))
        self.ctable.blockSignals(True)
        self.ctable.setRowCount(len(self._cands))
        for r, c in enumerate(self._cands):
            name = _item(c.template.replace(self.draft.platform + "_", ""), c.template)
            if c.confident:
                f = name.font()
                f.setBold(True)
                name.setFont(f)
            self.ctable.setItem(r, 0, name)
            self.ctable.setItem(r, 1, _item(c.command))
            self.ctable.setItem(r, 2, _item(c.reason + ("" if c.on_topic else " · off topic")))
        self.ctable.blockSignals(False)
        self.ctable.resizeColumnsToContents()

    def _on_candidate_selected(self) -> None:
        rows = {i.row() for i in self.ctable.selectedIndexes()}
        if not rows:
            return
        c = self._cands[rows.pop()]
        self.cmd.setText(c.command)
        self._set_template_choices([c.template], select=c.template)
        self._fill_sample_sources()
        self._fill_fields({f: m for f, m in c.mapping.items()})
        self.run_preview()

    def _set_template_choices(self, names: List[str], select: str = "") -> None:
        self.template.blockSignals(True)
        self.template.clear()
        self.template.addItem("auto (resolve by command)", "")
        for n in names:
            self.template.addItem(n, n)
        if select:
            self.template.setCurrentIndex(max(0, self.template.findData(select)))
        self.template.blockSignals(False)

    def _on_template_changed(self) -> None:
        t = self.template.currentData()
        if not self._widget:
            return
        vals = pb.template_values(self.store.content(t) or "") if t else []
        self._fill_fields(pb.suggest_field_map(self.widgets[self._widget], vals))
        self.run_preview()

    def _load_binding(self, b: dict) -> None:
        """Show an existing pack binding: its command, template and overlays."""
        w = self.widgets[self._widget]
        cmd = b.get("command") or w.command_for(self.draft.platform) or ""
        self.cmd.setText(cmd)
        self.sudo.setChecked(bool(b.get("sudo")))
        t = b.get("template") or Parser.exact_template(self.draft.platform, cmd)
        known = self.store.get(t) is not None
        names = [t] if known else []
        for c in self._cands:
            if c.template not in names:
                names.append(c.template)
        self._set_template_choices(names, select=t if known else "")
        vals = pb.template_values(self.store.content(t) or "") if known else []
        mapping = pb.suggest_field_map(w, vals)
        for f, al in (b.get("fields") or {}).items():
            if f in mapping:
                mapping[f] = pb.FieldMatch(f, al[0], "picked", 1.0)
        self._fill_fields(mapping)
        self._fill_sample_sources()
        self.run_preview()

    def _fill_fields(self, mapping: Dict[str, pb.FieldMatch]) -> None:
        w = self.widgets[self._widget]
        t = self.template.currentData()
        self._values = pb.template_values(self.store.content(t) or "") if t else []
        used = set(pb.used_fields(w))
        order = pb.used_fields(w) + [f for f in w.fields if f not in used]
        self.ftable.setRowCount(len(order))
        for r, f in enumerate(order):
            m = mapping.get(f) or pb.FieldMatch(f, None, "none")
            self.ftable.setItem(r, 0, _item(f, f))
            self.ftable.setItem(r, 1, _item("yes" if f in used else ""))
            combo = QComboBox()
            combo.addItem(_NONE, None)
            for v in self._values:
                combo.addItem(v, v)
            if m.value and combo.findData(m.value) < 0:
                combo.addItem(m.value, m.value)
            combo.setCurrentIndex(max(0, combo.findData(m.value)))
            combo.currentIndexChanged.connect(lambda _i, row=r: self._on_field_picked(row))
            self.ftable.setCellWidget(r, 2, combo)
            how = _item(m.how if m.value else ("unresolved" if f in used else ""))
            if m.how == "fuzzy":
                how.setForeground(Qt.darkYellow)
            elif not m.value and f in used:
                how.setForeground(Qt.red)
            self.ftable.setItem(r, 3, how)
        self.ftable.resizeColumnToContents(0)

    def _on_field_picked(self, row: int) -> None:
        self.ftable.setItem(row, 3, _item("picked"))
        self.run_preview()

    def chosen_fields(self) -> Dict[str, Optional[str]]:
        out = {}
        for r in range(self.ftable.rowCount()):
            combo = self.ftable.cellWidget(r, 2)
            out[self.ftable.item(r, 0).data(Qt.UserRole)] = combo.currentData() if combo else None
        return out

    def current_binding(self) -> Optional[dict]:
        if not self._widget or not self.cmd.text().strip():
            return None
        w = self.widgets[self._widget]
        cmd = self.cmd.text().strip()
        b = pb.make_binding(w, self.draft.platform, cmd, self.template.currentData() or "",
                            self._values, self.chosen_fields())
        if cmd == w.command_for(self.draft.platform) and not self.template.currentData():
            # Same command the widget file already has: don't restate it -- a pack
            # command replaces the widget's own parser/requires (python parsers on
            # Linux would be lost). Keep only what differs.
            b.pop("command", None)
        if self.sudo.isChecked():
            b["sudo"] = True
        return b

    def _spec_for(self, name: str, binding: dict) -> WidgetDef:
        return Platforms([parse_pack({"platform": self.draft.platform,
                                      "bindings": {name: binding}})]).apply(self.widgets,
                                                                              self.draft.platform)[name]

    # -- sample + preview --------------------------------------------------------

    def _fill_sample_sources(self) -> None:
        plat, cmd = self.draft.platform, self.cmd.text().strip()
        t = self.template.currentData() or (Parser.exact_template(plat, cmd) if cmd else "")
        self.sample_src.blockSignals(True)
        self.sample_src.clear()
        rec = self.store.get(t) if t else None
        if rec and rec.sample:
            self.sample_src.addItem(f"DB sample of {t}", ("db", rec.sample))
        for s in self.store.samples(plat, cmd) if cmd else []:
            self.sample_src.addItem(f"stored #{s.id} {s.label or s.created}", ("stored", s.output))
        for c in self._captures():
            if c.platform == plat:
                self.sample_src.addItem(f"live: {c.label} {c.command}", ("live", c.output))
        self.sample_src.addItem("paste / edit below", ("paste", None))
        self.sample_src.blockSignals(False)
        self.sample_src.setCurrentIndex(0)
        self._on_sample_source()

    def _on_sample_source(self) -> None:
        d = self.sample_src.currentData()
        if d and d[1] is not None:
            self.sample.setPlainText(d[1])

    def run_preview(self) -> Optional[pb.Preview]:
        b = self.current_binding()
        if b is None:
            self.pv_status.setText("")
            return None
        spec = self._spec_for(self._widget, b)
        pv = pb.preview(self.parser, spec, self.draft.platform, spec.command_for(self.draft.platform),
                        b.get("template") or self.template.currentData()
                        or spec.template_for(self.draft.platform),
                        self.sample.toPlainText())
        used = pb.used_fields(self.widgets[self._widget])
        if pv.error:
            self.pv_status.setText(f"<span style='color:{_ERR}'>{pv.error}</span>")
            self.pv_table.setRowCount(0)
            return pv
        filled = [f for f in used if any(r.get(f) not in (None, "") for r in pv.rows)]
        color = _OK if pv.rows and len(filled) == len(used) else _ERR if not pv.rows else "#c98a00"
        note = ("" if pv.rows or not pv.records else
                " -- the widget's own filters dropped every row; this template may be the wrong kind")
        self.pv_status.setText(
            f"<span style='color:{color}'>{pv.records} records -> {len(pv.rows)} rows; "
            f"fields filled {len(filled)}/{len(used)}</span> via {pv.template}{note}"
            + (f"<br><span style='color:{_MUTED}'>empty: {', '.join(f for f in used if f not in filled)}</span>"
               if pv.rows and len(filled) < len(used) else ""))
        self.pv_table.setColumnCount(len(used))
        self.pv_table.setHorizontalHeaderLabels(used)
        self.pv_table.setRowCount(min(len(pv.rows), 200))
        for r, row in enumerate(pv.rows[:200]):
            for c, f in enumerate(used):
                v = row.get(f)
                self.pv_table.setItem(r, c, _item("" if v is None else str(v)))
        self.pv_table.resizeColumnsToContents()
        return pv

    # -- binding actions -----------------------------------------------------------

    def bind_current(self) -> Optional[dict]:
        b = self.current_binding()
        if b is None:
            self._say("pick a template or enter a command first", _ERR)
            return None
        self.draft.bindings[self._widget] = b
        self._mark_dirty(f"bound {self._widget}")
        return b

    def set_off(self) -> None:
        if self._widget:
            self.draft.bindings[self._widget] = None
            self._mark_dirty(f"{self._widget} off on {self.draft.platform}")

    def clear_binding(self) -> None:
        if self._widget and self._widget in self.draft.bindings:
            del self.draft.bindings[self._widget]
            self._mark_dirty(f"removed {self._widget} from the pack")

    def accept_confident(self) -> List[str]:
        """Bind every widget with nothing feeding it on this platform whose best
        suggestion is confident and previews with rows on the DB sample."""
        plat, taken = self.draft.platform, []
        for name, w in sorted(self.widgets.items()):
            if name in self.draft.bindings or w.command_for(plat):
                continue
            best = next((c for c in pb.suggest_templates(self.store, plat, w, limit=3, catalog=self._catalog) if c.confident), None)
            if best is None:
                continue
            vals = pb.template_values(self.store.content(best.template) or "")
            b = pb.make_binding(w, plat, best.command, best.template, vals,
                                {f: m.value for f, m in best.mapping.items()})
            pv = pb.preview(self.parser, self._spec_for(name, b), plat, b.get("command") or w.command_for(plat),
                            b.get("template") or best.template,
                            pb.sample_for(self.store, best.template, plat, best.command))
            if pv.rows:
                self.draft.bindings[name] = b
                taken.append(name)
        self._mark_dirty(f"accepted {len(taken)}: {', '.join(taken)}" if taken else
                         "no unbound widget has a confident suggestion that previews with rows")
        return taken

    # -- counters ----------------------------------------------------------------------

    def _on_preset(self) -> None:
        p = self.c_preset.currentData()
        custom = p is None
        self.c_rx.setReadOnly(not custom)
        self.c_tx.setReadOnly(not custom)
        if p is not None:
            self.c_rx.setText(p.rx)
            self.c_tx.setText(p.tx)

    def detect_counters(self) -> bool:
        hits = pb.detect_counters(self.store, self.draft.platform)
        if not hits:
            self.c_result.setText("no preset matched this platform's DB samples; paste output and Test")
            self.c_result.setStyleSheet(f"color: {_ERR};")
            return False
        cmd, tname, preset, (rx, tx) = hits[0]
        self.c_mode.setCurrentIndex(1)
        self.c_preset.setCurrentIndex(pb.COUNTER_PRESETS.index(preset))
        self._on_preset()                        # no signal when the index doesn't change
        self.c_cmd.setEditText(cmd)
        self.c_sample.setPlainText(pb.sample_for(self.store, tname, self.draft.platform,
                                                 cmd.replace(" {intf}", "")))
        self.c_result.setText(f"{preset.name}: rx {rx:,} tx {tx:,} bytes on the {tname} sample")
        self.c_result.setStyleSheet(f"color: {_OK};")
        return True

    def test_counters(self) -> Optional[tuple]:
        try:
            p = pb.CounterPreset("custom", self.c_rx.text(), self.c_tx.text())
            v = pb.try_preset(p, self.c_sample.toPlainText())
        except Exception as e:
            self.c_result.setText(f"bad regex: {e}")
            self.c_result.setStyleSheet(f"color: {_ERR};")
            return None
        if v is None:
            self.c_result.setText("no match in the sample")
            self.c_result.setStyleSheet(f"color: {_ERR};")
            return None
        self.c_result.setText(f"rx {v[0]:,}  tx {v[1]:,} bytes")
        self.c_result.setStyleSheet(f"color: {_OK};")
        return v

    # ═══════════════════════════════════════════════════════════════════════
    # Draft <- form, validate, save
    # ═══════════════════════════════════════════════════════════════════════

    def collect(self) -> pb.PackDraft:
        """Session and counters tabs into the draft (bindings are live already)."""
        d = self.draft
        d.title = self.s_title.text().strip()
        d.aliases = [a.lower() for a in _csv(self.s_aliases.text())]
        d.vendors = [v.lower() for v in _csv(self.s_vendors.text())]
        d.models = _csv(self.s_models.text())
        d.tested = self.s_tested.isChecked()
        mode = self.s_paging_mode.currentText()
        d.paging = (None if mode == _SHOTGUN else [] if mode == _NOPAGING else
                    [x.strip() for x in self.s_paging.toPlainText().splitlines() if x.strip()])
        d.enable = self.s_enable.currentText().strip() or None
        d.username_suffix = self.s_suffix.text().strip() or None
        d.shell = self.s_shell.currentText() or None
        d.read_timeout = float(self.s_read.value()) or None
        d.layout = self.s_layout.currentText() or None
        mode_c = self.c_mode.currentIndex()
        cmd = self.c_cmd.currentText().strip()
        if mode_c == 0 or not cmd:
            d.counters = None
        elif mode_c == 2:
            d.counters = {"command": cmd, "parser": "builtin"}
        else:
            d.counters = {"command": cmd, "rx": self.c_rx.text(), "tx": self.c_tx.text()}
        return d

    def refresh_yaml(self) -> None:
        self.yaml_view.setPlainText(self.collect().to_yaml())

    def run_validate(self) -> List[str]:
        """Parse the draft as a pack and check bindings against the widgets."""
        d = self.collect()
        try:
            pack = d.validate()
        except PackError as e:
            self._say(str(e), _ERR)
            return [str(e)]
        warns: List[str] = []
        Platforms([pack]).apply(self.widgets, d.platform, warnings=warns)
        self._say("valid" if not warns else "; ".join(warns), _OK if not warns else _ERR)
        return warns

    def save(self, confirm: bool = True) -> Optional[Path]:
        """Validate, then write the user pack. A pack that doesn't parse is
        never written; binding warnings (unknown widget/field) are shown but
        don't block -- check_platforms reports them too."""
        self.run_validate()
        try:
            self.draft.validate()
        except PackError:
            return None
        path = self.save_dir / f"{self.draft.platform}.yaml"
        if confirm and path.exists() and QMessageBox.question(
                self, "Save pack", f"Replace {path}?") != QMessageBox.Yes:
            return None
        self.save_dir.mkdir(parents=True, exist_ok=True)
        path.write_text(self.draft.to_yaml(), encoding="utf-8")
        folded = self._fold_other_packs(path)
        set_registry(None)                       # next registry() call reloads from disk
        self._dirty = False
        self._say(f"saved {path}" + (f"; folded in and set aside: {', '.join(folded)}" if folded else "")
                  + " -- reload open device windows to use it", _OK)
        self.saved.emit(str(path))
        return path

    def _fold_other_packs(self, saved: Path) -> List[str]:
        """The draft started from the effective pack (bundled + every user
        file for this platform, merges included), so the saved file already
        contains what the others contributed. Left in place they'd load on top
        of it and undo this save (a merge overlay setting read_timeout: 3 beats
        the 20 just saved). Rename them *.yaml.folded -- kept, not deleted."""
        import yaml as _yaml
        out = []
        for p in sorted(self.save_dir.glob("*.yaml")):
            if p == saved:
                continue
            try:
                data = _yaml.safe_load(p.read_text(encoding="utf-8")) or {}
            except Exception:
                continue
            if isinstance(data, dict) and str(data.get("platform", "")).strip() == self.draft.platform:
                p.rename(p.with_name(p.name + ".folded"))
                out.append(p.name)
        return out

    # -- helpers ---------------------------------------------------------------------

    def _mark_dirty(self, msg: str) -> None:
        self._dirty = True
        self.refresh_widgets()
        self._say(msg + "  (unsaved)", _MUTED)

    def _say(self, text: str, color: str) -> None:
        self.status.setText(text)
        self.status.setStyleSheet(f"color: {color};")

    def closeEvent(self, ev):
        if self._dirty and QMessageBox.question(
                self, "Platform Pack editor", "Discard unsaved pack changes?") != QMessageBox.Yes:
            ev.ignore()
            return
        super().closeEvent(ev)
