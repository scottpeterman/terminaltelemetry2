"""Connect form -- what `tt2` with no arguments opens.

Pre-filled from the last connection; the Host box lists recent hosts and
picking one fills every field it was used with. Secrets (password, key
passphrase, jump password) are asked for every time and never saved.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Dict, List, Optional, Sequence

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFileDialog, QFormLayout, QGroupBox,
    QHBoxLayout, QLabel, QLineEdit, QPushButton, QSpinBox, QVBoxLayout, QWidget,
)

from ..sessions import parse_jump


class ConnectDialog(QDialog):
    def __init__(self, platforms: Sequence[tuple], layouts: Sequence[str],
                 saved: Dict[str, object], parent: Optional[QWidget] = None):
        """platforms: (id, title) pairs; saved: recent.load() result."""
        super().__init__(parent)
        self.setWindowTitle("Connect - terminaltelemetry2")
        self.setMinimumWidth(520)
        self._recent: List[dict] = list(saved.get("recent") or [])

        self.host = QComboBox()
        self.host.setEditable(True)
        self.host.setInsertPolicy(QComboBox.NoInsert)
        for r in self._recent:
            self.host.addItem(self._label(r), r)
        self.host.lineEdit().setPlaceholderText("hostname or IP")
        self.host.activated.connect(self._on_recent)
        self.port = QSpinBox()
        self.port.setRange(1, 65535)
        self.port.setValue(22)
        self.user = QLineEdit()
        self.platform = QComboBox()
        for pid, title in platforms:
            self.platform.addItem(f"{pid}  -  {title}" if title and title != pid else pid, pid)
        self.password = QLineEdit()
        self.password.setEchoMode(QLineEdit.Password)
        env = bool(os.environ.get("TERMINALTELEMETRY2_PASSWORD"))
        self.password.setPlaceholderText("from $TERMINALTELEMETRY2_PASSWORD" if env
                                         else "not saved -- asked every time")
        self.key = QLineEdit()
        self.key.setPlaceholderText("optional: private key file (key auth)")
        browse = QPushButton("...")
        browse.setFixedWidth(32)
        browse.clicked.connect(lambda: self._browse(self.key))
        key_row = QHBoxLayout()
        key_row.addWidget(self.key, 1)
        key_row.addWidget(browse)
        self.passphrase = QLineEdit()
        self.passphrase.setEchoMode(QLineEdit.Password)
        self.passphrase.setPlaceholderText("only for an encrypted key")

        host_row = QHBoxLayout()
        host_row.addWidget(self.host, 1)
        host_row.addWidget(QLabel("port"))
        host_row.addWidget(self.port)

        form = QFormLayout()
        form.setFieldGrowthPolicy(QFormLayout.AllNonFixedFieldsGrow)
        form.addRow("Host", host_row)
        form.addRow("Platform", self.platform)
        form.addRow("User", self.user)
        form.addRow("Password", self.password)
        form.addRow("Key file", key_row)
        form.addRow("Key passphrase", self.passphrase)

        # -- jump host -----------------------------------------------------------------
        self.jump = QLineEdit()
        self.jump.setPlaceholderText("[user@]bastion[:port]  (optional)")
        self.jump_key = QLineEdit()
        self.jump_key.setPlaceholderText("default: the device key")
        jbrowse = QPushButton("...")
        jbrowse.setFixedWidth(32)
        jbrowse.clicked.connect(lambda: self._browse(self.jump_key))
        jk_row = QHBoxLayout()
        jk_row.addWidget(self.jump_key, 1)
        jk_row.addWidget(jbrowse)
        self.jump_password = QLineEdit()
        self.jump_password.setEchoMode(QLineEdit.Password)
        self.jump_password.setPlaceholderText("default: the device password")
        jform = QFormLayout()
        jform.setFieldGrowthPolicy(QFormLayout.AllNonFixedFieldsGrow)
        jform.addRow("Jump host", self.jump)
        jform.addRow("Jump key", jk_row)
        jform.addRow("Jump password", self.jump_password)

        # -- advanced ------------------------------------------------------------------
        self.layout_name = QComboBox()
        self.layout_name.addItem("(platform default)", "")
        for n in sorted(layouts):
            self.layout_name.addItem(n, n)
        self.enable = QComboBox()
        self.enable.setEditable(True)
        self.enable.addItems(["", "enable", "enable 15"])
        self.enable.setToolTip("Only for devices that land in user mode; the platform pack "
                               "supplies one otherwise")
        self.legacy = QCheckBox("legacy SSH algorithms (old gear)")
        self.advanced = QCheckBox("Advanced options")
        aform = QFormLayout()
        aform.setFieldGrowthPolicy(QFormLayout.AllNonFixedFieldsGrow)
        aform.addRow("Layout", self.layout_name)
        aform.addRow("Enable command", self.enable)
        aform.addRow("", self.legacy)
        self._adv_body = QGroupBox()
        self._adv_body.setLayout(aform)
        self._adv_body.setVisible(False)

        def _toggle(on: bool) -> None:
            self._adv_body.setVisible(on)
            self.adjustSize()                    # shrink back when collapsed
        self.advanced.toggled.connect(_toggle)
        jbox = QGroupBox("Jump host")
        jbox.setLayout(jform)

        self.error = QLabel("")
        self.error.setStyleSheet("color: #d9534f;")
        self.error.setWordWrap(True)
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.button(QDialogButtonBox.Ok).setText("Connect")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)

        lay = QVBoxLayout(self)
        lay.addLayout(form)
        lay.addWidget(jbox)
        lay.addWidget(self.advanced)
        lay.addWidget(self._adv_body)
        lay.addWidget(self.error)
        lay.addWidget(buttons)

        self.fill(saved.get("last") or {})

    # -- filling ---------------------------------------------------------------------

    @staticmethod
    def _label(r: dict) -> str:
        return f"{r.get('host')}   ({r.get('user', '?')}, {r.get('platform', '?')})"

    def _on_recent(self, idx: int) -> None:
        r = self.host.itemData(idx)
        if isinstance(r, dict):
            self.fill(r)

    def fill(self, r: dict) -> None:
        """Set every non-secret field from a saved entry; secrets are cleared."""
        self.host.setEditText(str(r.get("host", "")))
        self.port.setValue(int(r.get("port", 22)))
        self.user.setText(str(r.get("user", "")))
        i = self.platform.findData(r.get("platform"))
        if i >= 0:
            self.platform.setCurrentIndex(i)
        self.key.setText(str(r.get("key", "")))
        self.jump.setText(str(r.get("jump", "")))
        self.jump_key.setText(str(r.get("jump_key", "")))
        self.layout_name.setCurrentIndex(max(0, self.layout_name.findData(r.get("layout", ""))))
        self.enable.setEditText(str(r.get("enable_command", "")))
        self.legacy.setChecked(bool(r.get("legacy_ssh")))
        self.advanced.setChecked(bool(r.get("layout") or r.get("enable_command") or r.get("legacy_ssh")))
        for w in (self.password, self.passphrase, self.jump_password):
            w.clear()
        (self.password if self.host.currentText() else self.host).setFocus()

    def _browse(self, target: QLineEdit) -> None:
        start = str(Path(target.text()).expanduser().parent) if target.text() else str(Path.home() / ".ssh")
        f, _ = QFileDialog.getOpenFileName(self, "Private key", start)
        if f:
            target.setText(f)

    # -- result ----------------------------------------------------------------------

    def host_text(self) -> str:
        t = self.host.currentText().strip()
        # a picked recent shows "host   (user, platform)" -- the host is the first word
        return t.split()[0] if t else ""

    def values(self) -> dict:
        adv = self.advanced.isChecked()
        return {
            "host": self.host_text(), "port": self.port.value(), "user": self.user.text().strip(),
            "platform": self.platform.currentData(), "password": self.password.text(),
            "key": self.key.text().strip(), "key_passphrase": self.passphrase.text(),
            "jump": self.jump.text().strip(), "jump_key": self.jump_key.text().strip(),
            "jump_password": self.jump_password.text(),
            "layout": self.layout_name.currentData() if adv else "",
            "enable_command": self.enable.currentText().strip() if adv else "",
            "legacy_ssh": self.legacy.isChecked() if adv else False,
        }

    def problems(self) -> List[str]:
        v = self.values()
        out = []
        if not v["host"]:
            out.append("host is required")
        if not v["user"]:
            out.append("user is required")
        if not v["platform"]:
            out.append("pick a platform")
        if v["key"] and not Path(v["key"]).expanduser().is_file():
            out.append(f"key file not found: {v['key']}")
        if not (v["password"] or v["key"] or os.environ.get("TERMINALTELEMETRY2_PASSWORD")):
            out.append("enter a password or a key file")
        if v["jump"]:
            try:
                parse_jump(v["jump"])
            except ValueError as e:
                out.append(f"jump host: {e}")
        if v["jump_key"] and not Path(v["jump_key"]).expanduser().is_file():
            out.append(f"jump key file not found: {v['jump_key']}")
        return out

    def accept(self) -> None:
        p = self.problems()
        if p:
            self.error.setText("; ".join(p))
            return
        super().accept()
