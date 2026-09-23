"""Broker + controller + views against a fake SSH device."""
from conftest import fixture_text, wait_until

from terminaltelemetry2.controller import DeviceController
from terminaltelemetry2.layout import build_layout, parse_layout
from terminaltelemetry2.paths import PKG_DATA
from terminaltelemetry2.ssh.client import SSHClientConfig
from terminaltelemetry2.widgets import load_widgets

WIDGETS, _ = load_widgets([PKG_DATA / "widgets"])
LAYOUT = parse_layout({"layout": "t", "terminal": {"position": "none"},
                       "rows": [["bgp_down", "port_errdisabled"], ["bgp_peers"],
                                ["port_status", "intf_counters", "no_such_widget"]]})


def test_controller_end_to_end(qapp, parser, fake_device):
    dev = fake_device({
        "show ip bgp summary": fixture_text("arista_eos_show_ip_bgp_summary.raw"),
        "show interfaces status": fixture_text("arista_eos_show_interfaces_status.raw"),
    })
    _, placed = build_layout(LAYOUT, WIDGETS, None, "arista_eos")
    views = {d.name: v for d, v in placed}
    assert set(views) == {"bgp_down", "port_errdisabled", "bgp_peers", "port_status"}   # EOS: no counters

    cfg = SSHClientConfig(host="127.0.0.1", port=dev.port, username="u", password="p",
                          paging_disable_command="terminal length 0", shell_timeout=1.0)
    ctrl = DeviceController(cfg, "arista_eos", parser, placed)
    assert sorted(ctrl.commands) == ["show interfaces status", "show ip bgp summary"]
    ctrl.start()
    try:
        assert wait_until(qapp, lambda: views["bgp_peers"].table.rowCount() == 2
                          and views["port_status"].table.rowCount() > 0)
        assert views["bgp_down"].current == 0.0
        assert views["port_errdisabled"].current == 1.0          # fixture has one errdisabled
        # two widgets per command -> one execution each
        assert dev.counts == {"show ip bgp summary": 1, "show interfaces status": 1}

        dev.outputs["show ip bgp summary"] = dev.outputs["show ip bgp summary"].replace(
            "02:59:41 7", "00:00:12 Active")
        ctrl.refresh()
        assert wait_until(qapp, lambda: views["bgp_down"].current == 1.0)

        dev.kill_sessions()
        ctrl.refresh()                                            # notices the dead transport
        assert wait_until(qapp, lambda: "stale" in views["bgp_peers"]._status.text())
        ctrl.refresh()                                            # reconnects
        assert wait_until(qapp, lambda: views["bgp_peers"]._status.text()[:1].isdigit(), 20)
    finally:
        ctrl.stop()


def test_terminal_bridge_with_fallback(qapp, fake_device):
    from terminaltelemetry2.session import TerminalBridge
    from terminaltelemetry2.terminal import FallbackTerminal
    from PySide6.QtCore import QByteArray

    dev = fake_device({"show clock": "12:00:00.000 UTC Mon Sep 21 2026"})
    term = FallbackTerminal()
    br = TerminalBridge(SSHClientConfig(host="127.0.0.1", port=dev.port, username="u", password="p"), term)
    opened = []
    br.opened.connect(lambda: opened.append(1))
    br.open()
    try:
        assert wait_until(qapp, lambda: opened and "rtr1#" in term.toPlainText())
        term.dataReady.emit(QByteArray(b"show clock\r\n"))
        assert wait_until(qapp, lambda: "12:00:00.000" in term.toPlainText())
        br.resize(100, 30)
    finally:
        br.close()


def test_terminal_bridge_with_anytermqt(qapp, fake_device):
    import pytest
    anytermqt = pytest.importorskip("anytermqt")
    from terminaltelemetry2.session import TerminalBridge
    from terminaltelemetry2.terminal import grid_size, load_terminal_widget
    from PySide6.QtCore import QByteArray

    term = load_terminal_widget()
    assert isinstance(term, anytermqt.TerminalWidget)
    dev = fake_device({"show clock": "12:00:00.000 UTC Mon Sep 21 2026"})
    cols, rows = grid_size(term)
    br = TerminalBridge(SSHClientConfig(host="127.0.0.1", port=dev.port, username="u", password="p"),
                        term, cols=cols, rows=rows)
    fed = []
    opened = []
    br.opened.connect(lambda: opened.append(1))
    br.open()
    try:
        assert wait_until(qapp, lambda: opened)
        br._reader.data.connect(lambda b: fed.append(b))
        term.dataReady.emit(QByteArray(b"show clock\r\n"))
        assert wait_until(qapp, lambda: b"12:00:00.000" in b"".join(fed))
        term.resized.emit(100, 30)                    # propagates to resize_pty without error
    finally:
        br.close()
