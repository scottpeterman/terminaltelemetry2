"""Widget Designer -- a new widget from a template and a real sample.

    Platform [v]  Template [v]  Sample [v]   Start over
    Name  Title  Interval  View [table|kv|stat]  Key [v]  unique
    Fields (include, name, kind, label) | Columns + sort | Patterns
    Preview: the real widget view on the sample        | YAML
    Add to layout [v]   Bind other platforms...   Save widget

Everything starts filled in (designer.suggest_spec): field names from the
bundled widgets' vocabulary, kinds from the sample, alert/rate/bar patterns
with values taken from the sample. Edits re-render the preview through the
same pipeline and view classes a device window uses.
"""
from __future__ import annotations

from pathlib import Path
from typing import Callable, Dict, Iterable, List, Optional

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QAbstractItemView, QCheckBox, QComboBox, QDoubleSpinBox, QFormLayout, QGroupBox,
    QHBoxLayout, QHeaderView, QLabel, QLineEdit, QListWidget, QListWidgetItem, QMainWindow,
    QMessageBox, QPlainTextEdit, QPushButton, QSpinBox, QSplitter, QTableWidget,
    QTableWidgetItem, QTabWidget, QVBoxLayout, QWidget,
)

from .. import designer as dz
from ..layout import LayoutDef
from ..parsing import Parser
from ..paths import PKG_DATA
from ..widgets.schema import AGGREGATES, MIN_INTERVAL, WidgetDef
from ..widgets.views import make_view
from .template_manager import LiveCapture, _ERR, _MUTED, _OK, _item, _mono

_NO_LAYOUT = "(don't add to a layout)"


class WidgetDesigner(QMainWindow):
    saved = Signal(str, str)                   # widget name, path
    packs_requested = Signal(str)              # open the Pack editor on this platform

    def __init__(self, parser: Parser, widgets: Dict[str, WidgetDef],
                 layouts: Dict[str, LayoutDef],
                 captures: Optional[Callable[[], Iterable[LiveCapture]]] = None,
                 widget_dir: Optional[Path] = None, layout_dir: Optional[Path] = None,
                 platform: str = "", template: str = "", parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.parser = parser
        self.store = parser.store
        self.widgets = dict(widgets)
        self.layouts = dict(layouts)
        self.vocab = dz.Vocabulary(self.widgets)
        self._captures = captures or (lambda: [])
        if widget_dir is None or layout_dir is None:
            from ..paths import layout_dirs, widget_dirs
            widget_dir = widget_dir or widget_dirs()[-1]
            layout_dir = layout_dir or layout_dirs()[-1]
        self.widget_dir, self.layout_dir = Path(widget_dir), Path(layout_dir)
        self.spec: Optional[dz.WidgetSpec] = None
        self.records: List[dict] = []
        self._view = None
        self._quiet = False                    # suppress handlers while repopulating
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(200)
        self._timer.timeout.connect(self.refresh)
        self.setWindowTitle("Widget Designer - terminaltelemetry2")
        self.resize(1600, 1000)

        # -- source row ----------------------------------------------------------------
        self.platform = QComboBox()
        self.platform.setMinimumWidth(220)
        self.platform.currentIndexChanged.connect(lambda *_: self._on_platform())
        self.template = QComboBox()
        self.template.setEditable(True)
        self.template.setInsertPolicy(QComboBox.NoInsert)
        self.template.setMinimumWidth(480)
        self.template.activated.connect(lambda *_: self.start(self.template.currentData()))
        self.sample_src = QComboBox()
        self.sample_src.setMinimumWidth(260)
        self.sample_src.activated.connect(lambda *_: self._on_sample_source())
        restart = QPushButton("Start over")
        restart.setToolTip("Re-suggest everything from this template and sample")
        restart.clicked.connect(lambda: self.start(self.template.currentData(), keep_sample=True))
        src = QHBoxLayout()
        for lab, w in (("Platform", self.platform), ("Template", self.template), ("Sample", self.sample_src)):
            src.addWidget(QLabel(lab))
            src.addWidget(w)
        src.addWidget(restart)
        src.addStretch(1)

        # -- identity row --------------------------------------------------------------
        self.name = QLineEdit()
        self.name.setMaximumWidth(260)
        self.name.textEdited.connect(lambda t: self._set("name", t.strip()))
        self.title = QLineEdit()
        self.title.setMaximumWidth(260)
        self.title.textEdited.connect(lambda t: self._set("title", t))
        self.interval = QSpinBox()
        self.interval.setRange(int(MIN_INTERVAL), 3600)
        self.interval.setSuffix(" s")
        self.interval.valueChanged.connect(lambda v: self._set("interval", float(v)))
        self.view_type = QComboBox()
        self.view_type.addItems(["table", "kv", "stat"])
        self.view_type.currentTextChanged.connect(self._on_view_type)
        self.key = QComboBox()
        self.key.currentIndexChanged.connect(lambda *_: self._on_key())
        self.unique = QCheckBox("one row per key")
        self.unique.toggled.connect(lambda v: self._set("unique", v))
        ident = QHBoxLayout()
        for lab, w in (("Name", self.name), ("Title", self.title), ("Interval", self.interval),
                       ("View", self.view_type), ("Key", self.key)):
            ident.addWidget(QLabel(lab))
            ident.addWidget(w)
        ident.addWidget(self.unique)
        ident.addStretch(1)

        # -- fields ---------------------------------------------------------------------
        self.ftable = QTableWidget(0, 6)
        self.ftable.setHorizontalHeaderLabels(("Use", "Template Value", "Field", "Kind", "Sample", "Label"))
        self.ftable.verticalHeader().setVisible(False)
        self.ftable.horizontalHeader().setSectionResizeMode(4, QHeaderView.Stretch)
        self.ftable.itemChanged.connect(self._on_field_item)
        fbox = QGroupBox("Fields")
        fl = QVBoxLayout(fbox)
        fl.addWidget(self.ftable)

        # -- columns / sort / stat ---------------------------------------------------------
        self.columns = QListWidget()
        self.columns.setDragDropMode(QAbstractItemView.InternalMove)
        self.columns.itemChanged.connect(lambda *_: self._on_columns())
        self.columns.model().rowsMoved.connect(lambda *_: self._on_columns())
        self.sort = QComboBox()
        self.sort.currentIndexChanged.connect(lambda *_: self._on_sort())
        self.sort_desc = QCheckBox("descending")
        self.sort_desc.toggled.connect(lambda v: self._set("sort_desc", v))
        self.limit = QSpinBox()
        self.limit.setRange(0, 1000)
        self.limit.setSpecialValueText("all rows")
        self.limit.valueChanged.connect(lambda v: self._set("limit", v or None))
        self.monitor = QComboBox()
        self.monitor.currentIndexChanged.connect(lambda *_: self._set("monitor", self.monitor.currentData()))
        tform = QFormLayout()
        tform.addRow("Sort by", self.sort)
        tform.addRow("", self.sort_desc)
        tform.addRow("Top N", self.limit)
        tform.addRow("Right-click Monitor", self.monitor)
        self.table_opts = QWidget()
        self.table_opts.setLayout(tform)

        self.agg = QComboBox()
        self.agg.addItems(sorted(AGGREGATES))
        self.agg.currentTextChanged.connect(lambda t: self._set("stat_aggregate", t))
        self.agg_field = QComboBox()
        self.agg_field.currentIndexChanged.connect(lambda *_: self._set("stat_field", self.agg_field.currentData()))
        self.where_field = QComboBox()
        self.where_field.currentIndexChanged.connect(lambda *_: self._on_where())
        self.where_op = QComboBox()
        self.where_op.addItem("isn't healthy", "alert_unhealthy")
        self.where_op.addItem("matches", "alert_match")
        self.where_op.currentIndexChanged.connect(lambda *_: self._on_where())
        self.where_val = QLineEdit()
        self.where_val.textEdited.connect(lambda *_: self._on_where())
        self.alert_above = QDoubleSpinBox()
        self.alert_above.setRange(-1, 1e12)
        self.alert_above.setSpecialValueText("never")
        self.alert_above.setValue(-1)
        self.alert_above.valueChanged.connect(
            lambda v: self._set("stat_alert_above", None if v < 0 else v))
        sform = QFormLayout()
        sform.addRow("Aggregate", self.agg)
        sform.addRow("Of field", self.agg_field)
        wrow = QHBoxLayout()
        wrow.addWidget(self.where_field)
        wrow.addWidget(self.where_op)
        wrow.addWidget(self.where_val, 1)
        sform.addRow("Count rows where", wrow)
        sform.addRow("Alert above", self.alert_above)
        self.stat_opts = QWidget()
        self.stat_opts.setLayout(sform)

        cbox = QGroupBox("Columns (check, drag to order) and view options")
        cl = QVBoxLayout(cbox)
        cl.addWidget(self.columns, 1)
        cl.addWidget(self.table_opts)
        cl.addWidget(self.stat_opts)

        # -- patterns ---------------------------------------------------------------------
        self.ptable = QTableWidget(0, 3)
        self.ptable.setHorizontalHeaderLabels(("Use", "Pattern", "Value"))
        self.ptable.verticalHeader().setVisible(False)
        self.ptable.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        self.ptable.itemChanged.connect(self._on_pattern_item)
        pbox = QGroupBox("Patterns (from the bundled widgets)")
        pl = QVBoxLayout(pbox)
        pl.addWidget(self.ptable)

        top_split = QSplitter(Qt.Horizontal)
        for w in (fbox, cbox, pbox):
            top_split.addWidget(w)
        top_split.setSizes([640, 380, 580])

        # -- preview / yaml / sample -----------------------------------------------------------
        self.pv_status = QLabel("")
        self.pv_status.setWordWrap(True)
        self.pv_host = QWidget()
        self.pv_layout = QVBoxLayout(self.pv_host)
        self.pv_layout.setContentsMargins(0, 0, 0, 0)
        pv = QWidget()
        pvl = QVBoxLayout(pv)
        pvl.setContentsMargins(0, 0, 0, 0)
        pvl.addWidget(self.pv_status)
        pvl.addWidget(self.pv_host, 1)
        self.yaml_view = QPlainTextEdit()
        self.yaml_view.setReadOnly(True)
        self.yaml_view.setFont(_mono())
        self.sample = QPlainTextEdit()
        self.sample.setFont(_mono())
        self.sample.setLineWrapMode(QPlainTextEdit.NoWrap)
        self.sample.textChanged.connect(lambda: self._timer.start())
        self.bottom = QTabWidget()
        self.bottom.addTab(pv, "Preview")
        self.bottom.addTab(self.yaml_view, "YAML")
        self.bottom.addTab(self.sample, "Sample output")

        split = QSplitter(Qt.Vertical)
        split.addWidget(top_split)
        split.addWidget(self.bottom)
        split.setSizes([480, 460])

        # -- save row ---------------------------------------------------------------------------
        self.add_layout = QComboBox()
        self.add_layout.addItem(_NO_LAYOUT, None)
        for n in sorted(self.layouts):
            self.add_layout.addItem(f"add to layout: {n}", n)
        bind = QPushButton("Bind other platforms...")
        bind.setToolTip("Open the Platform Pack editor to feed this widget on other platforms")
        bind.clicked.connect(lambda: self.packs_requested.emit(""))
        save = QPushButton("Save widget")
        save.clicked.connect(lambda: self.save())
        self.status = QLabel("")
        self.status.setWordWrap(True)
        srow = QHBoxLayout()
        srow.addWidget(self.status, 1)
        srow.addWidget(self.add_layout)
        srow.addWidget(bind)
        srow.addWidget(save)

        root = QWidget()
        rl = QVBoxLayout(root)
        rl.addLayout(src)
        rl.addLayout(ident)
        rl.addWidget(split, 1)
        rl.addLayout(srow)
        self.setCentralWidget(root)
        from .about_dialog import add_help_menu
        add_help_menu(self)

        self._fill_platforms(platform)
        if template:
            i = self.template.findData(template)
            if i >= 0:
                self.template.setCurrentIndex(i)
                self.start(template)

    # ═══════════════════════════════════════════════════════════════════════
    # Source: platform, template, sample
    # ═══════════════════════════════════════════════════════════════════════

    def _fill_platforms(self, select: str = "") -> None:
        self._quiet = True
        self.platform.clear()
        for p, n, on in self.store.platforms():
            if p and on:
                self.platform.addItem(f"{p}  ({on})", p)
        self._quiet = False
        i = self.platform.findData(select) if select else 0
        self.platform.setCurrentIndex(max(0, i))
        self._on_platform()

    def _on_platform(self) -> None:
        if self._quiet:
            return
        plat = self.platform.currentData()
        self.template.blockSignals(True)
        self.template.clear()
        for info in self.store.list(platform=plat, enabled=True):
            self.template.addItem(f"{info.command}    ({info.name})", info.name)
        self.template.blockSignals(False)
        self.template.setCurrentIndex(-1)
        self.template.setEditText("")
        self.template.lineEdit().setPlaceholderText(f"pick one of {self.template.count()} "
                                                    f"{plat} templates (type to search)")

    def _fill_sample_sources(self, template: str, command: str) -> None:
        plat = self.platform.currentData()
        self.sample_src.clear()
        rec = self.store.get(template)
        if rec and rec.sample:
            self.sample_src.addItem("DB sample", rec.sample)
        for s in self.store.samples(plat, command):
            self.sample_src.addItem(f"stored #{s.id} {s.label or s.created}", s.output)
        for c in self._captures():
            if c.platform == plat:
                self.sample_src.addItem(f"live: {c.label} {c.command}", c.output)
        self.sample_src.addItem("paste into Sample output", None)

    def _on_sample_source(self) -> None:
        text = self.sample_src.currentData()
        if text is not None:
            self.sample.setPlainText(text)
            self._reparse()
            self._timer.start()
        else:
            self.bottom.setCurrentIndex(2)

    def _reparse(self) -> None:
        if self.spec is None:
            return
        p = self.parser.parse(self.spec.platform, self.spec.command, self.sample.toPlainText(),
                              self.spec.template or "auto")
        self.records = p.records if not p.error else []
        self._fill_fields()                   # sample column

    def start(self, template: Optional[str], keep_sample: bool = False) -> Optional[dz.WidgetSpec]:
        """Suggest a whole widget from `template` and its sample."""
        rec = self.store.get(template) if template else None
        if rec is None:
            return None
        plat = self.platform.currentData()
        if not keep_sample:
            self._fill_sample_sources(template, rec.command)
            self.sample.blockSignals(True)
            self.sample.setPlainText(self.sample_src.itemData(0) or "")
            self.sample.blockSignals(False)
        text = self.sample.toPlainText()
        parsed = self.parser.parse(plat, rec.command, text, template)
        self.records = parsed.records if not parsed.error else []
        self.spec = dz.suggest_spec(self.vocab, plat, rec.command, template, rec.content,
                                    self.records, taken_names=list(self.widgets))
        self._populate()
        self.refresh()
        return self.spec

    # ═══════════════════════════════════════════════════════════════════════
    # Populate controls from the spec
    # ═══════════════════════════════════════════════════════════════════════

    def _populate(self) -> None:
        s = self.spec
        self._quiet = True
        self.name.setText(s.name)
        self.title.setText(s.title)
        self.interval.setValue(int(s.interval))
        self.view_type.setCurrentText(s.view)
        self.unique.setChecked(s.unique)
        self.sort_desc.setChecked(s.sort_desc)
        self.limit.setValue(s.limit or 0)
        self.agg.setCurrentText(s.stat_aggregate)
        self._quiet = False
        self._fill_fields()
        self._fill_field_combos()
        self._fill_columns()
        self._fill_patterns()
        self._show_view_opts()

    def _fill_fields(self) -> None:
        if self.spec is None:
            return
        self._quiet = True
        self.ftable.setRowCount(len(self.spec.fields))
        for r, f in enumerate(self.spec.fields):
            use = QTableWidgetItem()
            use.setFlags(Qt.ItemIsUserCheckable | Qt.ItemIsEnabled)
            use.setCheckState(Qt.Checked if f.include else Qt.Unchecked)
            self.ftable.setItem(r, 0, use)
            self.ftable.setItem(r, 1, _item(f.value))
            name = QTableWidgetItem(f.name)
            self.ftable.setItem(r, 2, name)
            kind = QComboBox()
            kind.addItems(dz.KINDS)
            kind.setCurrentText(f.kind)
            kind.currentTextChanged.connect(lambda t, row=r: self._on_kind(row, t))
            self.ftable.setCellWidget(r, 3, kind)
            vals = dz._nonempty(self.records, f.value)
            self.ftable.setItem(r, 4, _item(", ".join(dict.fromkeys(vals[:4])) or "(empty in sample)"))
            self.ftable.setItem(r, 5, QTableWidgetItem(f.label))
        self.ftable.resizeColumnsToContents()
        self._quiet = False

    def _names(self, with_derived: bool = True) -> List[str]:
        return self.spec.known() if with_derived else [f.name for f in self.spec.included()]

    def _fill_field_combos(self) -> None:
        s = self.spec
        self._quiet = True
        base = self._names(with_derived=False)
        known = self._names()
        for combo, items, cur, none in ((self.key, base, s.key, "(no key: rows as parsed)"),
                                        (self.sort, known, s.sort, "(parse order)"),
                                        (self.monitor, base, s.monitor, "(none)"),
                                        (self.agg_field, known, s.stat_field, "(rows)"),
                                        (self.where_field, known,
                                         s.stat_where.field if s.stat_where else None, "(every row)")):
            combo.clear()
            combo.addItem(none, None)
            for n in items:
                combo.addItem(n, n)
            combo.setCurrentIndex(max(0, combo.findData(cur)))
        if s.stat_where:
            self.where_op.setCurrentIndex(max(0, self.where_op.findData(s.stat_where.pattern)))
            self.where_val.setText(s.stat_where.param)
        self._quiet = False

    def _fill_columns(self) -> None:
        s = self.spec
        self._quiet = True
        self.columns.clear()
        known = self._names()
        shown = [c for c in s.columns if c in known]
        for n in shown + [k for k in known if k not in shown]:
            it = QListWidgetItem(n)
            it.setFlags(it.flags() | Qt.ItemIsUserCheckable | Qt.ItemIsDragEnabled)
            it.setCheckState(Qt.Checked if n in shown else Qt.Unchecked)
            self.columns.addItem(it)
        self._quiet = False

    def _fill_patterns(self) -> None:
        s = self.spec
        self._quiet = True
        rows = []
        on = {(u.pattern, u.field): u for u in s.patterns}
        for f in s.included():
            samples = dz._nonempty(self.records, f.value)
            for p in dz.applicable(s, f):
                u = on.get((p.id, f.name))
                rows.append((p, f, u, u.param if u else dz.default_param(p, f.kind, samples)))
        self.ptable.setRowCount(len(rows))
        for r, (p, f, u, param) in enumerate(rows):
            use = QTableWidgetItem()
            use.setFlags(Qt.ItemIsUserCheckable | Qt.ItemIsEnabled)
            use.setCheckState(Qt.Checked if u else Qt.Unchecked)
            use.setData(Qt.UserRole, (p.id, f.name))
            self.ptable.setItem(r, 0, use)
            self.ptable.setItem(r, 1, _item(p.label.format(f=f.name)))
            val = QTableWidgetItem(param)
            if not p.param:
                val.setFlags(Qt.ItemIsEnabled)
            else:
                val.setToolTip(p.param)
            self.ptable.setItem(r, 2, val)
        self.ptable.resizeColumnToContents(0)
        self.ptable.resizeColumnToContents(2)
        self._quiet = False

    def _show_view_opts(self) -> None:
        v = self.spec.view if self.spec else "table"
        self.table_opts.setVisible(v == "table")
        self.stat_opts.setVisible(v == "stat")
        self.columns.setEnabled(v != "stat")

    # ═══════════════════════════════════════════════════════════════════════
    # Handlers -> spec
    # ═══════════════════════════════════════════════════════════════════════

    def _set(self, attr: str, value) -> None:
        if self._quiet or self.spec is None:
            return
        setattr(self.spec, attr, value)
        self._timer.start()

    def _on_view_type(self, t: str) -> None:
        if self._quiet or self.spec is None:
            return
        self.spec.view = t
        self._show_view_opts()
        self._fill_patterns()                  # bars/status/spark are table-only
        self._timer.start()

    def _on_key(self) -> None:
        if self._quiet or self.spec is None:
            return
        self.spec.key = self.key.currentData()
        if not self.spec.key:
            self.spec.unique = False
            self.spec.patterns = [u for u in self.spec.patterns
                                  if not dz.PATTERN_BY_ID[u.pattern].needs_key]
        self._fill_patterns()
        self._fill_columns()
        self._timer.start()

    def _on_sort(self) -> None:
        if not self._quiet and self.spec is not None:
            self.spec.sort = self.sort.currentData()
            self._timer.start()

    def _on_where(self) -> None:
        if self._quiet or self.spec is None:
            return
        f = self.where_field.currentData()
        pat = self.where_op.currentData()
        if f and not self.where_val.text().strip():
            spec_f = next((x for x in self.spec.fields if x.name == f), None)
            samples = dz._nonempty(self.records, spec_f.value) if spec_f else []
            self._quiet = True
            self.where_val.setText(dz.healthy_regex(samples) if pat == "alert_unhealthy" else "(?i)err")
            self._quiet = False
        self.spec.stat_where = dz.PatternUse(pat, f, self.where_val.text()) if f else None
        self._timer.start()

    def _on_columns(self) -> None:
        if self._quiet or self.spec is None:
            return
        self.spec.columns = [self.columns.item(i).text() for i in range(self.columns.count())
                             if self.columns.item(i).checkState() == Qt.Checked]
        self._timer.start()

    def _on_kind(self, row: int, kind: str) -> None:
        if self._quiet:
            return
        self.spec.fields[row].kind = kind
        self._fill_patterns()
        self._timer.start()

    def _on_field_item(self, it: QTableWidgetItem) -> None:
        if self._quiet or self.spec is None:
            return
        f = self.spec.fields[it.row()]
        if it.column() == 0:
            f.include = it.checkState() == Qt.Checked
            self._structural()
        elif it.column() == 2:
            new = dz.snake(it.text()) if it.text().strip() else f.name
            if new != f.name and new not in {x.name for x in self.spec.fields}:
                self.rename_field(f.name, new)
            else:
                self._quiet = True
                it.setText(f.name)
                self._quiet = False
        elif it.column() == 5:
            f.label = it.text().strip()
            self._timer.start()

    def rename_field(self, old: str, new: str) -> None:
        """Rename a field and every reference to it (key, columns, sort,
        monitor, patterns, stat)."""
        s = self.spec
        for f in s.fields:
            if f.name == old:
                f.name = new
        ren = lambda x: new if x == old else x
        s.key, s.sort, s.monitor, s.stat_field = map(ren, (s.key, s.sort, s.monitor, s.stat_field))
        s.columns = [ren(c) for c in s.columns]
        for u in s.patterns:
            u.field = ren(u.field)
        if s.stat_where:
            s.stat_where.field = ren(s.stat_where.field)
        self._structural()

    def _structural(self) -> None:
        self._fill_fields()
        self._fill_field_combos()
        self._fill_columns()
        self._fill_patterns()
        self._timer.start()

    def _on_pattern_item(self, it: QTableWidgetItem) -> None:
        if self._quiet or self.spec is None:
            return
        pid, fname = self.ptable.item(it.row(), 0).data(Qt.UserRole)
        on = self.ptable.item(it.row(), 0).checkState() == Qt.Checked
        param = self.ptable.item(it.row(), 2).text()
        self.set_pattern(pid, fname, on, param)

    def set_pattern(self, pid: str, fname: str, on: bool, param: Optional[str] = None) -> None:
        s = self.spec
        s.patterns = [u for u in s.patterns if (u.pattern, u.field) != (pid, fname)]
        if on:
            if param is None:
                f = next(x for x in s.fields if x.name == fname)
                param = dz.default_param(dz.PATTERN_BY_ID[pid], f.kind, dz._nonempty(self.records, f.value))
            s.patterns.append(dz.PatternUse(pid, fname, param))
        if pid in ("rate", "delta"):             # derived fields appear/disappear
            derived = f"{fname}_ps" if pid == "rate" else f"{fname}_chg"
            if on and derived not in s.columns:
                s.columns.append(derived)
            elif not on:
                s.columns = [c for c in s.columns if c != derived]
                if s.sort == derived:
                    s.sort = None
            self._fill_field_combos()
            self._fill_columns()
        self._timer.start()

    # ═══════════════════════════════════════════════════════════════════════
    # Preview, save
    # ═══════════════════════════════════════════════════════════════════════

    def refresh(self) -> Optional[dz.DesignPreview]:
        if self.spec is None:
            return None
        pv = dz.preview_spec(self.parser, self.spec, self.sample.toPlainText())
        if self._view is not None:
            self.pv_layout.removeWidget(self._view)
            self._view.deleteLater()
            self._view = None
        if pv.widget is not None:
            self._view = make_view(pv.widget)
            self.pv_layout.addWidget(self._view)
            if pv.error:
                self._view.set_error(pv.error)
            else:
                self._view.update_rows(pv.rows)
                self._view.set_status("preview", "muted", "rendered from the sample output")
        self.yaml_view.setPlainText(self.spec.to_yaml() if pv.widget is not None else "")
        if pv.widget is None:
            self.pv_status.setText(f"<span style='color:{_ERR}'>{pv.error}</span>")
        elif pv.error:
            self.pv_status.setText(f"<span style='color:{_ERR}'>{pv.error}</span>")
        else:
            color = _OK if pv.rows else _ERR
            note = "" if pv.rows or not pv.records else " -- drop rules removed every row"
            self.pv_status.setText(f"<span style='color:{color}'>{pv.records} records -> "
                                   f"{len(pv.rows)} rows</span> via {self.spec.template or 'auto'}{note}")
        return pv

    def save(self, confirm: bool = True) -> Optional[Path]:
        s = self.spec
        if s is None:
            return None
        try:
            s.validate()
        except Exception as e:
            self._say(str(e), _ERR)
            return None
        bundled = self.widgets.get(s.name)
        if bundled is not None and str(PKG_DATA) in str(bundled.source):
            self._say(f"{s.name!r} is a bundled widget; pick another name", _ERR)
            return None
        path = self.widget_dir / f"{s.name}.yaml"
        if confirm and path.exists() and QMessageBox.question(
                self, "Save widget", f"Replace {path}?") != QMessageBox.Yes:
            return None
        path = dz.save_widget(s, self.widget_dir)
        msg = f"saved {path}"
        lay = self.add_layout.currentData()
        if lay:
            src = Path(self.layouts[lay].source)
            out = dz.add_to_layout(src, s.name, self.layout_dir)
            msg += f"; added to layout {lay} ({out})"
        self.widgets[s.name] = s.validate()
        self.vocab = dz.Vocabulary(self.widgets)
        self._say(msg + " -- new device windows show it", _OK)
        self.saved.emit(s.name, str(path))
        return path

    def _say(self, text: str, color: str) -> None:
        self.status.setText(text)
        self.status.setStyleSheet(f"color: {color};")
