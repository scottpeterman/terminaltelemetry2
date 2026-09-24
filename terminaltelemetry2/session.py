"""
terminal telemetry -- per-device SSH sessions on top of netlapse's SSH client.

Two connections per device:

  TelemetryBroker   persistent prompt-driven shell owned by one worker thread.
                    Widgets subscribe (command, interval); polls are serialized
                    on the single shell, de-duplicated across widgets, and the
                    session reconnects with backoff when it drops.

  TerminalBridge    raw PTY shell wired to an anytermqt widget. Same connect
                    path (two-pass algos, jump host, emulation redirect), no
                    banner drain, no ANSI filtering.

terminaltelemetry2/ssh/{client,emulation,proxy}.py are copied from netlapse/ssh/ so that
importing them does not pull in netlapse.ssh.__init__ -> executor -> storage.
"""
from __future__ import annotations

import itertools
import logging
import queue
import threading
import time
from dataclasses import replace
from typing import Callable, Dict, Optional, Tuple

import shiboken6
from PySide6.QtCore import QByteArray, QObject, QThread, QTimer, Signal, Slot

from .ssh.client import SSHClient, SSHClientConfig

log = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════════════════
# Telemetry connection
# ═══════════════════════════════════════════════════════════════════════════

class TelemetrySSHClient(SSHClient):
    """Netlapse client + single-command execution + read-completion tracking."""

    def __init__(self, config: SSHClientConfig):
        super().__init__(config)
        self.last_read_complete = False

    def _wait_for_prompt(self, timeout: float, sent: Optional[str] = None) -> str:
        # The base method returns partial output on idle/hard timeout with only
        # a log warning. Record whether the read actually ended on the prompt.
        out = super()._wait_for_prompt(timeout, sent)
        prompt = (self._expect_prompt or self._detected_prompt or "").rstrip()
        self.last_read_complete = bool(prompt) and out.rstrip().endswith(prompt)
        return out

    def run_one(self, command: str, timeout: Optional[float] = None) -> Tuple[str, bool]:
        """
        Run exactly one command. Unlike execute_command(): no comma splitting
        (EOS ranges like 'Ethernet1-4,10' survive) and no inter_command_time sleep.
        Returns (output, complete).
        """
        if not self._shell:
            raise RuntimeError("Not connected")
        timeout = timeout or self.config.expect_prompt_timeout / 1000
        self._drain_output()
        self._shell.send(command + "\n")
        raw = self._wait_for_prompt(timeout, sent=command)
        out = self._strip_echo_and_prompt(
            raw, command, self._expect_prompt or self._detected_prompt
        )
        # Devices send CRLF; TextFSM '$' anchors choke on a trailing '\r'.
        return out.replace("\r\n", "\n").replace("\r", ""), self.last_read_complete

    def is_alive(self) -> bool:
        transport = self._client.get_transport() if self._client else None
        return bool(
            transport and transport.is_active()
            and self._shell is not None and not self._shell.closed
        )


# ═══════════════════════════════════════════════════════════════════════════
# Shell normalization (unix-like platforms)
# ═══════════════════════════════════════════════════════════════════════════
#
# A login shell's prompt is whatever the user built: clocks, git branches,
# multi-line PS1, xterm title OSCs from PROMPT_COMMAND, zsh RPROMPT, fish's
# function prompts. Prompt detection can't be made robust against all of that,
# so the telemetry channel doesn't try: it replaces the login shell with a
# plain POSIX sh whose prompt is a fixed sentinel. `exec env` works from
# bash, zsh, fish, tcsh, dash and busybox ash alike; exported variables (e.g.
# $NOMAD_ADDR) survive the exec. The interactive terminal pane is untouched.
#
#   -u PROMPT_COMMAND   bash-as-sh (RHEL) would still run an exported one
#   ENV=                POSIX sh sources $ENV when interactive
#   HISTFILE=/dev/null  bash-as-sh keeps history; polls must not land in it
#   LC_ALL=C            stable number/date formats and ASCII-only markers

SENTINEL = "tt2$"
SHELL_PRIMES: Dict[str, str] = {           # platform pack session.shell -> prime
    "posix": ("exec env -u PROMPT_COMMAND ENV= HISTFILE=/dev/null LC_ALL=C "
              f"PS1='{SENTINEL} ' PS2='' /bin/sh"),
}
SHELL_PRIME: Dict[str, str] = {"linux": SHELL_PRIMES["posix"]}   # platform-keyed (legacy)


def _prime_shell(client: "TelemetrySSHClient", prime: str, timeout: float = 15.0) -> None:
    """Replace the login shell and wait for the sentinel prompt."""
    client._drain_output()
    client._shell.send(prime + "\n")
    buf = ""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if client._shell.recv_ready():
            buf += client._recv_filtered().replace("\r", "")
            if buf.rstrip(" ").endswith(SENTINEL):
                break
        else:
            time.sleep(0.05)
    else:
        tail = " | ".join(ln.strip() for ln in buf.splitlines()[-3:] if ln.strip())
        raise RuntimeError(f"shell prime failed (no {SENTINEL!r} prompt): {tail[-200:]!r}")
    client._drain_output()
    client.set_expect_prompt(SENTINEL)
    client._detected_prompt = SENTINEL
    # Where /bin/sh is bash (RHEL family) line editing is still on, and readline
    # redraws any command longer than the PTY width -- the echo no longer
    # matches and the read idles out. Turn it off; dash/ash just ignore it.
    client.run_one("set +o emacs +o vi 2>/dev/null; stty cols 4096 2>/dev/null", timeout=5)


def open_telemetry_session(config: SSHClientConfig,
                           prime: Optional[str] = None,
                           on_client: Optional[Callable[["TelemetrySSHClient"], None]] = None,
                           ) -> TelemetrySSHClient:
    """Connect and prime a telemetry shell (mirrors executor._attempt_collect).
    With `prime`, the login shell is replaced (see SHELL_PRIME) and prompt
    detection, enable and pagination are skipped -- they don't apply.
    `on_client` receives the client before connecting, so another thread can
    cancel() a connect that would otherwise block for the full timeout."""
    client = TelemetrySSHClient(replace(config))   # connect() mutates config
    if on_client is not None:
        on_client(client)
    try:
        client.connect()
        client._client.get_transport().set_keepalive(30)
        if prime and not client.is_emulated:
            _prime_shell(client, prime)
            return client
        client.set_expect_prompt(client.find_prompt())
        if client.config.enable_command:
            client.send_enable()
            # send_enable re-detects the prompt ('>' -> '#') but does not touch
            # _expect_prompt, which wins in _wait_for_prompt. Re-pin it.
            client.set_expect_prompt(client.detected_prompt)
        client.disable_pagination()
        return client
    except Exception:
        client.disconnect()
        raise


class _SessionDown(Exception):
    """Session unavailable (connecting failed or in backoff). Not per-command."""


# ═══════════════════════════════════════════════════════════════════════════
# Thread shutdown
# ═══════════════════════════════════════════════════════════════════════════
#
# Destroying a QThread that is still running aborts the process ("QThread:
# Destroyed while thread is still running"). Owners cancel their work and wait
# a bounded time; a thread that still hasn't returned (a DNS lookup can't be
# interrupted) is detached here instead -- unparented, kept referenced, and
# deleted once it finishes. main() reaps these before the interpreter exits.

CLOSE_WAIT_MS = 3000
_ORPHANS: "set[QThread]" = set()


def _prune_orphans() -> None:
    for th in list(_ORPHANS):
        if not shiboken6.isValid(th) or not th.isRunning():
            _ORPHANS.discard(th)


def orphan_thread(th: Optional[QThread]) -> None:
    """Detach a still-running QThread from its (about to be destroyed) parent."""
    if th is None or not shiboken6.isValid(th) or not th.isRunning():
        return
    _prune_orphans()
    th.setParent(None)
    th.finished.connect(th.deleteLater)
    _ORPHANS.add(th)


def stop_thread(th: Optional[QThread], wait_ms: int = CLOSE_WAIT_MS) -> bool:
    """Wait for a thread whose work was already cancelled; orphan it if it
    overruns. True if it finished inside the wait."""
    if th is None or not shiboken6.isValid(th):
        return True
    if th.wait(wait_ms):
        return True
    log.warning("thread %s still running after %d ms; detaching", type(th).__name__, wait_ms)
    orphan_thread(th)
    return False


def reap_threads(wait_ms: int = CLOSE_WAIT_MS) -> int:
    """At exit: wait for detached threads; returns how many are still running."""
    _prune_orphans()
    deadline = time.monotonic() + wait_ms / 1000
    for th in list(_ORPHANS):
        th.wait(max(0, int((deadline - time.monotonic()) * 1000)))
    _prune_orphans()
    return len(_ORPHANS)


class TelemetryBroker(QThread):
    """
    One persistent telemetry shell per device.

    GUI thread: subscribe / unsubscribe / poll_now, and the scheduler tick.
    Worker thread (run): owns the SSH client exclusively; executes commands
    one at a time from a queue. A command already queued or running is not
    queued again, so N widgets on the same command cost one execution.

    Signals (delivered queued to GUI-thread receivers):
      result(command, output, complete, epoch_seconds)
      error(command, message)
      state(state, detail)   state in: connecting, ready, down, stopped
    """

    result = Signal(str, str, bool, float)
    error = Signal(str, str)
    state = Signal(str, str)

    TICK_MS = 500
    BACKOFF = (5, 10, 30, 60)

    def __init__(self, config: SSHClientConfig, parent: Optional[QObject] = None,
                 prime: Optional[str] = None):
        super().__init__(parent)
        self._config = config
        self._prime = prime

        # shared between threads
        self._q: "queue.Queue[Optional[str]]" = queue.Queue()
        self._inflight: set = set()
        self._lock = threading.Lock()
        self._stopping = threading.Event()

        # worker thread writes; close() reads to cancel (cancel() is thread-safe)
        self._client: Optional[TelemetrySSHClient] = None
        self._connecting: Optional[TelemetrySSHClient] = None
        self._fails = 0
        self._retry_at = 0.0

        # GUI-thread only
        self._ids = itertools.count(1)
        self._subs: Dict[int, Tuple[str, float]] = {}
        self._due: Dict[str, float] = {}
        self._timer = QTimer(self)
        self._timer.setInterval(self.TICK_MS)
        self._timer.timeout.connect(self._tick)

    # -- GUI-thread API ------------------------------------------------------

    def open(self) -> None:
        self.start()
        self._timer.start()

    def close(self, wait_ms: int = CLOSE_WAIT_MS) -> bool:
        """Stop polling and the worker. Cancels an in-progress connect or read
        so this returns promptly; a worker that still overruns is detached
        (never destroyed while running). True if it stopped inside wait_ms."""
        self._timer.stop()
        self._stopping.set()
        self._q.put(None)
        for c in (self._connecting, self._client):
            if c is not None:
                c.cancel()
        return stop_thread(self, wait_ms)

    def subscribe(self, command: str, interval_s: float) -> int:
        sid = next(self._ids)
        self._subs[sid] = (command, float(interval_s))
        self._due.setdefault(command, 0.0)          # first poll on next tick
        return sid

    def unsubscribe(self, sid: int) -> None:
        command, _ = self._subs.pop(sid, (None, None))
        if command and not any(c == command for c, _ in self._subs.values()):
            self._due.pop(command, None)

    def poll_now(self, command: str) -> None:
        self._enqueue(command)

    @Slot()
    def _tick(self) -> None:
        now = time.monotonic()
        intervals: Dict[str, float] = {}
        for command, iv in self._subs.values():
            intervals[command] = min(iv, intervals.get(command, iv))
        for command, iv in intervals.items():
            if now >= self._due.get(command, 0.0):
                self._due[command] = now + iv
                self._enqueue(command)

    def _enqueue(self, command: str) -> bool:
        with self._lock:
            if command in self._inflight:
                return False
            self._inflight.add(command)
        self._q.put(command)
        return True

    # -- worker thread -------------------------------------------------------

    def run(self) -> None:
        while not self._stopping.is_set():
            try:
                command = self._q.get(timeout=0.5)
            except queue.Empty:
                continue
            if command is None or self._stopping.is_set():
                break
            try:
                self._ensure_session()
                out, complete = self._client.run_one(command)
                self.result.emit(command, out, complete, time.time())
                if not complete:
                    self._resync()
            except _SessionDown:
                pass                                  # state already signalled
            except Exception as e:
                self.error.emit(command, f"{type(e).__name__}: {e}")
                self._drop(f"{type(e).__name__}: {e}")
            finally:
                with self._lock:
                    self._inflight.discard(command)
        self._drop("closing")
        self.state.emit("stopped", "")

    def _ensure_session(self) -> None:
        if self._client is not None:
            if self._client.is_alive():
                return
            self._drop("transport closed")
        if time.monotonic() < self._retry_at:
            raise _SessionDown()
        self.state.emit("connecting", self._config.host)

        def _hold(c):
            self._connecting = c
            if self._stopping.is_set():              # close() ran before we registered
                c.cancel()
        try:
            self._client = open_telemetry_session(self._config, self._prime, on_client=_hold)
        except Exception as e:
            self._connecting = None
            if self._stopping.is_set():
                raise _SessionDown() from e
            delay = self.BACKOFF[min(self._fails, len(self.BACKOFF) - 1)]
            self._fails += 1
            self._retry_at = time.monotonic() + delay
            self.state.emit("down", f"{type(e).__name__}: {e} (retry in {delay}s)")
            raise _SessionDown() from e
        self._connecting = None
        self._fails = 0
        self.state.emit("ready", self._client.detected_prompt or "")

    RESYNC_GRACE = 60.0     # seconds of silence to wait for a slow command's own prompt

    def _resync(self) -> None:
        """After a timed-out read: the command is usually still running. Wait
        for *its* prompt first (discarding the late output) -- probing with
        newlines while it runs finds no prompt, reads as drift, and drops a
        healthy session every poll. Only if it never finishes, probe."""
        try:
            expected = (self._client._expect_prompt or "").strip()
            if expected:
                # 1 s slices so a dead channel or close() ends the wait at once;
                # the grace is idle time -- it restarts whenever output arrives.
                late, deadline = "", time.monotonic() + self.RESYNC_GRACE
                while time.monotonic() < deadline and not self._stopping.is_set():
                    if not self._client.is_alive():
                        self._drop("session closed during a slow command")
                        return
                    chunk = self._client._wait_for_prompt(1.0, sent=None)
                    if chunk:
                        late += chunk
                        deadline = time.monotonic() + self.RESYNC_GRACE
                    if late.rstrip().endswith(expected):
                        log.info("slow command finished late; session kept (%d bytes discarded)",
                                 len(late))
                        return
            seen = (self._client.find_prompt(attempt_count=2, timeout=3.0) or "").strip()
            if expected and seen != expected:
                self._drop(f"prompt drift: expected {expected!r}, saw {seen!r}")
        except Exception as e:
            self._drop(f"resync failed: {type(e).__name__}: {e}")

    def _drop(self, reason: str) -> None:
        if self._client is None:
            return
        try:
            self._client.disconnect()
        except Exception:
            pass
        self._client = None
        self.state.emit("down", reason)


# ═══════════════════════════════════════════════════════════════════════════
# Terminal connection
# ═══════════════════════════════════════════════════════════════════════════

class TerminalSSHClient(SSHClient):
    """Netlapse connect path, raw PTY instead of the collection shell."""

    def _create_shell(self) -> None:
        # Base version sleeps 2s and drains/filters the banner. The terminal
        # wants every byte, escape sequences included, so open the PTY later.
        pass

    def open_pty(self, cols: int, rows: int, term: str = "xterm-256color"):
        transport = self._client.get_transport()
        transport.set_keepalive(30)
        chan = transport.open_session()
        chan.get_pty(term=term, width=cols, height=rows)
        chan.invoke_shell()
        self._shell = chan                     # so disconnect() closes it
        return chan


class _Call(QThread):
    """Run a blocking callable off the GUI thread."""
    done = Signal(object)
    failed = Signal(str)

    def __init__(self, fn: Callable[[], object], parent: Optional[QObject] = None):
        super().__init__(parent)
        self._fn = fn

    def run(self) -> None:
        try:
            self.done.emit(self._fn())
        except Exception as e:
            self.failed.emit(f"{type(e).__name__}: {e}")


class _PtyReader(QThread):
    data = Signal(bytes)

    def __init__(self, chan, parent: Optional[QObject] = None):
        super().__init__(parent)
        self._chan = chan

    def run(self) -> None:
        while True:
            try:
                buf = self._chan.recv(65536)
            except Exception:
                break
            if not buf:
                break
            self.data.emit(buf)


class TerminalBridge(QObject):
    """
    Wires an anytermqt widget to its own SSH connection.

    Widget contract (anytermqt DESIGN.md): feed(bytes) in, dataReady out,
    caller propagates resize. Connect the widget's resize notification to
    resize(cols, rows).
    """

    opened = Signal()
    failed = Signal(str)
    closed = Signal()

    def __init__(self, config: SSHClientConfig, term_widget,
                 cols: int = 120, rows: int = 40, parent: Optional[QObject] = None):
        super().__init__(parent)
        self._client = TerminalSSHClient(replace(config))
        self._term = term_widget
        self._cols, self._rows = cols, rows
        self._chan = None
        self._reader: Optional[_PtyReader] = None
        self._connector: Optional[_Call] = None
        self._wired = False                          # dataReady -> _send connected

    def open(self) -> None:
        def _connect():
            self._client.connect()
            return self._client.open_pty(self._cols, self._rows)

        self._connector = _Call(_connect, self)
        self._connector.done.connect(self._on_connected)
        self._connector.failed.connect(self.failed)
        self._connector.start()

    @Slot(object)
    def _on_connected(self, chan) -> None:
        self._chan = chan
        self._reader = _PtyReader(chan, self)
        self._reader.data.connect(self._feed)
        self._reader.finished.connect(self.closed)
        self._term.dataReady.connect(self._send)
        self._wired = True
        self._reader.start()
        self.opened.emit()

    @Slot(bytes)
    def _feed(self, data: bytes) -> None:
        # Python bytes -> the widget's feed(QByteArray) as a direct call on the GUI thread.
        self._term.feed(QByteArray(data))

    @Slot(object)
    def _send(self, data) -> None:
        if self._chan is not None and not self._chan.closed:
            self._chan.sendall(bytes(data))

    @Slot(int, int)
    def resize(self, cols: int, rows: int) -> None:
        self._cols, self._rows = cols, rows
        if self._chan is not None and not self._chan.closed:
            self._chan.resize_pty(width=cols, height=rows)

    def close(self) -> None:
        """Cancel a connect in progress, close the PTY, and stop both threads
        (bounded; an overrunning thread is detached, never destroyed running)."""
        if self._wired:
            try:
                self._term.dataReady.disconnect(self._send)
            except (RuntimeError, TypeError):
                pass
            self._wired = False
        self._client.cancel()                        # unblocks connect() and recv()
        stop_thread(self._connector)
        stop_thread(self._reader)
        self._client.disconnect()
