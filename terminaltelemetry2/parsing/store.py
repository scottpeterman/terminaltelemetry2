"""TemplateStore -- the template DB as a managed object, free of Qt.

Schema v2 (PRAGMA user_version = 2):

    templates   one row per template, unique by name (cli_command)
                platform / command  derived on migration, editable
                enabled             0 = out of the scored sweep, sibling families
                                    and exact/explicit resolution
                note, updated
    samples     capture corpus: raw output per platform + command, deduped
                by hash -- what the manager re-tests templates against

The name stays in the `cli_command` column because the sweep's term filter
matches on it; `platform`/`command` are metadata for browsing and the vendor
wizard, never used for resolution.
"""
from __future__ import annotations

import hashlib
import io
import re
import shutil
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

import textfsm

SCHEMA_VERSION = 2

# Second-token verbs that mark a one-token platform (fortinet_get_..., linux_ip_...).
_VERBS = {"show", "display", "get", "execute", "diagnose", "ip", "cat", "run",
          "dir", "fnsysctl"}

_SCHEMA = """
CREATE TABLE templates (
    id              INTEGER PRIMARY KEY,
    cli_command     TEXT NOT NULL UNIQUE,
    platform        TEXT NOT NULL DEFAULT '',
    command         TEXT NOT NULL DEFAULT '',
    cli_content     TEXT NOT NULL DEFAULT '',
    textfsm_content TEXT NOT NULL,
    textfsm_hash    TEXT NOT NULL DEFAULT '',
    source          TEXT NOT NULL DEFAULT '',
    enabled         INTEGER NOT NULL DEFAULT 1,
    note            TEXT NOT NULL DEFAULT '',
    created         TEXT NOT NULL DEFAULT '',
    updated         TEXT NOT NULL DEFAULT ''
);
CREATE INDEX ix_templates_platform ON templates(platform);
CREATE TABLE samples (
    id          INTEGER PRIMARY KEY,
    platform    TEXT NOT NULL,
    command     TEXT NOT NULL,
    output      TEXT NOT NULL,
    output_hash TEXT NOT NULL,
    label       TEXT NOT NULL DEFAULT '',
    created     TEXT NOT NULL,
    UNIQUE (platform, command, output_hash)
);
"""


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def split_name(name: str) -> Tuple[str, str]:
    """Best-effort (platform, command) from a template name.
    'hp_comware_display_lldp_neighbor-information_list'
        -> ('hp_comware', 'display lldp neighbor-information list')
    'fortinet_get_system_status' -> ('fortinet', 'get system status')
    Names with spaces are bare commands with no platform."""
    if " " in name or "_" not in name:
        return "", name
    t = name.split("_")
    if t[0] in _VERBS or t[0] == "sh":            # show_system_..., a bare command
        return "", name.replace("_", " ")
    if len(t) >= 2 and t[1] in _VERBS:
        return t[0], " ".join(t[1:])
    if len(t) >= 3:
        return "_".join(t[:2]), " ".join(t[2:])
    return "", name.replace("_", " ")


def compile_check(content: str) -> Optional[str]:
    """None if the template compiles, else the textfsm error text."""
    try:
        textfsm.TextFSM(io.StringIO(content))
        return None
    except Exception as e:                      # TextFSMTemplateError and friends
        return f"{type(e).__name__}: {e}"


@dataclass
class TemplateInfo:
    name: str
    platform: str
    command: str
    source: str
    enabled: bool
    note: str
    hash: str
    updated: str
    has_sample: bool


@dataclass
class TemplateRecord(TemplateInfo):
    content: str = ""
    sample: str = ""


@dataclass
class Sample:
    id: int
    platform: str
    command: str
    output: str
    label: str
    created: str


@dataclass
class RankRow:
    name: str
    enabled: bool
    score: float
    records: int
    fields: int
    error: Optional[str] = None


@dataclass
class RegressRow:
    sample_id: int
    label: str
    records: int
    error: Optional[str] = None


_INFO_COLS = ("cli_command, platform, command, source, enabled, note, textfsm_hash, "
              "updated, length(cli_content) > 0")


def _info(row) -> TemplateInfo:
    return TemplateInfo(row[0], row[1], row[2], row[3], bool(row[4]), row[5],
                        row[6], row[7], bool(row[8]))


class TemplateStore:
    def __init__(self, db_path: Path | str):
        self.db_path = str(db_path)
        self._scorer = None                     # TextFSMAutoEngine, built on first rank()
        migrate(self.db_path)

    def _conn(self) -> sqlite3.Connection:
        c = sqlite3.connect(self.db_path, timeout=10)
        c.execute("PRAGMA busy_timeout = 10000")
        return c

    # -- browse ----------------------------------------------------------------

    def platforms(self) -> List[Tuple[str, int, int]]:
        """(platform, total, enabled), largest first."""
        with closing(self._conn()) as c:
            return c.execute(
                "SELECT platform, COUNT(*), SUM(enabled) FROM templates "
                "GROUP BY platform ORDER BY COUNT(*) DESC, platform").fetchall()

    def list(self, platform: Optional[str] = None, text: str = "",
             source: Optional[str] = None, enabled: Optional[bool] = None) -> List[TemplateInfo]:
        """Filter by exact platform, source, enabled; `text` terms are AND-ed
        substrings over name, command and note."""
        q, p = f"SELECT {_INFO_COLS} FROM templates WHERE 1=1", []
        if platform is not None:
            q += " AND platform = ?"; p.append(platform)
        if source is not None:
            q += " AND source = ?"; p.append(source)
        if enabled is not None:
            q += " AND enabled = ?"; p.append(int(enabled))
        for term in text.split():
            q += " AND (cli_command LIKE ? OR command LIKE ? OR note LIKE ?)"
            p += [f"%{term}%"] * 3
        q += " ORDER BY cli_command"
        with closing(self._conn()) as c:
            return [_info(r) for r in c.execute(q, p)]

    def get(self, name: str) -> Optional[TemplateRecord]:
        with closing(self._conn()) as c:
            r = c.execute(f"SELECT {_INFO_COLS}, textfsm_content, cli_content "
                          "FROM templates WHERE cli_command = ?", (name,)).fetchone()
        if r is None:
            return None
        i = _info(r)
        return TemplateRecord(**i.__dict__, content=r[9], sample=r[10])

    def content(self, name: str, enabled_only: bool = False) -> Optional[str]:
        q = "SELECT textfsm_content FROM templates WHERE cli_command = ?"
        if enabled_only:
            q += " AND enabled = 1"
        with closing(self._conn()) as c:
            r = c.execute(q, (name,)).fetchone()
        return r[0] if r else None

    def source_of(self, name: str) -> Optional[str]:
        with closing(self._conn()) as c:
            r = c.execute("SELECT source FROM templates WHERE cli_command = ?", (name,)).fetchone()
        return (r[0] or "unknown") if r else None

    def names_glob(self, pattern: str, enabled_only: bool = True) -> List[str]:
        q = "SELECT cli_command FROM templates WHERE cli_command GLOB ?"
        if enabled_only:
            q += " AND enabled = 1"
        with closing(self._conn()) as c:
            return [r[0] for r in c.execute(q, (pattern,))]

    def duplicates(self, content: str) -> List[str]:
        """Templates whose content is byte-identical to `content`."""
        with closing(self._conn()) as c:
            return [r[0] for r in c.execute(
                "SELECT cli_command FROM templates WHERE textfsm_hash = ?", (_sha(content),))]

    # -- families ----------------------------------------------------------------

    def next_sibling(self, name: str) -> str:
        """Base with trailing digits stripped, one past the highest suffix in
        the DB (enabled or not -- a disabled sibling still owns its number)."""
        base = re.sub(r"\d+$", "", name)
        pat = re.compile(re.escape(base) + r"(\d+)$")
        nums = [int(m.group(1)) for n in self.names_glob(base + "*", enabled_only=False)
                if (m := pat.match(n))]
        return f"{base}{max(nums, default=1) + 1}"

    # -- write ---------------------------------------------------------------------

    def save(self, name: str, content: str, *, source: str = "custom", sample: Optional[str] = None,
             platform: Optional[str] = None, command: Optional[str] = None,
             note: Optional[str] = None, allow_overwrite: bool = False) -> None:
        """Insert or update. Refuses content that doesn't compile, and refuses
        to overwrite a non-custom row unless allow_overwrite."""
        err = compile_check(content)
        if err:
            raise ValueError(f"template does not compile: {err}")
        if " " in name or not name.strip():
            raise ValueError(f"template name {name!r} must be non-empty with no spaces")
        now = _now()
        with closing(self._conn()) as c:
            row = c.execute("SELECT source FROM templates WHERE cli_command = ?", (name,)).fetchone()
            if row and not allow_overwrite and (row[0] or "").lower() != "custom":
                raise ValueError(
                    f"{name!r} is a {row[0]} template; save it as a sibling "
                    f"(e.g. {self.next_sibling(name)}) rather than overwriting it")
            if row is None:
                dp, dc = split_name(name)
                m = re.match(r"(.+?)(\d+)$", name)
                if m:                                   # a numbered sibling: inherit the base's
                    base = c.execute("SELECT platform, command FROM templates WHERE cli_command = ?",
                                     (m.group(1),)).fetchone()
                    if base:
                        dp, dc = base
                c.execute(
                    "INSERT INTO templates (cli_command, platform, command, cli_content, "
                    "textfsm_content, textfsm_hash, source, note, created, updated) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (name, platform if platform is not None else dp,
                     command if command is not None else dc, sample or "",
                     content, _sha(content), source, note or "", now, now))
            else:
                sets = ["textfsm_content=?", "textfsm_hash=?", "source=?", "updated=?"]
                vals: list = [content, _sha(content), source, now]
                for col, v in (("cli_content", sample), ("platform", platform),
                               ("command", command), ("note", note)):
                    if v is not None:
                        sets.append(f"{col}=?"); vals.append(v)
                c.execute(f"UPDATE templates SET {', '.join(sets)} WHERE cli_command=?",
                          (*vals, name))
            c.commit()

    def clone(self, src: str, dst: Optional[str] = None) -> str:
        """Copy a template (content, sample, platform, command) to a new custom
        row; defaults to the next free sibling so it competes with the base."""
        rec = self.get(src)
        if rec is None:
            raise KeyError(f"{src!r} is not in the DB")
        dst = dst or self.next_sibling(src)
        if self.get(dst) is not None:
            raise ValueError(f"{dst!r} already exists")
        self.save(dst, rec.content, sample=rec.sample, platform=rec.platform,
                  command=rec.command, note=f"cloned from {src}")
        return dst

    def set_enabled(self, names: Iterable[str], enabled: bool) -> int:
        names = list(names)
        with closing(self._conn()) as c:
            n = c.executemany("UPDATE templates SET enabled=?, updated=? WHERE cli_command=?",
                              [(int(enabled), _now(), x) for x in names]).rowcount
            c.commit()
        return n

    def set_meta(self, name: str, *, platform: Optional[str] = None,
                 command: Optional[str] = None, note: Optional[str] = None) -> None:
        sets, vals = [], []
        for col, v in (("platform", platform), ("command", command), ("note", note)):
            if v is not None:
                sets.append(f"{col}=?"); vals.append(v)
        if not sets:
            return
        with closing(self._conn()) as c:
            if c.execute(f"UPDATE templates SET {', '.join(sets)}, updated=? WHERE cli_command=?",
                         (*vals, _now(), name)).rowcount == 0:
                raise KeyError(f"{name!r} is not in the DB")
            c.commit()

    def rename(self, old: str, new: str) -> None:
        if " " in new or not new.strip():
            raise ValueError(f"template name {new!r} must be non-empty with no spaces")
        with closing(self._conn()) as c:
            if c.execute("SELECT 1 FROM templates WHERE cli_command=?", (new,)).fetchone():
                raise ValueError(f"{new!r} already exists")
            if c.execute("UPDATE templates SET cli_command=?, updated=? WHERE cli_command=?",
                         (new, _now(), old)).rowcount == 0:
                raise KeyError(f"{old!r} is not in the DB")
            c.commit()

    def delete(self, name: str, force: bool = False) -> None:
        """Custom rows delete freely; ntc/chatgpt rows need force (disable
        is the usual answer for those)."""
        src = self.source_of(name)
        if src is None:
            raise KeyError(f"{name!r} is not in the DB")
        if src.lower() != "custom" and not force:
            raise ValueError(f"{name!r} is a {src} template; disable it, or delete with force")
        with closing(self._conn()) as c:
            c.execute("DELETE FROM templates WHERE cli_command = ?", (name,))
            c.commit()

    # -- files ---------------------------------------------------------------------

    def export(self, names: Iterable[str], directory: Path | str) -> List[Path]:
        d = Path(directory)
        d.mkdir(parents=True, exist_ok=True)
        out = []
        for n in names:
            content = self.content(n)
            if content is None:
                raise KeyError(f"{n!r} is not in the DB")
            p = d / f"{n}.textfsm"
            p.write_text(content, encoding="utf-8")
            out.append(p)
        return out

    def import_file(self, path: Path | str, name: Optional[str] = None,
                    overwrite: bool = False) -> str:
        p = Path(path)
        name = name or p.stem
        self.save(name, p.read_text(encoding="utf-8"), allow_overwrite=overwrite)
        return name

    # -- sample corpus -----------------------------------------------------------------

    def add_sample(self, platform: str, command: str, output: str, label: str = "") -> Optional[int]:
        """Store raw output; None if an identical capture is already there."""
        if not output.strip():
            return None
        with closing(self._conn()) as c:
            cur = c.execute(
                "INSERT OR IGNORE INTO samples (platform, command, output, output_hash, label, created) "
                "VALUES (?,?,?,?,?,?)", (platform, command, output, _sha(output), label, _now()))
            c.commit()
            return cur.lastrowid if cur.rowcount else None

    def samples(self, platform: Optional[str] = None, command: Optional[str] = None) -> List[Sample]:
        q, p = "SELECT id, platform, command, output, label, created FROM samples WHERE 1=1", []
        if platform is not None:
            q += " AND platform = ?"; p.append(platform)
        if command is not None:
            q += " AND command = ?"; p.append(command)
        with closing(self._conn()) as c:
            return [Sample(*r) for r in c.execute(q + " ORDER BY id DESC", p)]

    def delete_sample(self, sample_id: int) -> None:
        with closing(self._conn()) as c:
            c.execute("DELETE FROM samples WHERE id = ?", (sample_id,))
            c.commit()

    # -- testing ---------------------------------------------------------------------

    def rank(self, cleaned_output: str, filter_string: str,
             include_disabled: bool = True) -> List[RankRow]:
        """Every sweep candidate for `filter_string`, scored against the output
        exactly as the sweep scores it, best first. With include_disabled, shows
        what a disabled template would score -- the interference view."""
        from .tfsm_fire import TextFSMAutoEngine, filter_clause
        if self._scorer is None:
            self._scorer = TextFSMAutoEngine(self.db_path, verbose=False)
        scorer = self._scorer
        where, params = filter_clause(filter_string)
        if not include_disabled:
            where += " AND enabled = 1"
        with closing(self._conn()) as c:
            rows = c.execute(f"SELECT cli_command, textfsm_content, enabled FROM templates "
                             f"WHERE {where}", params).fetchall()
        out: List[RankRow] = []
        for name, content, en in rows:
            try:
                fsm = textfsm.TextFSM(io.StringIO(content))
                recs = [dict(zip(fsm.header, r)) for r in fsm.ParseText(cleaned_output)]
                score = scorer._score_parts(recs, name)["total"] if recs else 0.0
                out.append(RankRow(name, bool(en), round(score, 1), len(recs), len(fsm.header)))
            except Exception as e:
                out.append(RankRow(name, bool(en), 0.0, 0, 0, f"{type(e).__name__}: {e}"))
        out.sort(key=lambda r: (-r.score, r.name))
        return out

    def regress(self, name: str, cleaner=None, platform: Optional[str] = None,
                command: Optional[str] = None) -> List[RegressRow]:
        """Run one template against every stored sample for its platform +
        command (or the given ones). `cleaner` strips prompts/echo the way a
        poll would; pass Parser.clean_output."""
        rec = self.get(name)
        if rec is None:
            raise KeyError(f"{name!r} is not in the DB")
        plat = platform if platform is not None else rec.platform
        cmd = command if command is not None else rec.command
        out = []
        for s in self.samples(plat, cmd):
            text = cleaner(s.output) if cleaner else s.output
            try:
                fsm = textfsm.TextFSM(io.StringIO(rec.content))
                out.append(RegressRow(s.id, s.label, len(fsm.ParseText(text))))
            except Exception as e:
                out.append(RegressRow(s.id, s.label, 0, f"{type(e).__name__}: {e}"))
        return out


# ═══════════════════════════════════════════════════════════════════════════
# Migration v1 -> v2
# ═══════════════════════════════════════════════════════════════════════════

def schema_version(db_path: str) -> int:
    with closing(sqlite3.connect(db_path)) as c:
        return c.execute("PRAGMA user_version").fetchone()[0]


def migrate(db_path: str) -> bool:
    """Bring a v1 DB (flat `templates`, no constraints) to v2 in place.
    Backs the file up to <name>.v1.bak first. Duplicate names keep the last
    row. Siblings (<base>N with the base present) inherit the base's
    platform/command. Returns True if a migration ran."""
    if schema_version(db_path) >= SCHEMA_VERSION:
        return False
    bak = Path(db_path).with_suffix(Path(db_path).suffix + ".v1.bak")
    if not bak.exists():
        shutil.copy2(db_path, bak)
    with closing(sqlite3.connect(db_path)) as c:
        old = c.execute(
            "SELECT cli_command, cli_content, textfsm_content, textfsm_hash, source, created "
            "FROM templates ORDER BY rowid").fetchall()
        rows: Dict[str, tuple] = {}
        for r in old:
            if r[0] and r[2]:
                rows[r[0]] = r                              # last one wins
        meta = {n: split_name(n) for n in rows}
        for n in rows:
            m = re.match(r"(.+?)(\d+)$", n)
            if m and m.group(1) in rows:
                meta[n] = meta[m.group(1)]
        c.execute("BEGIN")
        c.execute("ALTER TABLE templates RENAME TO templates_v1")
        for stmt in filter(str.strip, _SCHEMA.split(";")):
            c.execute(stmt)
        c.executemany(
            "INSERT INTO templates (cli_command, platform, command, cli_content, textfsm_content, "
            "textfsm_hash, source, created, updated) VALUES (?,?,?,?,?,?,?,?,?)",
            [(n, meta[n][0], meta[n][1], r[1] or "", r[2],
              _sha(r[2]), (r[4] or "unknown").lower(), r[5] or "", r[5] or "")
             for n, r in rows.items()])
        c.execute("DROP TABLE templates_v1")
        c.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        c.commit()
    with closing(sqlite3.connect(db_path)) as c:
        c.execute("VACUUM")
    return True
