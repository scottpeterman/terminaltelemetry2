"""Config locations. User dirs are searched after package dirs, so a user
widget or layout with the same name overrides the bundled one."""
from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import List

PKG_DATA = Path(__file__).parent / "data"


def config_dir() -> Path:
    d = Path(os.environ.get("TERMINALTELEMETRY2_HOME", Path.home() / ".terminaltelemetry2")).expanduser()
    d.mkdir(parents=True, exist_ok=True)
    return d


def template_db() -> Path:
    """User copy of the template DB, seeded from the bundled one on first run."""
    dst = config_dir() / "tfsm_templates.db"
    if not dst.exists():
        shutil.copy2(PKG_DATA / "tfsm_templates.db", dst)
    return dst


def _user_subdir(name: str) -> Path:
    d = config_dir() / name
    d.mkdir(parents=True, exist_ok=True)
    return d


def template_override_dirs() -> List[Path]:
    """*.textfsm overrides, searched in order: user first, then bundled."""
    return [_user_subdir("templates"), PKG_DATA / "templates"]


def widget_dirs() -> List[Path]:
    return [PKG_DATA / "widgets", _user_subdir("widgets")]


def layout_dirs() -> List[Path]:
    return [PKG_DATA / "layouts", _user_subdir("layouts")]
