"""
Terminal widget selection.

Resolution order: --terminal / TERMINALTELEMETRY2_TERMINAL "module:Class" spec, then
anytermqt.TerminalWidget if installed, then FallbackTerminal.

Contract (anytermqt 0.1.0):
    feed(QByteArray)            slot, bytes from the device
    dataReady(QByteArray)       signal, bytes to the device
    resized(int, int)           signal, cols/rows -> PTY resize
    columns(), terminalRows()   current grid size (optional)

FallbackTerminal implements the same contract on a QPlainTextEdit. It strips
escape sequences and does no cursor addressing: fine for show commands only.
"""
from __future__ import annotations

import importlib
import json
import logging
import os
import sys
from typing import Optional, Tuple

from PySide6.QtCore import QByteArray, QEvent, QObject, Qt, Signal, Slot
from PySide6.QtGui import QCursor, QFontDatabase, QKeyEvent, QTextCursor
from PySide6.QtWidgets import QApplication, QMenu, QPlainTextEdit, QWidget

from .paths import config_dir
from .ssh.client import filter_ansi_sequences

log = logging.getLogger(__name__)

_KEYS = {
    Qt.Key_Return: b"\r", Qt.Key_Enter: b"\r", Qt.Key_Backspace: b"\x7f",
    Qt.Key_Tab: b"\t", Qt.Key_Backtab: b"\x1b[Z", Qt.Key_Escape: b"\x1b",
    Qt.Key_Delete: b"\x1b[3~",
    Qt.Key_Up: b"\x1b[A", Qt.Key_Down: b"\x1b[B",
    Qt.Key_Right: b"\x1b[C", Qt.Key_Left: b"\x1b[D",
    Qt.Key_Home: b"\x1b[H", Qt.Key_End: b"\x1b[F",
}


class FallbackTerminal(QPlainTextEdit):
    dataReady = Signal(QByteArray)
    resized = Signal(int, int)

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.setReadOnly(True)
        self.setUndoRedoEnabled(False)
        self.setLineWrapMode(QPlainTextEdit.NoWrap)
        self.setMaximumBlockCount(10000)
        self.setFont(QFontDatabase.systemFont(QFontDatabase.FixedFont))
        self.setFocusPolicy(Qt.StrongFocus)
        self._last_size: Tuple[int, int] = (0, 0)

    @Slot(bytes)
    def feed(self, data: bytes) -> None:
        text = filter_ansi_sequences(bytes(data).decode("utf-8", errors="replace"))
        text = text.replace("\r\n", "\n").replace("\r", "")
        cur = self.textCursor()
        cur.movePosition(QTextCursor.End)
        cur.insertText(text)
        self.setTextCursor(cur)
        self.ensureCursorVisible()

    def keyPressEvent(self, e: QKeyEvent) -> None:
        seq = _KEYS.get(e.key())
        if seq is None:
            text = e.text()
            if not text:
                return
            seq = text.encode("utf-8")
        self.dataReady.emit(QByteArray(seq))

    def paste(self, text: str) -> None:
        # Paste goes to the shell, not into the read-only display.
        if text:
            self.dataReady.emit(QByteArray(text.encode("utf-8")))

    def resizeEvent(self, e) -> None:
        super().resizeEvent(e)
        self._emit_grid()

    def changeEvent(self, e) -> None:
        super().changeEvent(e)
        if e.type() == QEvent.FontChange and hasattr(self, "_last_size"):
            self._emit_grid()

    def _emit_grid(self) -> None:
        fm = self.fontMetrics()
        vp = self.viewport().size()
        cols = max(20, vp.width() // max(1, fm.horizontalAdvance("M")))
        rows = max(5, vp.height() // max(1, fm.lineSpacing()))
        if (cols, rows) != self._last_size:
            self._last_size = (cols, rows)
            self.resized.emit(cols, rows)


DEFAULT_SPEC = "anytermqt:TerminalWidget"


def _load(spec: str) -> QWidget:
    module_name, _, cls_name = spec.partition(":")
    w = getattr(importlib.import_module(module_name), cls_name)()
    for attr in ("feed", "dataReady"):
        if not hasattr(w, attr):
            raise TypeError(f"{spec} has no {attr!r}")
    return w


def load_terminal_widget(spec: Optional[str] = None) -> QWidget:
    explicit = spec or os.environ.get("TERMINALTELEMETRY2_TERMINAL")
    for candidate in ([explicit] if explicit else []) + [DEFAULT_SPEC]:
        try:
            return _load(candidate)
        except Exception as e:
            level = logging.WARNING if candidate == explicit else logging.INFO
            log.log(level, "terminal widget %r unavailable (%s)", candidate, e)
    log.warning("using fallback terminal (pip install anytermqt for full emulation)")
    return FallbackTerminal()


def grid_size(term: QWidget, default: Tuple[int, int] = (120, 40)) -> Tuple[int, int]:
    try:
        return int(term.columns()), int(term.terminalRows())
    except Exception:
        return default


class _TabPassthrough(QObject):
    """When the terminal (or a child of it) has focus, deliver Tab/Backtab to
    the terminal's key handler instead of letting Qt move focus out of it.

    Qt resolves Tab in QWidget.event() via focusNextPrevChild(), which runs
    before keyPressEvent -- so a terminal never sees Tab unless something stops
    that traversal. This filter is app-wide but gated on focus: it only acts
    when focus is inside the terminal subtree, so Tab still traverses normally
    in the telemetry tables, the template lab, and dialogs.
    """

    _KEYS = (Qt.Key_Tab, Qt.Key_Backtab)

    def __init__(self, term: QWidget):
        super().__init__(term)                       # lifetime tied to the terminal
        self._term = term

    def eventFilter(self, obj: QObject, event: QEvent) -> bool:
        if event.type() == QEvent.KeyPress and event.key() in self._KEYS:
            term = self._term
            fw = QApplication.focusWidget()
            if fw is term or (fw is not None and term.isAncestorOf(fw)):
                term.keyPressEvent(event)            # emits the byte to the PTY
                return True                          # and swallow the focus move
        return False


def install_tab_passthrough(term: QWidget) -> Optional[QObject]:
    """Route Tab/Backtab to `term` while it has focus. Returns the filter (keep
    a reference so it isn't collected); no-op if there's no QApplication yet."""
    app = QApplication.instance()
    if app is None:
        return None
    filt = _TabPassthrough(term)
    app.installEventFilter(filt)
    return filt


class _TerminalMenu(QObject):
    """Right-click Copy/Paste/Select-All for the terminal. Uses anytermqt's
    primitives when present -- copySelection() joins soft-wrapped rows with no
    newline, so a wrapped SSH key copies as one line -- and degrades to the
    plain-widget API (copy/selectAll + a paste that sends to the PTY) otherwise.
    """

    def __init__(self, term: QWidget):
        super().__init__(term)
        self._term = term

    def _has_selection(self) -> bool:
        term = self._term
        if hasattr(term, "hasSelection"):
            return bool(term.hasSelection())
        if hasattr(term, "textCursor"):
            return term.textCursor().hasSelection()
        return False

    def show_at(self, _pos) -> None:
        term = self._term
        menu = QMenu(term)
        copy = menu.addAction("Copy")
        copy.setEnabled(self._has_selection())
        copy.triggered.connect(self._copy)
        paste = menu.addAction("Paste")
        paste.setEnabled(bool(QApplication.clipboard().text()))
        paste.triggered.connect(self._paste)
        menu.addSeparator()
        sel_all = menu.addAction("Select All")
        sel_all.setEnabled(hasattr(term, "selectAll"))
        sel_all.triggered.connect(self._select_all)
        # QCursor.pos() is robust whether the signal came from the widget or its
        # viewport (scroll-area terminals deliver context events to the viewport).
        menu.exec(QCursor.pos())

    def _copy(self) -> None:
        term = self._term
        if hasattr(term, "copySelection"):
            term.copySelection()             # anytermqt: wrap-safe, to clipboard
        elif hasattr(term, "copy"):
            term.copy()                       # QPlainTextEdit fallback

    def _paste(self) -> None:
        text = QApplication.clipboard().text()
        if text and hasattr(self._term, "paste"):
            self._term.paste(text)

    def _select_all(self) -> None:
        if hasattr(self._term, "selectAll"):
            self._term.selectAll()


def install_terminal_context_menu(term: QWidget) -> Optional[QObject]:
    """Install a right-click Copy/Paste menu on the terminal (and its viewport,
    for scroll-area widgets). Returns the menu object (keep a reference)."""
    menu = _TerminalMenu(term)
    targets = [term]
    vp = term.viewport() if hasattr(term, "viewport") and callable(term.viewport) else None
    if vp is not None:
        targets.append(vp)
    for t in targets:
        t.setContextMenuPolicy(Qt.CustomContextMenu)
        t.customContextMenuRequested.connect(menu.show_at)
    return menu


class _FontZoom(QObject):
    """Ctrl/Cmd + '+' / '-' / '0' and Ctrl/Cmd + wheel resize the terminal font.

    Keys are caught with an app-wide filter gated on the terminal's window, so
    they work whichever pane has focus and the terminal can't swallow them as
    input first. Wheel is caught on the terminal and its viewport only. The
    size is persisted to <config_dir>/terminal.json and applied to every new
    window. anytermqt's setTerminalFont() recomputes the grid and emits
    resized(cols, rows), which the TerminalBridge forwards to the PTY.
    """

    MIN_PT, MAX_PT, STEP = 6.0, 40.0, 1.0
    _UP = (Qt.Key_Plus, Qt.Key_Equal)
    _DOWN = (Qt.Key_Minus, Qt.Key_Underscore)
    _RESET = (Qt.Key_0,)

    def __init__(self, term: QWidget):
        super().__init__(term)
        self._term = term
        self._default = self._size()
        self._wheel_acc = 0
        vp = term.viewport() if hasattr(term, "viewport") and callable(term.viewport) else None
        self._wheel_targets = (term, vp) if vp is not None else (term,)
        # Qt maps Cmd to ControlModifier on macOS; accept the physical Ctrl
        # (MetaModifier there) too so either key works.
        self._mods = Qt.ControlModifier | (Qt.MetaModifier if sys.platform == "darwin" else Qt.NoModifier)
        saved = self._load()
        if saved:
            self._set(saved, persist=False)

    # -- font access (anytermqt API, else plain QWidget font) --------------

    def _font(self):
        t = self._term
        return t.terminalFont() if hasattr(t, "terminalFont") else t.font()

    def _size(self) -> float:
        f = self._font()
        return f.pointSizeF() if f.pointSizeF() > 0 else float(f.pixelSize() or 10)

    def _set(self, pt: float, persist: bool = True) -> None:
        pt = max(self.MIN_PT, min(self.MAX_PT, pt))
        f = self._font()
        f.setPointSizeF(pt)
        if hasattr(self._term, "setTerminalFont"):
            self._term.setTerminalFont(f)
        else:
            self._term.setFont(f)
        if persist:
            self._save(pt)

    def step(self, n: int) -> None:
        self._set(self._size() + n * self.STEP)

    def reset(self) -> None:
        self._set(self._default)

    # -- persistence ---------------------------------------------------------

    @staticmethod
    def _path():
        return config_dir() / "terminal.json"

    def _load(self) -> Optional[float]:
        try:
            v = json.loads(self._path().read_text(encoding="utf-8")).get("font_pt")
            return float(v) if v else None
        except (OSError, ValueError, TypeError, AttributeError):
            return None

    def _save(self, pt: float) -> None:
        try:
            p = self._path()
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                data = {}
            data["font_pt"] = pt
            p.write_text(json.dumps(data, indent=2), encoding="utf-8")
        except OSError as e:
            log.warning("could not save terminal font size: %s", e)

    # -- events --------------------------------------------------------------

    def eventFilter(self, obj: QObject, e: QEvent) -> bool:
        t = e.type()
        if t == QEvent.KeyPress:
            if not (e.modifiers() & self._mods):
                return False
            fw = QApplication.focusWidget()
            if fw is None or fw.window() is not self._term.window():
                return False
            k = e.key()
            if k in self._UP:
                self.step(1)
            elif k in self._DOWN:
                self.step(-1)
            elif k in self._RESET:
                self.reset()
            else:
                return False
            return True
        if t == QEvent.ShortcutOverride and e.modifiers() & self._mods and \
                e.key() in self._UP + self._DOWN + self._RESET:
            fw = QApplication.focusWidget()
            if fw is not None and fw.window() is self._term.window():
                e.accept()                       # deliver as KeyPress, handled above
                return True
            return False
        if t == QEvent.Wheel and obj in self._wheel_targets and e.modifiers() & self._mods:
            self._wheel_acc += e.angleDelta().y()
            steps = int(self._wheel_acc / 120)   # trackpads send small deltas
            if steps:
                self._wheel_acc -= steps * 120
                self.step(steps)
            return True
        return False


def install_font_zoom(term: QWidget) -> Optional[QObject]:
    """Ctrl/Cmd +/-/0 and Ctrl/Cmd+wheel font sizing for the terminal.
    Returns the handler (keep a reference); no-op without a QApplication."""
    app = QApplication.instance()
    if app is None:
        return None
    zoom = _FontZoom(term)
    # One app-wide filter: keys gated on the terminal's window, wheel gated on
    # the terminal widget/viewport (so Ctrl+wheel over a table does nothing).
    app.installEventFilter(zoom)
    return zoom
