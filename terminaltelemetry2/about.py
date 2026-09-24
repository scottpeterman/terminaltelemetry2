"""Who made this, what it's built on, and under which licenses.

One inventory drives the About box, the third-party licenses dialog,
`tt2 --licenses` and THIRD_PARTY_NOTICES.md. License texts ship inside the
package (data/licenses/) so pip installs and frozen builds carry them.

Qt is used under the LGPLv3 via PySide6: the notice below says so, points to
the Qt / Qt for Python sources, and notes that Qt is loaded from PySide6's own
shared libraries, which a user can replace with a compatible build.
"""
from __future__ import annotations

import importlib.metadata as md
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

from . import __version__
from .paths import PKG_DATA

APP_NAME = "terminaltelemetry2"
COPYRIGHT = "Copyright (C) 2026 Scott Peterman"
HOMEPAGE = "https://github.com/scottpeterman/terminaltelemetry2"
APP_LICENSE = "GPL-3.0-or-later"
LICENSE_DIR = PKG_DATA / "licenses"

GPL_NOTICE = (
    "This program is free software: you can redistribute it and/or modify it under the "
    "terms of the GNU General Public License as published by the Free Software Foundation, "
    "either version 3 of the License, or (at your option) any later version.\n\n"
    "This program is distributed in the hope that it will be useful, but WITHOUT ANY "
    "WARRANTY; without even the implied warranty of MERCHANTABILITY or FITNESS FOR A "
    "PARTICULAR PURPOSE. See the GNU General Public License for more details.")


@dataclass
class Component:
    name: str
    version: str
    license: str
    use: str
    homepage: str
    source: str
    files: List[str] = field(default_factory=list)      # under LICENSE_DIR

    def texts(self) -> str:
        out = []
        for f in self.files:
            p = LICENSE_DIR / f
            try:
                out.append(f"==== {f} ====\n\n{p.read_text(encoding='utf-8')}")
            except OSError:
                out.append(f"==== {f} ==== (missing from this installation)")
        return "\n\n".join(out)


def _ver(dist: str) -> Optional[str]:
    try:
        return md.version(dist)
    except md.PackageNotFoundError:
        return None


def qt_versions() -> tuple:
    try:
        import PySide6
        from PySide6.QtCore import qVersion
        return qVersion(), PySide6.__version__
    except Exception:
        return "?", "?"


def qt_notice() -> str:
    qt, pyside = qt_versions()
    return (f"This program uses Qt {qt} through PySide6 {pyside} (Qt for Python), licensed "
            "under the GNU Lesser General Public License version 3 (LGPLv3). Qt is loaded "
            "at run time from the PySide6 package's shared libraries; you may replace them "
            "with a compatible build of your own (for example with pip) without changing "
            "this program. Qt source: https://download.qt.io/official_releases/qt/ -- "
            "Qt for Python source: https://download.qt.io/official_releases/QtForPython/")


def components() -> List[Component]:
    qt, pyside = qt_versions()
    out = [
        Component(APP_NAME, __version__, APP_LICENSE, "this program", HOMEPAGE, HOMEPAGE,
                  ["GPL-3.0.txt"]),
        Component("Qt", qt, "LGPL-3.0-only", "GUI toolkit (dynamically loaded via PySide6)",
                  "https://www.qt.io", "https://download.qt.io/official_releases/qt/",
                  ["LGPL-3.0.txt", "GPL-3.0.txt"]),
        Component("PySide6 / Shiboken6", pyside, "LGPL-3.0-only", "Qt for Python bindings",
                  "https://www.qt.io/qt-for-python",
                  "https://download.qt.io/official_releases/QtForPython/",
                  ["LGPL-3.0.txt", "GPL-3.0.txt"]),
        Component("paramiko", _ver("paramiko") or "?", "LGPL-2.1", "SSH client",
                  "https://www.paramiko.org", "https://github.com/paramiko/paramiko",
                  ["LGPL-2.1.txt"]),
        Component("TextFSM", _ver("textfsm") or "?", "Apache-2.0", "template parser",
                  "https://github.com/google/textfsm", "https://github.com/google/textfsm",
                  ["Apache-2.0.txt"]),
        Component("ntc-templates", "bundled", "Apache-2.0",
                  "TextFSM templates in the bundled template database; test fixtures",
                  "https://github.com/networktocode/ntc-templates",
                  "https://github.com/networktocode/ntc-templates",
                  ["ntc-templates-NOTICE.txt", "Apache-2.0.txt"]),
        Component("PyYAML", _ver("PyYAML") or "?", "MIT", "YAML parsing",
                  "https://pyyaml.org", "https://github.com/yaml/pyyaml", ["PyYAML-MIT.txt"]),
    ]
    at = _ver("anytermqt")
    if at:                                   # optional terminal widget
        lic = md.metadata("anytermqt").get("License") or "see the package's metadata"
        out.append(Component("anytermqt", at, lic, "terminal emulator widget (optional)",
                             "https://pypi.org/project/anytermqt/",
                             "https://pypi.org/project/anytermqt/"))
    return out


def notices_text() -> str:
    """Plain-text inventory: `tt2 --licenses` and THIRD_PARTY_NOTICES.md."""
    lines = [f"{APP_NAME} {__version__}", COPYRIGHT, f"License: {APP_LICENSE}", "",
             GPL_NOTICE, "", qt_notice(), "", "Third-party components:", ""]
    for c in components()[1:]:
        lines.append(f"- {c.name} {c.version} -- {c.license} -- {c.use}")
        lines.append(f"  {c.homepage}  (source: {c.source})")
        if c.files:
            lines.append(f"  license text: {', '.join(c.files)}")
    lines += ["", f"License texts: {LICENSE_DIR}"]
    return "\n".join(lines)
