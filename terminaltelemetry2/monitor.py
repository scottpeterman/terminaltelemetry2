"""
Per-interface traffic monitor.

Rates are computed here from octet-counter deltas, not read from the device's
own rate fields: those are load-interval averages (IOS/EOS 5 min by default,
NX-OS 30 s), in vendor-specific units, and far too slow to watch a drain.
bps = delta(octets) * 8 / delta(t), with t the broker's receive timestamp.

One command per monitored interface, polled on the device's existing telemetry
shell (serialized with the widget polls, deduplicated by the broker). Tx and
Rx rows for the same interface share that one poll.

    arista_eos     show interfaces <if>            "N packets input, N bytes"
    cisco_ios      show interfaces <if>            same
    cisco_nxos     show interface <if>             "N input packets  N bytes"
    juniper_junos  show interfaces <if> detail     "Input  bytes  :  N"  (physical block),
                                                   or the "Bundle:" row for an ae unit

Text commands so the NetEmulate mocks answer them too; parse_octets() also
accepts EOS/NX-OS '| json' and Junos '| display xml' if you switch commands.
"""
from __future__ import annotations

import json
import re
import time
import xml.etree.ElementTree as ET
from collections import deque
from dataclasses import dataclass, field
from typing import Deque, Dict, List, Optional, Tuple

from PySide6.QtCore import QPointF, QRectF, Qt, QTimer, Slot
from PySide6.QtGui import QColor, QFont, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QFrame, QHBoxLayout, QLabel, QLineEdit, QPushButton,
    QScrollArea, QSizePolicy, QSpinBox, QToolButton, QVBoxLayout, QWidget,
)

COUNTER_COMMANDS = {
    "arista_eos": "show interfaces {intf}",
    "cisco_ios": "show interfaces {intf}",
    "cisco_nxos": "show interface {intf}",
    "juniper_junos": "show interfaces {intf} detail",
    "linux": "cat /sys/class/net/{intf}/statistics/rx_bytes /sys/class/net/{intf}/statistics/tx_bytes",
}

RX_COLOR = QColor("#3b8fd9")
TX_COLOR = QColor("#e8892b")
SPAN_S = 300                 # chart window
KEEP_S = 600                 # history kept per interface


class CounterError(ValueError):
    pass


# ═══════════════════════════════════════════════════════════════════════════
# Parsing: output -> (rx_octets, tx_octets)
# ═══════════════════════════════════════════════════════════════════════════

def _json_blob(text: str) -> dict:
    i, j = text.find("{"), text.rfind("}")
    if i < 0 or j <= i:
        raise CounterError(_first_line(text) or "no JSON in output")
    try:
        return json.loads(text[i:j + 1])
    except ValueError as e:
        raise CounterError(f"bad JSON: {e}")


def _first_line(text: str) -> str:
    return next((ln.strip() for ln in text.splitlines() if ln.strip()), "")


def _eos_json(d: dict) -> Tuple[int, int]:
    if d.get("errors"):
        raise CounterError("; ".join(map(str, d["errors"])))
    intfs = d.get("interfaces") or {}
    c = next(iter(intfs.values()), {}).get("interfaceCounters") if intfs else None
    if not c or "inOctets" not in c:
        raise CounterError("no interfaceCounters in JSON")
    return int(c["inOctets"]), int(c["outOctets"])


_NX_IN = re.compile(r"in_?bytes$")
_NX_OUT = re.compile(r"out_?bytes$")


def _nxos_json(d: dict) -> Tuple[int, int]:
    row = d["TABLE_interface"]["ROW_interface"]
    if isinstance(row, list):
        row = row[0]
    rx = next((v for k, v in row.items() if _NX_IN.search(k)), None)
    tx = next((v for k, v in row.items() if _NX_OUT.search(k)), None)
    if rx is None or tx is None:
        raise CounterError("no in/out byte counters in ROW_interface")
    return int(rx), int(tx)


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _child(el, name: str):
    return next((c for c in el if _local(c.tag) == name), None)


def _junos_xml(xml: str) -> Tuple[int, int]:
    try:
        root = ET.fromstring(xml)
    except ET.ParseError as e:
        raise CounterError(f"bad XML: {e}")
    # A physical query nests logical units (each with their own stats) inside;
    # take the physical's direct child. A unit query (ae0.0) has only the logical.
    for scope in ("physical-interface", "logical-interface"):
        node = next((e for e in root.iter() if _local(e.tag) == scope), None)
        ts = _child(node, "traffic-statistics") if node is not None else None
        if ts is not None:
            rx, tx = _child(ts, "input-bytes"), _child(ts, "output-bytes")
            if rx is not None and tx is not None:
                return int(rx.text.strip()), int(tx.text.strip())
    msg = next((e for e in root.iter() if _local(e.tag) == "message"), None)
    raise CounterError(msg.text.strip() if msg is not None and msg.text else
                       "no traffic-statistics in XML")


# First match wins, so the physical interface's block (printed before its
# logical units) is the one read on Junos.
_TEXT = [
    # IOS / IOS-XE / EOS:  "  1234 packets input, 567890 bytes"
    (re.compile(r"packets input,\s*(\d+)\s*bytes"), re.compile(r"packets output,\s*(\d+)\s*bytes")),
    # NX-OS:               "  1234 input packets  567890 bytes"
    (re.compile(r"input packets\s+(\d+)\s+bytes"), re.compile(r"output packets\s+(\d+)\s+bytes")),
    # Junos detail:        "  Input  bytes  :   567890   1234 bps"
    (re.compile(r"Input\s+bytes\s*:\s*(\d+)"), re.compile(r"Output\s+bytes\s*:\s*(\d+)")),
    # Junos detail, logical unit on an aggregate (ae0.1001): a Packets/pps/Bytes/bps
    # table whose Bundle: rows are the unit totals (the Link: rows are per member).
    (re.compile(r"Bundle:\s*\n\s*Input\s*:\s*\d+\s+\d+\s+(\d+)"),
     re.compile(r"Bundle:\s*\n\s*Input\s*:.*\n\s*Output\s*:\s*\d+\s+\d+\s+(\d+)")),
]


_JSON_START = re.compile(r'\s*\{\s*"')


def parse_octets(platform: str, text: str) -> Tuple[int, int]:
    """Command output -> (rx_octets, tx_octets). Accepts the default text
    commands, and also EOS/NX-OS '| json' and Junos '| display xml' output if
    COUNTER_COMMANDS is switched to them."""
    if platform == "linux":                      # two sysfs lines: rx_bytes, tx_bytes
        nums = [ln.strip() for ln in text.splitlines() if ln.strip()]
        if len(nums) >= 2 and nums[0].isdigit() and nums[1].isdigit():
            return int(nums[0]), int(nums[1])
        raise CounterError(_first_line(text) or "no sysfs counters")
    i, j = text.find("<rpc-reply"), text.rfind("</rpc-reply>")
    if i >= 0 and j > i:
        return _junos_xml(text[i:j + len("</rpc-reply>")])
    if _JSON_START.match(text):                  # not Junos '{master:0}' banners
        d = _json_blob(text)
        try:
            return _nxos_json(d) if "TABLE_interface" in d else _eos_json(d)
        except (KeyError, TypeError, IndexError, StopIteration) as e:
            raise CounterError(f"unexpected JSON shape: {e!r}")
    # Earliest block wins: a physical ae0 query prints its own "Input bytes"
    # before its units' Bundle tables; a unit query prints only the Bundle table.
    best = None
    for rx_re, tx_re in _TEXT:
        mi, mo = rx_re.search(text), tx_re.search(text)
        if mi and mo and (best is None or mi.start() < best[0].start()):
            best = (mi, mo)
    if best:
        return int(best[0].group(1)), int(best[1].group(1))
    raise CounterError(_first_line(text) or "no byte counters in output")


# ═══════════════════════════════════════════════════════════════════════════
# Rates
# ═══════════════════════════════════════════════════════════════════════════

@dataclass
class RateTracker:
    """Counter samples -> (ts, rx_bps, tx_bps) history."""
    prev: Optional[Tuple[float, int, int]] = None
    points: Deque[Tuple[float, float, float]] = field(default_factory=deque)

    def reset(self) -> None:
        self.prev = None

    def add(self, ts: float, rx: int, tx: int) -> Optional[Tuple[float, float]]:
        prev, self.prev = self.prev, (ts, rx, tx)
        if prev is None:
            return None
        dt = ts - prev[0]
        drx, dtx = rx - prev[1], tx - prev[2]
        if dt <= 0.2 or drx < 0 or dtx < 0:        # clear counters / wrap / reboot: rebaseline
            return None
        r = (drx * 8 / dt, dtx * 8 / dt)
        self.points.append((ts, *r))
        while self.points and self.points[0][0] < ts - KEEP_S:
            self.points.popleft()
        return r


def fmt_bps(v: Optional[float]) -> str:
    if v is None:
        return "-"
    for unit in ("bps", "Kbps", "Mbps", "Gbps"):
        if abs(v) < 1000:
            return f"{v:.3g} {unit}" if unit != "bps" else f"{v:.0f} bps"
        v /= 1000
    return f"{v:.3g} Tbps"


def nice_ceil(v: float) -> float:
    if v <= 0:
        return 1000.0
    mag = 10 ** int(f"{v:e}".split("e")[1])
    for m in (1, 2, 2.5, 5, 10):
        if v <= m * mag:
            return m * mag
    return 10 * mag


# ═══════════════════════════════════════════════════════════════════════════
# Widgets
# ═══════════════════════════════════════════════════════════════════════════

class RateChart(QWidget):
    """Time-based line chart: right edge is now, left edge now - SPAN_S."""

    def __init__(self, tracker: RateTracker, idx: int, color: QColor, parent=None):
        super().__init__(parent)
        self._t, self._idx, self._color = tracker, idx, color
        self.setMinimumHeight(70)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)

    def paintEvent(self, _e) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        r = QRectF(self.rect()).adjusted(1, 1, -1, -1)
        pal = self.palette()
        p.fillRect(r, pal.base())
        grid = QColor(pal.text().color())
        grid.setAlpha(40)
        now = time.time()
        pts = [(pt[0], pt[self._idx]) for pt in self._t.points if pt[0] >= now - SPAN_S]
        top = nice_ceil(max((v for _, v in pts), default=0) * 1.05)
        p.setPen(QPen(grid, 1))
        for k in (0.25, 0.5, 0.75):
            y = r.top() + r.height() * k
            p.drawLine(QPointF(r.left(), y), QPointF(r.right(), y))
        p.drawRect(r)
        if pts:
            def xy(ts, v):
                return QPointF(r.right() - (now - ts) / SPAN_S * r.width(),
                               r.bottom() - (v / top) * r.height())
            path = QPainterPath(xy(*pts[0]))
            for pt in pts[1:]:
                path.lineTo(xy(*pt))
            fill = QPainterPath(path)
            fill.lineTo(QPointF(xy(*pts[-1]).x(), r.bottom()))
            fill.lineTo(QPointF(xy(*pts[0]).x(), r.bottom()))
            fill.closeSubpath()
            c = QColor(self._color)
            c.setAlpha(50)
            p.fillPath(fill, c)
            p.setPen(QPen(self._color, 1.6))
            p.drawPath(path)
            p.setBrush(self._color)
            p.drawEllipse(xy(*pts[-1]), 2.5, 2.5)
        txt = QColor(pal.text().color())
        txt.setAlpha(150)
        p.setPen(txt)
        f = p.font()
        f.setPointSizeF(max(7.0, f.pointSizeF() - 2))
        p.setFont(f)
        p.drawText(r.adjusted(4, 2, -4, -2), Qt.AlignLeft | Qt.AlignTop, fmt_bps(top))
        p.drawText(r.adjusted(4, 2, -4, -2), Qt.AlignLeft | Qt.AlignBottom, f"-{SPAN_S // 60}m")
        p.end()


class MonitorRow(QFrame):
    def __init__(self, intf: str, direction: str, tracker: RateTracker, on_remove, parent=None):
        super().__init__(parent)
        self.intf, self.direction = intf, direction
        self.setFrameShape(QFrame.StyledPanel)
        color = TX_COLOR if direction == "tx" else RX_COLOR
        idx = 2 if direction == "tx" else 1                 # points are (ts, rx, tx)

        name = QLabel(f"<b>{intf}</b><br><span style='color:{color.name()}'>"
                      f"{'Tx' if direction == 'tx' else 'Rx'}</span>")
        name.setFixedWidth(150)
        name.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.value = QLabel("waiting")
        self.value.setFixedWidth(120)
        self.value.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        f = QFont(self.value.font())
        f.setPointSizeF(f.pointSizeF() + 4)
        f.setBold(True)
        self.value.setFont(f)
        self.chart = RateChart(tracker, idx, color)
        rm = QToolButton()
        rm.setText("✕")
        rm.setAutoRaise(True)
        rm.setToolTip("Stop monitoring this row")
        rm.clicked.connect(lambda: on_remove(self))

        h = QHBoxLayout(self)
        h.setContentsMargins(6, 4, 4, 4)
        h.addWidget(name)
        h.addWidget(self.value)
        h.addWidget(self.chart, 1)
        h.addWidget(rm, 0, Qt.AlignTop)

    def show_rate(self, rx: float, tx: float) -> None:
        self.value.setStyleSheet("")
        self.value.setToolTip("")
        self.value.setText(fmt_bps(tx if self.direction == "tx" else rx))
        self.chart.update()

    def show_status(self, text: str, tooltip: str = "", kind: str = "muted") -> None:
        color = {"muted": "#8a8a8a", "error": "#d9534f", "stale": "#d09a28"}[kind]
        self.value.setStyleSheet(f"color: {color};")
        self.value.setText(text)
        self.value.setToolTip(tooltip)


class TrafficMonitor(QWidget):
    """Non-modal child window of a device window: rows of Tx/Rx rate charts.

    Closing it stops every poll it started. Reopening (right-click Monitor)
    starts fresh.
    """

    def __init__(self, broker, platform: str, device_label: str, parent: QWidget,
                 counters=None):
        super().__init__(parent, Qt.Window)
        self.setWindowTitle(f"Traffic - {device_label}")
        self.resize(820, 420)
        self._broker, self._platform = broker, platform
        # counters: platforms.Counters from the pack; None = the builtin table
        self._counters = counters
        self._template = counters.command if counters is not None else COUNTER_COMMANDS[platform]
        self._trackers: Dict[str, RateTracker] = {}
        self._sids: Dict[str, int] = {}
        self._cmd_intf: Dict[str, str] = {}
        self._last_ok: Dict[str, float] = {}
        self._rows: List[MonitorRow] = []

        # -- controls ----------------------------------------------------------
        self.add_edit = QLineEdit()
        self.add_edit.setPlaceholderText("interface")
        self.add_dir = QComboBox()
        self.add_dir.addItems(["Tx / Rx", "Tx", "Rx"])
        add_btn = QPushButton("Add")
        add_btn.clicked.connect(self._add_from_edit)
        self.add_edit.returnPressed.connect(self._add_from_edit)
        self.interval = QSpinBox()
        self.interval.setRange(2, 60)
        self.interval.setValue(5)
        self.interval.setSuffix(" s")
        self.interval.valueChanged.connect(self._resubscribe)
        self.on_top = QCheckBox("Stay on top")
        self.on_top.toggled.connect(self._set_on_top)
        clear = QPushButton("Clear")
        clear.clicked.connect(self.clear)

        bar = QHBoxLayout()
        bar.addWidget(self.add_edit, 1)
        bar.addWidget(self.add_dir)
        bar.addWidget(add_btn)
        bar.addSpacing(16)
        bar.addWidget(QLabel("poll"))
        bar.addWidget(self.interval)
        bar.addWidget(self.on_top)
        bar.addWidget(clear)

        self._rows_box = QVBoxLayout()
        self._rows_box.setSpacing(4)
        self._rows_box.addStretch(1)
        holder = QWidget()
        holder.setLayout(self._rows_box)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(holder)
        self.status = QLabel("Rates from octet-counter deltas, normalized to bps.")
        self.status.setStyleSheet("color: #8a8a8a;")

        lay = QVBoxLayout(self)
        lay.addLayout(bar)
        lay.addWidget(scroll, 1)
        lay.addWidget(self.status)

        broker.result.connect(self._on_result)
        broker.error.connect(self._on_error)
        broker.state.connect(self._on_state)
        self._tick = QTimer(self)                    # scroll charts, flag stale rows
        self._tick.setInterval(1000)
        self._tick.timeout.connect(self._on_tick)
        self._tick.start()

    # -- public ----------------------------------------------------------------

    def add(self, intf: str, which: str) -> None:
        intf = intf.strip()
        if not intf:
            return
        dirs = ["tx", "rx"] if which == "both" else [which]
        for d in dirs:
            if any(r.intf == intf and r.direction == d for r in self._rows):
                continue
            tracker = self._trackers.setdefault(intf, RateTracker())
            row = MonitorRow(intf, d, tracker, self._remove_row)
            row.show_status("waiting")
            self._rows_box.insertWidget(self._rows_box.count() - 1, row)
            self._rows.append(row)
        if intf not in self._sids:
            self._subscribe(intf)
            self._broker.poll_now(self._command(intf))
        self.show()
        self.raise_()
        self.activateWindow()

    def clear(self) -> None:
        for row in list(self._rows):
            self._remove_row(row)

    # -- subscriptions ---------------------------------------------------------

    def _command(self, intf: str) -> str:
        return self._template.format(intf=intf)

    def _subscribe(self, intf: str) -> None:
        cmd = self._command(intf)
        self._cmd_intf[cmd] = intf
        self._sids[intf] = self._broker.subscribe(cmd, float(self.interval.value()))

    def _unsubscribe(self, intf: str) -> None:
        sid = self._sids.pop(intf, None)
        if sid is not None:
            self._broker.unsubscribe(sid)
        self._cmd_intf.pop(self._command(intf), None)
        self._trackers.pop(intf, None)
        self._last_ok.pop(intf, None)

    @Slot(int)
    def _resubscribe(self, _v: int) -> None:
        for intf in list(self._sids):
            self._broker.unsubscribe(self._sids[intf])
            self._sids[intf] = self._broker.subscribe(self._command(intf), float(self.interval.value()))

    def _remove_row(self, row: MonitorRow) -> None:
        self._rows.remove(row)
        self._rows_box.removeWidget(row)
        row.deleteLater()
        if not any(r.intf == row.intf for r in self._rows):
            self._unsubscribe(row.intf)

    def _add_from_edit(self) -> None:
        which = {"Tx / Rx": "both", "Tx": "tx", "Rx": "rx"}[self.add_dir.currentText()]
        self.add(self.add_edit.text(), which)
        self.add_edit.clear()

    def _set_on_top(self, on: bool) -> None:
        self.setWindowFlag(Qt.WindowStaysOnTopHint, on)
        self.show()                                   # changing flags hides the window

    # -- broker ----------------------------------------------------------------

    def _rows_for(self, intf: str) -> List[MonitorRow]:
        return [r for r in self._rows if r.intf == intf]

    @Slot(str, str, bool, float)
    def _on_result(self, command: str, output: str, complete: bool, ts: float) -> None:
        intf = self._cmd_intf.get(command)
        if intf is None:
            return
        tracker = self._trackers[intf]
        if not complete:
            tracker.reset()
            for r in self._rows_for(intf):
                r.show_status("timeout", "read timed out; rebaselining", "stale")
            return
        try:
            if self._counters is not None and self._counters.parser == "regex":
                rx, tx = self._counters.parse(output)
            else:
                rx, tx = parse_octets(self._platform, output)
        except (CounterError, ValueError) as e:
            tracker.reset()
            for r in self._rows_for(intf):
                r.show_status("error", f"{command}\n{e}", "error")
            return
        rate = tracker.add(ts, rx, tx)
        self._last_ok[intf] = ts
        for r in self._rows_for(intf):
            if rate is None:
                r.show_status("baseline")
            else:
                r.show_rate(*rate)

    @Slot(str, str)
    def _on_error(self, command: str, message: str) -> None:
        intf = self._cmd_intf.get(command)
        if intf is not None:
            self._trackers[intf].reset()
            for r in self._rows_for(intf):
                r.show_status("error", message, "error")

    @Slot(str, str)
    def _on_state(self, state: str, detail: str) -> None:
        if state == "down":
            for t in self._trackers.values():
                t.reset()                             # don't average across an outage
            for r in self._rows:
                r.show_status("session down", detail, "stale")

    @Slot()
    def _on_tick(self) -> None:
        if not self.isVisible():
            return
        now = time.time()
        stale_after = 3 * self.interval.value() + 5
        for r in self._rows:
            r.chart.update()
            last = self._last_ok.get(r.intf)
            if last is not None and now - last > stale_after and r.value.text() not in ("stale", "error"):
                r.show_status("stale", f"no sample for {now - last:.0f}s", "stale")

    def closeEvent(self, e) -> None:
        self.clear()                                  # stop every poll this window started
        super().closeEvent(e)
