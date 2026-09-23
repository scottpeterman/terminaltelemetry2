"""Template-lab core, DB write-back, and the show-processes-top sibling.

The dialog (Qt) is exercised by the app; these lock down the textfsm runner,
its structured error extraction, the instance-numbered sibling written into
the DB, and that the resolver's exact->scored fallthrough selects that sibling
where the stock base fails while keeping the base where it works.
"""
import sqlite3

import pytest

from conftest import PKG_DB, fixture_text
from terminaltelemetry2.parsing import Parser
from terminaltelemetry2.widgets.lab import LabResult, run_textfsm

TOP = "arista_eos_show_processes_top_once"

# top with a real-time kernel thread: PR = -51 is what breaks the stock template.
# TOP_RAW carries the command echo a live capture includes; TOP_CLEAN is what the
# resolver/dialog feed run_textfsm after clean_output strips that preamble.
TOP_RAW = """\
agg1.lab1#show processes top once
top - 01:41:26 up  1:46,  3 users,  load average: 0.09, 0.06, 0.07
Tasks: 151 total,   1 running, 150 sleeping,   0 stopped,   0 zombie
%Cpu(s): 11.1 us,  0.0 sy,  0.0 ni, 83.3 id,  0.0 wa,  5.6 hi,  0.0 si,  0.0 st
MiB Mem :   1933.5 total,     52.9 free,   1179.5 used,   1118.4 buff/cache
MiB Swap:      0.0 total,      0.0 free,      0.0 used.    753.9 avail Mem

    PID USER      PR  NI    VIRT    RES    SHR S  %CPU  %MEM     TIME+ COMMAND
      1 root      20   0   21648  12388   9000 S   0.0   0.6   0:04.68 systemd
     39 root     -51   0       0      0      0 S   0.0   0.0   0:00.00 watchdogd
"""

TOP_CLEAN = "\n".join(TOP_RAW.splitlines()[1:]) + "\n"   # echo line stripped

SIMPLE_TPL = """\
Value KEY (\\S+)
Value VAL (\\d+)

Start
  ^${KEY}\\s+${VAL} -> Record
"""


def _db_template(name: str) -> str:
    con = sqlite3.connect(PKG_DB)
    try:
        row = con.execute(
            "SELECT textfsm_content FROM templates WHERE cli_command = ?", (name,)
        ).fetchone()
    finally:
        con.close()
    assert row, f"{name} missing from DB"
    return row[0]


# -- run_textfsm -----------------------------------------------------------

def test_run_textfsm_success():
    res = run_textfsm(SIMPLE_TPL, "alpha 1\nbeta 22\n")
    assert res.ok
    assert res.header == ["KEY", "VAL"]
    assert res.records == [{"KEY": "alpha", "VAL": "1"}, {"KEY": "beta", "VAL": "22"}]
    assert res.error is None


def test_run_textfsm_state_error_names_the_line():
    # the stock DB template chokes on PR=-51; the runner should surface where.
    res = run_textfsm(_db_template(TOP), TOP_CLEAN)
    assert not res.ok
    assert res.error_kind == "state"
    assert res.rule_line == 89
    assert res.input_line is not None
    assert "watchdogd" in res.input_line and "-51" in res.input_line


def test_run_textfsm_syntax_error():
    res = run_textfsm("Value X (\nStart\n  ^$X -> Record\n", "whatever\n")
    assert not res.ok
    assert res.error_kind == "syntax"
    assert res.records == []


# -- the DB sibling --------------------------------------------------------

SIB = TOP + "2"                                   # the scoped fix, shipped as a sibling

# real EOS: rt AND -51 priorities plus 100.0 idle -- every case the stock base fails
EOS_CLEAN = """\
top - 02:17:45 up  2:22,  3 users,  load average: 0.07, 0.08, 0.08
Tasks: 151 total,   1 running, 150 sleeping,   0 stopped,   0 zombie
%Cpu(s):  0.0 us,  0.0 sy,  0.0 ni,100.0 id,  0.0 wa,  0.0 hi,  0.0 si,  0.0 st
MiB Mem :   1933.5 total,     65.3 free,   1166.6 used,   1107.4 buff/cache
MiB Swap:      0.0 total,      0.0 free,      0.0 used.    766.9 avail Mem

    PID USER      PR  NI    VIRT    RES    SHR S  %CPU  %MEM     TIME+ COMMAND
      1 root      20   0   21648  12388   9000 S   6.2   0.6   0:04.68 systemd
     14 root      rt   0       0      0      0 S   0.0   0.0   0:00.00 migration/0
     34 root     -51   0       0      0      0 S   0.0   0.0   0:00.00 watchdogd
"""


def test_sibling_shipped_in_db():
    content = _db_template(SIB)                    # raises if the sibling isn't there
    assert "-> Process" in content                 # guarded process state
    assert "GLOBAL_SWAP" not in content            # swap deliberately dropped
    assert "Value PRIORITY" not in content         # unconsumed column not typed


def test_sibling_parses_rt_and_idle():
    res = run_textfsm(_db_template(SIB), EOS_CLEAN)
    assert res.ok
    cmds = {r["COMMAND"] for r in res.records}
    assert {"systemd", "migration/0", "watchdogd"} <= cmds   # rt AND -51 rows kept
    assert res.records[0]["GLOBAL_CPU_PERCENT_IDLE"] == "100.0"
    assert not any(k.startswith("GLOBAL_SWAP") for k in res.header)   # swap ignored


def test_stock_base_still_breaks_on_rt():
    # the base row is untouched, so it still hard-fails on the rt/-51 rows --
    # which is exactly what lets the sweep hand the poll to the sibling.
    res = run_textfsm(_db_template(TOP), EOS_CLEAN)
    assert not res.ok and res.error_kind == "state"


# -- through the real resolver ---------------------------------------------

def test_resolver_selects_sibling_on_broken_output(parser):
    # base fails on the exact path -> scored sweep over the family -> sibling wins.
    parsed = parser.parse("arista_eos", "show processes top once", EOS_CLEAN)
    assert parsed.error is None, parsed.error
    assert parsed.template == SIB
    assert parsed.method == "scored"
    cmds = {r.get("COMMAND") for r in parsed.records}
    assert {"systemd", "migration/0", "watchdogd"} <= cmds


def test_resolver_keeps_base_where_it_works():
    # benign capture the stock base was authored against -> exact path -> base.
    # Fresh parser so no pin from another test leaks in.
    p = Parser(PKG_DB, [PKG_DB.parent / "templates"])
    benign = (
        "top - 14:30:44 up 68 days, 15 min,  1 user,  load average: 0.94, 0.84, 0.82\n"
        "Tasks: 223 total,  1 running, 222 sleeping,  0 stopped,  0 zombie\n"
        "%Cpu(s): 4.6 us, 3.1 sy, 0.0 ni, 90.8 id, 0.0 wa, 1.5 hi, 0.0 si, 0.0 st\n"
        "MiB Mem :  7956.2 total,  1755.3 free,  2052.9 used,  4148.0 buff/cache\n"
        "MiB Swap:   0.0 total,   0.0 free,   0.0 used.  5325.2 avail Mem\n\n"
        " PID USER   PR NI  VIRT  RES  SHR S %CPU %MEM   TIME+ COMMAND\n"
        " 3067 root   20  0 968336 542116 236404 S  6.2  6.7  8315:39 SandFapNi\n"
        "  1 root   20  0  6512  5176  4012 S  0.0  0.1  1:01.29 systemd\n"
    )
    parsed = p.parse("arista_eos", "show processes top once", benign)
    assert parsed.error is None, parsed.error
    assert parsed.template == TOP and parsed.method == "exact"


# -- DB write-back (what the lab's Save to database does) ------------------

def test_next_sibling_name(parser):
    # base2 already ships, so the next free sibling of the family is base3,
    # whether asked from the base or from the existing sibling.
    assert parser.next_sibling_name(TOP) == TOP + "3"
    assert parser.next_sibling_name(SIB) == TOP + "3"


def test_save_template_to_db_creates_sibling(tmp_path):
    import shutil
    db = tmp_path / "tfsm.db"
    shutil.copy2(PKG_DB, db)
    p = Parser(db)                                       # no override dir: DB only
    name = p.next_sibling_name(SIB)                      # -> ..._top_once3
    p.save_template_to_db(name, _db_template(SIB), sample=EOS_CLEAN)
    assert p.template_content(name) is not None          # readable back
    parsed = p.parse("arista_eos", "show processes top once", EOS_CLEAN)
    assert parsed.error is None
    assert parsed.template in (SIB, name)                # a scoped sibling won


def test_save_template_refuses_to_overwrite_vendor_base(tmp_path):
    import shutil
    db = tmp_path / "tfsm.db"
    shutil.copy2(PKG_DB, db)
    p = Parser(db)
    with pytest.raises(ValueError, match="sibling"):
        p.save_template_to_db(TOP, "Value X (\\S+)\n\nStart\n  ^${X} -> Record\n")


def test_exact_template_name():
    assert Parser.exact_template("arista_eos", "show processes top once") == TOP
    assert Parser.exact_template("cisco_ios", "show running-config") == \
        "cisco_ios_show_running_config"


def test_clean_output_strips_command_echo(parser):
    cleaned = parser.clean_output(TOP_RAW)
    assert "show processes top once" not in cleaned.splitlines()[0]
    assert "watchdogd" in cleaned
