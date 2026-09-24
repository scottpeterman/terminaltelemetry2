"""
Netlapse Parse Engine — Structured CLI output parsing via tfsm-fire.

Wraps TextFSMAutoEngine for use in the collection pipeline. After SSH
collection produces raw CLI output, the parse engine finds the best
TextFSM template, parses the output into structured records, and
populates Snapshot.parsed_data / template_name / template_score.

The "output selects template" paradigm: you hand it raw text and a
platform hint, it tries every matching template and scores the results.
No manual template selection required.

Usage:
    engine = ParseEngine(db_path="~/.netlapse/tfsm_templates.db")
    engine.enrich_snapshot(snapshot, platform_profile="cisco_ios")

    if snapshot.parsed_data:
        print(f"Template: {snapshot.template_name}")
        print(f"Score: {snapshot.template_score}")
        print(f"Records: {len(snapshot.parsed_data['records'])}")
"""

import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional

from .tfsm_fire import TextFSMAutoEngine

logger = logging.getLogger(__name__)

# Default locations to search for the template database
DEFAULT_DB_PATHS = [
    Path("~/.terminaltelemetry2/tfsm_templates.db"),
    Path(__file__).parent.parent / "data" / "tfsm_templates.db",
]

# Minimum score to consider a parse result valid.
# The scoring is 0-100 across record count (90), field richness (90),
# population rate (25), and consistency (15). A threshold of 15 filters
# out junk matches while keeping legitimate single-record parses.
DEFAULT_MIN_SCORE = 15.0


@dataclass
class ParseResult:
    """Result of parsing a single CLI output."""
    success: bool
    template: Optional[str] = None
    records: Optional[List[Dict]] = None
    score: float = 0.0
    error: Optional[str] = None

    @property
    def record_count(self) -> int:
        return len(self.records) if self.records else 0


class ParseEngine:
    """
    TextFSM-based output parsing engine for the Netlapse pipeline.

    Thread-safe: the underlying TextFSMAutoEngine uses thread-local
    SQLite connections. Safe to share across the scheduler's
    ThreadPoolExecutor workers.

    Attributes:
        db_path: Path to tfsm_templates.db
        min_score: Minimum score to accept a parse (default 15.0)
        template_count: Number of templates in the database
    """

    def __init__(
        self,
        db_path: Optional[str] = None,
        min_score: float = DEFAULT_MIN_SCORE,
    ):
        if db_path is None:
            for p in DEFAULT_DB_PATHS:
                resolved = p.expanduser()
                if resolved.exists():
                    db_path = str(resolved)
                    break
            else:
                searched = ", ".join(str(p) for p in DEFAULT_DB_PATHS)
                raise FileNotFoundError(
                    f"TextFSM template database not found. "
                    f"Searched: {searched}"
                )

        resolved_path = Path(db_path).expanduser()
        if not resolved_path.exists():
            raise FileNotFoundError(
                f"TextFSM template database not found: {db_path}"
            )

        self.db_path = str(resolved_path)
        self.min_score = min_score
        self._engine = TextFSMAutoEngine(self.db_path, verbose=False)

        # Count templates for startup log
        import sqlite3
        with sqlite3.connect(self.db_path) as conn:
            self.template_count = conn.execute(
                "SELECT COUNT(*) FROM templates WHERE enabled = 1"
            ).fetchone()[0]

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def parse(
        self,
        raw_output: str,
        platform_profile: str,
        command: str = "",
    ) -> ParseResult:
        """
        Parse raw CLI output into structured records.

        Uses a specific filter: platform_profile + command.
        For a broader vendor-only retry, see parse_vendor_fallback().

        Args:
            raw_output: Raw CLI text from SSH collection.
            platform_profile: Platform hint (e.g. 'cisco_ios', 'arista_eos').
            command: CLI command that produced the output (e.g. 'show ip arp').

        Returns:
            ParseResult with parsed records and scoring metadata.
        """
        if not raw_output or not raw_output.strip():
            return ParseResult(success=False, error="empty output")

        filter_string = self._build_filter(platform_profile, command)

        try:
            cleaned = self._clean_output(raw_output)

            template, parsed_data, score = self._engine.find_best_template(
                cleaned, filter_string
            )

            if score >= self.min_score and parsed_data:
                return ParseResult(
                    success=True,
                    template=template,
                    records=parsed_data,
                    score=round(score, 1),
                )
            else:
                return ParseResult(
                    success=False,
                    template=template,
                    score=round(score, 1),
                )

        except Exception as e:
            return ParseResult(success=False, error=str(e))

    def parse_vendor_fallback(
        self,
        raw_output: str,
        platform_profile: str,
    ) -> ParseResult:
        """
        Broad vendor-only parse — tries all templates for the vendor.

        Separate call from parse() so the template set is explicit:
        'juniper_junos' → filter on just 'juniper', which matches all
        22 Juniper templates. The scoring engine picks the best one
        purely from output structure.

        Use when parse() returns success=False — the command name
        didn't align with the template naming convention but the
        output might still match a vendor template.

        Args:
            raw_output: Raw CLI text (will be cleaned).
            platform_profile: e.g. 'cisco_ios', 'arista_eos', 'juniper_junos'.

        Returns:
            ParseResult from the best-scoring vendor template.
        """
        if not raw_output or not raw_output.strip():
            return ParseResult(success=False, error="empty output")

        # Extract vendor name: cisco_ios → cisco, juniper_junos → juniper
        vendor = platform_profile.split("_")[0] if platform_profile else ""
        if not vendor:
            return ParseResult(success=False, error="no vendor")

        try:
            cleaned = self._clean_output(raw_output)

            template, parsed_data, score = self._engine.find_best_template(
                cleaned, vendor
            )

            if score >= self.min_score and parsed_data:
                return ParseResult(
                    success=True,
                    template=template,
                    records=parsed_data,
                    score=round(score, 1),
                )
            else:
                return ParseResult(
                    success=False,
                    template=template,
                    score=round(score, 1),
                )

        except Exception as e:
            return ParseResult(success=False, error=str(e))

    def enrich_snapshot(
        self,
        snapshot,
        platform_profile: str,
    ) -> bool:
        """
        Parse a Snapshot's raw_text and populate its parsed_data fields.

        Tries specific filter first (platform + command), then falls
        back to vendor-only filter if that misses.

        Modifies the snapshot in-place. Returns True if parsing succeeded.

        Args:
            snapshot: A storage.backend.Snapshot instance.
            platform_profile: Platform hint from dcim_platform.

        Returns:
            True if parsed_data was populated, False otherwise.
        """
        # Attempt 1: specific filter (platform + command)
        result = self.parse(
            raw_output=snapshot.raw_text,
            platform_profile=platform_profile,
            command=snapshot.command,
        )

        # Attempt 2: vendor-only fallback
        if not result.success:
            result = self.parse_vendor_fallback(
                raw_output=snapshot.raw_text,
                platform_profile=platform_profile,
            )

        if result.success:
            snapshot.parsed_data = {
                "records": result.records,
            }
            snapshot.template_name = result.template
            snapshot.template_score = result.score
            return True

        return False

    def list_templates(self, filter_string: Optional[str] = None) -> List[str]:
        """List available template names matching a filter."""
        with self._engine.connection_manager.get_connection() as conn:
            templates = self._engine.get_filtered_templates(conn, filter_string)
            return [t["cli_command"] for t in templates]

    def test_template(
        self,
        textfsm_content: str,
        raw_output: str,
        command: str = "",
        clean: bool = True,
    ) -> Dict:
        """
        Author-test a single ad-hoc template against raw output.

        Runs ONE caller-supplied template (not the database) and returns
        compile/runtime errors verbatim plus the parsed records and score
        breakdown. Powers the Template Lab author-test loop.

        Args:
            textfsm_content: the TextFSM template source to test.
            raw_output: raw CLI text to parse.
            command: command/template name hint (used only for score's
                version-command detection).
            clean: if True (default), strip session preamble the same way
                the collection pipeline does, so the score matches what
                find_best_template / Parse Audit would produce. Turn off to
                test against the exact text pasted (line numbers preserved).
        """
        text = self._clean_output(raw_output) if clean else raw_output
        return self._engine.test_template(
            textfsm_content, text, cli_command=command,
        )

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    @staticmethod
    def _build_filter(platform_profile: str, command: str) -> str:
        """
        Build a tfsm-fire filter string from platform + command.

        The template database uses cli_command names like:
            arista_eos_show_ip_arp
            cisco_ios_show_ip_bgp_summary

        The filter engine splits on '_' and requires each term (>2 chars)
        to appear in the template name via LIKE. So we combine the
        platform profile and command into one underscore-separated string.

        Examples:
            ('arista_eos', 'show ip arp') → 'arista_eos_show_ip_arp'
            ('cisco_ios', 'show ip bgp summary') → 'cisco_ios_show_ip_bgp_summary'
            ('cisco_ios', 'show running-config') → 'cisco_ios_show_running_config'
        """
        if not platform_profile:
            platform_profile = ""

        # Normalize command: spaces and hyphens to underscores
        cmd_normalized = command.strip().replace(" ", "_").replace("-", "_")

        parts = [platform_profile, cmd_normalized]
        return "_".join(p for p in parts if p)

    @staticmethod
    def _clean_output(raw_output: str) -> str:
        """
        Clean raw CLI output for TextFSM parsing.

        SSH session captures include preamble (command echo, banners,
        pagination disable), the actual command output, and trailing
        prompts. TextFSM templates expect ONLY the command output.

        Strategy: find the LAST hostname-prefixed command echo line
        (e.g. "router#show ip arp" or "user@switch> show arp") and
        take everything after it. This skips all preamble regardless
        of how messy the session transcript is.

        Fallback: if no hostname-prefixed echo is found, strip known
        preamble patterns from the top and trailing prompts from the
        bottom.
        """
        lines = raw_output.split("\n")

        # Pattern: hostname + prompt char + optional space + command
        # Matches: router#show ip arp, user@switch> show arp no-resolve,
        # switch(config)#show version, hostname$ display arp
        prompt_cmd_pattern = re.compile(
            r"^[\w\-\.@/]+[\#\>\$\)]\s*"
            r"(show|display|get|dir|bash)\s+",
            re.IGNORECASE,
        )

        # Trailing prompt pattern (line is JUST a prompt, no command)
        trailing_prompt_pattern = re.compile(
            r"^[\w\-\.@/]+[\#\>\$\)]\s*$"
        )

        # ── Strategy 1: find last command echo with hostname ─────
        last_echo_idx = -1
        for i, line in enumerate(lines):
            if prompt_cmd_pattern.match(line.strip()):
                last_echo_idx = i

        if last_echo_idx >= 0:
            # Take everything after the last command echo
            output_lines = lines[last_echo_idx + 1:]

            # Strip trailing prompts and empty lines
            while output_lines and (
                not output_lines[-1].strip()
                or trailing_prompt_pattern.match(output_lines[-1].strip())
            ):
                output_lines.pop()

            return "\n".join(output_lines)

        # ── Strategy 2: fallback — strip known preamble from top ─
        # Used when SSH capture doesn't include a hostname-prefixed
        # command echo (e.g. some exec channels or emulated devices)
        preamble_patterns = [
            r"^terminal\s+(length|width)",
            r"^screen.length",
            r"^pagination\s+disabled",
            r"^set\s+cli\s+screen",
            r"^Screen\s+length\s+set",
            r"^---\s+JUNOS\s+",
            r"^\{master:",
            r"^(show|display|get)\s+",   # bare command echo (no hostname)
            r"^\s*$",
        ]

        cleaned_lines = []
        found_start = False

        for line in lines:
            stripped = line.strip()

            if not found_start:
                is_preamble = any(
                    re.match(p, stripped, re.IGNORECASE)
                    for p in preamble_patterns
                )
                if is_preamble:
                    continue
                found_start = True

            # Skip trailing prompts
            if trailing_prompt_pattern.match(stripped):
                continue

            cleaned_lines.append(line)

        # Remove trailing empty lines
        while cleaned_lines and not cleaned_lines[-1].strip():
            cleaned_lines.pop()

        return "\n".join(cleaned_lines)