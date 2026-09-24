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


def test_hp_comware_pack_end_to_end(qapp, parser, fake_device):
    """A pack-only platform through the real session stack: Comware's <sw1>
    prompt, the pack's paging command, bindings resolving ntc templates."""
    from pathlib import Path
    from terminaltelemetry2.layout import load_layouts, pick_layout
    from terminaltelemetry2.platforms import load_platforms

    hpe = Path(__file__).parent / "fixtures" / "hp_comware"
    packs, _ = load_platforms([PKG_DATA / "platforms"])
    pack = packs.get("hp_comware")
    widgets = packs.apply(WIDGETS, "hp_comware")
    dev = fake_device({
        "screen-length disable": "Info: The configuration takes effect on the current user terminal interface only.",
        "display lldp neighbor-information list":
            (hpe / "hp_comware_display_lldp_neighbor-information_list.raw").read_text(),
        "display interface brief": (hpe / "hp_comware_display_interface_brief.raw").read_text(),
        "display interface": (hpe / "hp_comware_display_interface.raw").read_text(),
        "display device manuinfo": (hpe / "hp_comware_display_device_manuinfo.raw").read_text(),
    }, prompt="<sw1>")
    layouts, _ = load_layouts([PKG_DATA / "layouts"])
    _, placed = build_layout(pick_layout(layouts, "hp_comware"), widgets, None, "hp_comware")
    views = {d.name: v for d, v in placed}
    assert {"lldp_neighbors", "port_status", "intf_updown", "version"} <= set(views)

    cfg = SSHClientConfig(host="127.0.0.1", port=dev.port, username="u", password="p",
                          paging_disable_command=pack.paging_config, shell_timeout=1.0)
    ctrl = DeviceController(cfg, "hp_comware", parser, placed, prime=None)
    ctrl.start()
    try:
        assert wait_until(qapp, lambda: views["lldp_neighbors"].table.rowCount() > 0
                          and views["port_status"].table.rowCount() > 0
                          and views["intf_updown"].table.rowCount() > 0), \
            {n: v._status.text() for n, v in views.items()}
        assert dev.counts.get("screen-length disable") == 1
    finally:
        ctrl.stop()


def _session_seen(fake_device, paging, prompt="fw1 #"):
    from terminaltelemetry2.session import open_telemetry_session
    from terminaltelemetry2.ssh.client import PAGINATION_DISABLE_SHOTGUN
    dev = fake_device({"show version": "v1"}, prompt=prompt)
    cfg = SSHClientConfig(host="127.0.0.1", port=dev.port, username="u", password="p",
                          paging_disable_command=paging, shell_timeout=1.0)
    c = open_telemetry_session(cfg)
    try:
        return [x for x in dev.seen], PAGINATION_DISABLE_SHOTGUN
    finally:
        c.disconnect()


def test_paging_sequence_sent_in_order_once(fake_device):
    seq = ["config global", "config system console", "set output standard", "end", "end"]
    seen, shotgun = _session_seen(fake_device, seq)
    assert [x for x in seen if x in seq or x in shotgun] == seq


def test_empty_paging_sends_nothing(fake_device):
    seen, shotgun = _session_seen(fake_device, [])
    assert not any(x in shotgun for x in seen)


def test_null_paging_is_shotgun(fake_device):
    seen, shotgun = _session_seen(fake_device, None)
    assert [x for x in seen if x in shotgun] == list(shotgun)


def test_layout_hides_unbound_widgets(qapp):
    from terminaltelemetry2.layout import load_layouts, parse_layout, pick_layout
    from terminaltelemetry2.platforms import load_platforms
    from terminaltelemetry2.widgets.views import MissingView
    packs, _ = load_platforms([PKG_DATA / "platforms"])
    widgets = packs.apply(WIDGETS, "hp_comware")
    layouts, _ = load_layouts([PKG_DATA / "layouts"])
    lay = pick_layout(layouts, "hp_comware")
    root, placed = build_layout(lay, widgets, None, "hp_comware")
    assert len(placed) == 4 and not root.findChildren(MissingView)
    show = parse_layout({"layout": "x", "unbound": "show", "rows": [["bgp_peers", "version"]]})
    root, placed = build_layout(show, widgets, None, "hp_comware")
    assert len(placed) == 1 and len(root.findChildren(MissingView)) == 1


def test_pack_built_in_editor_drives_a_live_session(qapp, parser, fake_device, tmp_path):
    """Zero code, end to end: accept the editor's suggestions for a platform
    nobody bound by hand, save, and poll a device serving the DB's own
    sample output through the real session stack."""
    from terminaltelemetry2.layout import load_layouts, pick_layout
    from terminaltelemetry2.platforms import load_platforms, set_registry
    from terminaltelemetry2.widgets.pack_editor import PackEditor
    from terminaltelemetry2.parsing import Parser

    bundled, _ = load_platforms([PKG_DATA / "platforms"])
    set_registry(bundled)
    try:
        ed = PackEditor(parser, WIDGETS, ["default"], save_dir=tmp_path, platform="cisco_asa")
        taken = ed.accept_confident()
        path = ed.save(confirm=False)
        ed._dirty = False
        ed.close()
    finally:
        set_registry(None)
    packs, errors = load_platforms([PKG_DATA / "platforms", tmp_path])
    assert errors == [] and path.exists()
    pack = packs.get("cisco_asa")
    widgets = packs.apply(WIDGETS, "cisco_asa")

    outputs = {pack.paging_config: ""}
    for name in taken:
        w = widgets[name]
        t = w.template_for("cisco_asa")
        t = t if t != "auto" else Parser.exact_template("cisco_asa", w.command_for("cisco_asa"))
        outputs[w.command_for("cisco_asa")] = parser.store.get(t).sample
    dev = fake_device(outputs, prompt="asa1#")
    layouts, _ = load_layouts([PKG_DATA / "layouts"])
    _, placed = build_layout(pick_layout(layouts, "cisco_asa"), widgets, None, "cisco_asa")
    views = {d.name: v for d, v in placed}
    shown = [n for n in taken if n in views]            # the default layout doesn't place every widget
    assert len(shown) >= 5 and set(views) == set(shown)  # and nothing unbound leaks in

    cfg = SSHClientConfig(host="127.0.0.1", port=dev.port, username="u", password="p",
                          paging_disable_command=pack.paging_config, shell_timeout=1.0)
    ctrl = DeviceController(cfg, "cisco_asa", parser, placed, prime=None)
    ctrl.start()
    tables = [n for n in shown if hasattr(views[n], "table")]
    try:
        assert wait_until(qapp, lambda: all(views[n].table.rowCount() > 0 for n in tables), 30), \
            {n: views[n]._status.text() for n in shown}
        assert dev.counts.get("terminal pager 0") == 1
    finally:
        ctrl.stop()


def test_python_widget_failure_is_visible_and_inspectable(qapp, parser, fake_device):
    """Regression: a python-parsed widget whose command fails showed only an
    'error' status (cause in a tooltip) and no { } button."""
    from terminaltelemetry2.widgets.schema import parse_widget
    from terminaltelemetry2.widgets.views import make_view
    cmd = "docker ps -a --format '{{json .}}'"
    dev = fake_device({cmd: "permission denied while trying to connect to the Docker daemon "
                            "socket at unix:///var/run/docker.sock: connect: permission denied"},
                      prompt="tt2$")
    w = WIDGETS["containers"]
    w = parse_widget({"widget": w.name, "title": w.title, "commands": {"linux": cmd},
                      "templates": {"linux": "py:docker_ps"}, "key": "name",
                      "fields": dict(w.fields), "view": {"type": "table"}})
    view = make_view(w)
    cfg = SSHClientConfig(host="127.0.0.1", port=dev.port, username="u", password="p",
                          paging_disable_command=[], shell_timeout=1.0)
    ctrl = DeviceController(cfg, "linux", parser, [(w, view)], prime=None)
    got = []
    ctrl.lab_requested.connect(got.append)
    ctrl.start()
    try:
        assert wait_until(qapp, lambda: "docker group" in view.error_text, 20), view.error_text
        assert view._lab_btn.isVisibleTo(view)
        view._lab_btn.click()
        assert got and got[0].template == "py:docker_ps" and "permission denied" in got[0].output
    finally:
        ctrl.stop()


def test_sudo_binding_over_a_live_session(qapp, parser, fake_device):
    """The device receives `sudo -n <cmd>` and the python parser still parses it."""
    from terminaltelemetry2.platforms import Platforms, parse_pack
    from terminaltelemetry2.widgets.views import make_view
    cmd = "sudo -n docker ps -a --format '{{json .}}'"
    dev = fake_device({cmd: '{"ID":"a1","Names":"web","Image":"nginx","State":"running",'
                            '"Status":"Up","Ports":""}'}, prompt="tt2$")
    reg = Platforms([parse_pack({"platform": "linux", "bindings": {"containers": {"sudo": True}}})])
    w = reg.apply(WIDGETS, "linux")["containers"]
    w = __import__("dataclasses").replace(w, requires={})     # the fake device has no shell
    view = make_view(w)
    cfg = SSHClientConfig(host="127.0.0.1", port=dev.port, username="u", password="p",
                          paging_disable_command=[], shell_timeout=1.0)
    ctrl = DeviceController(cfg, "linux", parser, [(w, view)], prime=None)
    ctrl.start()
    try:
        assert wait_until(qapp, lambda: view.table.rowCount() == 1, 20), view.error_text
        assert dev.counts.get(cmd) >= 1
    finally:
        ctrl.stop()


def test_slow_command_does_not_drop_the_session(qapp, parser, fake_device):
    """Regression: a command silent longer than the read timeout made the
    resync probe find no prompt -> 'prompt drift' -> session dropped every
    poll, so every tile oscillated to 'stale: session down'."""
    from terminaltelemetry2.widgets.schema import parse_widget
    from terminaltelemetry2.widgets.views import make_view
    slow, fast = "docker ps", "show clock"
    dev = fake_device({slow: '{"Names":"web","Image":"nginx","State":"running"}',
                       fast: "12:00:00"}, prompt="tt2$", delays={slow: 4.0})
    ws = parse_widget({"widget": "slow", "commands": {"linux": slow}, "interval": 5,
                       "templates": {"linux": "py:docker_ps"}, "key": "name",
                       "fields": {"name": ["NAME"]}, "view": {"type": "table"}})
    wf = parse_widget({"widget": "fast", "commands": {"linux": fast}, "interval": 5,
                       "templates": {"linux": "py:linux_version"},
                       "fields": {"x": ["HOST"]}, "view": {"type": "kv"}})
    vs, vf = make_view(ws), make_view(wf)
    cfg = SSHClientConfig(host="127.0.0.1", port=dev.port, username="u", password="p",
                          paging_disable_command=[], shell_timeout=1.0,
                          expect_prompt_timeout=1000)             # 1 s: the 4 s command overruns
    ctrl = DeviceController(cfg, "linux", parser, [(ws, vs), (wf, vf)], prime=None)
    states = []
    ctrl.broker.state.connect(lambda s, d: states.append((s, d)))
    ctrl.start()
    try:
        assert wait_until(qapp, lambda: "read timed out" in vs.error_text, 20), vs.error_text
        assert wait_until(qapp, lambda: dev.counts.get(slow, 0) >= 2, 25)   # polled again, same session
        assert [s for s, _ in states if s == "down"] == [], states
        assert sum(1 for s, _ in states if s == "connecting") == 1
        assert "read_timeout" in vs.error_text
    finally:
        ctrl.stop()
