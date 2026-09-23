"""
Per-device telemetry controller.

    TelemetryBroker.result (worker thread)
        -> parse on a single background thread, once per (command, template)
        -> Pipeline.apply per widget (GUI thread)
        -> view.update_rows

Widgets that share a command and template share one poll and one parse.
"""
from __future__ import annotations

import logging
import re
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

from PySide6.QtCore import QObject, Signal, Slot

from .parsing import Parsed, Parser
from .parsing.pyparsers import is_py
from .session import SHELL_PRIME, TelemetryBroker
from .ssh.client import SSHClientConfig
from .widgets.lab import LabSource
from .widgets.pipeline import Pipeline
from .widgets.schema import WidgetDef
from .widgets.views import WidgetFrame

log = logging.getLogger(__name__)


@dataclass
class _Binding:
    defn: WidgetDef
    view: WidgetFrame
    pipeline: Pipeline
    template: str
    command: str = ""
    requires: Optional[str] = None     # shell test gating this widget on this platform
    sid: Optional[int] = None          # broker subscription; None = not polling


# ═══════════════════════════════════════════════════════════════════════════
# Capability probe: every distinct `requires:` test in one round trip
# ═══════════════════════════════════════════════════════════════════════════

PROBE_INTERVAL_S = 300.0               # also re-run on every reconnect
_CAP_LINE = re.compile(r"^@tt2cap(\d+)=([01])\s*$", re.M)


def build_probe(tests: List[str]) -> str:
    """One POSIX-sh command reporting @tt2capN=1|0 for each test."""
    return "; ".join(
        f"if {{ {t} ; }} >/dev/null 2>&1; then echo @tt2cap{i}=1; else echo @tt2cap{i}=0; fi"
        for i, t in enumerate(tests)
    )


def parse_probe(output: str, n: int) -> Optional[Dict[int, bool]]:
    """Index -> present, or None if any answer is missing (garbled/partial read)."""
    got = {int(i): v == "1" for i, v in _CAP_LINE.findall(output)}
    return got if all(i in got for i in range(n)) else None


class _ParseRelay(QObject):
    parsed = Signal(str, str, object, float)      # command, template, Parsed, ts


class DeviceController(QObject):
    state = Signal(str, str)                       # relayed broker state
    lab_requested = Signal(object)                 # LabSource, relayed from any widget
    monitor_requested = Signal(str, str)           # interface, "tx" | "rx" | "both"

    def __init__(self, config: SSHClientConfig, platform: str, parser: Parser,
                 placed: List[Tuple[WidgetDef, WidgetFrame]],
                 parent: Optional[QObject] = None):
        super().__init__(parent)
        self.platform = platform
        self.parser = parser
        self.broker = TelemetryBroker(config, self, prime=SHELL_PRIME.get(platform))
        self._by_cmd: Dict[str, List[_Binding]] = {}
        self._gated: Dict[str, List[_Binding]] = {}      # requires test -> bindings
        self._last_output: Dict[str, str] = {}     # last complete capture per command, for the lab
        self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="terminaltelemetry2-parse")
        self._relay = _ParseRelay()
        self._relay.parsed.connect(self._on_parsed)

        for defn, view in placed:
            cmd = defn.command_for(platform)
            if cmd is None:
                continue
            b = _Binding(defn, view, Pipeline(defn), defn.template_for(platform),
                         command=cmd, requires=defn.requires_for(platform))
            self._by_cmd.setdefault(cmd, []).append(b)
            view.lab_requested.connect(self.lab_requested)   # bubble up to the window
            view.monitor_requested.connect(self.monitor_requested)
            if b.requires:
                self._gated.setdefault(b.requires, []).append(b)
                view.set_status("probing", "muted", f"requires: {b.requires}")
            else:
                b.sid = self.broker.subscribe(cmd, defn.interval)

        self._tests = list(self._gated)
        self._probe = build_probe(self._tests) if self._tests else None
        if self._probe:
            self.broker.subscribe(self._probe, PROBE_INTERVAL_S)

        self.broker.result.connect(self._on_result)
        self.broker.error.connect(self._on_error)
        self.broker.state.connect(self._on_state)

    @property
    def commands(self) -> List[str]:
        return list(self._by_cmd)

    def start(self) -> None:
        self.broker.open()

    def stop(self) -> None:
        self.broker.close()
        self._pool.shutdown(wait=True, cancel_futures=True)

    def refresh(self) -> None:
        if self._probe:
            self.broker.poll_now(self._probe)
        for cmd, bindings in self._by_cmd.items():
            if any(b.sid is not None for b in bindings):
                self.broker.poll_now(cmd)

    def _apply_probe(self, output: str) -> None:
        caps = parse_probe(output, len(self._tests))
        if caps is None:
            log.warning("capability probe unreadable; keeping previous gates")
            return
        for i, test in enumerate(self._tests):
            for b in self._gated[test]:
                if caps[i] and b.sid is None:
                    b.sid = self.broker.subscribe(b.command, b.defn.interval)
                    b.view.set_status("waiting", "muted")
                    self.broker.poll_now(b.command)
                elif not caps[i]:
                    if b.sid is not None:
                        self.broker.unsubscribe(b.sid)
                        b.sid = None
                    b.pipeline.reset()
                    b.view.update_rows([])
                    b.view.set_lab_source(None)
                    b.view.set_status("not on this host", "muted", f"requires: {test}")

    # -- broker -> parse -------------------------------------------------------

    @Slot(str, str, bool, float)
    def _on_result(self, command: str, output: str, complete: bool, ts: float) -> None:
        if command == self._probe:
            if complete:
                self._apply_probe(output)
            return
        bindings = self._by_cmd.get(command)
        if not bindings:
            return                                # not a widget poll (e.g. traffic monitor)
        if not complete:
            for b in bindings:
                if b.sid is not None:
                    b.view.mark_stale("read timed out")
            return
        self._last_output[command] = output       # keep the capture the lab will replay
        for template in {b.template for b in bindings}:
            self._pool.submit(self._parse_job, command, template, output, ts)

    def _parse_job(self, command: str, template: str, output: str, ts: float) -> None:
        try:
            parsed = self.parser.parse(self.platform, command, output, template)
        except Exception as e:                    # never kill the parse thread
            parsed = Parsed(error=f"{type(e).__name__}: {e}")
        self._relay.parsed.emit(command, template, parsed, ts)

    # -- parse -> views --------------------------------------------------------

    @Slot(str, str, object, float)
    def _on_parsed(self, command: str, template: str, parsed: Parsed, ts: float) -> None:
        output = self._last_output.get(command, "")
        for b in self._by_cmd.get(command, []):
            if b.template != template or b.sid is None:
                continue
            failed = bool(parsed.error) and not parsed.records
            # The template lab is TextFSM-only; python-parsed widgets don't offer it.
            b.view.set_lab_source(None if is_py(b.template)
                                  else self._lab_source(b, command, parsed, output, failed))
            if failed:
                b.view.set_error(f"{command}: {parsed.error}")
                continue
            try:
                rows = b.pipeline.apply(parsed.records, ts)
                b.view.update_rows(rows)
                b.view.set_updated(ts, parsed.template)
            except Exception as e:
                log.exception("widget %s failed to render", b.defn.name)
                b.view.set_error(f"{type(e).__name__}: {e}")

    def _lab_source(self, b: _Binding, command: str, parsed: Parsed,
                    output: str, failed: bool) -> LabSource:
        # Prefer the template the parser actually resolved; fall back to the
        # widget's explicit choice, then to the <platform>_<command> guess the
        # AUTO path would have tried -- that's the one to fix when a poll fails.
        name = parsed.template or (b.template if b.template != "auto" else "") \
            or self.parser.exact_template(self.platform, command)
        base = b.template if b.template != "auto" else self.parser.exact_template(self.platform, command)
        return LabSource(
            platform=self.platform, command=command, template=name,
            output=output, widget=b.defn.title or b.defn.name,
            error=parsed.error if failed else None, base=base,
        )

    @Slot(str, str)
    def _on_error(self, command: str, message: str) -> None:
        for b in self._by_cmd.get(command, []):
            if b.sid is not None:
                b.view.mark_stale(message)

    @Slot(str, str)
    def _on_state(self, state: str, detail: str) -> None:
        if state == "down":
            for bindings in self._by_cmd.values():
                for b in bindings:
                    b.pipeline.reset()           # a new session restarts counter history
                    if b.sid is not None:
                        b.view.mark_stale("session down")
        elif state == "ready" and self._probe:
            self.broker.poll_now(self._probe)    # host may have changed while we were away
        self.state.emit(state, detail)
