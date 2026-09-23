#!/usr/bin/env python3
"""Seed the arista_eos_show_processes_top_once2 sibling into a template DB.

The stock ntc `arista_eos_show_processes_top_once` row is left untouched -- it
still parses the benign captures it was authored against. This scoped sibling
handles real EOS output (rt/-51 priorities, 100.0 idle) by parsing only the
fields the system/top_procs widgets consume and guarding ^. -> Error to the
process rows. The resolver's exact->scored fallthrough selects whichever of
the two parses a given device's output.

This script is the source of truth for the sibling's textfsm content; the DB
row is generated from it. Idempotent -- safe to re-run after a DB re-seed.

    python scripts/add_top_once_sibling.py                 # bundled DB
    python scripts/add_top_once_sibling.py ~/.terminaltelemetry2/tfsm_templates.db
"""
from __future__ import annotations

import hashlib
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

NAME = "arista_eos_show_processes_top_once2"

TEMPLATE = r"""# arista_eos_show_processes_top_once2 -- scoped sibling of the ntc base.
#
# Parses only what the widgets consume:
#   system    -> load 1m/5m, cpu user/sys/idle, mem used/free, tasks/zombies
#   top_procs -> pid, %cpu, %mem, rss, time, command
# Unconsumed top(1) output (swap, buffers, nice, virt/shr, the extra cpu
# states) is deliberately not parsed. Summary lines fall through and are
# ignored; only process rows are guarded by ^. -> Error, so a malformed
# process row still hard-fails (letting the sweep pick another sibling) while
# a swap line we never read can't take the parse down. Unconsumed process
# columns are matched as \S+, which also retires the PR = -51 break.
Value Filldown GLOBAL_LOAD_AVERAGE_1_MINUTES (\d+\.?\d*)
Value Filldown GLOBAL_LOAD_AVERAGE_5_MINUTES (\d+\.?\d*)
Value Filldown GLOBAL_CPU_PERCENT_USER (\d+\.\d+)
Value Filldown GLOBAL_CPU_PERCENT_SYSTEM (\d+\.\d+)
Value Filldown GLOBAL_CPU_PERCENT_IDLE (\d+\.\d+)
Value Filldown GLOBAL_MEM_USED (\d+\.?\d*)
Value Filldown GLOBAL_MEM_FREE (\d+\.?\d*)
Value Filldown GLOBAL_TASKS_TOTAL (\d+)
Value Filldown GLOBAL_TASKS_ZOMBIE (\d+)
Value Required PID (\d+)
Value RESIDENT_MEMORY_SIZE (\d+\.?\d*[kmgtKMGT]?)
Value PERCENT_CPU (\d+\.\d+)
Value PERCENT_MEMORY (\d+\.\d+)
Value CPU_TIME (\d+:\d+\.?\d*)
Value COMMAND (\S+)


Start
  ^top - \S+ up .*load average:\s+${GLOBAL_LOAD_AVERAGE_1_MINUTES},\s+${GLOBAL_LOAD_AVERAGE_5_MINUTES},\s+\S+\s*$$
  ^Tasks:\s+${GLOBAL_TASKS_TOTAL} total,.*,\s+${GLOBAL_TASKS_ZOMBIE} zombie\s*$$
  ^[%]?Cpu[(]s[)]:\s+${GLOBAL_CPU_PERCENT_USER}[ %]us,\s*${GLOBAL_CPU_PERCENT_SYSTEM}[ %]sy,\s*\S+[ %]ni,\s*${GLOBAL_CPU_PERCENT_IDLE}[ %]id,.*$$
  ^\S+\s+Mem.*:\s+\S+\s+total,\s*${GLOBAL_MEM_FREE}\s+free,\s*${GLOBAL_MEM_USED}\s+used,.*$$
  ^\S+\s+Mem.*:\s+\S+\s+total,\s*${GLOBAL_MEM_USED}\s+used,\s*${GLOBAL_MEM_FREE}\s+free,.*$$
  ^\s*PID\s+USER\s+PR\s.*COMMAND\s*$$ -> Process

Process
  ^\s*${PID}\s+\S+\s+\S+\s+\S+\s+\S+\s+${RESIDENT_MEMORY_SIZE}\s+\S+\s+\S+\s+${PERCENT_CPU}\s+${PERCENT_MEMORY}\s+${CPU_TIME}\s+${COMMAND}\s*$$ -> Record
  ^\s*$$
  ^. -> Error
"""

SAMPLE = """top - 02:17:45 up  2:22,  3 users,  load average: 0.07, 0.08, 0.08
Tasks: 151 total,   1 running, 150 sleeping,   0 stopped,   0 zombie
%Cpu(s):  0.0 us,  0.0 sy,  0.0 ni,100.0 id,  0.0 wa,  0.0 hi,  0.0 si,  0.0 st
MiB Mem :   1933.5 total,     65.3 free,   1166.6 used,   1107.4 buff/cache
MiB Swap:      0.0 total,      0.0 free,      0.0 used.    766.9 avail Mem

    PID USER      PR  NI    VIRT    RES    SHR S  %CPU  %MEM     TIME+ COMMAND
      1 root      20   0   21648  12388   9000 S   0.0   0.6   0:04.68 systemd
     14 root      rt   0       0      0      0 S   0.0   0.0   0:00.00 migration/0
     34 root     -51   0       0      0      0 S   0.0   0.0   0:00.00 watchdogd"""


def default_db() -> Path:
    return Path(__file__).resolve().parent.parent / "terminaltelemetry2" / "data" / "tfsm_templates.db"


def main(argv: list[str]) -> int:
    db = Path(argv[1]).expanduser() if len(argv) > 1 else default_db()
    if not db.is_file():
        print(f"no template DB at {db}", file=sys.stderr)
        return 2
    h = hashlib.sha256(TEMPLATE.encode("utf-8")).hexdigest()
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    con = sqlite3.connect(db)
    try:
        row = con.execute(
            "SELECT source FROM templates WHERE cli_command = ?", (NAME,)
        ).fetchone()
        if row and (row[0] or "").lower() != "custom":
            print(f"refusing to overwrite non-custom row {NAME!r} (source={row[0]})",
                  file=sys.stderr)
            return 3
        con.execute("DELETE FROM templates WHERE cli_command = ?", (NAME,))
        nid = (con.execute("SELECT MAX(id) FROM templates").fetchone()[0] or 0) + 1
        con.execute(
            "INSERT INTO templates (id, cli_command, cli_content, textfsm_content, "
            "textfsm_hash, source, created) VALUES (?,?,?,?,?,?,?)",
            (nid, NAME, SAMPLE, TEMPLATE, h, "custom", now),
        )
        con.commit()
    finally:
        con.close()
    print(f"seeded {NAME} (id={nid}, source=custom) into {db}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
