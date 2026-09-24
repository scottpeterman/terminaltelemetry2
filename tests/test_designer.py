import shutil

import pytest
import yaml
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication

from conftest import PKG_DB
from terminaltelemetry2 import designer as dz
from terminaltelemetry2 import packbuilder as pb
from terminaltelemetry2.layout import load_layouts
from terminaltelemetry2.parsing import Parser
from terminaltelemetry2.paths import PKG_DATA
from terminaltelemetry2.widgets import load_widgets
from terminaltelemetry2.widgets.schema import parse_widget


@pytest.fixture(scope="module")
def W():
    w, _ = load_widgets([PKG_DATA / "widgets"])
    return w


@pytest.fixture(scope="module")
def parser():
    return Parser(PKG_DB)


@pytest.fixture(scope="module")
def vocab(W):
    return dz.Vocabulary(W)


def _spec(vocab, parser, W, template):
    rec = parser.store.get(template)
    parsed = parser.parse(rec.platform, rec.command, rec.sample, template)
    return dz.suggest_spec(vocab, rec.platform, rec.command, template, rec.content,
                           parsed.records, list(W)), rec


# -- engine ----------------------------------------------------------------------

def test_every_db_template_designs_a_valid_widget(vocab, parser, W):
    """Regression sweep: every platform template that parses its own sample
    yields a widget that validates and renders rows through the pipeline."""
    bad, n = [], 0
    for info in parser.store.list(enabled=True):
        if not info.platform or not info.has_sample:
            continue
        rec = parser.store.get(info.name)
        parsed = parser.parse(info.platform, rec.command, rec.sample, info.name)
        if parsed.error or not parsed.records:
            continue
        n += 1
        spec = dz.suggest_spec(vocab, info.platform, rec.command, info.name, rec.content,
                               parsed.records, list(W))
        pv = dz.preview_spec(parser, spec, rec.sample)
        if pv.error or not pv.rows:
            bad.append((info.name, pv.error))
    assert n > 500 and bad == []


def test_vocabulary_names_and_sibling_aliases(vocab, parser, W):
    spec, _ = _spec(vocab, parser, W, "arista_eos_show_ip_bgp_summary")
    f = {x.name: x for x in spec.fields}
    assert f["peer"].aliases[0] == "BGP_NEIGH" and "NEIGHBOR" in f["peer"].aliases
    assert "asn" in f and "up_down" in f
    # two Values of this template never collapse into one field
    assert "STATE_PFXRCD" not in f["state"].aliases and "state_pfxrcd" in f
    assert spec.key == "peer" and spec.view == "table"


def test_active_is_never_suggested_healthy(vocab, parser, W):
    spec, _ = _spec(vocab, parser, W, "arista_eos_show_ip_bgp_summary")
    u = next(u for u in spec.patterns if u.pattern == "alert_unhealthy" and u.field == "state")
    assert "activ" not in u.param.lower()
    assert dz.healthy_regex(["Active", "Idle"]) != "(?i)^(active)"
    assert dz.healthy_regex(["up", "down", "up"]) == "(?i)^(up)"
    assert dz.healthy_regex(["Estab", "1203"]) == r"(?i)^(estab|\d)"


def test_columns_skip_constant_headers(vocab, parser, W):
    spec, _ = _spec(vocab, parser, W, "arista_eos_show_ip_bgp_summary")
    assert spec.columns[0] == "peer"
    assert not {"router_id", "local_as"} & set(spec.columns)


def test_single_record_is_kv(vocab, parser, W):
    spec, _ = _spec(vocab, parser, W, "cisco_ios_show_version")
    assert spec.view == "kv" and spec.key is None
    assert {"version", "hostname", "uptime"} <= {f.name for f in spec.fields}


def test_kinds(parser):
    recs = [{"LINK_STATUS": "up", "INPUT_ERRORS": "3", "CPU_PCT": "12", "UPTIME": "1d"},
            {"LINK_STATUS": "down", "INPUT_ERRORS": "0", "CPU_PCT": "90", "UPTIME": "2d"}]
    assert dz.infer_kind("LINK_STATUS", recs) == "state"
    assert dz.infer_kind("INPUT_ERRORS", recs) == "counter"
    assert dz.infer_kind("CPU_PCT", recs) == "pct"
    assert dz.infer_kind("UPTIME", recs) == "time"


def test_pattern_yaml(vocab, parser, W):
    spec, rec = _spec(vocab, parser, W, "cisco_ios_show_interfaces")
    spec.patterns += [dz.PatternUse("rate", "in_pkts"), dz.PatternUse("delta", "in_err"),
                      dz.PatternUse("mute_down", "link", dz.DOWN_DEFAULT),
                      dz.PatternUse("status_cell", "proto", "(?i)^up"),
                      dz.PatternUse("drop_empty", "intf"), dz.PatternUse("spark", "in_pkts_ps")]
    spec.columns += ["in_pkts_ps", "in_err_chg"]
    d = spec.to_data()
    assert d["rates"] == {"in_pkts_ps": "in_pkts"} and d["deltas"] == {"in_err_chg": "in_err"}
    assert d["drop"] == [{"field": "intf", "op": "empty"}]
    alerts = d["view"]["alerts"]
    assert alerts[-1]["style"] == "muted"                        # muted listed after alerts
    assert d["view"]["cells"]["proto"]["type"] == "status"
    assert d["view"]["cells"]["in_pkts_ps"]["type"] == "spark"
    assert d["monitor"] == "intf"
    w = parse_widget(yaml.safe_load(spec.to_yaml()))              # the YAML round-trips
    pv = dz.preview_spec(parser, spec, rec.sample)
    assert pv.error is None and pv.rows and "in_pkts_ps" in pv.rows[0]


def test_stat_view(vocab, parser, W):
    spec, rec = _spec(vocab, parser, W, "cisco_ios_show_interfaces")
    spec.view = "stat"
    spec.stat_where = dz.PatternUse("alert_unhealthy", "link", "(?i)^up")
    spec.stat_alert_above = 0
    d = spec.to_data()
    assert d["view"]["where"] == {"field": "link", "op": "not_match", "value": "(?i)^up"}
    assert "alerts" not in d["view"]
    pv = dz.preview_spec(parser, spec, rec.sample)
    assert pv.error is None


def test_add_to_layout(tmp_path):
    out = dz.add_to_layout(PKG_DATA / "layouts" / "default.yaml", "vlans", tmp_path)
    layouts, errors = load_layouts([PKG_DATA / "layouts", tmp_path])
    assert errors == [] and layouts["default"].rows[-1] == [("vlans", 1)]
    again = dz.add_to_layout(out, "vlans", tmp_path)              # idempotent
    layouts, _ = load_layouts([tmp_path])
    assert sum(1 for r in layouts["default"].rows for n, _ in r if n == "vlans") == 1


# -- window ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def wd(qapp, tmp_path, W):
    from terminaltelemetry2.widgets.widget_designer import WidgetDesigner
    db = tmp_path / "t.db"
    shutil.copy2(PKG_DB, db)
    layouts, _ = load_layouts([PKG_DATA / "layouts"])
    d = WidgetDesigner(Parser(db), W, layouts, widget_dir=tmp_path / "widgets",
                       layout_dir=tmp_path / "layouts", platform="arista_eos",
                       template="arista_eos_show_vlan")
    yield d
    d.close()


def test_starts_filled_and_previews(wd):
    assert wd.spec.name == "vlan" and wd.spec.key
    pv = wd.refresh()
    assert pv.error is None and pv.rows
    assert wd._view is not None and "vlan" in wd.yaml_view.toPlainText()


def test_rename_updates_references(wd):
    old = wd.spec.key
    wd.rename_field(old, "vlan_id")
    assert wd.spec.key == "vlan_id" and wd.spec.columns[0] == "vlan_id" and wd.spec.sort == "vlan_id"
    assert wd.refresh().error is None


def test_pattern_toggle_and_view_switch(wd):
    state = next((f for f in wd.spec.fields if f.kind == "state"), None)
    if state:
        wd.set_pattern("mute_down", state.name, True)
        assert "muted" in wd.spec.to_yaml()
    wd.view_type.setCurrentText("stat")
    assert wd.spec.view == "stat" and wd.refresh().error is None
    wd.view_type.setCurrentText("table")


def test_save_and_layout(wd, tmp_path):
    wd.add_layout.setCurrentIndex(wd.add_layout.findData("default"))
    path = wd.save(confirm=False)
    assert path == tmp_path / "widgets" / "vlan.yaml"
    widgets, errors = load_widgets([PKG_DATA / "widgets", tmp_path / "widgets"])
    assert errors == [] and "vlan" in widgets
    layouts, errors = load_layouts([PKG_DATA / "layouts", tmp_path / "layouts"])
    assert errors == [] and ("vlan", 1) in layouts["default"].rows[-1]


def test_refuses_bundled_name(wd, tmp_path):
    wd.spec.name = "bgp_peers"
    assert wd.save(confirm=False) is None
    assert "bundled" in wd.status.text()


def test_designed_widget_binds_on_another_vendor(wd, tmp_path, parser):
    """The designer seeds field aliases from the vocabulary so the Pack
    editor can bind the new widget elsewhere: a VLAN widget designed on EOS
    gets a confident, row-producing suggestion on IOS and NX-OS."""
    wd.save(confirm=False)
    widgets, _ = load_widgets([PKG_DATA / "widgets", tmp_path / "widgets"])
    w = widgets["vlan"]
    for plat in ("cisco_ios", "cisco_nxos"):
        top = next(c for c in pb.suggest_templates(parser.store, plat, w, limit=3) if c.confident)
        assert "vlan" in top.command
        vals = pb.template_values(parser.store.content(top.template))
        b = pb.make_binding(w, plat, top.command, top.template, vals,
                            {f: m.value for f, m in top.mapping.items()})
        from terminaltelemetry2.platforms import Platforms, parse_pack
        spec = Platforms([parse_pack({"platform": plat, "bindings": {"vlan": b}})]).apply(widgets, plat)["vlan"]
        pv = pb.preview(parser, spec, plat, b["command"], b.get("template") or top.template,
                        pb.sample_for(parser.store, top.template, plat, top.command))
        assert pv.rows, (plat, pv.error)
