"""
Template resolution for widget polls. Order, first hit wins:

  1. explicit  widget names a template for this platform -> that template's
               family only: the name itself, then its instance-numbered
               siblings (<name>2, <name>3, ... newest first) from the override
               dirs and the DB. The winner is pinned. Siblings are how the lab
               saves a fix for gear the base doesn't parse without breaking the
               gear it does.
  2. pinned    template that worked last time for (platform, command)
  3. exact     template named <platform>_<command> (e.g. arista_eos_show_ip_bgp_summary)
  4. scored    netlapse ParseEngine sweep over the platform+command filter
  5. vendor    vendor-only sweep -- OFF by default (see below)

Steps 3-5 pin their winner. A pinned template that returns zero records is
unpinned and resolution restarts on that poll.

Template content comes from override files first (*.textfsm named by
cli_command, user dir then bundled dir), then the SQLite DB. Overrides are how
a template gets fixed without touching the DB.

Vendor-only fallback is off for widgets: the command is known, and a vendor
sweep "succeeds" with the wrong template (a failed EOS BGP summary parse scores
arista_eos_show_ip_interface_brief instead, neighbors mapped as interfaces).
A wrong table is worse than an error. Enable it for ad-hoc parsing only.
"""
from __future__ import annotations

import io
import re
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

import textfsm

from . import pyparsers
from .engine import ParseEngine
from .store import TemplateStore


def unsudo(command: str) -> str:
    """Inverse of platforms.sudo_wrap: the command a template is named for."""
    import shlex
    c = command.strip()
    if c.startswith("sudo -n sh -c "):
        try:
            return shlex.split(c[len("sudo -n sh -c "):])[0]
        except (ValueError, IndexError):
            return c
    for pre in ("sudo -n ", "sudo "):
        if c.startswith(pre):
            return c[len(pre):]
    return c

AUTO = "auto"


@dataclass
class Parsed:
    records: List[Dict] = field(default_factory=list)
    template: Optional[str] = None
    score: Optional[float] = None
    method: str = "none"          # explicit | pinned | exact | scored | vendor | none
    error: Optional[str] = None


class Parser:
    def __init__(self, db_path: Path, override_dirs: Iterable[Path] = (),
                 min_score: Optional[float] = None, vendor_fallback: bool = False):
        kwargs = {} if min_score is None else {"min_score": min_score}
        self.db_path = str(db_path)
        self.override_dirs = [Path(d) for d in override_dirs]
        self.vendor_fallback = vendor_fallback
        self.store = TemplateStore(self.db_path)       # migrates a v1 DB before the engine reads it
        self._engine = ParseEngine(db_path=self.db_path, **kwargs)
        # Keyed by what the widget asked for as well as platform + command: two
        # widgets can share a command's poll yet parse it with different
        # templates (Interface Up/Down vs IP Addresses on EOS 'show interfaces').
        self._pins: Dict[Tuple[str, str, str], Tuple[str, str]] = {}
        self._content: Dict[Tuple[str, bool], Optional[str]] = {}
        self._families: Dict[str, List[str]] = {}
        self._lock = threading.Lock()

    @property
    def template_count(self) -> int:
        return self._engine.template_count

    def pinned(self, platform: str, command: str, template: Optional[str] = None) -> Optional[str]:
        """The template currently pinned for this platform + command, for the
        given request (an explicit template name, or AUTO). With no request
        given, an explicit pin for any family is not returned -- only AUTO's."""
        req = template or AUTO
        with self._lock:
            pin = self._pins.get((platform, command, req)) or self._pins.get((platform, command, AUTO))
        return pin[0] if pin else None

    def reload_overrides(self) -> None:
        with self._lock:
            self._content.clear()
            self._pins.clear()
            self._families.clear()

    # -- lab support (public views onto resolution internals) ------------------

    def template_content(self, name: str) -> Optional[str]:
        """The .textfsm text a template name resolves to (override dir then DB),
        or None, disabled rows included. Public entry point for the lab/manager."""
        return self._template_content(name, enabled_only=False)

    def clean_output(self, raw: str) -> str:
        """Strip session preamble/prompts exactly as a widget poll would, so the
        lab parses the same text the widget did."""
        return ParseEngine._clean_output(raw)

    @staticmethod
    def exact_template(platform: str, command: str) -> str:
        """The <platform>_<command> template name a widget resolves to under
        AUTO -- the one to preload in the lab when a poll fails. A sudo wrapper
        (platforms.sudo_wrap) is ignored: templates are named for the command."""
        return ParseEngine._build_filter(platform, unsudo(command))

    # -- DB write-back (the lab saves fixes here, never to a flat file) --------

    def next_sibling_name(self, name: str) -> str:
        """Next instance-numbered sibling of a template family: the base name
        with trailing digits stripped, plus one past the highest existing
        suffix. `..._top_once` -> `..._top_once2`; `..._top_once2` -> `...3`.
        The scored sweep matches the whole family off the base's term filter,
        so a sibling competes with the base instead of overwriting it."""
        return self.store.next_sibling(name)

    def family(self, base: str) -> List[str]:
        """`base` then its numbered siblings (`<base>N`), highest N first, from
        the override dirs and the DB. Exact-prefix, so a base that itself ends
        in a digit (..._vpnv4) doesn't lose it."""
        with self._lock:
            hit = self._families.get(base)
        if hit is not None:
            return list(hit)
        pat = re.compile(re.escape(base) + r"(\d+)$")
        found: Dict[str, int] = {}
        for d in self.override_dirs:
            if d.is_dir():
                for p in d.glob(f"{base}*.textfsm"):
                    m = pat.match(p.stem)
                    if m:
                        found[p.stem] = int(m.group(1))
        for cmd in self.store.names_glob(base + "*"):          # enabled only
            m = pat.match(cmd)
            if m:
                found[cmd] = int(m.group(1))
        fam = [base] + sorted(found, key=found.get, reverse=True)
        with self._lock:
            self._families[base] = fam
        return list(fam)

    def next_in_family(self, base: str) -> str:
        pat = re.compile(re.escape(base) + r"(\d+)$")
        nums = [int(m.group(1)) for n in self.family(base) if (m := pat.match(n))]
        return f"{base}{max(nums, default=1) + 1}"

    def template_origin(self, name: str) -> str:
        """'override', 'db:<source>', or 'missing' -- for the lab's picker."""
        for d in self.override_dirs:
            if (d / f"{name}.textfsm").is_file():
                return "override"
        src = self.store.source_of(name)
        return f"db:{src.lower()}" if src is not None else "missing"

    def delete_template_from_db(self, cli_command: str, base: str) -> None:
        """Remove a custom sibling. Refuses the family base and any non-custom
        row -- those are what other gear still parses with."""
        if cli_command == base or not re.fullmatch(re.escape(base) + r"\d+", cli_command):
            raise ValueError(f"{cli_command!r} is not a numbered sibling of {base!r}")
        src = self.store.source_of(cli_command)
        if src is None:
            raise ValueError(f"{cli_command!r} is not in the DB")
        if src.lower() != "custom":
            raise ValueError(f"{cli_command!r} is a {src} template; not deleting it")
        self.store.delete(cli_command)
        self.reload_overrides()

    def save_template_to_db(self, cli_command: str, textfsm_content: str,
                            source: str = "custom", sample: str = "",
                            allow_overwrite: bool = False) -> None:
        """Write a template into the DB the parser and the scored sweep read.
        Refuses to overwrite a non-custom (e.g. ntc) row unless allow_overwrite
        -- clobbering a vendor base can break the gear it still works for; save
        a sibling instead. Clears pins so the next poll re-resolves the family."""
        self.store.save(cli_command, textfsm_content, source=source,
                        sample=sample or None, allow_overwrite=allow_overwrite)
        self.reload_overrides()

    # -- resolution ------------------------------------------------------------

    def parse(self, platform: str, command: str, output: str,
              template: Optional[str] = None) -> Parsed:
        # Python parsers own their empty-output semantics: "no failed units" and
        # "no containers" are valid empty answers, not parse failures.
        if pyparsers.is_py(template):
            recs = pyparsers.get(template)(output or "")   # run_one already stripped echo/prompt
            return Parsed(recs, template, None, "python")
        if not output or not output.strip():
            return Parsed(error="empty output")

        command = unsudo(command)            # resolve by the command, not its sudo wrapper
        key = (platform, command, template if template and template != AUTO else AUTO)
        if template and template != AUTO:
            order = self.family(template)
            with self._lock:
                pin = self._pins.get(key)
            if pin and pin[0] in order:              # last winner first
                order.remove(pin[0])
                order.insert(0, pin[0])
            base_err: Optional[str] = None
            for name in order:
                try:
                    recs = self._run_named(name, output)
                except Exception as e:
                    if name == template:
                        base_err = f"{type(e).__name__}: {e}"
                    continue
                if recs:
                    self._pin(key, name, "explicit")
                    return Parsed(recs, name, None, "explicit")
            with self._lock:
                self._pins.pop(key, None)
            n = len(order) - 1
            tried = f" (and {n} sibling{'' if n == 1 else 's'})" if n else ""
            return Parsed(template=template, method="explicit",
                          error=base_err or f"explicit template{tried} produced no records")

        with self._lock:
            pin = self._pins.get(key)
        if pin:
            recs = self._try_named(pin[0], output)
            if recs:
                return Parsed(recs, pin[0], None, "pinned")
            with self._lock:
                self._pins.pop(key, None)

        exact = ParseEngine._build_filter(platform, command)
        recs = self._try_named(exact, output)
        if recs:
            self._pin(key, exact, "exact")
            return Parsed(recs, exact, None, "exact")

        r = self._engine.parse(output, platform, command)
        method = "scored"
        if not r.success and self.vendor_fallback:
            r = self._engine.parse_vendor_fallback(output, platform)
            method = "vendor"
        if r.success:
            self._pin(key, r.template, method)
            return Parsed(r.records, r.template, r.score, method)
        return Parsed(template=r.template, score=r.score, method="none",
                      error=r.error or f"no template for {exact!r} parsed this output")

    def _pin(self, key: Tuple[str, str, str], name: str, method: str) -> None:
        with self._lock:
            self._pins[key] = (name, method)

    # -- template execution ----------------------------------------------------

    def _try_named(self, name: str, output: str) -> List[Dict]:
        try:
            return self._run_named(name, output)
        except Exception:
            return []

    def _run_named(self, name: str, output: str) -> List[Dict]:
        content = self._template_content(name)
        if content is None:
            raise KeyError(f"template {name!r} not found in overrides or {self.db_path}")
        fsm = textfsm.TextFSM(io.StringIO(content))
        rows = fsm.ParseText(ParseEngine._clean_output(output))
        return [dict(zip(fsm.header, row)) for row in rows]

    def _template_content(self, name: str, enabled_only: bool = True) -> Optional[str]:
        """Override file first (always live), then the DB. Resolution passes
        enabled_only=True so a disabled row never parses."""
        key = (name, enabled_only)
        with self._lock:
            if key in self._content:
                return self._content[key]
        content: Optional[str] = None
        for d in self.override_dirs:
            p = d / f"{name}.textfsm"
            if p.is_file():
                content = p.read_text(encoding="utf-8")
                break
        if content is None:
            content = self.store.content(name, enabled_only=enabled_only)
        with self._lock:
            self._content[key] = content
        return content
