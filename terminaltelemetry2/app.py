"""
terminaltelemetry2 -- one device per window: terminal + telemetry widgets.

    terminaltelemetry2 --host 10.0.0.1 --user admin --platform arista_eos
    terminaltelemetry2 --sessions ~/sessions.yaml
    terminaltelemetry2 --host 10.0.0.1 --user admin --platform eos -J scott@bastion1:22
    terminaltelemetry2 --host eng-spine-1 --user admin --platform arista_eos --emulate ip_lookup.json
    terminaltelemetry2 --host 10.0.0.1 --user admin --platform cisco_ios --terminal mymod:TerminalWidget

--sessions without --host opens the device selector; with a session file
loaded, Ctrl+N opens another device in a new window.

Password: --password, else TERMINALTELEMETRY2_PASSWORD, else a prompt.
Jump host: -J [user@]host[:port]. Bastion auth inherits the device key and
password unless --jump-key / --jump-password (TERMINALTELEMETRY2_JUMP_PASSWORD)
are given.
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
from typing import List, Optional, Sequence

from PySide6.QtCore import Qt, Slot
from PySide6.QtGui import QAction, QKeySequence
from PySide6.QtWidgets import QApplication, QInputDialog, QLineEdit, QMainWindow, QMessageBox

from . import __version__
from .controller import DeviceController
from .layout import LayoutError, build_layout, load_layouts, pick_layout
from .parsing import Parser
from .paths import layout_dirs, template_db, template_override_dirs, widget_dirs
from .session import TerminalBridge
from .sessions import (
    ConnectTarget, JumpTarget, SessionEntry, jump_to_str, load_sessions,
    normalize_platform, parse_jump,
)
from .ssh import emulation
from .ssh.client import SSHClientConfig
from .terminal import (
    grid_size, install_font_zoom, install_tab_passthrough, install_terminal_context_menu,
    load_terminal_widget,
)
from .widgets import load_widgets
from .widgets.lab_dialog import TemplateLab

log = logging.getLogger("terminaltelemetry2")

# Top-level windows; Qt does not hold a Python reference for us.
_WINDOWS: List["MainWindow"] = []


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="terminaltelemetry2", description="Terminal + telemetry for one device")
    p.add_argument("-S", "--sessions", metavar="FILE",
                   help="session YAML (folder_name/sessions); without --host opens the device selector")
    p.add_argument("--host")
    p.add_argument("--user")
    p.add_argument("--platform", type=normalize_platform,
                   help="arista_eos, cisco_ios, cisco_nxos, juniper_junos (short forms: eos, ios, nxos, junos)")
    p.add_argument("--port", type=int, default=22)
    p.add_argument("--password")
    p.add_argument("-i", "--key", metavar="FILE",
                   help="private key file for key-based auth (e.g. ~/.ssh/id_ed25519)")
    p.add_argument("--key-passphrase",
                   help="passphrase for an encrypted key "
                        "(else $TERMINALTELEMETRY2_KEY_PASSPHRASE)")
    p.add_argument("-J", "--jump", metavar="[USER@]HOST[:PORT]",
                   help="jump host / bastion (single hop)")
    p.add_argument("--jump-key", metavar="FILE", help="bastion key (default: device key)")
    p.add_argument("--jump-password",
                   help="bastion password (else $TERMINALTELEMETRY2_JUMP_PASSWORD, else device password)")
    p.add_argument("--layout", help="layout name (default: first matching platform, else 'default')")
    p.add_argument("--terminal", help="terminal widget as module:Class (default: anytermqt, else fallback)")
    p.add_argument("--enable-command", help="e.g. 'enable' for IOS devices that land in user mode")
    p.add_argument("--paging-command", help="exact paging-disable command (default: netlapse shotgun)")
    p.add_argument("--legacy-ssh", action="store_true")
    p.add_argument("--emulate", nargs="?", const="", metavar="IP_LOOKUP_JSON",
                   help="route SSH to NetEmulate mock devices")
    p.add_argument("--debug", action="store_true")
    p.add_argument("--version", action="version", version=f"terminaltelemetry2 {__version__}")
    args = p.parse_args(argv)

    if not args.host and not args.sessions:
        p.error("either --host or --sessions is required")
    if args.host and not (args.user and args.platform):
        p.error("--host requires --user and --platform")
    if args.jump:
        try:
            parse_jump(args.jump)
        except ValueError as e:
            p.error(f"--jump: {e}")
    return args


def known_platforms() -> List[str]:
    widgets, _ = load_widgets(widget_dirs())
    return sorted({p for w in widgets.values() for p in w.commands})


def jump_from_args(args: argparse.Namespace) -> Optional[JumpTarget]:
    if not args.jump or args.emulate is not None:
        return None
    user, host, port = parse_jump(args.jump)
    return JumpTarget(
        host=host, port=port, username=user,
        password=args.jump_password or os.environ.get("TERMINALTELEMETRY2_JUMP_PASSWORD"),
        key_file=args.jump_key,
    )


def build_ssh_config(target: ConnectTarget, args: argparse.Namespace) -> SSHClientConfig:
    return SSHClientConfig(
        host=target.host, port=target.port, username=target.username, password=target.password,
        key_file=target.key_file, key_passphrase=target.key_passphrase,
        enable_command=args.enable_command, paging_disable_command=args.paging_command,
        legacy_ssh=True if args.legacy_ssh else None,
        jump=target.jump_spec(),
    )


class MainWindow(QMainWindow):
    def __init__(self, args: argparse.Namespace, target: ConnectTarget,
                 entries: Optional[Sequence[SessionEntry]] = None):
        super().__init__()
        self.setAttribute(Qt.WA_DeleteOnClose)
        self._args = args
        self._entries = entries
        via = f" via {jump_to_str(target.jump)}" if target.jump else ""
        self.setWindowTitle(f"{target.label} ({target.platform}){via} - terminaltelemetry2")
        self.resize(1500, 900)

        widgets, werr = load_widgets(widget_dirs())
        known = sorted({p for w in widgets.values() for p in w.commands})
        if target.platform not in known:
            raise LayoutError(f"no widgets define platform {target.platform!r}; known: {', '.join(known)}")
        layouts, lerr = load_layouts(layout_dirs())
        layout = pick_layout(layouts, target.platform, args.layout)

        # Build the config first: a bad jump key should fail before any widgets exist.
        config = build_ssh_config(target, args)

        self.term = load_terminal_widget(args.terminal)
        # Tab is for shell completion in the terminal, not focus traversal.
        self._tab_filter = install_tab_passthrough(self.term)
        # Right-click Copy/Paste (wrap-safe copy via anytermqt's copySelection).
        self._term_menu = install_terminal_context_menu(self.term)
        # Ctrl/Cmd +/-/0 and Ctrl/Cmd+wheel; size persists across windows.
        self._font_zoom = install_font_zoom(self.term)
        root, placed = build_layout(layout, widgets, self.term, target.platform)
        self.setCentralWidget(root)

        self.parser = Parser(template_db(), template_override_dirs())
        self.ctrl = DeviceController(config, target.platform, self.parser, placed, self)
        self.ctrl.state.connect(self._on_state)
        # Template lab: every widget's { } button routes here, preloaded with the
        # capture it collected and the template it used. Overrides save to the
        # user templates dir (first entry), which the parser searches before the DB.
        self.lab = TemplateLab(self.parser, template_override_dirs()[0], self)
        self.ctrl.lab_requested.connect(self.lab.open_source)
        # Right-click an interface -> Monitor Tx/Rx: one traffic window per device.
        self._target = target
        self.monitor = None
        self.ctrl.monitor_requested.connect(self._monitor)

        cols, rows = grid_size(self.term)
        self.bridge = TerminalBridge(config, self.term, cols=cols, rows=rows, parent=self)
        self.bridge.failed.connect(lambda m: self.statusBar().showMessage(f"terminal: {m}"))
        self.bridge.closed.connect(lambda: self.statusBar().showMessage("terminal: session closed"))
        if hasattr(self.term, "resized"):
            self.term.resized.connect(self.bridge.resize)

        refresh = QAction("Refresh", self)
        refresh.setShortcut(QKeySequence("F5"))
        refresh.triggered.connect(self.ctrl.refresh)
        self.addAction(refresh)

        if entries:
            open_dev = QAction("Open device...", self)
            open_dev.setShortcut(QKeySequence.New)            # Ctrl+N / Cmd+N
            open_dev.setShortcutContext(Qt.WindowShortcut)
            open_dev.triggered.connect(self._open_device)
            self.addAction(open_dev)

        problems = werr + lerr
        self.statusBar().showMessage(
            f"layout {layout.name}; {len(placed)} widgets; {self.parser.template_count} templates"
            + (f"; {len(problems)} definition error(s), see log" if problems else "")
        )
        self.ctrl.start()
        self.bridge.open()

    @Slot(str, str)
    def _monitor(self, intf: str, which: str) -> None:
        from .monitor import COUNTER_COMMANDS, TrafficMonitor
        if self._target.platform not in COUNTER_COMMANDS:
            QMessageBox.information(self, "terminaltelemetry2",
                                    f"No counter command for {self._target.platform}")
            return
        if self.monitor is None:
            self.monitor = TrafficMonitor(self.ctrl.broker, self._target.platform,
                                          self._target.label, self)
        self.monitor.add(intf, which)

    @Slot()
    def _open_device(self) -> None:
        target = run_selector(self._args, self._entries, parent=self)
        if target is not None:
            open_window(self._args, target, self._entries, parent=self)

    @Slot(str, str)
    def _on_state(self, state: str, detail: str) -> None:
        self.statusBar().showMessage(f"telemetry: {state}" + (f" - {detail}" if detail else ""))

    def closeEvent(self, e) -> None:
        if self.monitor is not None:
            self.monitor.close()
        self.ctrl.stop()
        self.bridge.close()
        if self in _WINDOWS:
            _WINDOWS.remove(self)
        super().closeEvent(e)


def run_selector(args: argparse.Namespace, entries: Sequence[SessionEntry],
                 parent=None) -> Optional[ConnectTarget]:
    from .selector import DeviceSelector
    prefill = {
        "username": args.user, "password": args.password or os.environ.get("TERMINALTELEMETRY2_PASSWORD"),
        "key_file": args.key,
        "key_passphrase": args.key_passphrase or os.environ.get("TERMINALTELEMETRY2_KEY_PASSPHRASE"),
        "jump": jump_from_args(args),
    }
    dlg = DeviceSelector(entries, known_platforms(), sessions_file=args.sessions or "",
                         prefill=prefill, emulate=args.emulate is not None, parent=parent)
    dlg.exec()
    return dlg.target()                   # set only on a validated Connect


def open_window(args: argparse.Namespace, target: ConnectTarget,
                entries: Optional[Sequence[SessionEntry]], parent=None) -> bool:
    try:
        win = MainWindow(args, target, entries)
    except (LayoutError, FileNotFoundError, ValueError) as e:
        QMessageBox.critical(parent, "terminaltelemetry2", str(e))
        return False
    _WINDOWS.append(win)
    win.show()
    return True


def target_from_args(args: argparse.Namespace) -> Optional[ConnectTarget]:
    password = args.password or os.environ.get("TERMINALTELEMETRY2_PASSWORD")
    key_passphrase = args.key_passphrase or os.environ.get("TERMINALTELEMETRY2_KEY_PASSPHRASE")
    if not password and args.emulate is not None:
        password = "emulated"            # emulation substitutes mock credentials
    # A key is an auth method on its own -- only prompt for a password when
    # there's neither a key nor a password (and we're not emulating).
    if not password and not args.key and args.emulate is None:
        password, ok = QInputDialog.getText(None, "terminaltelemetry2", f"Password for {args.user}@{args.host}",
                                            QLineEdit.Password)
        if not ok or not password:
            return None
    return ConnectTarget(
        host=args.host, port=args.port, platform=args.platform, username=args.user,
        password=password, key_file=args.key, key_passphrase=key_passphrase,
        jump=jump_from_args(args),
    )


def main(argv: Optional[List[str]] = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.debug else logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("paramiko").setLevel(logging.WARNING)

    app = QApplication(sys.argv[:1])
    if args.emulate is not None:
        n = emulation.enable_emulation(args.emulate or None)
        log.info("emulation: %d mock devices", n)

    entries: Optional[List[SessionEntry]] = None
    if args.sessions:
        try:
            entries = load_sessions(args.sessions)
        except (OSError, ValueError) as e:
            QMessageBox.critical(None, "terminaltelemetry2", f"session file: {e}")
            return 2
        log.info("sessions: %d devices from %s", len(entries), args.sessions)

    if args.host:
        target = target_from_args(args)
    else:
        target = run_selector(args, entries)
    if target is None:
        return 1
    if not open_window(args, target, entries):
        return 2
    return app.exec()
