import pytest

from terminaltelemetry2.layout import LayoutError, load_layouts, parse_layout, pick_layout
from terminaltelemetry2.paths import PKG_DATA
from terminaltelemetry2.widgets import WidgetError, load_widgets, parse_widget


def test_bundled_widgets_and_layouts_load_clean():
    widgets, werr = load_widgets([PKG_DATA / "widgets"])
    layouts, lerr = load_layouts([PKG_DATA / "layouts"])
    assert werr == [] and lerr == []
    assert {"bgp_peers", "bgp_down", "port_status", "port_errdisabled", "intf_counters", "ospf_neighbors", "ospf_down", "lldp_neighbors", "intf_updown", "version", "system", "top_procs"} <= set(widgets)
    for lay in layouts.values():
        for row in lay.rows:
            for name, _ in row:
                assert name in widgets, f"{lay.name} references missing widget {name}"


def _base(**over):
    d = {"widget": "w", "commands": {"arista_eos": "show x"}, "fields": {"a": ["A"], "n": "N"},
         "view": {"type": "table", "columns": ["a"]}}
    d.update(over)
    return d


@pytest.mark.parametrize("over, msg", [
    ({"view": {"type": "table", "columns": ["nope"]}}, "unknown field 'nope'"),
    ({"rates": {"r": "n"}}, "rates/deltas need a key"),
    ({"deltas": {"d": "a"}, "key": "a", "rates": {"d": "a"}}, "collides"),
    ({"view": {"type": "table", "columns": ["a"], "limit": 5}}, "needs view.sort"),
    ({"rates": {"r": "zz"}, "key": "a"}, "source 'zz' is not a field"),
    ({"interval": 1}, "interval must be >="),
    ({"view": {"type": "gauge"}}, "view.type"),
    ({"view": {"type": "table", "alerts": [{"field": "a", "op": "gt"}]}}, "needs a value"),
    ({"view": {"type": "stat", "aggregate": "sum"}}, "needs a known field"),
    ({"commands": {}}, "commands"),
])
def test_widget_validation_errors(over, msg):
    with pytest.raises(WidgetError, match=msg.replace("(", r"\(")):
        parse_widget(_base(**over))


def test_pick_layout_prefers_platform_then_default():
    layouts = {l.name: l for l in (
        parse_layout({"layout": "default", "rows": [["a"]]}),
        parse_layout({"layout": "ios", "platforms": ["cisco_ios"], "rows": [["a"]]}),
    )}
    assert pick_layout(layouts, "cisco_ios").name == "ios"
    assert pick_layout(layouts, "arista_eos").name == "default"
    assert pick_layout(layouts, "cisco_ios", "default").name == "default"
    with pytest.raises(LayoutError):
        pick_layout(layouts, "x", "missing")
