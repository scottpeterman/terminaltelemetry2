"""
Device selector: searchable folder tree over a session file, plus credentials
and an optional default jump host.

Persisted to <config_dir>/last_connect.json (no secrets): username, key file,
jump host/port/user/key, last sessions file, last device. Passwords and
passphrases are kept only in process memory, so a second window (Ctrl+N)
comes up prefilled without anything touching disk.

Search: whitespace-separated terms, all must match (AND) against folder,
name, host, platform, vendor, model. Down moves from the search box into the
tree; Enter connects the selected device.
"""
from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Sequence

import paramiko
from PySide6.QtCore import QEvent, QModelIndex, QObject, QSortFilterProxyModel, Qt, QTimer
from PySide6.QtGui import QFont, QStandardItem, QStandardItemModel
from PySide6.QtWidgets import (
    QAbstractItemView, QComboBox, QDialog, QDialogButtonBox, QFileDialog, QFormLayout,
    QGroupBox, QHBoxLayout, QHeaderView, QLabel, QLineEdit, QMessageBox, QPushButton,
    QSpinBox, QTreeView, QVBoxLayout, QWidget,
)

from .paths import config_dir
from .sessions import ConnectTarget, JumpTarget, SessionEntry

log = logging.getLogger(__name__)

ENTRY_ROLE = Qt.UserRole + 1
HAY_ROLE = Qt.UserRole + 2

# In-process only: survives across selector invocations, never written.
_SECRETS: Dict[str, str] = {}


# ═══════════════════════════════════════════════════════════════════════════
# Persistence
# ═══════════════════════════════════════════════════════════════════════════

@dataclass
class LastUsed:
    username: str = ""
    key_file: str = ""
    jump_enabled: bool = False
    jump_host: str = ""
    jump_port: int = 22
    jump_username: str = ""
    jump_key_file: str = ""
    sessions_file: str = ""
    last_host: str = ""
    last_name: str = ""
    platform_overrides: Dict[str, str] = field(default_factory=dict)   # host -> platform

    @staticmethod
    def path() -> Path:
        return config_dir() / "last_connect.json"

    @classmethod
    def load(cls) -> "LastUsed":
        try:
            raw = json.loads(cls.path().read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return cls()
        known = cls.__dataclass_fields__
        return cls(**{k: v for k, v in raw.items() if k in known})

    def save(self) -> None:
        p = self.path()
        tmp = p.with_suffix(".tmp")
        try:
            tmp.write_text(json.dumps(self.__dict__, indent=2), encoding="utf-8")
            os.replace(tmp, p)
        except OSError as e:
            log.warning("could not save %s: %s", p, e)


# ═══════════════════════════════════════════════════════════════════════════
# Filter
# ═══════════════════════════════════════════════════════════════════════════

class _TermFilter(QSortFilterProxyModel):
    """Leaf rows match when every term is a substring of the precomputed
    haystack. Folder rows never match on their own; recursive filtering shows
    them when any child does (the folder name is in each child's haystack)."""

    def __init__(self, parent: Optional[QObject] = None):
        super().__init__(parent)
        self._terms: List[str] = []
        self.setRecursiveFilteringEnabled(True)

    def set_query(self, text: str) -> None:
        self._terms = text.lower().split()
        self.invalidateFilter()

    def filterAcceptsRow(self, row: int, parent: QModelIndex) -> bool:
        hay = self.sourceModel().index(row, 0, parent).data(HAY_ROLE)
        if hay is None:
            return False
        return all(t in hay for t in self._terms)


class _DownToTree(QObject):
    def __init__(self, tree: QTreeView, parent: QObject):
        super().__init__(parent)
        self._tree = tree

    def eventFilter(self, obj: QObject, e: QEvent) -> bool:
        if e.type() == QEvent.KeyPress and e.key() in (Qt.Key_Down, Qt.Key_PageDown):
            self._tree.setFocus()
            return True
        return False


# ═══════════════════════════════════════════════════════════════════════════
# Dialog
# ═══════════════════════════════════════════════════════════════════════════

class DeviceSelector(QDialog):
    """
    prefill: values from the command line, which win over last_connect.json
      (username, key_file, key_passphrase, password, jump: JumpTarget).
    """

    def __init__(self, entries: Sequence[SessionEntry], platforms: Sequence[str],
                 sessions_file: str = "", prefill: Optional[dict] = None,
                 emulate: bool = False, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.setWindowTitle("terminaltelemetry2 - select device")
        self.resize(900, 720)
        self._entries = list(entries)
        self._platforms = sorted(platforms)
        self._sessions_file = str(Path(sessions_file).expanduser().resolve()) if sessions_file else ""
        self._emulate = emulate
        self._last = LastUsed.load()
        self._target: Optional[ConnectTarget] = None
        prefill = prefill or {}

        # -- search + tree ---------------------------------------------------
        self.search = QLineEdit()
        self.search.setPlaceholderText("Search name, host, platform, folder  (terms AND together)")
        self.search.setClearButtonEnabled(True)
        self.count = QLabel()

        self.model = QStandardItemModel(0, 3, self)
        self.model.setHorizontalHeaderLabels(["Device", "Host", "Platform"])
        self._populate()
        self.proxy = _TermFilter(self)
        self.proxy.setSourceModel(self.model)

        self.tree = QTreeView()
        self.tree.setModel(self.proxy)
        self.tree.setUniformRowHeights(True)
        self.tree.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.tree.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.tree.setSelectionMode(QAbstractItemView.SingleSelection)
        hdr = self.tree.header()
        hdr.setSectionResizeMode(0, QHeaderView.Stretch)
        hdr.setSectionResizeMode(1, QHeaderView.ResizeToContents)
        hdr.setSectionResizeMode(2, QHeaderView.ResizeToContents)
        self.tree.expandAll()
        self.tree.doubleClicked.connect(self._on_double_click)
        self.tree.selectionModel().currentRowChanged.connect(self._on_current)

        self._down = _DownToTree(self.tree, self)
        self.search.installEventFilter(self._down)
        self._debounce = QTimer(self)
        self._debounce.setSingleShot(True)
        self._debounce.setInterval(120)
        self._debounce.timeout.connect(self._apply_filter)
        self.search.textChanged.connect(lambda _t: self._debounce.start())

        self.session_jump = QLabel()
        self.session_jump.setStyleSheet("color: palette(link);")
        self.session_jump.hide()

        # -- credentials -----------------------------------------------------
        self.platform = QComboBox()
        self.platform.addItem("")                       # forces an explicit pick
        self.platform.addItems(self._platforms)
        self.username = QLineEdit(prefill.get("username") or self._last.username)
        self.password = QLineEdit(prefill.get("password") or _SECRETS.get("password", ""))
        self.password.setEchoMode(QLineEdit.Password)
        self.password.setPlaceholderText("optional with a key")
        self.key_file = QLineEdit(prefill.get("key_file") or self._last.key_file)
        self.key_file.setPlaceholderText("~/.ssh/id_ed25519")
        self.passphrase = QLineEdit(prefill.get("key_passphrase") or _SECRETS.get("passphrase", ""))
        self.passphrase.setEchoMode(QLineEdit.Password)
        self.passphrase.setPlaceholderText("only for an encrypted key")

        creds = QGroupBox("Connection")
        cf = QFormLayout(creds)
        cf.addRow("Platform", self.platform)
        cf.addRow("Username", self.username)
        cf.addRow("Password", self.password)
        cf.addRow("Key file", self._with_browse(self.key_file))
        cf.addRow("Passphrase", self.passphrase)

        # -- default jump host -----------------------------------------------
        pj: Optional[JumpTarget] = prefill.get("jump")
        self.jump_box = QGroupBox("Jump host (default for all devices)")
        self.jump_box.setCheckable(True)
        self.jump_box.setChecked(bool(pj) or self._last.jump_enabled)
        self.jump_host = QLineEdit(pj.host if pj else self._last.jump_host)
        self.jump_port = QSpinBox()
        self.jump_port.setRange(1, 65535)
        self.jump_port.setValue(pj.port if pj else (self._last.jump_port or 22))
        self.jump_user = QLineEdit((pj.username if pj else None) or self._last.jump_username)
        self.jump_user.setPlaceholderText("same as device")
        self.jump_password = QLineEdit((pj.password if pj else None) or _SECRETS.get("jump_password", ""))
        self.jump_password.setEchoMode(QLineEdit.Password)
        self.jump_password.setPlaceholderText("same as device")
        self.jump_key = QLineEdit((pj.key_file if pj else None) or self._last.jump_key_file)
        self.jump_key.setPlaceholderText("same as device")
        hostport = QWidget()
        hp = QHBoxLayout(hostport)
        hp.setContentsMargins(0, 0, 0, 0)
        hp.addWidget(self.jump_host, 1)
        hp.addWidget(QLabel("port"))
        hp.addWidget(self.jump_port)
        jf = QFormLayout(self.jump_box)
        jf.addRow("Host", hostport)
        jf.addRow("Username", self.jump_user)
        jf.addRow("Password", self.jump_password)
        jf.addRow("Key file", self._with_browse(self.jump_key))

        if emulate:
            self.jump_box.setChecked(False)
            self.jump_box.setEnabled(False)
            self.jump_box.setTitle("Jump host (bypassed in emulation)")

        # -- layout ----------------------------------------------------------
        bottom = QHBoxLayout()
        bottom.addWidget(creds, 1)
        bottom.addWidget(self.jump_box, 1)

        self.buttons = QDialogButtonBox(QDialogButtonBox.Cancel)
        self.connect_btn = self.buttons.addButton("Connect", QDialogButtonBox.AcceptRole)
        self.connect_btn.setDefault(True)
        self.buttons.accepted.connect(self._on_accept)
        self.buttons.rejected.connect(self.reject)

        top = QHBoxLayout()
        top.addWidget(self.search, 1)
        top.addWidget(self.count)

        lay = QVBoxLayout(self)
        lay.addLayout(top)
        lay.addWidget(self.tree, 1)
        lay.addWidget(self.session_jump)
        lay.addLayout(bottom)
        lay.addWidget(self.buttons)

        self._update_count()
        self._select_initial()
        self.search.setFocus()

    # -- construction helpers ------------------------------------------------

    def _with_browse(self, edit: QLineEdit) -> QWidget:
        w = QWidget()
        h = QHBoxLayout(w)
        h.setContentsMargins(0, 0, 0, 0)
        h.addWidget(edit, 1)
        b = QPushButton("...")
        b.setAutoDefault(False)
        b.setFixedWidth(32)
        b.clicked.connect(lambda: self._browse(edit))
        h.addWidget(b)
        return w

    def _browse(self, edit: QLineEdit) -> None:
        start = str(Path(edit.text() or "~/.ssh").expanduser().parent
                    if edit.text() else Path("~/.ssh").expanduser())
        path, _ = QFileDialog.getOpenFileName(self, "Private key", start)
        if path:
            edit.setText(path)

    def _populate(self) -> None:
        bold = QFont()
        bold.setBold(True)
        folders: Dict[str, QStandardItem] = {}
        for i, e in enumerate(self._entries):
            parent = folders.get(e.folder)
            if parent is None:
                parent = QStandardItem(e.folder)
                parent.setFont(bold)
                folders[e.folder] = parent
                self.model.appendRow([parent, QStandardItem(), QStandardItem()])
            name = QStandardItem(e.name)
            name.setData(i, ENTRY_ROLE)
            name.setData(e.haystack(), HAY_ROLE)
            plat = e.guess_platform(self._platforms) or e.platform_hint
            parent.appendRow([name, QStandardItem(e.host), QStandardItem(plat)])

    # -- navigation ----------------------------------------------------------

    def _leaves(self):
        """Visible leaf proxy indexes in display order."""
        for r in range(self.proxy.rowCount()):
            top = self.proxy.index(r, 0)
            if top.data(ENTRY_ROLE) is not None:
                yield top
                continue
            for c in range(self.proxy.rowCount(top)):
                yield self.proxy.index(c, 0, top)

    def _update_count(self) -> None:
        n = sum(1 for _ in self._leaves())
        self.count.setText(f"{n} / {len(self._entries)}")

    def _apply_filter(self) -> None:
        self.proxy.set_query(self.search.text())
        self.tree.expandAll()
        self._update_count()
        if self._current_entry() is None:          # current row filtered out, or a folder
            first = next(self._leaves(), None)
            if first is not None:
                self.tree.setCurrentIndex(first)
            else:
                self.session_jump.hide()

    def _select_initial(self) -> None:
        want = None
        if self._last.sessions_file == self._sessions_file and self._last.last_host:
            for idx in self._leaves():
                e = self._entries[idx.data(ENTRY_ROLE)]
                if e.host == self._last.last_host and e.name == self._last.last_name:
                    want = idx
                    break
        if want is None:
            want = next(self._leaves(), None)
        if want is not None:
            self.tree.setCurrentIndex(want)
            self.tree.scrollTo(want, QAbstractItemView.PositionAtCenter)

    def _current_entry(self) -> Optional[SessionEntry]:
        idx = self.tree.currentIndex()
        if not idx.isValid():
            return None
        i = self.proxy.index(idx.row(), 0, idx.parent()).data(ENTRY_ROLE)
        return None if i is None else self._entries[i]

    def _on_current(self, _cur: QModelIndex, _prev: QModelIndex) -> None:
        e = self._current_entry()
        if e is None:
            self.session_jump.hide()
            return
        plat = self._last.platform_overrides.get(e.host) or e.guess_platform(self._platforms)
        self.platform.setCurrentIndex(max(0, self.platform.findText(plat or "")))
        if e.username:
            self.username.setText(e.username)
        if e.jump_host and not self._emulate:
            who = f"{e.jump_username}@" if e.jump_username else ""
            self.session_jump.setText(
                f"Session jump host: {who}{e.jump_host}:{e.jump_port}  (overrides the default)")
            self.session_jump.show()
        else:
            self.session_jump.hide()

    def _on_double_click(self, idx: QModelIndex) -> None:
        if self.proxy.index(idx.row(), 0, idx.parent()).data(ENTRY_ROLE) is not None:
            self._on_accept()

    # -- accept --------------------------------------------------------------

    def _fail(self, msg: str, widget: Optional[QWidget] = None) -> None:
        QMessageBox.warning(self, "terminaltelemetry2", msg)
        if widget is not None:
            widget.setFocus()

    @staticmethod
    def _check_key(path: str, passphrase: Optional[str]) -> Optional[str]:
        p = Path(path).expanduser()
        if not p.is_file():
            return f"key file not found: {p}"
        try:
            paramiko.PKey.from_path(str(p), passphrase=passphrase.encode() if passphrase else None)
        except paramiko.PasswordRequiredException:
            return f"{p.name} is encrypted: enter its passphrase"
        except TypeError as e:
            # OpenSSH-format keys: cryptography raises TypeError, not paramiko's exception.
            if "password" in str(e).lower() and not passphrase:
                return f"{p.name} is encrypted: enter its passphrase"
            return f"{p.name}: {e}"
        except Exception as e:
            return f"{p.name}: {type(e).__name__}: {e}"
        return None

    def _on_accept(self) -> None:
        e = self._current_entry()
        if e is None:
            return self._fail("Select a device.", self.tree)
        plat = self.platform.currentText()
        if plat not in self._platforms:
            return self._fail("Pick a platform for this device.", self.platform)
        user = self.username.text().strip()
        if not user:
            return self._fail("Username is required.", self.username)
        password = self.password.text() or None
        key_file = self.key_file.text().strip() or None
        passphrase = self.passphrase.text() or None
        if self._emulate and not password:
            password = "emulated"                        # emulation substitutes mock creds
        if not password and not key_file:
            return self._fail("Enter a password or a key file.", self.password)
        if key_file and not self._emulate:
            err = self._check_key(key_file, passphrase)
            if err:
                return self._fail(err, self.passphrase if "passphrase" in err else self.key_file)

        jump = None
        if not self._emulate:
            default_on = self.jump_box.isChecked() and self.jump_host.text().strip()
            if self.jump_box.isChecked() and not default_on and not e.jump_host:
                return self._fail("Jump host is enabled but empty.", self.jump_host)
            jkey = self.jump_key.text().strip() or None
            jpw = self.jump_password.text() or None
            if e.jump_host:
                jump = JumpTarget(host=e.jump_host, port=e.jump_port,
                                  username=e.jump_username or self.jump_user.text().strip() or None,
                                  password=jpw, key_file=jkey)
            elif default_on:
                jump = JumpTarget(host=self.jump_host.text().strip(), port=self.jump_port.value(),
                                  username=self.jump_user.text().strip() or None,
                                  password=jpw, key_file=jkey)
            if jump and jkey and jkey != key_file:
                err = self._check_key(jkey, passphrase)
                if err:
                    return self._fail(f"jump {err}", self.jump_key)

        self._target = ConnectTarget(
            host=e.host, port=e.port, platform=plat, username=user,
            password=password, key_file=key_file, key_passphrase=passphrase,
            display_name=e.name, jump=jump,
        )

        # remember (no secrets on disk)
        last = self._last
        last.username = user
        last.key_file = key_file or ""
        if not self._emulate:
            last.jump_enabled = self.jump_box.isChecked()
            last.jump_host = self.jump_host.text().strip()
            last.jump_port = self.jump_port.value()
            last.jump_username = self.jump_user.text().strip()
            last.jump_key_file = self.jump_key.text().strip()
        last.sessions_file = self._sessions_file
        last.last_host, last.last_name = e.host, e.name
        if plat != e.guess_platform(self._platforms):
            last.platform_overrides[e.host] = plat
        else:
            last.platform_overrides.pop(e.host, None)
        last.save()
        for k, v in (("password", self.password.text()), ("passphrase", passphrase or ""),
                     ("jump_password", self.jump_password.text())):
            if v:
                _SECRETS[k] = v
            else:
                _SECRETS.pop(k, None)
        self.accept()

    def target(self) -> Optional[ConnectTarget]:
        return self._target
