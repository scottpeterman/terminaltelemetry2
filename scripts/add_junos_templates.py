#!/usr/bin/env python3
"""Seed custom juniper_junos templates into a template DB (idempotent).

These are scoped to what the widgets consume and use the SAME Value names as
the equivalent arista/ios templates, so the widgets bind cross-vendor with no
alias changes. Authored against ntc-templates test fixtures (real device
output). Re-runnable after a DB re-seed; refuses to overwrite non-custom rows.

    python scripts/add_junos_templates.py                       # bundled DB
    python scripts/add_junos_templates.py ~/.terminaltelemetry2/tfsm_templates.db
"""
from __future__ import annotations

import hashlib
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

# name -> (textfsm, sample). Add more Junos templates here as they're authored.
TEMPLATES = {
    "juniper_junos_show_system_processes_summary": (
        r"""# juniper_junos_show_system_processes_summary -- scoped (custom).
# FreeBSD top on Junos. Parses only what system + top_procs consume, with the
# SAME Value names as arista_eos_show_processes_top_once so both widgets bind
# with no alias changes. Unconsumed process columns are matched as \S+ (PRI can
# be negative, NICE can be "ki31"/"-"); COMMAND may hold spaces/braces. Error is
# scoped to the process rows, so the memory/swap lines can't take the parse down.
Value Filldown GLOBAL_LOAD_AVERAGE_1_MINUTES (\d+\.?\d*)
Value Filldown GLOBAL_LOAD_AVERAGE_5_MINUTES (\d+\.?\d*)
Value Filldown GLOBAL_CPU_PERCENT_USER (\d+\.?\d*)
Value Filldown GLOBAL_CPU_PERCENT_SYSTEM (\d+\.?\d*)
Value Filldown GLOBAL_CPU_PERCENT_IDLE (\d+\.?\d*)
Value Filldown GLOBAL_MEM_USED (\S+)
Value Filldown GLOBAL_MEM_FREE (\S+)
Value Filldown GLOBAL_TASKS_TOTAL (\d+)
Value Filldown GLOBAL_TASKS_ZOMBIE (\d+)
Value Required PID (\d+)
Value RESIDENT_MEMORY_SIZE (\S+)
Value PERCENT_CPU (\d+\.?\d*)
Value CPU_TIME (\S+)
Value COMMAND (.+?)

Start
  ^last pid:.*load averages:\s+${GLOBAL_LOAD_AVERAGE_1_MINUTES},\s+${GLOBAL_LOAD_AVERAGE_5_MINUTES},\s+\S+.*$$
  ^\s*${GLOBAL_TASKS_TOTAL} threads:.*,\s*${GLOBAL_TASKS_ZOMBIE} zombie.*$$
  ^\s*${GLOBAL_TASKS_TOTAL} threads:.*$$
  ^CPU:\s+${GLOBAL_CPU_PERCENT_USER}% user,\s+\S+ nice,\s+${GLOBAL_CPU_PERCENT_SYSTEM}% system,\s+\S+ interrupt,\s+${GLOBAL_CPU_PERCENT_IDLE}% idle\s*$$
  ^Mem:\s+${GLOBAL_MEM_USED}\s+Active,.*,\s+${GLOBAL_MEM_FREE}\s+Free\s*$$
  ^\s*PID\s+USERNAME\s+PRI\s+NICE\s+SIZE\s+RES\s+STATE\s+C\s+TIME\s+WCPU\s+COMMAND\s*$$ -> Process

Process
  ^\s*${PID}\s+\S+\s+\S+\s+\S+\s+\S+\s+${RESIDENT_MEMORY_SIZE}\s+\S+\s+\S+\s+${CPU_TIME}\s+${PERCENT_CPU}%\s+${COMMAND}\s*$$ -> Record
  ^\s*$$
  ^. -> Error
""",
        """last pid: 24931;  load averages:  0.89,  0.94,  0.81  up 41+06:26:24    00:51:55
545 threads:   9 running, 481 sleeping, 1 zombie, 54 waiting
CPU: 14.7% user,  0.0% nice,  4.1% system,  0.2% interrupt, 81.0% idle
Mem: 3977M Active, 14G Inact, 1815M Wired, 504M Buf, 74G Free
Swap: 12G Total, 12G Free

  PID USERNAME    PRI NICE   SIZE    RES STATE    C   TIME    WCPU COMMAND
19880 root         25    0   801M    74M select   2 132.6H  15.09% cosd
   34 root        -51   0       0      0 WAIT     0   0:00   0.00% watchdog""",
    ),
    "juniper_junos_show_interfaces_extensive": (
        r"""# juniper_junos_show_interfaces_extensive -- physical-port status (custom).
# Superset of the stock 5-field template: adds an optional :N channel to
# INTERFACE (xe-0/0/0:3 -- the stock regex missed channelized ports) and
# captures SPEED for the Ports tile. Scoped to transport families
# (ge/xe/et/fe/so); logical/mgmt/aggregate names are skipped. Description is
# optional and cleared between records; Record fires on the Link-level line.
Value Required INTERFACE ((?:ge|xe|et|fe|so)-\d+/\d+/\d+(?::\d+)?)
Value ADMIN_STATUS (Enabled|Disabled)
Value LINK_STATUS (Up|Down)
Value DESCRIPTION (.*?)
Value MTU (\d+|Unlimited)
Value SPEED ([^,\s]+)

Start
  ^Physical interface:\s+${INTERFACE},\s+${ADMIN_STATUS},\s+Physical link is\s+${LINK_STATUS}
  ^\s+Description:\s+${DESCRIPTION}\s*$$
  ^\s+Link-level type:.*?\bMTU:\s+${MTU}\b(?:.*?\bSpeed:\s+${SPEED})? -> Record
  ^.*$$
""",
        """Physical interface: xe-0/0/0:3, Enabled, Physical link is Down
  Interface index: 167, SNMP ifIndex: 540, Generation: 201
  Description: Available
  Link-level type: Ethernet, MTU: 1514, MRU: 1522, LAN-PHY mode, Speed: 10Gbps,
  Last flapped   : 2024-12-12 06:42:34 UTC (92w5d 08:07 ago)""",
    ),
    "juniper_junos_show_system_processes_extensive": (
        r"""# juniper_junos_show_system_processes_extensive -- FreeBSD top (custom).
# Spans JUNOS generations. The process table drifted across versions:
#   23.x:        PID USERNAME     PRI NICE SIZE RES STATE C TIME WCPU COMMAND  (C/core col)
#   13.x:        PID USERNAME THR PRI NICE SIZE RES STATE   TIME WCPU COMMAND  (THR col)
#   QFX5100 21.x PID USERNAME     PRI NICE SIZE RES STATE   TIME WCPU COMMAND  (neither)
# The header selects the process state that counts columns correctly for that
# layout -- otherwise RES captures the wrong column. STATE is a fixed 6-char
# column that can hold spaces ("long p", "PCI Sc") or "-", so it's matched
# lazily and the row is anchored from the right on TIME / WCPU%.
# The summary line says "threads" on 23.x and "processes" on 13.x/QFX; those
# omit the CPU line entirely (CPU% is blank -- it isn't in this output). Parses
# only what system + top_procs consume, using the SAME Value names as the arista
# template so both widgets bind with no alias changes. No Error rule: the
# template is resolved explicitly, so it records the rows it matches and
# ignores the rest (trailing prompt, {master:0}, the absent CPU line, etc.).
Value Filldown GLOBAL_LOAD_AVERAGE_1_MINUTES (\d+\.?\d*)
Value Filldown GLOBAL_LOAD_AVERAGE_5_MINUTES (\d+\.?\d*)
Value Filldown GLOBAL_CPU_PERCENT_USER (\d+\.?\d*)
Value Filldown GLOBAL_CPU_PERCENT_SYSTEM (\d+\.?\d*)
Value Filldown GLOBAL_CPU_PERCENT_IDLE (\d+\.?\d*)
Value Filldown GLOBAL_MEM_USED (\S+)
Value Filldown GLOBAL_MEM_FREE (\S+)
Value Filldown GLOBAL_TASKS_TOTAL (\d+)
Value Filldown GLOBAL_TASKS_ZOMBIE (\d+)
Value Required PID (\d+)
Value RESIDENT_MEMORY_SIZE (\S+)
Value PERCENT_CPU (\d+\.?\d*)
Value CPU_TIME (\S+)
Value COMMAND (.+?)

Start
  ^last pid:.*load averages:\s+${GLOBAL_LOAD_AVERAGE_1_MINUTES},\s+${GLOBAL_LOAD_AVERAGE_5_MINUTES},\s+\S+.*$$
  ^\s*${GLOBAL_TASKS_TOTAL} (?:threads|processes):.*,\s*${GLOBAL_TASKS_ZOMBIE} zombie.*$$
  ^\s*${GLOBAL_TASKS_TOTAL} (?:threads|processes):.*$$
  ^CPU:\s+${GLOBAL_CPU_PERCENT_USER}% user,\s+\S+ nice,\s+${GLOBAL_CPU_PERCENT_SYSTEM}% system,\s+\S+ interrupt,\s+${GLOBAL_CPU_PERCENT_IDLE}% idle\s*$$
  ^Mem:\s+${GLOBAL_MEM_USED}\s+Active,.*,\s+${GLOBAL_MEM_FREE}\s+Free\s*$$
  ^\s*PID\s+USERNAME\s+PRI\s+NICE\s+SIZE\s+RES\s+STATE\s+C\s+TIME\s+WCPU\s+COMMAND\s*$$ -> Process_C
  ^\s*PID\s+USERNAME\s+THR\s+PRI\s+NICE\s+SIZE\s+RES\s+STATE\s+TIME\s+WCPU\s+COMMAND\s*$$ -> Process_THR
  ^\s*PID\s+USERNAME\s+PRI\s+NICE\s+SIZE\s+RES\s+STATE\s+TIME\s+WCPU\s+COMMAND\s*$$ -> Process_Plain

Process_C
  ^\s*${PID}\s+\S+\s+\S+\s+\S+\s+\S+\s+${RESIDENT_MEMORY_SIZE}\s+.+?\s+-?\d+\s+${CPU_TIME}\s+${PERCENT_CPU}%\s+${COMMAND}\s*$$ -> Record

Process_THR
  ^\s*${PID}\s+\S+\s+\d+\s+\S+\s+\S+\s+\S+\s+${RESIDENT_MEMORY_SIZE}\s+.+?\s+${CPU_TIME}\s+${PERCENT_CPU}%\s+${COMMAND}\s*$$ -> Record

Process_Plain
  ^\s*${PID}\s+\S+\s+\S+\s+\S+\s+\S+\s+${RESIDENT_MEMORY_SIZE}\s+.+?\s+${CPU_TIME}\s+${PERCENT_CPU}%\s+${COMMAND}\s*$$ -> Record
""",
        """last pid: 28146;  load averages:  0.53,  0.43,  0.36  up 693+17:48:16    15:03:48
514 threads:   7 running, 459 sleeping, 48 waiting
CPU:  1.5% user,  0.0% nice,  1.1% system,  0.1% interrupt, 97.3% idle
Mem: 186M Active, 9808M Inact, 1117M Wired, 460M Buf, 36G Free
Swap: 3072M Total, 3072M Free

  PID USERNAME    PRI NICE   SIZE    RES STATE    C   TIME    WCPU COMMAND
33723 root         21    0   803M    86M select   3 417.8H   2.29% mib2d
   12 root        -52    -     0B   768K WAIT    -1   0:00   0.00% intr{swi6: task queue}""",
    ),
}


def main(argv: list[str]) -> int:
    if len(argv) > 1:
        db = Path(argv[1]).expanduser()
    else:
        db = Path(__file__).resolve().parent.parent / "terminaltelemetry2" / "data" / "tfsm_templates.db"
    if not db.is_file():
        print(f"no template DB at {db}", file=sys.stderr)
        return 2
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    con = sqlite3.connect(db)
    try:
        for name, (tpl, sample) in TEMPLATES.items():
            row = con.execute("SELECT source FROM templates WHERE cli_command=?", (name,)).fetchone()
            # Protect explicitly vendor-tagged rows (e.g. ntc); untagged (NULL)
            # and custom rows are ours to replace.
            if row and row[0] and row[0].lower() != "custom":
                print(f"refusing to overwrite vendor {name!r} (source={row[0]})", file=sys.stderr)
                continue
            con.execute("DELETE FROM templates WHERE cli_command=?", (name,))
            nid = (con.execute("SELECT MAX(id) FROM templates").fetchone()[0] or 0) + 1
            h = hashlib.sha256(tpl.encode()).hexdigest()
            con.execute(
                "INSERT INTO templates (id, cli_command, cli_content, textfsm_content, "
                "textfsm_hash, source, created) VALUES (?,?,?,?,?,?,?)",
                (nid, name, sample, tpl, h, "custom", now),
            )
            print(f"seeded {name} (id={nid})")
        con.commit()
    finally:
        con.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
