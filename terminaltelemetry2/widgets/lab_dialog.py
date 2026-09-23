"""The template lab dialog. Opened from a widget's { } button, preloaded with
the exact capture that widget collected and the template it resolved to. Edit
the template against the real output, Test, and Save an override the running
widgets pick up on their next poll -- no restart, no DB surgery.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Optional

from PySide6.QtCore import Qt
from PySide6.QtGui import QFont, QTextCursor
from PySide6.QtWidgets import (
    QComboBox, QDialog, QHBoxLayout, QMessageBox, QHeaderView, QLabel, QLineEdit, QPlainTextEdit,
    QPushButton, QSplitter, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
)

from ..parsing import Parser
from .lab import LabResult, LabSource, run_textfsm

_OK = "#3aa35a"
_ERR = "#d9534f"
_MUTED = "#8a8a8a"


def _mono() -> QFont:
    f = QFont("monospace")
    f.setStyleHint(QFont.Monospace)
    f.setPointSize(9)
    return f


class TemplateLab(QDialog):
    """One reusable dialog per window; load() swaps in a new source."""

    def __init__(self, parser: Parser, override_dir: Path,
                 parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.parser = parser
        self.override_dir = Path(override_dir)
        self._src: Optional[LabSource] = None
        self._base = ""                             # template family the widget resolves in
        self._current = ""                          # family member in the editor
        self.setWindowTitle("Template Lab")
        self.resize(1100, 720)
        # a dialog, but non-modal: keep watching the live HUD while you fix a template
        self.setModal(False)

        self.raw = QPlainTextEdit()
        self.raw.setFont(_mono())
        self.raw.setLineWrapMode(QPlainTextEdit.NoWrap)
        self.tpl = QPlainTextEdit()
        self.tpl.setFont(_mono())
        self.tpl.setLineWrapMode(QPlainTextEdit.NoWrap)

        left = QWidget()
        ll = QVBoxLayout(left)
        ll.setContentsMargins(0, 0, 0, 0)
        ll.addWidget(QLabel("Raw CLI output"))
        ll.addWidget(self.raw, 1)
        right = QWidget()
        rl = QVBoxLayout(right)
        rl.setContentsMargins(0, 0, 0, 0)
        rl.addWidget(QLabel("TextFSM template"))
        rl.addWidget(self.tpl, 1)
        editors = QSplitter(Qt.Horizontal)
        editors.addWidget(left)
        editors.addWidget(right)
        editors.setSizes([520, 520])

        self.result = QTableWidget(0, 0)
        self.result.setEditTriggers(QTableWidget.NoEditTriggers)
        self.result.horizontalHeader().setSectionResizeMode(QHeaderView.Interactive)
        self.result.verticalHeader().setVisible(False)
        self.status = QLabel("")
        self.status.setWordWrap(True)

        panes = QSplitter(Qt.Vertical)
        panes.addWidget(editors)
        res = QWidget()
        resl = QVBoxLayout(res)
        resl.setContentsMargins(0, 0, 0, 0)
        resl.addWidget(self.status)
        resl.addWidget(self.result, 1)
        panes.addWidget(res)
        panes.setSizes([460, 240])

        test_btn = QPushButton("Test")
        test_btn.setDefault(True)
        test_btn.clicked.connect(self.run)
        reset_btn = QPushButton("Reset template")
        reset_btn.setToolTip("Reload the template as the widget resolved it")
        reset_btn.clicked.connect(self._reset_template)
        self.save_btn = QPushButton("Save as override")
        self.save_btn.setToolTip("Write a user override; running widgets re-parse next poll")
        self.save_btn.clicked.connect(self.save)
        # DB write-back: save as an instance-numbered sibling the scored sweep
        # will pick up. Never overwrites the base -- the name field defaults to
        # the next free sibling.
        self.db_name = QLineEdit()
        self.db_name.setMinimumWidth(260)
        self.db_name.setToolTip("Sibling name to write into the template DB")
        self.db_btn = QPushButton("Save to database")
        self.db_btn.setToolTip("Write this template into the DB as a sibling; the "
                               "scored sweep selects it where the base fails")
        self.db_btn.clicked.connect(self.save_to_db)
        buttons = QHBoxLayout()
        buttons.addWidget(test_btn)
        buttons.addWidget(reset_btn)
        buttons.addStretch(1)
        buttons.addWidget(self.db_name)
        buttons.addWidget(self.db_btn)
        buttons.addWidget(self.save_btn)

        # Family picker: the base plus every numbered sibling (DB + override
        # files), marking where each lives and which one the widget is using.
        self.picker = QComboBox()
        self.picker.setMinimumWidth(460)
        self.picker.currentIndexChanged.connect(self._on_pick)
        self.delete_btn = QPushButton("Delete sibling")
        self.delete_btn.setToolTip("Remove this custom sibling from the DB (never the base)")
        self.delete_btn.clicked.connect(self.delete_sibling)
        top = QHBoxLayout()
        top.addWidget(QLabel("Template"))
        top.addWidget(self.picker, 1)
        top.addWidget(self.delete_btn)

        outer = QVBoxLayout(self)
        outer.addLayout(top)
        outer.addWidget(panes, 1)
        outer.addLayout(buttons)

    # -- loading ---------------------------------------------------------------

    def load(self, src: LabSource) -> None:
        self._src = src
        self._base = src.base or src.template
        who = src.widget or src.command
        self.setWindowTitle(f"Template Lab -- {who} [{self._base}]")
        self.raw.setPlainText(src.output or "")
        self.result.clear()
        self.result.setRowCount(0)
        self.result.setColumnCount(0)
        self._fill_picker(select=src.template)
        if src.error:
            self._say(f"widget parse failed: {src.error}", _ERR)
        else:
            self._say("loaded; edit and Test", _MUTED)

    def _fill_picker(self, select: str) -> None:
        if self._src is None:
            return
        fam = self.parser.family(self._base)
        active = self.parser.pinned(self._src.platform, self._src.command, self._base)
        self.picker.blockSignals(True)
        self.picker.clear()
        for name in fam:
            origin = self.parser.template_origin(name)
            tag = "  \u25cf in use" if name == active else ""
            self.picker.addItem(f"{name}   [{origin}]{tag}", name)
        idx = self.picker.findData(select if select in fam else self._base)
        self.picker.setCurrentIndex(max(0, idx))
        self.picker.blockSignals(False)
        self._on_pick(self.picker.currentIndex(), run=False)

    def _on_pick(self, _idx: int, run: bool = True) -> None:
        name = self.picker.currentData()
        if not name:
            return
        self._current = name
        self.tpl.setPlainText(self.parser.template_content(name) or "")
        is_sibling = name != self._base
        # Saving a sibling updates it in place; saving from the base makes a new one.
        self.db_name.setText(name if is_sibling else self.parser.next_in_family(self._base))
        self.delete_btn.setEnabled(is_sibling and self.parser.template_origin(name) == "db:custom")
        if run:
            self.run()

    def open_source(self, src: LabSource) -> None:
        """Load a source and raise the dialog (from a widget's lab button)."""
        self.load(src)
        self.run()                                  # show the failure immediately
        self.show()
        self.raise_()
        self.activateWindow()

    def _reset_template(self) -> None:
        if self._current:
            self.tpl.setPlainText(self.parser.template_content(self._current) or "")

    # -- test ------------------------------------------------------------------

    def run(self) -> None:
        content = self.tpl.toPlainText()
        if not content.strip():
            self._say("no template to test", _ERR)
            return
        cleaned = self.parser.clean_output(self.raw.toPlainText())
        res = run_textfsm(content, cleaned)
        self._render(res)

    def _render(self, res: LabResult) -> None:
        if res.ok:
            self._fill_table(res)
            n = len(res.records)
            self._say(f"{n} record{'' if n == 1 else 's'}", _OK if n else _MUTED)
            return
        self.result.setRowCount(0)
        self.result.setColumnCount(0)
        if res.error_kind == "state" and res.input_line:
            self._say(
                f"State Error at template line {res.rule_line}: no rule matched "
                f"this input line -> {res.input_line}", _ERR)
            self._highlight_raw(res.input_line)
            if res.rule_line:
                self._goto_template_line(res.rule_line)
        elif res.error_kind == "syntax":
            self._say(f"template won't compile: {res.error}", _ERR)
        else:
            self._say(res.error or "parse failed", _ERR)

    def _fill_table(self, res: LabResult) -> None:
        self.result.setColumnCount(len(res.header))
        self.result.setHorizontalHeaderLabels(res.header)
        self.result.setRowCount(len(res.records))
        for r, rec in enumerate(res.records):
            for c, col in enumerate(res.header):
                self.result.setItem(r, c, QTableWidgetItem(str(rec.get(col, ""))))
        self.result.resizeColumnsToContents()

    # -- editor helpers --------------------------------------------------------

    def _say(self, text: str, color: str = _MUTED) -> None:
        self.status.setText(text)
        self.status.setStyleSheet(f"color: {color};")

    def _highlight_raw(self, line: str) -> None:
        needle = line.strip()
        if not needle:
            return
        cur = self.raw.document().find(needle)
        if not cur.isNull():
            self.raw.setTextCursor(cur)
            self.raw.centerCursor()

    def _goto_template_line(self, line_1based: int) -> None:
        block = self.tpl.document().findBlockByLineNumber(max(0, line_1based - 1))
        if block.isValid():
            cur = QTextCursor(block)
            cur.select(QTextCursor.LineUnderCursor)
            self.tpl.setTextCursor(cur)
            self.tpl.centerCursor()

    # -- save ------------------------------------------------------------------

    def save(self) -> None:
        if self._src is None:
            return
        name = self._current or self._src.template
        content = self.tpl.toPlainText()
        if not content.strip():
            self._say("nothing to save", _ERR)
            return
        try:
            self.override_dir.mkdir(parents=True, exist_ok=True)
            path = self.override_dir / f"{name}.textfsm"
            path.write_text(content, encoding="utf-8")
        except OSError as e:
            self._say(f"could not write override: {e}", _ERR)
            return
        self.parser.reload_overrides()              # drop cached content + pins
        self._fill_picker(select=name)
        self._say(f"saved {path.name}; widgets re-parse on next poll", _OK)

    def save_to_db(self) -> None:
        if self._src is None:
            return
        name = self.db_name.text().strip()
        content = self.tpl.toPlainText()
        if not name:
            self._say("give the sibling a name to save to the DB", _ERR)
            return
        if not content.strip():
            self._say("nothing to save", _ERR)
            return
        # carry the current capture in as the row's cli_content sample -- it's
        # exactly the output this template was proven against.
        sample = self.parser.clean_output(self.raw.toPlainText())
        existed = name in self.parser.family(self._base)
        if not re.fullmatch(re.escape(self._base) + r"\d*", name):
            self._say(f"{name!r} is outside the {self._base} family; the widget "
                      f"only tries {self._base} and {self._base}N", _ERR)
            return
        try:
            self.parser.save_template_to_db(name, content, sample=sample)
        except ValueError as e:                     # refused: would overwrite a vendor base
            self._say(str(e), _ERR)
            return
        except Exception as e:
            self._say(f"DB write failed: {e}", _ERR)
            return
        self._fill_picker(select=name)
        self._say(f"{'updated' if existed else 'added'} {name} in the DB; the widget "
                  f"tries the base, then siblings newest-first, on the next poll", _OK)

    def delete_sibling(self) -> None:
        name = self._current
        if not name or name == self._base:
            return
        if QMessageBox.question(self, "Template Lab", f"Delete {name} from the template DB?") \
                != QMessageBox.Yes:
            return
        try:
            self.parser.delete_template_from_db(name, self._base)
        except ValueError as e:
            self._say(str(e), _ERR)
            return
        self._fill_picker(select=self._base)
        self._say(f"deleted {name}", _OK)
