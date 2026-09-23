import sqlite3
import textfsm
import re
from typing import Dict, List, Tuple, Optional
import io
import time
import click
from multiprocessing import Process, Queue
import multiprocessing
import sys
import threading
from contextlib import contextmanager


class ThreadSafeConnection:
    """Thread-local storage for SQLite connections"""

    def __init__(self, db_path: str, verbose: bool = False):
        self.db_path = db_path
        self.verbose = verbose
        self._local = threading.local()

    @contextmanager
    def get_connection(self):
        """Get a thread-local connection"""
        if not hasattr(self._local, 'connection'):
            self._local.connection = sqlite3.connect(self.db_path)
            self._local.connection.row_factory = sqlite3.Row
            if self.verbose:
                click.echo(f"Created new connection in thread {threading.get_ident()}")

        try:
            yield self._local.connection
        except Exception as e:
            if hasattr(self._local, 'connection'):
                self._local.connection.close()
                delattr(self._local, 'connection')
            raise e

    def close_all(self):
        """Close connection if it exists for current thread"""
        if hasattr(self._local, 'connection'):
            self._local.connection.close()
            delattr(self._local, 'connection')


class TextFSMAutoEngine:
    def __init__(self, db_path: str, verbose: bool = False):
        self.db_path = db_path
        self.verbose = verbose
        self.connection_manager = ThreadSafeConnection(db_path, verbose)

    def _score_parts(
            self,
            parsed_data: List[Dict],
            cli_command: str,
    ) -> Dict[str, float]:
        """
        Compute the four scoring components and total (0-100 scale).

        Single source of truth for scoring math, used by both the
        selection sweep (_calculate_template_score) and the Template Lab
        author-test path (test_template). Returns a dict of:
          records, fields, population, consistency, total

        Factors:
        - Record count (0-90 pts): Did the template find data?
        - Field richness (0-90 pts): How many fields per record?
        - Population rate (0-25 pts): Are fields actually filled?
        - Consistency (0-15 pts): Uniform data across records?
        """
        if not parsed_data:
            return {
                "records": 0.0, "fields": 0.0,
                "population": 0.0, "consistency": 0.0, "total": 0.0,
            }

        num_records = len(parsed_data)
        num_fields = len(parsed_data[0].keys()) if parsed_data else 0
        is_version_cmd = 'version' in (cli_command or '').lower()

        # === Factor 1: Record Count (0-90 points) ===
        if is_version_cmd:
            # Version commands: expect exactly 1 record
            record_score = 90.0 if num_records == 1 else max(0, 15 - (num_records - 1) * 5)
        else:
            # Diminishing returns: log scale capped at 90
            # 1 rec = 10, 3 rec = 20, 10+ rec = 90
            if num_records >= 10:
                record_score = 90.0
            elif num_records >= 3:
                record_score = 20.0 + (num_records - 3) * (10.0 / 7.0)
            else:
                record_score = num_records * 10.0

        # === Factor 2: Field Richness (0-90 points) ===
        # More fields = richer data extraction
        # 1-2 fields = weak, 3-5 = decent, 6-10 = good, 10+ = excellent
        if num_fields >= 10:
            field_score = 90.0
        elif num_fields >= 6:
            field_score = 20.0 + (num_fields - 6) * 2.5
        elif num_fields >= 3:
            field_score = 10.0 + (num_fields - 3) * (10.0 / 3.0)
        else:
            field_score = num_fields * 5.0

        # === Factor 3: Population Rate (0-25 points) ===
        # What percentage of cells have actual data?
        total_cells = num_records * num_fields
        populated_cells = 0

        for record in parsed_data:
            for value in record.values():
                if value is not None and str(value).strip():
                    populated_cells += 1

        population_rate = populated_cells / total_cells if total_cells > 0 else 0
        population_score = population_rate * 25.0

        # === Factor 4: Consistency (0-15 points) ===
        # Are the same fields populated across all records?
        if num_records > 1:
            # Check which fields are populated in each record
            field_fill_counts = {key: 0 for key in parsed_data[0].keys()}

            for record in parsed_data:
                for key, value in record.items():
                    if value is not None and str(value).strip():
                        field_fill_counts[key] += 1

            # Consistency = fields that are either always filled or never filled
            consistent_fields = sum(
                1 for count in field_fill_counts.values()
                if count == 0 or count == num_records
            )
            consistency_rate = consistent_fields / num_fields if num_fields > 0 else 0
            consistency_score = consistency_rate * 15.0
        else:
            # Single record = perfectly consistent
            consistency_score = 15.0

        total_score = record_score + field_score + population_score + consistency_score

        return {
            "records": record_score,
            "fields": field_score,
            "population": population_score,
            "consistency": consistency_score,
            "total": total_score,
        }

    def _calculate_template_score(
            self,
            parsed_data: List[Dict],
            cli_command: str,
            raw_output: Optional[str] = None,
    ) -> float:
        """
        Score template match quality (0-100 scale).

        Thin wrapper over _score_parts that preserves the verbose log
        line used by the selection sweep. `cli_command` is the template
        name (used only to detect version commands, which expect 1 record).
        """
        parts = self._score_parts(parsed_data, cli_command)

        if self.verbose:
            click.echo(f"    Scoring: records={parts['records']:.1f}, "
                       f"fields={parts['fields']:.1f}, "
                       f"population={parts['population']:.1f}, "
                       f"consistency={parts['consistency']:.1f} "
                       f"-> {parts['total']:.1f}")

        return parts["total"]

    def test_template(
            self,
            textfsm_content: str,
            raw_output: str,
            cli_command: str = "",
    ) -> Dict:
        """
        Author-test a single TextFSM template against raw output.

        Unlike find_best_template, this runs ONE caller-supplied template
        (not the database) and never swallows failures — compile and
        runtime (State Error) failures are returned with their message and,
        when present, the offending rule line and input line. On success it
        returns the parsed records plus the full score breakdown, so the
        Template Lab can show exactly why a template scores what it does.

        Returns a dict:
            compiled, success, error, error_type ('compile'|'runtime'|None),
            rule_line, input_line, header, records, record_count,
            field_count, score, breakdown
        """
        zero = {"records": 0.0, "fields": 0.0, "population": 0.0,
                "consistency": 0.0, "total": 0.0}
        result = {
            "compiled": False, "success": False,
            "error": None, "error_type": None,
            "rule_line": None, "input_line": None,
            "header": [], "records": [],
            "record_count": 0, "field_count": 0,
            "score": 0.0, "breakdown": zero,
        }

        # --- Compile ---
        try:
            fsm = textfsm.TextFSM(io.StringIO(textfsm_content))
        except Exception as e:
            result["error"] = str(e)
            result["error_type"] = "compile"
            return result

        result["compiled"] = True
        result["header"] = list(fsm.header)

        # --- Run ---
        try:
            rows = fsm.ParseText(raw_output)
        except Exception as e:
            msg = str(e)
            result["error"] = msg
            result["error_type"] = "runtime"
            m = re.search(r"Rule Line:\s*(\d+)", msg)
            if m:
                result["rule_line"] = int(m.group(1))
            m2 = re.search(r"Input Line:\s*(.*)", msg)
            if m2:
                result["input_line"] = m2.group(1).strip()
            return result

        parsed = [dict(zip(fsm.header, row)) for row in rows]
        parts = self._score_parts(parsed, cli_command or "")

        result["success"] = True
        result["records"] = parsed
        result["record_count"] = len(parsed)
        result["field_count"] = len(fsm.header)
        result["score"] = round(parts["total"], 1)
        result["breakdown"] = {k: round(v, 1) for k, v in parts.items()}
        return result

    def find_best_template(self, device_output: str, filter_string: Optional[str] = None) -> Tuple[
        Optional[str], Optional[List[Dict]], float]:


        """Try filtered templates against the output and return the best match."""
        best_template = None
        best_parsed_output = None
        best_score = 0

        # Get filtered templates using thread-safe connection
        with self.connection_manager.get_connection() as conn:
            templates = self.get_filtered_templates(conn, filter_string)
            total_templates = len(templates)

            if self.verbose:
                click.echo(f"Found {total_templates} matching templates for filter: {filter_string}")

            # Try each template
            for idx, template in enumerate(templates, 1):
                if self.verbose:
                    percentage = (idx / total_templates) * 100
                    click.echo(f"\nTemplate {idx}/{total_templates} ({percentage:.1f}%): {template['cli_command']}")

                try:
                    textfsm_template = textfsm.TextFSM(io.StringIO(template['textfsm_content']))
                    parsed = textfsm_template.ParseText(device_output)
                    parsed_dicts = [dict(zip(textfsm_template.header, row)) for row in parsed]
                    score = self._calculate_template_score(parsed_dicts, template['cli_command'], device_output)

                    if self.verbose:
                        click.echo(f" -> Score={score:.2f}, Records={len(parsed_dicts)}")

                    if score > best_score:
                        best_score = score
                        best_template = template['cli_command']
                        best_parsed_output = parsed_dicts
                        if self.verbose:
                            click.echo(click.style("  New best match!", fg='green'))

                except Exception as e:
                    if self.verbose:
                        click.echo(f" -> Failed to parse: {str(e)}")
                    continue

        return best_template, best_parsed_output, best_score

    def get_filtered_templates(self, connection: sqlite3.Connection, filter_string: Optional[str] = None):
        """Get filtered templates from database using provided connection."""
        cursor = connection.cursor()
        if filter_string:
            filter_terms = filter_string.replace('-', '_').split('_')
            query = "SELECT * FROM templates WHERE 1=1"
            params = []
            for term in filter_terms:
                if term and len(term) > 2:
                    query += " AND cli_command LIKE ?"
                    params.append(f"%{term}%")
            cursor.execute(query, params)
        else:
            cursor.execute("SELECT * FROM templates")
        return cursor.fetchall()

    def __del__(self):
        """Clean up connections on deletion"""
        self.connection_manager.close_all()


# Example usage
if __name__ == '__main__':
    multiprocessing.freeze_support()


    # Example of using the engine in multiple threads
    def worker(engine, output, filter_str):
        result = engine.find_best_template(output, filter_str)
        print(f"Thread {threading.get_ident()}: Found template: {result[0]}")


    engine = TextFSMAutoEngine("./secure_cartography/tfsm_templates.db", verbose=True)
    threads = []

    # Create multiple threads
    for i in range(3):
        t = threading.Thread(target=worker, args=(engine, "sample output", "show version"))
        threads.append(t)
        t.start()

    # Wait for all threads to complete
    for t in threads:
        t.join()