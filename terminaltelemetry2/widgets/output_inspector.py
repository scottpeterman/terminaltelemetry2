"""Output inspector -- the { } view for python-parsed widgets.

Their parsers are code, so there is no template to edit; what's needed to
troubleshoot is exactly what the device returned, the parser's error, and a
plain-language hint (hints.py). Non-modal; reused per window.
"""
from __future__ import annotations

import html
from typing import Optional

from PySide6.QtCore import Qt
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import (
    QDialog, QHBoxLayout, QLabel, QPlainTextEdit, QPushButton, QVBoxLayout, QWidget,
)

from ..hints import hint_for
from .lab import LabSource
from .template_manager import _ERR, _MUTED, _OK, _mono


class OutputInspector(QDialog):
    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.setModal(False)
        self.resize(900, 560)
        self.head = QLabel()
        self.head.setWordWrap(True)
        self.head.setTextFormat(Qt.RichText)
        self.error = QLabel()
        self.error.setWordWrap(True)
        self.output = QPlainTextEdit()
        self.output.setReadOnly(True)
        self.output.setFont(_mono())
        self.output.setLineWrapMode(QPlainTextEdit.NoWrap)
        copy_out = QPushButton("Copy output")
        copy_out.clicked.connect(lambda: QGuiApplication.clipboard().setText(self.output.toPlainText()))
        copy_cmd = QPushButton("Copy command")
        copy_cmd.setToolTip("Paste into the terminal pane to run it by hand")
        copy_cmd.clicked.connect(lambda: QGuiApplication.clipboard().setText(self._cmd))
        close = QPushButton("Close")
        close.clicked.connect(self.close)
        row = QHBoxLayout()
        row.addWidget(copy_cmd)
        row.addWidget(copy_out)
        row.addStretch(1)
        row.addWidget(close)
        lay = QVBoxLayout(self)
        lay.addWidget(self.head)
        lay.addWidget(self.error)
        lay.addWidget(QLabel("Raw output the device returned:"))
        lay.addWidget(self.output, 1)
        lay.addLayout(row)
        self._cmd = ""

    def open_source(self, src: LabSource) -> None:
        self._cmd = src.command
        self.setWindowTitle(f"Output - {src.widget or src.command}")
        self.head.setText(f"<b>{html.escape(src.widget)}</b> on {html.escape(src.platform)}<br>"
                          f"command: <code>{html.escape(src.command)}</code><br>"
                          f"parser: <code>{html.escape(src.template)}</code> (python; no template to edit)")
        if src.error:
            hint = hint_for(src.error, src.output)
            self.error.setText(f"<span style='color:{_ERR}'>{html.escape(src.error)}</span>"
                               + (f"<br><b>{html.escape(hint)}</b>" if hint else ""))
        else:
            self.error.setText(f"<span style='color:{_OK}'>last poll parsed fine</span>")
        self.output.setPlainText(src.output or "(no output)")
        self.show()
        self.raise_()
        self.activateWindow()
