"""
terminaltelemetry2 -- one device per window: terminal + telemetry widgets.

    terminaltelemetry2 --host 10.0.0.1 --user admin --platform arista_eos
    terminaltelemetry2 --sessions ~/sessions.yaml
    terminaltelemetry2 --host 10.0.0.1 --user admin --platform eos -J scott@bastion1:22
    terminaltelemetry2 --host eng-spine-1 --user admin --platform arista_eos --emulate ip_lookup.json
    terminaltelemetry2 --host 10.0.0.1 --user admin --platform cisco_ios --terminal mymod:TerminalWidget

--templates opens the Template Manager alone (no device); Ctrl+T opens it from
any device window.

--sessions without --host opens the device selector; with a session file
loaded, Ctrl+N opens another device in a new window.

Password: --password, else TERMINALTELEMETRY2_PASSWORD, else a prompt.
Jump host: -J [user@]host[:port]. Bastion auth inherits the device key and
password unless --jump-key / --jump-password (TERMINALTELEMETRY2_JUMP_PASSWORD)
are given.
"""
from __future__ import annotations

import argparse
from pathlib import Path
import logging
import os
import signal
import sys
from typing import List, Optional, Sequence, Tuple

from PySide6.QtCore import Qt, QTimer, Slot
from PySide6.QtGui import QAction, QKeySequence
from PySide6.QtWidgets import (QApplication, QDialog, QInputDialog, QLineEdit, QMainWindow,
                               QMessageBox, QPushButton)

from . import __version__
from .controller import DeviceController
from .layout import LayoutError, build_layout, load_layouts, pick_layout
from .parsing import Parser
from .platforms import registry
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
_MANAGER = None                         # the one Template Manager, shared by every window


def _live_captures():
    from .widgets.template_manager import LiveCapture
    for w in list(_WINDOWS):
        for cmd, out in w.ctrl.captures().items():
            if out and not cmd.startswith("if {"):           # skip the capability probe
                yield LiveCapture(w._target.label, w._target.platform, cmd, out)


def _reload_all_parsers() -> None:
    for w in list(_WINDOWS):
        w.parser.reload_overrides()


_PACKS = None                           # the one Platform Pack editor


def open_pack_editor(platform: str = ""):
    """Show (creating once) the Platform Pack editor, optionally on `platform`."""
    global _PACKS
    from .widgets.pack_editor import PackEditor
    if _PACKS is None:
        widgets, _ = load_widgets(widget_dirs())
        layouts, _ = load_layouts(layout_dirs())
        _PACKS = PackEditor(Parser(template_db(), template_override_dirs()), widgets, layouts,
                            captures=_live_captures, platform=platform)
        _PACKS.saved.connect(_pack_saved)
    elif platform:
        i = _PACKS.platform.findData(platform)
        if i >= 0:
            _PACKS.platform.setCurrentIndex(i)
    _PACKS.show()
    _PACKS.raise_()
    _PACKS.activateWindow()
    return _PACKS


_DESIGNER = None                        # the one Widget Designer


def open_designer(platform: str = "", template: str = ""):
    """Show (creating once) the Widget Designer, optionally on a template."""
    global _DESIGNER
    from .widgets.widget_designer import WidgetDesigner
    if _DESIGNER is None:
        widgets, _ = load_widgets(widget_dirs())
        layouts, _ = load_layouts(layout_dirs())
        _DESIGNER = WidgetDesigner(Parser(template_db(), template_override_dirs()), widgets,
                                   layouts, captures=_live_captures, platform=platform,
                                   template=template)
        _DESIGNER.packs_requested.connect(open_pack_editor)
        _DESIGNER.saved.connect(_widgets_changed)
    elif platform:
        i = _DESIGNER.platform.findData(platform)
        if i >= 0:
            _DESIGNER.platform.setCurrentIndex(i)
        if template:
            j = _DESIGNER.template.findData(template)
            if j >= 0:
                _DESIGNER.template.setCurrentIndex(j)
                _DESIGNER.start(template)
    _DESIGNER.show()
    _DESIGNER.raise_()
    _DESIGNER.activateWindow()
    return _DESIGNER


def _pack_saved(path: str) -> None:
    """Tell open device windows their platform's pack changed on disk."""
    from .platforms import load_pack
    try:
        platform = load_pack(Path(path)).platform
    except Exception:
        return
    for w in list(_WINDOWS):
        w.pack_changed(platform)


def _widgets_changed(*_):
    """A widget was saved: the Pack editor (if open) ranks it from now on."""
    if _PACKS is not None:
        widgets, _ = load_widgets(widget_dirs())
        _PACKS.set_widgets(widgets)


def open_manager():
    """Show (creating once) the app-wide Template Manager. Its writes clear
    every open window's parser pins, so widgets re-resolve on the next poll."""
    global _MANAGER
    if _MANAGER is None:
        from .widgets.template_manager import TemplateManager
        _MANAGER = TemplateManager(Parser(template_db(), template_override_dirs()),
                                   captures=_live_captures)
        _MANAGER.changed.connect(_reload_all_parsers)
        _MANAGER.packs_requested.connect(open_pack_editor)
        _MANAGER.design_requested.connect(open_designer)
    _MANAGER.refresh()
    _MANAGER.show()
    _MANAGER.raise_()
    _MANAGER.activateWindow()
    return _MANAGER


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="terminaltelemetry2", description="Terminal + telemetry for one device")
    p.add_argument("-S", "--sessions", metavar="FILE",
                   help="session YAML (folder_name/sessions); without --host opens the device selector")
    p.add_argument("--host")
    p.add_argument("--user")
    p.add_argument("--platform", type=normalize_platform,
                   help="a platform pack id or alias (arista_eos/eos, cisco_ios/ios, cisco_nxos/nxos, "
                        "juniper_junos/junos, linux, hp_comware/comware, ... -- see data/platforms)")
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
    p.add_argument("--templates", action="store_true",
                   help="open the Template Manager without connecting to a device")
    p.add_argument("--designer", nargs="?", const="", metavar="PLATFORM",
                   help="open the Widget Designer (optionally on PLATFORM) without a device")
    p.add_argument("--packs", nargs="?", const="", metavar="PLATFORM",
                   help="open the Platform Pack editor (optionally on PLATFORM) without a device")
    p.add_argument("--licenses", action="store_true",
                   help="print the license notice and third-party components, and exit")
    p.add_argument("--check-platforms", action="store_true",
                   help="validate platform packs (bundled + user), print coverage, and exit "
                        "(non-zero on any problem)")
    p.add_argument("--debug", action="store_true")
    p.add_argument("--version", action="version", version=f"terminaltelemetry2 {__version__}")
    args = p.parse_args(argv)

    if args.check_platforms or args.licenses:
        return args
    if args.packs is not None:
        args.packs = normalize_platform(args.packs) if args.packs else ""
    if args.designer is not None:
        args.designer = normalize_platform(args.designer) if args.designer else ""
    # Nothing to connect to and no tool asked for: main() opens the Connect form.
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
    return registry().known(widgets)


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
    # Command-line flags win; else the platform pack's session defaults.
    pack = registry().get(target.platform)
    username = target.username
    if pack and pack.username_suffix and not username.endswith(pack.username_suffix):
        username += pack.username_suffix
    extra = {}
    if pack and pack.read_timeout:
        extra["expect_prompt_timeout"] = int(pack.read_timeout * 1000)
    return SSHClientConfig(
        host=target.host, port=target.port, username=username, password=target.password,
        key_file=target.key_file, key_passphrase=target.key_passphrase,
        enable_command=args.enable_command or (pack.enable if pack else None),
        paging_disable_command=(args.paging_command if args.paging_command
                                else pack.paging_config if pack else None),
        legacy_ssh=True if args.legacy_ssh else None,
        jump=target.jump_spec(),
        **extra
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
        plats = registry()
        known = plats.known(widgets)
        if target.platform not in known:
            raise LayoutError(f"no widgets define platform {target.platform!r}; known: {', '.join(known)}")
        # One window, one platform: specialize every widget with the pack's bindings.
        werr = list(werr)
        widgets = plats.apply(widgets, target.platform, warnings=werr)
        self._pack = plats.get(target.platform)
        layouts, lerr = load_layouts(layout_dirs())
        want = args.layout or (self._pack.layout if self._pack and self._pack.layout in layouts else None)
        layout = pick_layout(layouts, target.platform, want)

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
        from .widgets.about_dialog import add_help_menu
        add_help_menu(self)

        self.parser = Parser(template_db(), template_override_dirs())
        from .session import SHELL_PRIMES
        prime = (SHELL_PRIMES.get(self._pack.shell) if self._pack and self._pack.shell else None) \
            if self._pack else ...
        self.ctrl = DeviceController(config, target.platform, self.parser, placed, self, prime=prime)
        self.ctrl.state.connect(self._on_state)
        # Template lab: every widget's { } button routes here, preloaded with the
        # capture it collected and the template it used. Overrides save to the
        # user templates dir (first entry), which the parser searches before the DB.
        self.lab = TemplateLab(self.parser, template_override_dirs()[0], self)
        self.inspector = None
        self.ctrl.lab_requested.connect(self._open_lab)
        self._reload_btn = QPushButton("Platform pack changed - reload window")
        self._reload_btn.setToolTip("This window read its platform pack when it opened; reload "
                                    "to reconnect with the saved pack (timeouts, sudo, bindings)")
        self._reload_btn.clicked.connect(self.reload)
        self._reload_btn.setVisible(False)
        self.statusBar().addPermanentWidget(self._reload_btn)
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

        design = QAction("Widget Designer", self)
        design.setShortcut(QKeySequence("Ctrl+Shift+W"))
        design.setShortcutContext(Qt.WindowShortcut)
        design.triggered.connect(lambda: open_designer(self._target.platform))
        self.addAction(design)

        packs = QAction("Platform Pack editor", self)
        packs.setShortcut(QKeySequence("Ctrl+Shift+P"))
        packs.setShortcutContext(Qt.WindowShortcut)
        packs.triggered.connect(lambda: open_pack_editor(self._target.platform))
        self.addAction(packs)

        templates = QAction("Template Manager", self)
        templates.setShortcut(QKeySequence("Ctrl+T"))
        templates.setShortcutContext(Qt.WindowShortcut)
        templates.triggered.connect(open_manager)
        self.addAction(templates)

        # Ctrl+N: the device selector with a sessions file, else the Connect form
        open_dev = QAction("Open device...", self)
        open_dev.setShortcut(QKeySequence.New)            # Ctrl+N / Cmd+N
        open_dev.setShortcutContext(Qt.WindowShortcut)
        open_dev.triggered.connect(self._open_device)
        self.addAction(open_dev)

        from .platforms import registry_errors
        problems = werr + lerr + registry_errors() + plats.conflicts
        self.statusBar().showMessage(
            f"layout {layout.name}; {len(placed)} widgets; {self.parser.template_count} templates"
            + (f"; {len(problems)} definition error(s), see log" if problems else "")
        )
        self.ctrl.start()
        self.bridge.open()

    def pack_changed(self, platform: str) -> None:
        """A pack for this window's platform was saved: offer a reload."""
        if platform == self._target.platform:
            self._reload_btn.setVisible(True)

    def reload(self) -> None:
        """Reopen this device with the current packs, widgets and layouts,
        then close this window (same target, same credentials)."""
        if open_window(self._args, self._target, self._entries, parent=self):
            self.close()

    def _open_lab(self, src) -> None:
        """{ } on a widget: the TextFSM lab, or for python-parsed widgets the
        output inspector (raw output, parser error, hint)."""
        if src.template.startswith("py:"):
            if self.inspector is None:
                from .widgets.output_inspector import OutputInspector
                self.inspector = OutputInspector(self)
            self.inspector.open_source(src)
        else:
            self.lab.open_source(src)

    @Slot(str, str)
    def _monitor(self, intf: str, which: str) -> None:
        from .monitor import COUNTER_COMMANDS, TrafficMonitor
        counters = self._pack.counters if self._pack else None
        if counters is None and self._target.platform not in COUNTER_COMMANDS:
            QMessageBox.information(self, "terminaltelemetry2",
                                    f"No counters: section in the {self._target.platform} platform pack")
            return
        if self.monitor is None:
            self.monitor = TrafficMonitor(self.ctrl.broker, self._target.platform,
                                          self._target.label, self, counters=counters)
        self.monitor.add(intf, which)

    @Slot()
    def _open_device(self) -> None:
        if self._entries:
            target = run_selector(self._args, self._entries, parent=self)
            if target is not None:
                open_window(self._args, target, self._entries, parent=self)
            return
        picked = run_connect_form(self._args, parent=self)
        if picked is not None:
            open_window(picked[0], picked[1], None, parent=self)

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


def run_connect_form(args: argparse.Namespace, parent=None
                     ) -> Optional[Tuple[argparse.Namespace, ConnectTarget]]:
    """The Connect form, pre-filled from the last connection. Returns a copy
    of `args` carrying the form's choices (so each window keeps its own) and
    the target; None on cancel. Remembers the non-secret fields."""
    from . import recent
    from .widgets.connect_dialog import ConnectDialog
    plats = registry()
    choices = [(p, (plats.get(p).title if plats.get(p) else p)) for p in known_platforms()]
    layouts, _ = load_layouts(layout_dirs())
    dlg = ConnectDialog(choices, list(layouts), recent.load(), parent)
    if dlg.exec() != QDialog.Accepted:
        return None
    v = dlg.values()
    a = argparse.Namespace(**vars(args))
    a.host, a.port, a.user, a.platform = v["host"], v["port"], v["user"], v["platform"]
    a.password = v["password"] or None
    a.key = v["key"] or None
    a.key_passphrase = v["key_passphrase"] or None
    a.jump = v["jump"] or None
    a.jump_key = v["jump_key"] or None
    a.jump_password = v["jump_password"] or None
    a.layout = v["layout"] or args.layout
    a.enable_command = v["enable_command"] or args.enable_command
    a.legacy_ssh = v["legacy_ssh"] or args.legacy_ssh
    target = target_from_args(a)
    if target is None:
        return None
    try:
        recent.remember(v)
    except OSError as e:
        log.warning("could not save recent connections: %s", e)
    return a, target


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
    if args.licenses:
        from .about import notices_text
        print(notices_text())
        return 0
    if args.check_platforms:
        return check_platforms()
    logging.basicConfig(level=logging.DEBUG if args.debug else logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("paramiko").setLevel(logging.WARNING)

    app = QApplication.instance() or QApplication(sys.argv[:1])
    _install_sigint(app)
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

    if (args.templates or args.packs is not None or args.designer is not None) \
            and not args.host and not args.sessions:
        if args.templates:
            open_manager()
        if args.packs is not None:
            open_pack_editor(args.packs)
        if args.designer is not None:
            open_designer(args.designer)
        return _exit(app.exec())

    if args.host:
        target = target_from_args(args)
    elif entries is not None:
        target = run_selector(args, entries)
    else:
        picked = run_connect_form(args)          # bare `tt2`: the Connect form
        if picked is None:
            return 1
        args, target = picked
    if target is None:
        return 1
    if not open_window(args, target, entries):
        return 2
    return _exit(app.exec())


def check_platforms() -> int:
    """`tt2 --check-platforms`: no Qt window, prints the pack report."""
    import sqlite3
    from contextlib import closing
    from .platforms import check_report, load_platforms
    from .paths import platform_dirs
    from .parsing.store import migrate
    reg, errors = load_platforms(platform_dirs())
    widgets, werr = load_widgets(widget_dirs())
    db = template_db()
    migrate(str(db))
    with closing(sqlite3.connect(str(db))) as c:
        counts = dict(c.execute("SELECT platform, SUM(enabled) FROM templates GROUP BY platform"))
    report, ok = check_report(reg, errors, widgets, counts)
    print(report)
    for e in werr:
        print(f"PROBLEM: widget: {e}")
    print("\nsearched: " + ", ".join(str(d) for d in platform_dirs()))
    return 0 if ok and not werr else 1


def _install_sigint(app: QApplication) -> None:
    """Ctrl+C closes every window (so each stops its threads cleanly), then
    quits. A second Ctrl+C while that is in progress exits immediately.
    The idle timer hands control back to Python so the handler can run --
    otherwise the signal waits until the next Qt event."""
    state = {"n": 0}

    def _on_sigint(*_):
        state["n"] += 1
        if state["n"] > 1:
            os._exit(130)
        log.info("interrupt: closing windows")
        app.closeAllWindows()
        app.quit()

    signal.signal(signal.SIGINT, _on_sigint)
    tick = QTimer(app)
    tick.timeout.connect(lambda: None)
    tick.start(250)


def _exit(rc: int) -> int:
    """After the event loop: wait for any detached worker threads. If one is
    still stuck (an unresponsive DNS lookup), skip interpreter teardown --
    finalizing a running QThread aborts the process."""
    from .session import reap_threads
    left = reap_threads()
    if left:
        log.warning("%d worker thread(s) still blocked at exit; exiting without teardown", left)
        logging.shutdown()
        os._exit(rc)
    return rc
