"""Shutdown under a hung connect: the window must close promptly, and no
QThread may be destroyed while running (that aborts the process).

BLACKHOLE is an address whose SYN goes unanswered -- the mistyped-IP case.
SILENT is a TCP listener that accepts but never sends an SSH banner -- a hung
sshd, or a firewall that completes the handshake."""
import socket
import threading
import time

import pytest

from conftest import wait_until
from terminaltelemetry2.session import TelemetryBroker, TerminalBridge, _ORPHANS, reap_threads
from terminaltelemetry2.ssh.client import ConnectCancelled, SSHClient, SSHClientConfig
from terminaltelemetry2.terminal import FallbackTerminal

BLACKHOLE = "10.255.255.1"


def _blackholed() -> bool:
    try:
        socket.create_connection((BLACKHOLE, 22), timeout=0.5).close()
    except socket.timeout:
        return True
    except OSError:
        return False
    return False


needs_blackhole = pytest.mark.skipif(not _blackholed(), reason="no blackholed address here")


@pytest.fixture
def silent():
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(8)
    held = []
    stop = threading.Event()

    def accept():
        srv.settimeout(0.2)
        while not stop.is_set():
            try:
                c, _ = srv.accept()
                held.append(c)
            except OSError:
                pass
    threading.Thread(target=accept, daemon=True).start()
    yield srv.getsockname()[1]
    stop.set()
    for c in held:
        c.close()
    srv.close()


def _cfg(host, port, timeout=30):
    return SSHClientConfig(host=host, port=port, username="u", password="p", timeout=timeout)


def _cancel_after(client, delay):
    t = threading.Timer(delay, client.cancel)
    t.start()
    return t


@needs_blackhole
def test_cancel_interrupts_tcp_connect():
    c = SSHClient(_cfg(BLACKHOLE, 22))
    _cancel_after(c, 0.3)
    t = time.monotonic()
    with pytest.raises(ConnectCancelled):
        c.connect()
    assert time.monotonic() - t < 1.5


def test_cancel_interrupts_ssh_negotiation(silent):
    c = SSHClient(_cfg("127.0.0.1", silent))
    _cancel_after(c, 0.3)
    t = time.monotonic()
    with pytest.raises(ConnectCancelled):
        c.connect()
    assert time.monotonic() - t < 2.0                   # banner_timeout alone is 15s


@needs_blackhole
def test_connect_timeout_not_retried():
    c = SSHClient(_cfg(BLACKHOLE, 22, timeout=1))
    calls = []
    real = c._tcp_connect
    c._tcp_connect = lambda *a: (calls.append(a), real(*a))[1]
    t = time.monotonic()
    with pytest.raises(socket.timeout):
        c.connect()
    assert len(calls) == 1 and time.monotonic() - t < 2.0   # was 2 x timeout


def test_refused_not_retried():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()                                           # nothing listening
    c = SSHClient(_cfg("127.0.0.1", port))
    calls = []
    real = c._tcp_connect
    c._tcp_connect = lambda *a: (calls.append(a), real(*a))[1]
    with pytest.raises(ConnectionRefusedError):
        c.connect()
    assert len(calls) == 1


def _broker_close_during_connect(qapp, host, port):
    b = TelemetryBroker(_cfg(host, port))
    states = []
    b.state.connect(lambda s, d: states.append(s))
    b.subscribe("show version", 30)
    b.open()
    assert wait_until(qapp, lambda: "connecting" in states, 5)
    time.sleep(0.3)                                     # now blocked inside connect
    t = time.monotonic()
    assert b.close() is True
    assert time.monotonic() - t < 2.0
    assert not b.isRunning()


@needs_blackhole
def test_broker_close_during_blackholed_connect(qapp):
    _broker_close_during_connect(qapp, BLACKHOLE, 22)


def test_broker_close_during_silent_negotiation(qapp, silent):
    _broker_close_during_connect(qapp, "127.0.0.1", silent)


def test_bridge_close_during_connect(qapp, silent):
    term = FallbackTerminal()
    br = TerminalBridge(_cfg("127.0.0.1", silent), term)
    br.open()
    time.sleep(0.4)
    assert br._connector.isRunning()
    t = time.monotonic()
    br.close()
    assert time.monotonic() - t < 2.0
    assert not br._connector.isRunning()


def test_overrunning_thread_is_orphaned_not_destroyed(qapp):
    from PySide6.QtCore import QObject, QThread

    release = threading.Event()

    class Stuck(QThread):
        def run(self):
            release.wait(10)

    from terminaltelemetry2.session import stop_thread
    parent = QObject()
    th = Stuck(parent)
    th.start()
    assert stop_thread(th, 100) is False
    assert th.parent() is None and th in _ORPHANS
    parent.deleteLater()                                # would abort if th were still its child
    qapp.processEvents()
    release.set()
    assert reap_threads(3000) == 0
