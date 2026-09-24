# Third-party notices

```
terminaltelemetry2 0.1.0
Copyright (C) 2026 Scott Peterman
License: GPL-3.0-or-later

This program is free software: you can redistribute it and/or modify it under the terms of the GNU General Public License as published by the Free Software Foundation, either version 3 of the License, or (at your option) any later version.

This program is distributed in the hope that it will be useful, but WITHOUT ANY WARRANTY; without even the implied warranty of MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the GNU General Public License for more details.

This program uses Qt 6.11.2 through PySide6 6.11.2 (Qt for Python), licensed under the GNU Lesser General Public License version 3 (LGPLv3). Qt is loaded at run time from the PySide6 package's shared libraries; you may replace them with a compatible build of your own (for example with pip) without changing this program. Qt source: https://download.qt.io/official_releases/qt/ -- Qt for Python source: https://download.qt.io/official_releases/QtForPython/

Third-party components:

- Qt 6.11.2 -- LGPL-3.0-only -- GUI toolkit (dynamically loaded via PySide6)
  https://www.qt.io  (source: https://download.qt.io/official_releases/qt/)
  license text: LGPL-3.0.txt, GPL-3.0.txt
- PySide6 / Shiboken6 6.11.2 -- LGPL-3.0-only -- Qt for Python bindings
  https://www.qt.io/qt-for-python  (source: https://download.qt.io/official_releases/QtForPython/)
  license text: LGPL-3.0.txt, GPL-3.0.txt
- paramiko 4.0.0 -- LGPL-2.1 -- SSH client
  https://www.paramiko.org  (source: https://github.com/paramiko/paramiko)
  license text: LGPL-2.1.txt
- TextFSM 2.1.0 -- Apache-2.0 -- template parser
  https://github.com/google/textfsm  (source: https://github.com/google/textfsm)
  license text: Apache-2.0.txt
- ntc-templates bundled -- Apache-2.0 -- TextFSM templates in the bundled template database; test fixtures
  https://github.com/networktocode/ntc-templates  (source: https://github.com/networktocode/ntc-templates)
  license text: ntc-templates-NOTICE.txt, Apache-2.0.txt
- PyYAML 6.0.3 -- MIT -- YAML parsing
  https://pyyaml.org  (source: https://github.com/yaml/pyyaml)
  license text: PyYAML-MIT.txt

License texts: terminaltelemetry2/data/licenses
```

Regenerate: `tt2 --licenses`
