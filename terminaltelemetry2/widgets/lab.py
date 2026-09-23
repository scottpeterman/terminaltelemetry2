"""Template-lab core, kept free of Qt so it can be exercised headlessly.

A widget that fails (or succeeds) to parse carries a LabSource -- the exact
output it collected and the template name it used. The lab preloads that,
lets the field engineer edit the template against the real capture, and
re-runs it through run_textfsm(), which returns a LabResult that names the
input line and rule line a State Error choked on instead of a bare traceback.
"""
from __future__ import annotations

import io
import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional

import textfsm

# TextFSM raises "State Error raised. Rule Line: 89. Input Line:      39 root ..."
# on a strict `^. -> Error`. Pull the two useful facts out of that message so the
# lab can point at the row, not just echo the exception.
_STATE_ERR = re.compile(
    r"Rule Line:\s*(\d+)\.\s*Input Line:\s*(.*)", re.DOTALL
)


@dataclass
class LabSource:
    """What a widget hands the lab: the capture and the template it tried."""
    platform: str
    command: str
    template: str                 # resolved name, or the exact <platform>_<command> guess
    output: str                   # raw CLI text the widget collected (uncleaned)
    widget: str = ""              # widget name, for the dialog title
    error: Optional[str] = None   # the widget's own parse error, if the poll failed
    base: str = ""                # template family the widget resolves in (explicit name
                                  # or the exact <platform>_<command>); "" = template


@dataclass
class LabResult:
    ok: bool
    records: List[Dict[str, str]] = field(default_factory=list)
    header: List[str] = field(default_factory=list)
    error: Optional[str] = None
    error_kind: str = ""          # "" | "state" | "syntax" | "other"
    input_line: Optional[str] = None   # the capture line that choked (state errors)
    rule_line: Optional[int] = None    # 1-based line in the template that raised


def run_textfsm(content: str, cleaned_output: str) -> LabResult:
    """Compile `content` and parse `cleaned_output` (already preamble-stripped;
    use Parser.clean_output first to match a live poll). Never raises: a compile
    or parse failure comes back as a LabResult with ok=False and, for a strict
    State Error, the offending input line and rule line broken out."""
    try:
        fsm = textfsm.TextFSM(io.StringIO(content))
    except textfsm.TextFSMTemplateError as e:
        return LabResult(ok=False, error=str(e), error_kind="syntax")
    except Exception as e:                       # malformed regex, bad value opts
        return LabResult(ok=False, error=f"{type(e).__name__}: {e}", error_kind="syntax")

    header = list(fsm.header)
    try:
        rows = fsm.ParseText(cleaned_output)
    except textfsm.TextFSMError as e:
        msg = str(e)
        m = _STATE_ERR.search(msg)
        if m:
            return LabResult(
                ok=False, header=header, error=msg, error_kind="state",
                rule_line=int(m.group(1)),
                input_line=m.group(2).strip() or None,
            )
        return LabResult(ok=False, header=header, error=msg, error_kind="other")
    except Exception as e:
        return LabResult(ok=False, header=header,
                         error=f"{type(e).__name__}: {e}", error_kind="other")

    records = [dict(zip(header, row)) for row in rows]
    return LabResult(ok=True, records=records, header=header)
