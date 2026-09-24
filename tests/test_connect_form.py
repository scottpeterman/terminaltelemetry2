import os
import stat

import pytest
import yaml
from PySide6.QtWidgets import QApplication, QDialog

from terminaltelemetry2 import recent


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("TERMINALTELEMETRY2_HOME", str(tmp_path))
    monkeypatch.delenv("TERMINALTELEMETRY2_PASSWORD", raising=False)
    return tmp_path


FULL = {"host": "host1.lab", "port": 22, "user": "speterman", "platform": "linux",
        "password": "hunter2", "key_passphrase": "pp", "jump_password": "jp",
        "key": "~/.ssh/id_ed25519", "jump": "bastion:2222", "layout": "", "legacy_ssh": False}


# -- persistence --------------------------------------------------------------------

def test_never_persists_secrets(home):
    p = recent.remember(FULL)
    text = p.read_text()
    assert "hunter2" not in text and "pp" not in text.split("\n", 1)[1] and "jp" not in text
    assert stat.S_IMODE(os.stat(p).st_mode) == 0o600
    assert "&id" not in text                                   # plain YAML, no anchors
    last = recent.load()["last"]
    assert last == {"host": "host1.lab", "port": 22, "user": "speterman", "platform": "linux",
                    "key": "~/.ssh/id_ed25519", "jump": "bastion:2222"}


def test_recent_mru_dedup_and_cap(home):
    for i in range(20):
        recent.remember({"host": f"h{i}", "user": "u", "platform": "linux"})
    recent.remember({"host": "H5", "user": "u", "platform": "linux"})     # case-insensitive dedupe
    r = recent.load()["recent"]
    assert len(r) == recent.MAX_RECENT and r[0]["host"] == "H5"
    assert sum(1 for x in r if x["host"].lower() == "h5") == 1
    assert recent.find("h19")["host"] == "h19"


def test_corrupt_file_is_empty(home):
    (home / "connections.yaml").write_text("{{{ not yaml")
    assert recent.load() == {"last": {}, "recent": []}


# -- dialog ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


def _dlg(saved):
    from terminaltelemetry2.widgets.connect_dialog import ConnectDialog
    return ConnectDialog([("linux", "Linux host"), ("arista_eos", "Arista EOS")],
                         ["default", "eos"], saved)


def test_prefills_last_and_recent_fill(qapp, home):
    recent.remember({"host": "spine1", "user": "admin", "platform": "arista_eos",
                     "layout": "eos", "enable_command": "enable"})
    recent.remember({"host": "host1", "user": "speterman", "platform": "linux", "port": 2222})
    d = _dlg(recent.load())
    assert (d.host_text(), d.port.value(), d.user.text(), d.platform.currentData()) == \
        ("host1", 2222, "speterman", "linux")
    assert d.password.text() == "" and not d.advanced.isChecked()
    d.host.setCurrentIndex(1)
    d._on_recent(1)                                            # pick spine1 from recents
    v = d.values()
    assert (v["host"], v["platform"], v["layout"], v["enable_command"]) == \
        ("spine1", "arista_eos", "eos", "enable")
    assert d.advanced.isChecked()


def test_validation(qapp, home, tmp_path):
    d = _dlg({"last": {}, "recent": []})
    assert set(d.problems()) >= {"host is required", "user is required",
                                 "enter a password or a key file"}
    d.host.setEditText("r1")
    d.user.setText("u")
    d.key.setText(str(tmp_path / "nope"))
    assert any("key file not found" in p for p in d.problems())
    key = tmp_path / "id"
    key.write_text("k")
    d.key.setText(str(key))
    d.jump.setText("user@@bad:port")
    assert any(p.startswith("jump host") for p in d.problems())
    d.jump.clear()
    assert d.problems() == []
    d.accept()
    assert d.result() == QDialog.Accepted


def test_env_password_satisfies_auth(qapp, home, monkeypatch):
    monkeypatch.setenv("TERMINALTELEMETRY2_PASSWORD", "x")
    d = _dlg({"last": {"host": "r1", "user": "u", "platform": "linux"}, "recent": []})
    assert d.problems() == []


# -- entry point ----------------------------------------------------------------------

def test_bare_tt2_opens_the_form(qapp, home, monkeypatch):
    from terminaltelemetry2 import app
    from terminaltelemetry2.widgets import connect_dialog
    seen = []

    def fake_exec(self):
        seen.append(self)
        return QDialog.Rejected
    monkeypatch.setattr(connect_dialog.ConnectDialog, "exec", fake_exec)
    assert app.parse_args([]).host is None                     # no argparse error any more
    assert app.main([]) == 1 and len(seen) == 1                # cancel -> exit 1


def test_form_result_becomes_window_args(qapp, home, monkeypatch):
    from terminaltelemetry2 import app
    from terminaltelemetry2.widgets import connect_dialog

    def fake_exec(self):
        self.host.setEditText("r1")
        self.user.setText("u")
        self.password.setText("pw")
        self.platform.setCurrentIndex(self.platform.findData("linux"))
        self.advanced.setChecked(True)
        self.enable.setEditText("enable")
        return QDialog.Accepted
    monkeypatch.setattr(connect_dialog.ConnectDialog, "exec", fake_exec)
    base = app.parse_args([])
    args, target = app.run_connect_form(base)
    assert (target.host, target.username, target.password, target.platform) == ("r1", "u", "pw", "linux")
    assert args.enable_command == "enable" and base.enable_command is None   # a copy, not shared
    assert recent.load()["last"]["host"] == "r1" and "pw" not in recent.path().read_text()
