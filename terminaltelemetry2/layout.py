"""
Layout model.

    layout: default
    platforms: []              # empty = any platform; otherwise preferred for these
    terminal:
      position: left           # left | right | top | bottom | none
      size: 0.5                # fraction of the window
    rows:                      # each row is a list of widget names
      - [bgp_down, port_errdisabled]
      - [bgp_peers]
      - [{name: port_status, span: 2}, intf_counters]   # span = relative width
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

import yaml
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QHBoxLayout, QSplitter, QVBoxLayout, QWidget

from .widgets.schema import WidgetDef
from .widgets.views import MissingView, WidgetFrame, make_view

log = logging.getLogger(__name__)

POSITIONS = {"left", "right", "top", "bottom", "none"}


class LayoutError(ValueError):
    pass


@dataclass
class LayoutDef:
    name: str
    platforms: List[str] = field(default_factory=list)
    terminal_position: str = "left"
    terminal_size: float = 0.5
    rows: List[List[Tuple[str, int]]] = field(default_factory=list)
    source: str = "<memory>"


def parse_layout(data: Any, source: str = "<memory>") -> LayoutDef:
    if not isinstance(data, dict) or not data.get("layout"):
        raise LayoutError(f"{source}: 'layout: <name>' is required")
    name = str(data["layout"])
    src = f"{source} [{name}]"
    platforms = data.get("platforms") or []
    if isinstance(platforms, str):
        platforms = [platforms]
    term = data.get("terminal") or {}
    pos = str(term.get("position", "left"))
    if pos not in POSITIONS:
        raise LayoutError(f"{src}: terminal.position must be one of {sorted(POSITIONS)}")
    try:
        size = float(term.get("size", 0.5))
    except (TypeError, ValueError):
        raise LayoutError(f"{src}: terminal.size must be a number")
    if not 0.1 <= size <= 0.9:
        raise LayoutError(f"{src}: terminal.size must be between 0.1 and 0.9")

    rows: List[List[Tuple[str, int]]] = []
    for i, raw in enumerate(data.get("rows") or []):
        if not isinstance(raw, list) or not raw:
            raise LayoutError(f"{src}: rows[{i}] must be a non-empty list")
        row: List[Tuple[str, int]] = []
        for j, item in enumerate(raw):
            if isinstance(item, str):
                row.append((item, 1))
            elif isinstance(item, dict) and item.get("name"):
                try:
                    span = max(1, int(item.get("span", 1)))
                except (TypeError, ValueError):
                    raise LayoutError(f"{src}: rows[{i}][{j}].span must be an integer")
                row.append((str(item["name"]), span))
            else:
                raise LayoutError(f"{src}: rows[{i}][{j}] must be a name or {{name, span}}")
        rows.append(row)
    return LayoutDef(name, [str(p) for p in platforms], pos, size, rows, source)


def load_layouts(dirs: Iterable[Path]) -> Tuple[Dict[str, LayoutDef], List[str]]:
    layouts: Dict[str, LayoutDef] = {}
    errors: List[str] = []
    for d in dirs:
        if not Path(d).is_dir():
            continue
        for p in sorted(Path(d).glob("*.yaml")):
            try:
                with open(p, "r", encoding="utf-8") as fh:
                    lay = parse_layout(yaml.safe_load(fh), str(p))
            except (LayoutError, yaml.YAMLError) as e:
                errors.append(str(e))
                log.warning("%s", e)
                continue
            layouts[lay.name] = lay
    return layouts, errors


def pick_layout(layouts: Dict[str, LayoutDef], platform: str,
                name: Optional[str] = None) -> LayoutDef:
    if name:
        if name not in layouts:
            raise LayoutError(f"layout {name!r} not found (have: {', '.join(sorted(layouts))})")
        return layouts[name]
    for lay in layouts.values():
        if platform in lay.platforms:
            return lay
    if "default" in layouts:
        return layouts["default"]
    for lay in layouts.values():
        if not lay.platforms:
            return lay
    raise LayoutError("no layout available (need one named 'default' or with no platforms)")


def build_layout(
    layout: LayoutDef,
    widgets: Dict[str, WidgetDef],
    terminal: Optional[QWidget],
    platform: str,
) -> Tuple[QWidget, List[Tuple[WidgetDef, WidgetFrame]]]:
    """Returns (root widget, placed (definition, view) pairs)."""
    grid = QWidget()
    vbox = QVBoxLayout(grid)
    vbox.setContentsMargins(0, 0, 0, 0)
    placed: List[Tuple[WidgetDef, WidgetFrame]] = []
    for row in layout.rows:
        hbox = QHBoxLayout()
        for name, span in row:
            defn = widgets.get(name)
            if defn is None:
                view: WidgetFrame = MissingView(name, f"widget {name!r} not found or invalid")
            elif defn.command_for(platform) is None:
                view = MissingView(defn.title, f"no command defined for platform {platform!r}")
            else:
                view = make_view(defn)
                placed.append((defn, view))
            hbox.addWidget(view, span)
        vbox.addLayout(hbox, 1)

    if terminal is None or layout.terminal_position == "none":
        return grid, placed

    pos = layout.terminal_position
    split = QSplitter(Qt.Horizontal if pos in ("left", "right") else Qt.Vertical)
    first, second = (terminal, grid) if pos in ("left", "top") else (grid, terminal)
    split.addWidget(first)
    split.addWidget(second)
    t = int(layout.terminal_size * 1000)
    sizes = [t, 1000 - t] if first is terminal else [1000 - t, t]
    split.setStretchFactor(0, sizes[0])
    split.setStretchFactor(1, sizes[1])
    split.setSizes(sizes)
    return split, placed
