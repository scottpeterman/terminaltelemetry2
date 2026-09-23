"""Native Qt widget views. Closed views (table, stat, kv) and a closed set of
per-column cell renderers (bar, status, spark). Users define bindings and
thresholds, not free rendering."""
from __future__ import annotations

import re
import time
from typing import Any, Dict, List, Optional

from PySide6.QtCore import QPointF, QSize, Qt, Signal
from PySide6.QtGui import QBrush, QColor, QFont, QPainter, QPen, QPolygonF
from PySide6.QtGui import QCursor
from PySide6.QtWidgets import (
    QAbstractItemView, QFormLayout, QFrame, QHBoxLayout, QHeaderView, QLabel, QMenu,
    QStyle, QStyledItemDelegate, QTableWidget, QTableWidgetItem, QToolButton,
    QVBoxLayout, QWidget,
)

from .lab import LabSource
from .pipeline import HIST_KEY, aggregate
from .rules import row_style, to_number
from .schema import CellSpec, WidgetDef

ROW_BG = {
    "alert": QColor(220, 60, 60, 70),
    "warn": QColor(230, 160, 40, 70),
    "ok": QColor(60, 170, 90, 50),
}
MUTED_FG = QColor(140, 140, 140)
STATUS_COLOR = {"ok": "#3aa35a", "stale": "#d09a28", "error": "#d9534f", "muted": "#8a8a8a"}

STYLE_FG = {
    "alert": QColor("#d9534f"), "warn": QColor("#d09a28"),
    "ok": QColor("#3aa35a"), "muted": QColor(150, 150, 150),
}
BAR_BASE = QColor("#4a90d9")
BAR_TRACK = QColor(255, 255, 255, 28)
BAR_BORDER = QColor(255, 255, 255, 50)
SPARK_COLOR = QColor("#5bc0de")
RENDER_ROLE = int(Qt.UserRole) + 1      # per-cell payload the delegate paints (sort still uses UserRole)


def format_value(v: Any) -> str:
    if v is None:
        return "-"
    if isinstance(v, float):
        a = abs(v)
        if v.is_integer() and a < 1000:
            return str(int(v))
        for div, suffix in ((1e9, "G"), (1e6, "M"), (1e3, "k")):
            if a >= div:
                return f"{v / div:.1f}{suffix}"
        return f"{v:.1f}" if a < 100 else f"{v:.0f}"
    return str(v)


def sort_key(v: Any):
    """Numbers numerically; text naturally (Et2 < Et10, Et3/1/1 < Et3/10/1); blanks last."""
    n = to_number(v)
    if n is not None:
        return (0, n, ())
    s = "" if v is None else str(v)
    if not s.strip():
        return (2, 0.0, ())
    return (1, 0.0, tuple((0, int(p), "") if p.isdigit() else (1, 0, p.lower())
                          for p in re.split(r"(\d+)", s) if p))


class _SortItem(QTableWidgetItem):
    """Numeric/natural sorting on the raw value; display text unchanged."""

    def __lt__(self, other: QTableWidgetItem) -> bool:
        return sort_key(self.data(Qt.UserRole)) < sort_key(other.data(Qt.UserRole))


class WidgetFrame(QFrame):
    lab_requested = Signal(object)                  # emits the current LabSource
    monitor_requested = Signal(str, str)            # interface, "tx" | "rx" | "both"

    def __init__(self, title: str, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.setFrameShape(QFrame.StyledPanel)
        self._title = QLabel(title)
        f = self._title.font()
        f.setBold(True)
        self._title.setFont(f)
        self._lab_src: Optional[LabSource] = None
        self._lab_btn = QToolButton()
        self._lab_btn.setText("{ }")                # template lab: parse this capture
        self._lab_btn.setAutoRaise(True)
        self._lab_btn.setCursor(Qt.PointingHandCursor)
        self._lab_btn.setFocusPolicy(Qt.NoFocus)
        self._lab_btn.setToolTip("Open this capture and template in the lab")
        self._lab_btn.setVisible(False)
        self._lab_btn.clicked.connect(self._emit_lab)
        self._status = QLabel("waiting")
        self._status.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        head = QHBoxLayout()
        head.setContentsMargins(6, 4, 6, 2)
        head.addWidget(self._title)
        head.addWidget(self._lab_btn)
        head.addStretch(1)
        head.addWidget(self._status)
        self.body = QVBoxLayout()
        self.body.setContentsMargins(4, 0, 4, 4)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        outer.addLayout(head)
        outer.addLayout(self.body, 1)

    def set_lab_source(self, src: Optional[LabSource]) -> None:
        """Latest capture+template for this widget. Enables the lab button; an
        errored poll tints it red so a failing tile advertises itself."""
        self._lab_src = src
        self._lab_btn.setVisible(src is not None)
        if src is not None:
            failed = bool(src.error)
            self._lab_btn.setStyleSheet(
                f"color: {STATUS_COLOR['error']};" if failed else ""
            )
            tip = "Open this capture and template in the lab"
            self._lab_btn.setToolTip(f"{tip}\nparse failed: {src.error}" if failed else tip)

    def _emit_lab(self) -> None:
        if self._lab_src is not None:
            self.lab_requested.emit(self._lab_src)

    def set_status(self, text: str, kind: str = "ok", tooltip: str = "") -> None:
        self._status.setText(text)
        self._status.setToolTip(tooltip)
        self._status.setStyleSheet(f"color: {STATUS_COLOR.get(kind, STATUS_COLOR['muted'])};")

    def set_updated(self, ts: float, template: Optional[str]) -> None:
        self.set_status(time.strftime("%H:%M:%S", time.localtime(ts)), "ok",
                        f"template: {template or '?'}")

    def mark_stale(self, reason: str) -> None:
        self.set_status(f"stale: {reason}", "stale")

    def set_error(self, message: str) -> None:
        self.set_status("error", "error", message)

    def update_rows(self, rows: List[Dict[str, Any]]) -> None:  # overridden
        raise NotImplementedError


class _CellDelegate(QStyledItemDelegate):
    """Base for painted cells. Delegates (not setCellWidget) so cells follow the
    row under client-side sorting -- a cell widget stays pinned to its screen
    coordinate and would smear across rows when the user clicks a header."""
    min_width = 70

    def _bg(self, painter: QPainter, option, index) -> None:
        if option.state & QStyle.State_Selected:
            painter.fillRect(option.rect, option.palette.highlight())
        else:
            bg = index.data(Qt.BackgroundRole)      # row alert tint set via setBackground
            if bg is not None:
                painter.fillRect(option.rect, bg if isinstance(bg, QBrush) else QBrush(bg))

    def sizeHint(self, option, index) -> QSize:
        s = super().sizeHint(option, index)
        return QSize(max(self.min_width, s.width()), s.height())


class BarDelegate(_CellDelegate):
    """Horizontal fill bar. Payload: (frac|None, text, color)."""
    min_width = 90

    def paint(self, painter, option, index) -> None:
        self._bg(painter, option, index)
        payload = index.data(RENDER_ROLE)
        if not payload:
            return
        frac, text, color = payload
        r = option.rect.adjusted(4, 3, -4, -3)
        painter.save()
        painter.setRenderHint(QPainter.Antialiasing, False)
        painter.fillRect(r, BAR_TRACK)
        if frac is not None:
            w = round(r.width() * max(0.0, min(1.0, frac)))
            painter.fillRect(r.x(), r.y(), w, r.height(), color)
        painter.setPen(BAR_BORDER)
        painter.drawRect(r.adjusted(0, 0, -1, -1))
        painter.setPen(option.palette.text().color())
        painter.drawText(r, Qt.AlignCenter, text)
        painter.restore()


class StatusDelegate(_CellDelegate):
    """Colored dot + label. Payload: (text, color|None)."""

    def paint(self, painter, option, index) -> None:
        self._bg(painter, option, index)
        payload = index.data(RENDER_ROLE)
        if not payload:
            return
        text, color = payload
        r = option.rect
        painter.save()
        painter.setRenderHint(QPainter.Antialiasing, True)
        if color is not None:
            painter.setPen(Qt.NoPen)
            painter.setBrush(color)
            painter.drawEllipse(QPointF(r.x() + 11, r.center().y() + 0.5), 4.0, 4.0)
            tr = r.adjusted(24, 0, -4, 0)
        else:
            tr = r.adjusted(8, 0, -4, 0)
        painter.setPen(option.palette.text().color())
        painter.drawText(tr, int(Qt.AlignVCenter | Qt.AlignLeft), text)
        painter.restore()


class SparkDelegate(_CellDelegate):
    """Sparkline of retained history. Payload: (series, color, min|None, max|None)."""
    min_width = 90

    def paint(self, painter, option, index) -> None:
        self._bg(painter, option, index)
        payload = index.data(RENDER_ROLE)
        if not payload:
            return
        series, color, lo, hi = payload
        if len(series) < 2:
            painter.setPen(STYLE_FG["muted"])
            painter.drawText(option.rect, Qt.AlignCenter, "\u00b7")
            return
        lo = min(series) if lo is None else lo
        hi = max(series) if hi is None else hi
        if hi <= lo:
            hi = lo + 1.0
        span = hi - lo
        r = option.rect.adjusted(5, 4, -5, -4)
        n = len(series)
        poly = QPolygonF()
        for i, val in enumerate(series):
            x = r.x() + r.width() * i / (n - 1)
            y = r.bottom() - (max(lo, min(hi, val)) - lo) / span * r.height()
            poly.append(QPointF(x, y))
        painter.save()
        painter.setRenderHint(QPainter.Antialiasing, True)
        painter.setPen(QPen(color, 1.4))
        painter.setBrush(Qt.NoBrush)
        painter.drawPolyline(poly)
        painter.setPen(Qt.NoPen)
        painter.setBrush(color)
        painter.drawEllipse(poly.at(n - 1), 1.8, 1.8)
        painter.restore()


_DELEGATES = {"bar": BarDelegate, "status": StatusDelegate, "spark": SparkDelegate}


class TableView(WidgetFrame):
    def __init__(self, defn: WidgetDef, parent: Optional[QWidget] = None):
        super().__init__(defn.title, parent)
        self.defn = defn
        v = defn.view
        self.table = QTableWidget(0, len(v.columns))
        self.table.setHorizontalHeaderLabels([v.label_for(c) for c in v.columns])
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.verticalHeader().setVisible(False)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.Interactive)
        self.table.horizontalHeader().setStretchLastSection(True)
        # Explicit: QHeaderView defaults its indicator to descending.
        col = v.columns.index(v.sort) if v.sort in v.columns else 0
        order = Qt.DescendingOrder if v.sort_desc else Qt.AscendingOrder
        self.table.horizontalHeader().setSortIndicator(col, order)
        self.table.setSortingEnabled(True)
        self._delegates: List[QStyledItemDelegate] = []     # keep refs; Qt borrows the pointer
        for i, name in enumerate(v.columns):
            spec = v.cells.get(name)
            if spec is None or spec.kind == "text":
                continue
            d = _DELEGATES[spec.kind](self.table)
            self.table.setItemDelegateForColumn(i, d)
            self._delegates.append(d)
        self.body.addWidget(self.table)
        self._sized = False
        if defn.monitor:
            self._monitor_col = v.columns.index(defn.monitor)
            self.table.setContextMenuPolicy(Qt.CustomContextMenu)
            self.table.customContextMenuRequested.connect(self._context_menu)

    def _context_menu(self, pos) -> None:
        idx = self.table.indexAt(pos)
        if not idx.isValid():
            return
        item = self.table.item(idx.row(), self._monitor_col)
        intf = item.data(Qt.UserRole) if item is not None else None
        if not intf:
            return
        intf = str(intf).strip()          # captured now: a poll may rebuild rows under the menu
        menu = QMenu(self.table)
        sub = menu.addMenu(f"Monitor {intf}")
        for label, which in (("Tx", "tx"), ("Rx", "rx"), ("Tx / Rx", "both")):
            sub.addAction(label).triggered.connect(
                lambda _=False, w=which: self.monitor_requested.emit(intf, w))
        menu.exec(QCursor.pos())

    def _payload(self, spec: CellSpec, col: str, row: Dict[str, Any], val: Any):
        if spec.kind == "bar":
            num = to_number(val)
            frac = None if num is None else (num - spec.min) / (spec.max - spec.min)
            color = QColor(BAR_BASE)
            for at, style in spec.thresholds:          # ascending; last satisfied wins
                if num is not None and num >= at:
                    color = STYLE_FG.get(style, color)
            text = "-" if num is None else f"{format_value(num)}{spec.suffix}"
            return (frac, text, color)
        if spec.kind == "status":
            for rule, label in spec.rules:             # full row available -> can weigh siblings
                if rule.test(row):
                    return (label or format_value(val), STYLE_FG.get(rule.style))
            return (format_value(val), None)
        series = row.get(HIST_KEY, {}).get(col, ())    # spark
        return (tuple(series), SPARK_COLOR, spec.min, spec.max)

    def update_rows(self, rows: List[Dict[str, Any]]) -> None:
        v = self.defn.view
        if v.limit:
            rows = sorted(rows, key=lambda r: sort_key(r.get(v.sort)), reverse=v.sort_desc)[: v.limit]
        t = self.table
        t.setSortingEnabled(False)
        t.setRowCount(len(rows))
        for r, row in enumerate(rows):
            style = row_style(v.alerts, row)
            for c, col in enumerate(v.columns):
                val = row.get(col)
                spec = v.cells.get(col)
                item = _SortItem("" if (spec and spec.kind != "text") else format_value(val))
                item.setData(Qt.UserRole, val)          # sort key, whatever the renderer
                if spec is not None and spec.kind != "text":
                    item.setData(RENDER_ROLE, self._payload(spec, col, row, val))
                if style in ROW_BG:
                    item.setBackground(ROW_BG[style])
                elif style == "muted":
                    item.setForeground(MUTED_FG)
                t.setItem(r, c, item)
        t.setSortingEnabled(True)
        if not self._sized and rows:
            t.resizeColumnsToContents()
            self._sized = True


class StatView(WidgetFrame):
    def __init__(self, defn: WidgetDef, parent: Optional[QWidget] = None):
        super().__init__(defn.title, parent)
        self.defn = defn
        self.value = QLabel("-")
        f = QFont(self.value.font())
        f.setPointSize(f.pointSize() * 3)
        f.setBold(True)
        self.value.setFont(f)
        self.value.setAlignment(Qt.AlignCenter)
        self.caption = QLabel(defn.view.label)
        self.caption.setAlignment(Qt.AlignCenter)
        self.body.addWidget(self.value, 1)
        self.body.addWidget(self.caption)
        self.current: Optional[float] = None

    def update_rows(self, rows: List[Dict[str, Any]]) -> None:
        v = self.defn.view
        self.current = aggregate(rows, v)
        text = "-" if self.current is None else (
            f"{self.current:.0f}" if float(self.current).is_integer() else format_value(self.current))
        self.value.setText(text)
        alarm = (v.alert_above is not None and self.current is not None
                 and self.current > v.alert_above)
        self.value.setStyleSheet("color: #d9534f;" if alarm else "")


class KVView(WidgetFrame):
    """First record as label/value pairs. Empty values are hidden, so one
    definition can span platforms whose templates expose different fields."""

    def __init__(self, defn: WidgetDef, parent: Optional[QWidget] = None):
        super().__init__(defn.title, parent)
        self.defn = defn
        holder = QWidget()
        self.form = QFormLayout(holder)
        self.form.setContentsMargins(6, 2, 6, 2)
        self.form.setLabelAlignment(Qt.AlignRight)
        self.setObjectName(f"kv_{defn.name}")
        self.values: Dict[str, QLabel] = {}
        for name in defn.view.columns:
            val = QLabel("-")
            val.setTextInteractionFlags(Qt.TextSelectableByMouse)
            self.form.addRow(defn.view.label_for(name), val)
            self.values[name] = val
        self.body.addWidget(holder)
        self.body.addStretch(1)

    def update_rows(self, rows: List[Dict[str, Any]]) -> None:
        row = rows[0] if rows else {}
        style = row_style(self.defn.view.alerts, row) if row else None
        for name, label in self.values.items():
            v = row.get(name)
            empty = v is None or str(v).strip() == ""
            self.form.setRowVisible(label, not empty)
            label.setText(format_value(v))
        # objectName selector: a bare QFrame rule would hit every QLabel inside too
        self.setStyleSheet(f"#{self.objectName()} {{ border: 1px solid #d9534f; }}"
                           if style == "alert" else "")


class MissingView(WidgetFrame):
    """Placeholder for a layout slot whose widget definition is missing or invalid."""

    def __init__(self, name: str, reason: str, parent: Optional[QWidget] = None):
        super().__init__(name, parent)
        msg = QLabel(reason)
        msg.setWordWrap(True)
        msg.setAlignment(Qt.AlignCenter)
        self.body.addWidget(msg)
        self.set_status("unavailable", "muted")

    def update_rows(self, rows: List[Dict[str, Any]]) -> None:
        pass


def make_view(defn: WidgetDef, parent: Optional[QWidget] = None) -> WidgetFrame:
    return {"table": TableView, "stat": StatView, "kv": KVView}[defn.view.type](defn, parent)
