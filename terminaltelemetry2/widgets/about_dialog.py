"""Help menu: About terminaltelemetry2, About Qt, Third-party licenses.

add_help_menu() gives any QMainWindow the same menu. The About and About Qt
actions carry Qt's menu roles, so on macOS they move into the application menu
where users look for them; elsewhere they stay under Help.
"""
from __future__ import annotations

import html
from typing import Optional

from PySide6.QtCore import Qt
from PySide6.QtGui import QAction
from PySide6.QtWidgets import (
    QDialog, QDialogButtonBox, QHBoxLayout, QLabel, QListWidget, QListWidgetItem, QMainWindow,
    QMessageBox, QPlainTextEdit, QPushButton, QSplitter, QVBoxLayout, QWidget,
)

from .. import about
from .template_manager import _mono


_URL = __import__("re").compile(r"https?://[^\s<]+[^\s<.,;)]")


def _linked(text: str) -> str:
    """HTML-escape, then turn bare URLs into links."""
    return _URL.sub(lambda m: f"<a href='{m.group(0)}'>{m.group(0)}</a>", html.escape(text))


class LicensesDialog(QDialog):
    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.setWindowTitle(f"Third-party licenses - {about.APP_NAME}")
        self.resize(900, 620)
        self.items = QListWidget()
        self.info = QLabel()
        self.info.setWordWrap(True)
        self.info.setTextFormat(Qt.RichText)
        self.info.setOpenExternalLinks(True)
        self.info.setTextInteractionFlags(Qt.TextBrowserInteraction)
        self.text = QPlainTextEdit()
        self.text.setReadOnly(True)
        self.text.setFont(_mono())
        self._components = about.components()
        for c in self._components:
            it = QListWidgetItem(f"{c.name}  ({c.license})")
            self.items.addItem(it)
        self.items.currentRowChanged.connect(self.show_component)
        right = QWidget()
        rl = QVBoxLayout(right)
        rl.setContentsMargins(0, 0, 0, 0)
        rl.addWidget(self.info)
        rl.addWidget(self.text, 1)
        split = QSplitter(Qt.Horizontal)
        split.addWidget(self.items)
        split.addWidget(right)
        split.setSizes([260, 640])
        close = QDialogButtonBox(QDialogButtonBox.Close)
        close.rejected.connect(self.reject)
        lay = QVBoxLayout(self)
        lay.addWidget(split, 1)
        lay.addWidget(close)
        self.items.setCurrentRow(0)

    def show_component(self, row: int) -> None:
        if not 0 <= row < len(self._components):
            return
        c = self._components[row]
        e = html.escape
        extra = f"<p>{_linked(about.qt_notice())}</p>" if c.name in ("Qt", "PySide6 / Shiboken6") else ""
        self.info.setText(
            f"<b>{e(c.name)}</b> {e(c.version)}<br>License: {e(c.license)}<br>Used for: {e(c.use)}<br>"
            f"Home: <a href='{e(c.homepage)}'>{e(c.homepage)}</a><br>"
            f"Source: <a href='{e(c.source)}'>{e(c.source)}</a>{extra}")
        self.text.setPlainText(c.texts() or "(license text: see the project's homepage)")


class AboutDialog(QDialog):
    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.setWindowTitle(f"About {about.APP_NAME}")
        self.setMinimumWidth(560)
        qt, pyside = about.qt_versions()
        e = html.escape
        body = QLabel(
            f"<h3>{e(about.APP_NAME)} {e(__import__('terminaltelemetry2').__version__)}</h3>"
            f"<p>Terminal + telemetry for one network device or host.</p>"
            f"<p>{e(about.COPYRIGHT)}<br>"
            f"<a href='{e(about.HOMEPAGE)}'>{e(about.HOMEPAGE)}</a></p>"
            f"<p>{e(about.GPL_NOTICE).replace(chr(10) * 2, '<br><br>')}</p>"
            f"<p>{_linked(about.qt_notice())}</p>"
            f"<p>Built with Qt {e(qt)} and PySide6 {e(pyside)}. Third-party components and "
            f"their licenses are listed under Third-party licenses.</p>")
        body.setWordWrap(True)
        body.setTextFormat(Qt.RichText)
        body.setOpenExternalLinks(True)
        body.setTextInteractionFlags(Qt.TextBrowserInteraction)
        lic = QPushButton("Third-party licenses...")
        lic.clicked.connect(lambda: LicensesDialog(self).exec())
        aqt = QPushButton("About Qt")
        aqt.clicked.connect(lambda: QMessageBox.aboutQt(self, "About Qt"))
        close = QPushButton("Close")
        close.setDefault(True)
        close.clicked.connect(self.accept)
        row = QHBoxLayout()
        row.addWidget(lic)
        row.addWidget(aqt)
        row.addStretch(1)
        row.addWidget(close)
        lay = QVBoxLayout(self)
        lay.addWidget(body)
        lay.addLayout(row)


def add_help_menu(win: QMainWindow) -> None:
    """Help > About terminaltelemetry2 / About Qt / Third-party licenses."""
    menu = win.menuBar().addMenu("&Help")
    a = QAction(f"About {about.APP_NAME}", win)
    a.setMenuRole(QAction.AboutRole)
    a.triggered.connect(lambda: AboutDialog(win).exec())
    q = QAction("About Qt", win)
    q.setMenuRole(QAction.AboutQtRole)
    q.triggered.connect(lambda: QMessageBox.aboutQt(win, "About Qt"))
    lic = QAction("Third-party licenses...", win)
    lic.setMenuRole(QAction.NoRole)
    lic.triggered.connect(lambda: LicensesDialog(win).exec())
    for act in (a, q, lic):
        menu.addAction(act)
