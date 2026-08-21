#!/usr/bin/env python3
"""Run .test files (a small sqllogictest-inspired subset - see SPEC.md's
"Test Script Format" section for the exact record grammar) against one
tinytable install.

Usage:
    python3 run_sql_tests.py --root clean sql-tests/official
    python3 run_sql_tests.py --root path/to/mutant path/to/golden/m03.test

One process per --root by design (mirrors score.py's own external,
subprocess-per-target invocation elsewhere in this eval suite) - this avoids
Python's module cache making a second `import tinytable` silently reuse the
first root's code if this were ever asked to compare two roots in one run.

The grammar carries a version number (SPEC.md's "Grammar version"); v2 adds
session/step/permutation, crash/restart/checkpoint, repeat/advance_clock/
threshold, and explain/assert-stats records on top of v1's statement/query.
crash/restart/checkpoint/advance_clock run against a per-file, seeded
substrate.Simulation (#20; see --sim-seed below) but aren't wired to
`tinytable` itself yet (no persistence layer exists until #11's WAL
lands there). explain/assert-stats still have no `tinytable` feature to
execute against at all (no planner, no stats interface) - those two are
reported as SKIPPED, not failed, and don't affect the exit code. See
SPEC.md's "Execution status summary" table for exactly which.

Exit code 0 iff every record in every file passed (skips don't count).
"""

from __future__ import annotations

import argparse
import operator as operator_module
import pathlib
import sys
from dataclasses import dataclass
from typing import Optional, Union

import scheduler
import substrate


SUPPORTED_GRAMMAR_VERSIONS = ("1", "2")
_LIFECYCLE_KINDS = ("crash", "restart", "checkpoint")
_THRESHOLD_OPS = ("<=", ">=", "<", ">", "==")
_STATS_OPS = {"<=": operator_module.le, ">=": operator_module.ge, "<": operator_module.lt, ">": operator_module.gt, "==": operator_module.eq}
_PERMUTATION_BASELINE = "__grammar_v2_permutation_baseline__"


@dataclass
class StatementRecord:
    kind: str  # "ok" | "error"
    error_pattern: Optional[str]
    sql: str
    line: int


@dataclass
class QueryRecord:
    types: str
    sort_mode: str
    sql: str
    expected: list[str]
    line: int


@dataclass
class VersionRecord:
    version: str
    line: int


@dataclass
class StepRecord:
    session: str
    name: str
    kind: str  # "ok" | "error"
    error_pattern: Optional[str]
    sql: str
    line: int


@dataclass
class PermutationRecord:
    steps: list[str]
    line: int


@dataclass
class LifecycleRecord:
    kind: str  # "crash" | "restart" | "checkpoint"
    line: int
    torn: bool = False  # crash-only: also truncate durable (fsync'd) bytes to a random prefix - see substrate.py


@dataclass
class RepeatRecord:
    count: int
    body: list[Record]
    line: int


@dataclass
class AdvanceClockRecord:
    duration: str
    line: int


@dataclass
class ThresholdRecord:
    stat: str
    op: str
    bound: str
    line: int


@dataclass
class ExplainRecord:
    sql: str
    expected: list[str]
    line: int


@dataclass
class StatsAssertRecord:
    stat: str
    mode: str  # "bounded" | "converges"
    op: Optional[str]
    bound: Optional[str]
    line: int


Record = Union[
    StatementRecord,
    QueryRecord,
    VersionRecord,
    StepRecord,
    PermutationRecord,
    LifecycleRecord,
    RepeatRecord,
    AdvanceClockRecord,
    ThresholdRecord,
    ExplainRecord,
    StatsAssertRecord,
]


class TestFileError(Exception):
    pass


def _is_blank(line: str) -> bool:
    return line.strip() == ""


def _ends_body(line: str) -> bool:
    """A record body (SQL text, or a query/explain's expected block) ends
    at a blank line or a bare '}' - the latter so a `repeat N { ... }`
    block's last record doesn't need an extra blank line before its
    closing brace."""
    return _is_blank(line) or line.strip() == "}"


def _read_sql_body(lines: list[str], i: int, n: int, path: pathlib.Path, header_line_no: int, record_name: str) -> tuple[str, int]:
    sql_lines = []
    while i < n and not _ends_body(lines[i]):
        sql_lines.append(lines[i])
        i += 1
    if not sql_lines:
        raise TestFileError(f"{path}:{header_line_no}: '{record_name}' record has no SQL text")
    return "\n".join(sql_lines), i


def _read_query_like(lines: list[str], i: int, n: int, path: pathlib.Path, header_line_no: int, record_name: str) -> tuple[str, list[str], int]:
    sql_lines = []
    while i < n and lines[i] != "----":
        if _is_blank(lines[i]):
            raise TestFileError(f"{path}:{i + 1}: '{record_name}' record hit a blank line before '----'")
        sql_lines.append(lines[i])
        i += 1
    if i >= n:
        raise TestFileError(f"{path}:{header_line_no}: '{record_name}' record missing '----' terminator")
    i += 1  # skip "----"
    expected = []
    while i < n and not _ends_body(lines[i]):
        expected.append(lines[i])
        i += 1
    return "\n".join(sql_lines), expected, i


def _parse_block(lines: list[str], i: int, n: int, path: pathlib.Path, *, in_repeat: bool) -> tuple[list[Record], int]:
    records: list[Record] = []
    current_session: Optional[str] = None
    known_steps: set[str] = set()

    while i < n:
        line = lines[i]
        if _is_blank(line) or line.lstrip().startswith("#"):
            i += 1
            continue
        stripped = line.strip()
        if in_repeat and stripped == "}":
            return records, i + 1

        header_line_no = i + 1
        parts = line.split()
        head = parts[0]

        if head == "version":
            raise TestFileError(f"{path}:{header_line_no}: 'version' must be the first line of the file")

        elif head == "statement":
            if len(parts) < 2 or parts[1] not in ("ok", "error"):
                raise TestFileError(f"{path}:{header_line_no}: expected 'statement ok' or 'statement error ...'")
            kind = parts[1]
            error_pattern = " ".join(parts[2:]) if kind == "error" and len(parts) > 2 else None
            i += 1
            sql, i = _read_sql_body(lines, i, n, path, header_line_no, "statement")
            records.append(StatementRecord(kind=kind, error_pattern=error_pattern, sql=sql, line=header_line_no))

        elif head == "query":
            types = parts[1] if len(parts) > 1 else ""
            sort_mode = parts[2] if len(parts) > 2 else "nosort"
            if sort_mode not in ("nosort", "rowsort"):
                raise TestFileError(f"{path}:{header_line_no}: unknown sort mode {sort_mode!r} (expected nosort/rowsort)")
            i += 1
            sql, expected, i = _read_query_like(lines, i, n, path, header_line_no, "query")
            records.append(QueryRecord(types=types, sort_mode=sort_mode, sql=sql, expected=expected, line=header_line_no))

        elif head == "explain":
            i += 1
            sql, expected, i = _read_query_like(lines, i, n, path, header_line_no, "explain")
            records.append(ExplainRecord(sql=sql, expected=expected, line=header_line_no))

        elif head == "session":
            if len(parts) != 2:
                raise TestFileError(f"{path}:{header_line_no}: expected 'session <name>'")
            current_session = parts[1]
            i += 1

        elif head == "step":
            if current_session is None:
                raise TestFileError(f"{path}:{header_line_no}: 'step' with no preceding 'session' header")
            if len(parts) < 2:
                raise TestFileError(f"{path}:{header_line_no}: expected 'step <name> [error [substring]]'")
            name = parts[1]
            if name in known_steps:
                raise TestFileError(f"{path}:{header_line_no}: duplicate step name {name!r}")
            known_steps.add(name)
            if len(parts) == 2:
                kind, error_pattern = "ok", None
            elif parts[2] == "error":
                kind, error_pattern = "error", (" ".join(parts[3:]) if len(parts) > 3 else None)
            else:
                raise TestFileError(f"{path}:{header_line_no}: expected 'step <name>' or 'step <name> error [substring]'")
            i += 1
            sql, i = _read_sql_body(lines, i, n, path, header_line_no, "step")
            records.append(StepRecord(session=current_session, name=name, kind=kind, error_pattern=error_pattern, sql=sql, line=header_line_no))

        elif head == "permutation":
            steps = parts[1:]
            if not steps:
                raise TestFileError(f"{path}:{header_line_no}: 'permutation' needs at least one step name")
            i += 1
            records.append(PermutationRecord(steps=steps, line=header_line_no))

        elif head in _LIFECYCLE_KINDS:
            torn = False
            if len(parts) == 2 and head == "crash" and parts[1] == "torn":
                torn = True
            elif len(parts) != 1:
                expected = "'crash' or 'crash torn'" if head == "crash" else f"{head!r}"
                raise TestFileError(f"{path}:{header_line_no}: {head!r} takes no arguments (expected {expected})")
            i += 1
            records.append(LifecycleRecord(kind=head, line=header_line_no, torn=torn))

        elif head == "repeat":
            if len(parts) != 3 or parts[2] != "{":
                raise TestFileError(f"{path}:{header_line_no}: expected 'repeat <N> {{'")
            try:
                count = int(parts[1])
            except ValueError:
                raise TestFileError(f"{path}:{header_line_no}: 'repeat' count must be an integer, got {parts[1]!r}")
            if count < 0:
                raise TestFileError(f"{path}:{header_line_no}: 'repeat' count must be >= 0, got {count}")
            i += 1
            body, i = _parse_block(lines, i, n, path, in_repeat=True)
            records.append(RepeatRecord(count=count, body=body, line=header_line_no))

        elif head == "advance_clock":
            if len(parts) != 2:
                raise TestFileError(f"{path}:{header_line_no}: expected 'advance_clock <duration>'")
            i += 1
            records.append(AdvanceClockRecord(duration=parts[1], line=header_line_no))

        elif head == "threshold":
            if len(parts) != 4 or parts[2] not in _THRESHOLD_OPS:
                raise TestFileError(f"{path}:{header_line_no}: expected 'threshold <stat> <op> <bound>' with op in {_THRESHOLD_OPS}")
            records.append(ThresholdRecord(stat=parts[1], op=parts[2], bound=parts[3], line=header_line_no))
            i += 1

        elif head == "assert":
            usage = "expected 'assert stats <stat> converges' or 'assert stats <stat> bounded <op> <bound>'"
            if len(parts) < 4 or parts[1] != "stats":
                raise TestFileError(f"{path}:{header_line_no}: {usage}")
            stat = parts[2]
            if len(parts) == 4 and parts[3] == "converges":
                records.append(StatsAssertRecord(stat=stat, mode="converges", op=None, bound=None, line=header_line_no))
            elif len(parts) == 6 and parts[3] == "bounded" and parts[4] in _STATS_OPS:
                records.append(StatsAssertRecord(stat=stat, mode="bounded", op=parts[4], bound=parts[5], line=header_line_no))
            else:
                raise TestFileError(f"{path}:{header_line_no}: {usage}")
            i += 1

        else:
            raise TestFileError(f"{path}:{header_line_no}: unrecognized record header {line!r}")

    if in_repeat:
        raise TestFileError(f"{path}: 'repeat' block is missing its closing '}}'")
    return records, i


def parse_test_file(path: pathlib.Path) -> list[Record]:
    lines = path.read_text().splitlines()
    n = len(lines)
    i = 0
    while i < n and (_is_blank(lines[i]) or lines[i].lstrip().startswith("#")):
        i += 1

    records: list[Record] = []
    if i < n:
        parts = lines[i].split()
        if parts and parts[0] == "version":
            if len(parts) != 2:
                raise TestFileError(f"{path}:{i + 1}: expected 'version <N>'")
            version = parts[1]
            if version not in SUPPORTED_GRAMMAR_VERSIONS:
                raise TestFileError(
                    f"{path}:{i + 1}: unsupported grammar version {version!r} (supported: {', '.join(SUPPORTED_GRAMMAR_VERSIONS)})"
                )
            records.append(VersionRecord(version=version, line=i + 1))
            i += 1

    body, i = _parse_block(lines, i, n, path, in_repeat=False)
    records.extend(body)
    return records


def _render(value) -> str:
    if value is None:
        return "NULL"
    if isinstance(value, bool):  # check before str(): str(True) is "True", not "TRUE"
        return "TRUE" if value else "FALSE"
    return str(value)


def _check_query(record: QueryRecord, columns: list[str], rows: list[tuple]) -> Optional[str]:
    if len(columns) != len(record.types):
        return f"column count mismatch: types={record.types!r} implies {len(record.types)}, got {len(columns)} columns {columns}"
    width = len(record.types)
    rendered_actual = [_render(v) for row in rows for v in row]
    if record.sort_mode == "rowsort":
        def grouped(flat: list[str]) -> list[tuple]:
            return sorted(tuple(flat[i : i + width]) for i in range(0, len(flat), width))

        actual_g = grouped(rendered_actual)
        expected_g = grouped(record.expected)
        if actual_g != expected_g:
            return f"rowsort mismatch:\n    expected: {expected_g}\n    actual:   {actual_g}"
    else:
        if rendered_actual != record.expected:
            return f"nosort mismatch:\n    expected: {record.expected}\n    actual:   {rendered_actual}"
    return None


class _ExecState:
    """Threaded through recursive record execution (top level + nested
    `repeat` bodies): steps declared anywhere are visible to any
    `permutation` later in the same file, and the permutation baseline
    savepoint is taken at most once per file."""

    def __init__(self) -> None:
        self.step_lookup: dict[str, StepRecord] = {}
        self.permutation_baseline_taken = False


def _execute_statement_like(kind: str, error_pattern: Optional[str], sql: str, line: int, db, failures: list[tuple[int, str]], label: str) -> None:
    try:
        db.execute(sql)
        error: Optional[Exception] = None
    except Exception as exc:  # noqa: BLE001 - deliberately broad, this is a test harness
        error = exc
    if kind == "ok" and error is not None:
        failures.append((line, f"{label}expected to succeed but raised {type(error).__name__}: {error}\n    sql: {sql}"))
    elif kind == "error" and error is None:
        failures.append((line, f"{label}expected to raise but succeeded\n    sql: {sql}"))
    elif kind == "error" and error is not None and error_pattern and error_pattern not in str(error):
        failures.append((line, f"{label}raised {type(error).__name__}({error!r}) but that does not contain expected text {error_pattern!r}\n    sql: {sql}"))


def _execute_query(record: QueryRecord, db, failures: list[tuple[int, str]]) -> None:
    try:
        result = db.execute(record.sql)
    except Exception as exc:  # noqa: BLE001
        failures.append((record.line, f"query raised {type(exc).__name__}: {exc}\n    sql: {record.sql}"))
        return
    problem = _check_query(record, result.columns, result.rows)
    if problem:
        failures.append((record.line, f"{problem}\n    sql: {record.sql}"))


def _execute_permutation(record: PermutationRecord, db, state: _ExecState, failures: list[tuple[int, str]]) -> None:
    missing = [name for name in record.steps if name not in state.step_lookup]
    if missing:
        failures.append((record.line, f"permutation references unknown step(s): {missing}"))
        return

    try:
        if state.permutation_baseline_taken:
            db.execute(f"ROLLBACK TO {_PERMUTATION_BASELINE}")
        else:
            db.execute(f"SAVEPOINT {_PERMUTATION_BASELINE}")
            state.permutation_baseline_taken = True
    except Exception as exc:  # noqa: BLE001
        failures.append((record.line, f"permutation {record.steps}: could not reset to baseline: {type(exc).__name__}: {exc}"))
        return

    # Delegate the actual interleaving to scheduler.py (#19) - this file's
    # only job here is translating StepRecord/PermutationRecord (parsed
    # from a .test file) into scheduler.Step/Schedule, running it against
    # the shared `db`, and reporting any ok/error contract violation the
    # same way a plain `statement` record's would be.
    schedule = scheduler.Schedule(
        steps=tuple(
            scheduler.Step(session=s.session, name=s.name, sql=s.sql, kind=s.kind, error_pattern=s.error_pattern)
            for s in state.step_lookup.values()
        ),
        order=tuple(record.steps),
    )
    result = scheduler.run_schedule(schedule, db=db)
    for outcome in result.contract_violations:
        step = state.step_lookup[outcome.step]
        label = f"step {step.name!r}: "
        if step.kind == "ok":
            failures.append((step.line, f"{label}expected to succeed but raised {outcome.raised}: {outcome.message}\n    sql: {step.sql}"))
        elif outcome.raised is None:
            failures.append((step.line, f"{label}expected to raise but succeeded\n    sql: {step.sql}"))
        else:
            failures.append(
                (step.line, f"{label}raised {outcome.raised}({outcome.message!r}) but that does not contain expected text {step.error_pattern!r}\n    sql: {step.sql}")
            )


def _execute_explain(record: ExplainRecord, db, failures: list[tuple[int, str]], skips: list[tuple[int, str]]) -> None:
    explain = getattr(db, "explain", None)
    if explain is None:
        skips.append((record.line, "explain: tinytable has no query planner yet (see SPEC.md's Execution status summary)"))
        return
    try:
        plan_lines = list(explain(record.sql))
    except Exception as exc:  # noqa: BLE001
        failures.append((record.line, f"explain raised {type(exc).__name__}: {exc}\n    sql: {record.sql}"))
        return
    if plan_lines != record.expected:
        failures.append((record.line, f"explain mismatch:\n    expected: {record.expected}\n    actual:   {plan_lines}\n    sql: {record.sql}"))


def _execute_stats_assert(record: StatsAssertRecord, db, failures: list[tuple[int, str]], skips: list[tuple[int, str]]) -> None:
    stats = getattr(db, "stats", None)
    if stats is None:
        skips.append((record.line, f"assert stats {record.stat}: tinytable has no stats interface yet (see SPEC.md's Execution status summary)"))
        return
    values = stats()
    if record.stat not in values:
        failures.append((record.line, f"assert stats {record.stat}: no such stat (available: {sorted(values)})"))
        return
    if record.mode == "converges":
        return  # convergence needs a history across repeat iterations, not a single sample; nothing to check here yet
    actual = values[record.stat]
    bound = type(actual)(record.bound)
    if not _STATS_OPS[record.op](actual, bound):
        failures.append((record.line, f"assert stats {record.stat} {record.op} {record.bound}: actual={actual}"))


def _execute_lifecycle(record: LifecycleRecord, sim: substrate.Simulation, failures: list[tuple[int, str]]) -> None:
    if record.kind == "checkpoint":
        sim.vfs.checkpoint()
    elif record.kind == "crash":
        sim.vfs.crash(torn=record.torn)
    else:
        assert record.kind == "restart"
        sim.vfs.restart()
    # #20's substrate.py is exercised (deterministically, given --sim-seed)
    # but not yet wired to `db` itself - no persistence layer exists until
    # #11's WAL lands there, so this has no SQL-visible effect yet.


def _execute_advance_clock(record: AdvanceClockRecord, sim: substrate.Simulation, failures: list[tuple[int, str]]) -> None:
    try:
        seconds = substrate.parse_duration(record.duration)
    except ValueError as exc:
        failures.append((record.line, str(exc)))
        return
    sim.clock.advance(seconds)


def _execute_records(records: list[Record], db, state: _ExecState, failures: list[tuple[int, str]], skips: list[tuple[int, str]], sim: substrate.Simulation) -> None:
    for record in records:
        if isinstance(record, VersionRecord):
            continue
        elif isinstance(record, StatementRecord):
            _execute_statement_like(record.kind, record.error_pattern, record.sql, record.line, db, failures, label="")
        elif isinstance(record, QueryRecord):
            _execute_query(record, db, failures)
        elif isinstance(record, StepRecord):
            state.step_lookup[record.name] = record  # declaration only; runs when a permutation names it
        elif isinstance(record, PermutationRecord):
            _execute_permutation(record, db, state, failures)
        elif isinstance(record, LifecycleRecord):
            _execute_lifecycle(record, sim, failures)
        elif isinstance(record, RepeatRecord):
            for _ in range(record.count):
                _execute_records(record.body, db, state, failures, skips, sim)
        elif isinstance(record, AdvanceClockRecord):
            _execute_advance_clock(record, sim, failures)
        elif isinstance(record, ThresholdRecord):
            skips.append((record.line, f"threshold {record.stat} {record.op} {record.bound}: recorded, not yet enforced (see SPEC.md's Execution status summary)"))
        elif isinstance(record, ExplainRecord):
            _execute_explain(record, db, failures, skips)
        elif isinstance(record, StatsAssertRecord):
            _execute_stats_assert(record, db, failures, skips)
        else:
            raise AssertionError(f"unhandled record type: {type(record)}")  # pragma: no cover


def run_file(path: pathlib.Path, tinytable, sim_seed: int = 0) -> tuple[list[tuple[int, str]], list[tuple[int, str]]]:
    """Returns (failures, skips) - see module docstring: a skip doesn't
    count as a failure. `sim_seed` seeds this file's substrate.Simulation
    (crash/restart/checkpoint/advance_clock - #20); same seed, same file
    -> byte-identical run, per substrate.py's own determinism contract."""
    records = parse_test_file(path)
    db = tinytable.Database()
    sim = substrate.Simulation(sim_seed)
    failures: list[tuple[int, str]] = []
    skips: list[tuple[int, str]] = []
    _execute_records(records, db, _ExecState(), failures, skips, sim)
    return failures, skips


def collect_test_files(paths: list[str]) -> list[pathlib.Path]:
    files: list[pathlib.Path] = []
    for raw in paths:
        p = pathlib.Path(raw)
        if p.is_dir():
            files.extend(sorted(p.rglob("*.test")))
        else:
            files.append(p)
    return files


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--root", required=True, help="directory containing a tinytable/ package to test")
    parser.add_argument(
        "--sim-seed", type=int, default=0,
        help="seed for each file's substrate.Simulation (crash/restart/checkpoint/advance_clock, #20); "
        "same seed -> byte-identical run (default: 0)",
    )
    parser.add_argument("paths", nargs="+", help="*.test files, or directories to search for them")
    args = parser.parse_args()

    root = pathlib.Path(args.root).resolve()
    sys.path.insert(0, str(root))
    import tinytable  # local import: must happen after sys.path is set up

    files = collect_test_files(args.paths)
    if not files:
        print("no .test files found", file=sys.stderr)
        return 1

    total_failures = 0
    total_skips = 0
    for path in files:
        try:
            failures, skips = run_file(path, tinytable, sim_seed=args.sim_seed)
        except TestFileError as exc:
            print(f"FAIL {path} (malformed test file)")
            print(f"  {exc}")
            total_failures += 1
            continue
        if failures:
            total_failures += len(failures)
            print(f"FAIL {path} ({len(failures)} failure(s))")
            for line, message in failures:
                print(f"  line {line}: {message}")
        else:
            suffix = f" ({len(skips)} skipped)" if skips else ""
            print(f"ok   {path}{suffix}")
        if skips:
            total_skips += len(skips)
            for line, message in skips:
                print(f"  SKIP line {line}: {message}")

    print()
    if total_failures:
        print(f"{total_failures} failure(s) across {len(files)} file(s)")
        return 1
    extra = f" ({total_skips} record(s) skipped - grammar directives without runtime support yet)" if total_skips else ""
    print(f"all {len(files)} file(s) passed{extra}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
