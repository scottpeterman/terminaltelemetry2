import pytest
from PySide6.QtWidgets import QApplication

from conftest import PKG_DB
from terminaltelemetry2.parsing import Parser
from terminaltelemetry2.parsing.parser import unsudo
from terminaltelemetry2.paths import PKG_DATA
from terminaltelemetry2.platforms import (
    PackError, Platforms, load_platforms, parse_pack, set_registry, sudo_wrap,
)
from terminaltelemetry2.widgets import load_widgets

DOCKER = "docker ps -a --format '{{json .}}'"
LINKS = "ip -j -s link show; echo @cc; grep -H . /sys/class/net/*/carrier_changes 2>/dev/null"
OVERLAY = ("platform: linux\nmerge: true\nbindings:\n  containers: {sudo: true}\n"
           "  bgp_peers: {sudo: true}\n")


@pytest.fixture(scope="module")
def W():
    w, _ = load_widgets([PKG_DATA / "widgets"])
    return w


@pytest.mark.parametrize("cmd,wrapped", [
    (DOCKER, "sudo -n " + DOCKER),
    ("vtysh -c 'show bgp summary json'", "sudo -n vtysh -c 'show bgp summary json'"),
    (LINKS, "sudo -n sh -c '" + LINKS + "'"),
    ("sudo -n already", "sudo -n already"),
])
def test_sudo_wrap_and_back(cmd, wrapped):
    assert sudo_wrap(cmd) == wrapped
    assert unsudo(sudo_wrap(cmd)) == cmd or cmd.startswith("sudo")


def test_sudo_binding_keeps_widget_parser_and_requires(W):
    reg = Platforms([parse_pack({"platform": "linux", "bindings": {"containers": {"sudo": True}}})])
    w = reg.apply(W, "linux")["containers"]
    assert w.command_for("linux") == "sudo -n " + DOCKER
    assert w.template_for("linux") == "py:docker_ps"
    assert w.requires_for("linux") == "command -v docker"


def test_sudo_must_be_bool():
    with pytest.raises(PackError, match="sudo"):
        parse_pack({"platform": "linux", "bindings": {"containers": {"sudo": "yes"}}})


def test_merge_overlay_keeps_bundled_pack(tmp_path, W):
    (tmp_path / "linux-sudo.yaml").write_text(OVERLAY)
    reg, errors = load_platforms([PKG_DATA / "platforms", tmp_path])
    assert errors == []
    lx = reg.get("linux")
    assert lx.shell == "posix" and lx.counters is not None and lx.tested    # bundled kept
    assert lx.bindings["containers"].sudo and "ubuntu" in lx.aliases
    spec = reg.apply(W, "linux")
    assert spec["bgp_peers"].command_for("linux").startswith("sudo -n vtysh")
    assert spec["version"].command_for("linux") == W["version"].command_for("linux")


def test_merge_order_independent_of_file_names(tmp_path):
    # 'a-...' sorts before 'linux.yaml', but whole packs load before merges
    (tmp_path / "a-sudo.yaml").write_text(OVERLAY)
    (tmp_path / "linux.yaml").write_text("platform: linux\ntitle: Mine\nsession: {shell: posix}\n")
    reg, errors = load_platforms([PKG_DATA / "platforms", tmp_path])
    assert errors == [] and reg.get("linux").title == "Mine"
    assert reg.get("linux").bindings["containers"].sudo


def test_merge_scalar_and_list_rules():
    base = parse_pack({"platform": "x_os", "aliases": ["a"], "tested": True,
                       "session": {"paging": "p"}, "bindings": {"w1": "c1", "w2": "c2"}})
    over = parse_pack({"platform": "x_os", "merge": True, "aliases": ["b"],
                       "session": {"enable": "enable"}, "bindings": {"w2": None}})
    from terminaltelemetry2.platforms import merge_packs
    m = merge_packs(base, over)
    assert m.aliases == ["a", "b"] and m.tested and m.paging == ["p"] and m.enable == "enable"
    assert m.bindings["w1"].command == "c1" and m.bindings["w2"].removed


def test_sudo_does_not_break_template_resolution():
    p = Parser(PKG_DB)
    assert Parser.exact_template("cisco_ios", "sudo -n show version") == \
        Parser.exact_template("cisco_ios", "show version")
    from conftest import fixture_text
    out = fixture_text("arista_eos_show_ip_bgp_summary.raw")
    assert p.parse("arista_eos", "sudo -n show ip bgp summary", out).template == \
        "arista_eos_show_ip_bgp_summary"


# -- Pack editor ------------------------------------------------------------------

@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


def test_editor_sudo_keeps_python_parser(qapp, tmp_path, W):
    from terminaltelemetry2.widgets.pack_editor import PackEditor
    reg, _ = load_platforms([PKG_DATA / "platforms"])
    set_registry(reg)
    try:
        ed = PackEditor(Parser(PKG_DB), W, ["default"], save_dir=tmp_path, platform="linux")
        ed.select_widget("containers")
        assert ed.cmd.text() == DOCKER
        ed.sudo.setChecked(True)
        b = ed.bind_current()
        assert b == {"sudo": True}                  # no command restated -> parser kept
        spec = ed._spec_for("containers", b)
        assert spec.template_for("linux") == "py:docker_ps"
        assert "[sudo]" in ed.wtable.item(
            next(r for r in range(ed.wtable.rowCount())
                 if ed.wtable.item(r, 0).data(0x0100) == "containers"), 1).text()
        path = ed.save(confirm=False)
        reg2, errors = load_platforms([path.parent])
        assert errors == [] and reg2.get("linux").bindings["containers"].sudo
        ed.select_widget("bgp_peers")
        ed.select_widget("containers")              # reload shows the saved state
        assert ed.sudo.isChecked()
        ed._dirty = False
        ed.close()
    finally:
        set_registry(None)
