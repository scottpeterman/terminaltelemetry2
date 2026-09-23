import os
import socket
import threading
import time
from pathlib import Path

import paramiko
import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

FIXTURES = Path(__file__).parent / "fixtures"
PKG_DB = Path(__file__).parent.parent / "terminaltelemetry2" / "data" / "tfsm_templates.db"


def fixture_text(name: str) -> str:
    return (FIXTURES / name).read_text()


class FakeDevice:
    """Minimal network-CLI SSH server: echoes input, prints outputs[cmd], then the prompt."""

    def __init__(self, outputs, prompt="rtr1#"):
        self.outputs = outputs            # command -> str (mutable between polls)
        self.prompt = prompt
        self.counts = {}
        self.transports = []
        self._key = paramiko.RSAKey.generate(2048)
        self._sock = socket.socket()
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind(("127.0.0.1", 0))
        self._sock.listen(8)
        self.port = self._sock.getsockname()[1]
        threading.Thread(target=self._accept, daemon=True).start()

    def _accept(self):
        while True:
            try:
                c, _ = self._sock.accept()
            except OSError:
                return
            threading.Thread(target=self._handle, args=(c,), daemon=True).start()

    def _handle(self, sock):
        dev = self

        class _S(paramiko.ServerInterface):
            def check_auth_password(self, u, p): return paramiko.AUTH_SUCCESSFUL
            def get_allowed_auths(self, u): return "password"
            def check_channel_request(self, kind, cid): return paramiko.OPEN_SUCCEEDED
            def check_channel_pty_request(self, *a): return True
            def check_channel_shell_request(self, c): return True
            def check_channel_window_change_request(self, *a): return True

        t = paramiko.Transport(sock)
        t.add_server_key(self._key)
        t.start_server(server=_S())
        self.transports.append(t)
        ch = t.accept(10)
        if ch is None:
            return
        ch.send(f"banner\r\n{self.prompt}".encode())
        buf = b""
        try:
            while True:
                d = ch.recv(4096)
                if not d:
                    return
                buf += d
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    cmd = line.decode().strip()
                    ch.send(line + b"\r\n")
                    if cmd in self.outputs:
                        self.counts[cmd] = self.counts.get(cmd, 0) + 1
                        ch.send(self.outputs[cmd].replace("\n", "\r\n").encode())
                        if not self.outputs[cmd].endswith("\n"):
                            ch.send(b"\r\n")
                    ch.send(self.prompt.encode())
        except Exception:
            return

    def kill_sessions(self):
        for t in self.transports:
            t.close()

    def close(self):
        self.kill_sessions()
        self._sock.close()


@pytest.fixture
def fake_device():
    devs = []

    def make(outputs, prompt="rtr1#"):
        d = FakeDevice(outputs, prompt)
        devs.append(d)
        return d

    yield make
    for d in devs:
        d.close()


@pytest.fixture(scope="session")
def qapp():
    from PySide6.QtWidgets import QApplication
    return QApplication.instance() or QApplication([])


@pytest.fixture(scope="session")
def parser():
    from terminaltelemetry2.parsing import Parser
    return Parser(PKG_DB, [PKG_DB.parent / "templates"])


def wait_until(qapp, predicate, timeout_s=15.0):
    from PySide6.QtCore import QDeadlineTimer, QEventLoop
    deadline = QDeadlineTimer(int(timeout_s * 1000))
    while not predicate():
        if deadline.hasExpired():
            return False
        qapp.processEvents(QEventLoop.AllEvents, 50)
        time.sleep(0.01)
    return True
